"""Contain and drain one owned CLI process without unbounded pipe reads."""
import os
import queue
import signal
import subprocess
import threading
import time

class _WindowsJob:
    """Assign before resume so venv launchers and grandchildren remain owned."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong), ('flags', wintypes.DWORD), ('minimum', ctypes.c_size_t), ('maximum', ctypes.c_size_t), ('active', wintypes.DWORD), ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign_resume(self, process):
        import ctypes
        from ctypes import wintypes
        if not self.api.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())
        # Popen closes the primary thread handle; resume the contained process.
        resume = ctypes.WinDLL('ntdll').NtResumeProcess
        resume.argtypes = [wintypes.HANDLE]
        resume.restype = ctypes.c_long
        if resume(wintypes.HANDLE(int(process._handle))) != 0:
            raise RuntimeError('Unable to resume contained development subprocess')

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class OwnedCLI:
    MAX_LINE = 1024 * 1024
    MAX_STDERR = 16384

    def __init__(self,cmd,env,cwd,stdin_text):
        self.job=_WindowsJob() if os.name=='nt' else None
        self.stopped=threading.Event()
        self.queue=queue.Queue(maxsize=32)
        self.stderr=''
        self.threads=[]
        try:
            self.proc=subprocess.Popen(cmd,stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace',
                env=env,cwd=cwd,bufsize=1,
                creationflags=(getattr(subprocess,'CREATE_NO_WINDOW',0)|0x4) if self.job else 0,
                start_new_session=os.name!='nt')
            if self.job:self.job.assign_resume(self.proc)
        except Exception:
            if self.job:self.job.close()
            if hasattr(self,'proc'):self.proc.kill();self.proc.wait(timeout=2)
            raise
        def put(value):
            while not self.stopped.is_set():
                try:self.queue.put(value,timeout=.1);return
                except queue.Full:pass
        def read(stream,kind):
            try:
                while not self.stopped.is_set():
                    line=stream.read(4096) if kind=='stderr' else stream.readline(self.MAX_LINE+1)
                    if not line:break
                    if kind=='stderr':self.stderr=(self.stderr+line)[-self.MAX_STDERR:];continue
                    if len(line)>self.MAX_LINE:
                        # One huge event (a CLI echoing a big tool result) must not end the run: drop the
                        # rest of that line and keep reading; replies and progress arrive on small lines.
                        while line and not line.endswith('\n') and not self.stopped.is_set():
                            line=stream.readline(self.MAX_LINE+1)
                        put(('oversize',None));continue
                    put((kind,line))
            except (ValueError,OSError):pass
            finally:put((kind,None))
        for stream,kind in ((self.proc.stdout,'stdout'),(self.proc.stderr,'stderr')):
            thread=threading.Thread(target=read,args=(stream,kind),daemon=True)
            self.threads.append(thread);thread.start()
        if stdin_text is not None:
            def write():
                try:self.proc.stdin.write(stdin_text);self.proc.stdin.close()
                except (BrokenPipeError,OSError,ValueError):put(('stdin_error',None))
            thread=threading.Thread(target=write,daemon=True)
            self.threads.append(thread);thread.start()

    def close(self):
        self.stopped.set()
        if self.job:self.job.close()
        elif os.name!='nt':
            try:os.killpg(self.proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        if self.proc.poll() is None:self.proc.kill()
        self.proc.wait(timeout=3)
        for thread in self.threads:thread.join(timeout=1)
        for stream in (self.proc.stdout,self.proc.stderr,self.proc.stdin):
            if stream is not None and not stream.closed:stream.close()
