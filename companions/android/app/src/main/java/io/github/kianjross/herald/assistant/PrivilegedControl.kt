package io.github.kianjross.herald.assistant

import android.content.ComponentName
import android.content.Context
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import org.json.JSONObject
import rikka.shizuku.Shizuku
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.FutureTask
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/** Adapt official Shizuku UserService binding; no root assumption or independent daemon. */
object PrivilegedControl {
    private val serial = Mutex()
    fun status(): Map<String, Any?> {
        val alive = runCatching { Shizuku.pingBinder() }.getOrDefault(false)
        val granted = alive && runCatching {
            !Shizuku.isPreV11() && Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED
        }.getOrDefault(false)
        val uid = if (alive) runCatching { Shizuku.getUid() }.getOrNull() else null
        return linkedMapOf("provider" to "shizuku", "binder_alive" to alive,
            "permission_granted" to granted, "uid" to uid, "root" to (uid == 0),
            "available" to (alive && granted && (uid == 0 || uid == 2000)),
            "observed_at_ms" to System.currentTimeMillis(),
            "scope" to if (uid == 0) "root" else if (uid == 2000) "adb_shell" else "unavailable",
            "limitations" to "ADB shell cannot read other apps' private data; protected paths and operations remain subject to Android permissions and SELinux.")
    }

    suspend fun execute(context: Context, command: String, arguments: Map<String, Any?>,
                        deadlineMillis: Long? = null, authority: (() -> Unit)? = null): Map<String, Any?> {
        // Keep the observation outside withContext: its return dispatch can be
        // cancelled after a real effect completed and after cleanup finished.
        val observed = AtomicReference<Map<String, Any?>?>(null)
        val effectDispatched = AtomicBoolean(false)
        try { return withContext(Dispatchers.IO) { serial.withLock {
            require(command in setOf("privileged_shell", "privileged_files")) { "Unsupported privileged command" }
            check(status()["available"] == true) { "Shizuku is disconnected or this app has not been granted Shizuku permission" }
            val deadline = minOf(deadlineMillis ?: Long.MAX_VALUE, System.currentTimeMillis() + 45000L)
            authority?.invoke()
            val args = Shizuku.UserServiceArgs(ComponentName(context.packageName, PrivilegedUserService::class.java.name))
                .daemon(false).processNameSuffix("privileged").debuggable(BuildConfig.DEBUG).version(BuildConfig.VERSION_CODE)
            val ready = CountDownLatch(1)
            var service: IPrivilegedService? = null
            var bindingError: Throwable? = null
            val connection = object : ServiceConnection {
                override fun onServiceConnected(name: ComponentName, binder: IBinder) {
                    service = IPrivilegedService.Stub.asInterface(binder); ready.countDown()
                }
                override fun onServiceDisconnected(name: ComponentName) { service = null; ready.countDown() }
            }
            val main = Handler(Looper.getMainLooper())
            main.post { try { Shizuku.bindUserService(args, connection) }
                catch (error: Throwable) { bindingError = error; ready.countDown() } }
            var call: FutureTask<Map<String, Any?>>? = null
            var dispatched = false
            try {
                check(ready.await(10, TimeUnit.SECONDS)) { "Shizuku user service connection timed out; no command was sent" }
                bindingError?.let { throw IllegalStateException("Shizuku binding failed", it) }
                val bound = checkNotNull(service) { "Shizuku user service disconnected before dispatch" }
                authority?.invoke()
                val remainingSeconds = (deadline - System.currentTimeMillis()) / 1000L
                check(remainingSeconds >= 1) { "Privileged command authority expired before execution" }
                val boundedArguments = JSONObject(arguments).put("timeout_seconds",
                    minOf((arguments["timeout_seconds"] as? Number)?.toLong()?.coerceIn(1,30) ?: 15L, remainingSeconds))
                val request = JSONObject().put("command", command).put("arguments", boundedArguments).toString()
                require(request.length <= 100000) { "Privileged request exceeds bounded size" }
                val task = FutureTask { jsonMap(JSONObject(bound.execute(request))) }
                call = task
                dispatched = true
                effectDispatched.set(true)
                Thread(task, "shizuku-command-result").apply { isDaemon = true; start() }
                while(!task.isDone) {
                    check(System.currentTimeMillis() < deadline) { "Privileged command lease or expiry elapsed" }
                    check(status()["available"] == true) { "Shizuku authority was revoked or disconnected" }
                    authority?.invoke()
                    delay(500)
                }
                task.get().also { observed.set(it) }
            } catch(error: Exception) {
                if(dispatched) {
                    // Cancellation at delay/cleanup may arrive after Binder
                    // returned a conclusive result. Preserve that observation.
                    val completed = call?.takeIf { it.isDone && !it.isCancelled }
                        ?.let { runCatching { it.get() }.getOrNull() }
                    (completed ?: unknownOutcome(error)).also { observed.set(it) }
                } else throw error
            } finally {
                // Removing the official non-daemon UserService stops its process;
                // effects already performed (or detached child work) remain unknown.
                withContext(NonCancellable + Dispatchers.Main) {
                    runCatching { Shizuku.unbindUserService(args, connection, true) }
                }
                call?.cancel(true)
            }
        } } } catch(cancelled: CancellationException) {
            // No suspension here: the worker must journal this result even when
            // its coroutine has already been cancelled by WorkManager.
            return observed.get()?.plus("worker_cancelled" to true)
                ?: if(effectDispatched.get()) unknownOutcome(cancelled).plus("worker_cancelled" to true)
                else throw cancelled
        }
    }

    private fun unknownOutcome(error: Exception): Map<String, Any?> = mapOf(
        "success" to false, "outcome_unknown" to true,
        "error" to "Privileged execution stopped after dispatch; do not blindly replay: ${error.message ?: error.javaClass.simpleName}")

    private fun jsonMap(value: JSONObject): Map<String, Any?> = value.keys().asSequence().associateWith { key ->
        when(val item = value.opt(key)) {
            JSONObject.NULL -> null
            is JSONObject -> jsonMap(item)
            is org.json.JSONArray -> (0 until item.length()).map { index ->
                val child = item.opt(index); if(child is JSONObject) jsonMap(child) else if(child == JSONObject.NULL) null else child
            }
            else -> item
        }
    }
}
