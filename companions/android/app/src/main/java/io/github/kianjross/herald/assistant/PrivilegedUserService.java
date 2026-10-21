package io.github.kianjross.herald.assistant;

import android.system.Os;
import androidx.annotation.Keep;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Base64;
import java.util.List;
import java.util.concurrent.FutureTask;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/** Official Shizuku non-daemon UserService pattern; executed UID is observed. */
@Keep
public class PrivilegedUserService extends IPrivilegedService.Stub {
    private volatile Process activeProcess;
    @Keep public PrivilegedUserService() {}
    @Override public void destroy() {
        Process process=activeProcess;
        if(process!=null) process.destroyForcibly();
        System.exit(0);
    }
    @Override public String execute(String requestJson) {
        try {
            if (requestJson.length() > 100000) throw new IllegalArgumentException("Request too large");
            JSONObject request = new JSONObject(requestJson);
            JSONObject result = request.optString("command").equals("privileged_files")
                ? files(request.getJSONObject("arguments")) : shell(request.getJSONObject("arguments"));
            result.put("uid", Os.getuid()).put("root", Os.getuid() == 0);
            // JSON escaping/non-ASCII output can expand beyond raw-byte bounds.
            // Keep room for the acknowledgement's execution-evidence envelope.
            while (result.toString().getBytes("UTF-8").length > 54000 && result.has("stdout")) {
                String out=result.optString("stdout"), err=result.optString("stderr");
                result.put("stdout",out.substring(0,out.length()/2)).put("stderr",err.substring(0,err.length()/2));
                result.put("truncated",true);
            }
            return result.toString();
        } catch (Exception error) {
            return "{\"success\":false,\"uid\":" + Os.getuid() + ",\"root\":" + (Os.getuid()==0)
                + ",\"error\":" + JSONObject.quote(error.toString()) + "}";
        }
    }
    private JSONObject shell(JSONObject args) throws Exception {
        List<String> argv = new ArrayList<>();
        JSONArray array = args.optJSONArray("argv");
        if (array != null) {
            if (array.length() == 0 || array.length() > 128) throw new IllegalArgumentException("Invalid argv");
            for (int i=0; i<array.length(); i++) argv.add(array.getString(i));
        } else {
            String command = args.getString("command");
            if (command.isEmpty() || command.length() > 8192) throw new IllegalArgumentException("Invalid shell command");
            argv.add("/system/bin/sh"); argv.add("-c"); argv.add(command);
        }
        for (String arg : argv) if (arg.indexOf('\0') >= 0 || arg.length() > 8192)
            throw new IllegalArgumentException("Invalid argument");
        int timeout = Math.max(1, Math.min(30, args.optInt("timeout_seconds", 15)));
        int limit = Math.max(1024, Math.min(16384, args.optInt("max_bytes", 8192)));
        Process process = new ProcessBuilder(argv).start();
        activeProcess = process;
        process.getOutputStream().close();
        AtomicBoolean truncated = new AtomicBoolean();
        FutureTask<String> out = drain(process.getInputStream(), limit, truncated);
        FutureTask<String> err = drain(process.getErrorStream(), limit, truncated);
        boolean completed = process.waitFor(timeout, TimeUnit.SECONDS);
        if (!completed) process.destroyForcibly();
        JSONObject result = new JSONObject().put("success", completed && process.exitValue() == 0)
            .put("timed_out", !completed).put("outcome_unknown", !completed)
            .put("exit_code", completed ? process.exitValue() : JSONObject.NULL);
        try { result.put("stdout", out.get(2, TimeUnit.SECONDS)); }
        catch (Exception e) { process.getInputStream().close(); out.cancel(true); result.put("stdout", "").put("output_incomplete", true); }
        try { result.put("stderr", err.get(2, TimeUnit.SECONDS)); }
        catch (Exception e) { process.getErrorStream().close(); err.cancel(true); result.put("stderr", "").put("output_incomplete", true); }
        return result.put("truncated", truncated.get());
    }
    private FutureTask<String> drain(InputStream stream, int limit, AtomicBoolean truncated) {
        FutureTask<String> task = new FutureTask<>(() -> {
            ByteArrayOutputStream kept = new ByteArrayOutputStream();
            byte[] buffer = new byte[4096]; int count;
            try (InputStream input=stream) {
                while ((count=input.read(buffer)) != -1) {
                    int take=Math.min(count, limit-kept.size());
                    if (take>0) kept.write(buffer,0,take);
                    if (take<count) truncated.set(true);
                }
            }
            return kept.toString("UTF-8");
        });
        Thread thread=new Thread(task,"bounded-shizuku-output"); thread.setDaemon(true); thread.start();
        return task;
    }
    private JSONObject files(JSONObject args) throws Exception {
        String raw=args.getString("path");
        if (!raw.startsWith("/") || raw.indexOf('\0') >= 0 || raw.length()>4096) throw new IllegalArgumentException("Bounded absolute Android path required");
        Path path=Paths.get(raw).normalize();
        String action=args.getString("action");
        JSONObject result=new JSONObject().put("success",true).put("path",path.toString());
        if (action.equals("list")) {
            JSONArray entries=new JSONArray(); int limit=Math.max(1,Math.min(100,args.optInt("limit",50)));
            boolean truncated=false;
            try (java.nio.file.DirectoryStream<Path> directory=Files.newDirectoryStream(path)) {
                for (Path child: directory) {
                    if (entries.length()>=limit) { truncated=true; break; }
                    entries.put(new JSONObject().put("name",child.getFileName().toString())
                        .put("path",child.toString()).put("directory",Files.isDirectory(child)));
                    if(entries.toString().getBytes("UTF-8").length>16384) { entries.remove(entries.length()-1); truncated=true; break; }
                }
            }
            return result.put("entries",entries).put("truncated",truncated);
        }
        if (action.equals("read")) {
            if(!Files.isRegularFile(path)) throw new IllegalArgumentException("Reading requires a regular file, not a device or pipe");
            int limit=Math.max(1,Math.min(32768,args.optInt("max_bytes",16384)));
            ByteArrayOutputStream bytes=new ByteArrayOutputStream(); boolean truncated=false;
            try (InputStream input=Files.newInputStream(path)) {
                byte[] buffer=new byte[4096]; int count;
                while ((count=input.read(buffer,0,Math.min(buffer.length,limit+1-bytes.size())))>0) {
                    bytes.write(buffer,0,count); if(bytes.size()>limit) { truncated=true; break; }
                }
            }
            byte[] data=bytes.toByteArray(); if(data.length>limit) data=java.util.Arrays.copyOf(data,limit);
            return result.put("data_base64",Base64.getEncoder().encodeToString(data)).put("bytes",data.length).put("truncated",truncated);
        }
        if (action.equals("write")) {
            if(Files.exists(path) && !Files.isRegularFile(path)) throw new IllegalArgumentException("Writing requires a regular file destination");
            byte[] data=args.has("data_base64") ? Base64.getDecoder().decode(args.getString("data_base64"))
                : args.getString("content").getBytes("UTF-8");
            if(data.length>32768) throw new IllegalArgumentException("Write exceeds 32768 bytes");
            Files.write(path,data);
            return result.put("bytes",data.length);
        }
        throw new IllegalArgumentException("Supported file actions: list, read, write");
    }
}
