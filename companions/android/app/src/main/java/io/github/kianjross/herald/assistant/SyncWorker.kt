package io.github.kianjross.herald.assistant

import android.content.Context
import androidx.work.*
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.isActive
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.RequestBody.Companion.asRequestBody
import java.io.File
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.UUID
import java.util.concurrent.TimeUnit

/** WorkManager supplies persistent scheduling; ControlStore owns command state.
 * A durable dispatch marker is written BEFORE any platform effect. An interrupted
 * marker is reconciled as ambiguous, never replayed. Results survive lost HTTP acks.
 */
class SyncWorker(appContext: Context, params: WorkerParameters) : CoroutineWorker(appContext, params) {
    companion object {
        private val JSON = "application/json; charset=utf-8".toMediaType()
        private fun constraints() = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED).setRequiresBatteryNotLow(true).build()
        fun scheduleImmediateSync(context: Context) {
            WorkManager.getInstance(context).enqueueUniqueWork("ua_sync_immediate",
                ExistingWorkPolicy.KEEP, OneTimeWorkRequestBuilder<SyncWorker>()
                    .setConstraints(constraints()).build())
        }
        fun schedulePeriodicSync(context: Context) {
            WorkManager.getInstance(context).enqueueUniquePeriodicWork("ua_sync_worker",
                ExistingPeriodicWorkPolicy.KEEP, PeriodicWorkRequestBuilder<SyncWorker>(15, TimeUnit.MINUTES)
                    .setConstraints(constraints()).build())
        }
        // Periodic and immediate requests share one journal/dispatch critical section.
        private val syncMutex = kotlinx.coroutines.sync.Mutex()
    }
    private val identity = DeviceIdentity(applicationContext)
    private val http = OkHttpClient.Builder().connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS).writeTimeout(120, TimeUnit.SECONDS).build()
    private val journal = applicationContext.getSharedPreferences("phone_command_results", Context.MODE_PRIVATE)
    private val announcementJournal = applicationContext.getSharedPreferences("phone_announcement_deliveries", Context.MODE_PRIVATE)

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        syncMutex.lock()
        try {
            val snapshot = identity.snapshot() ?: return@withContext Result.success()
            val root = identity.backendUrl(applicationContext.getString(R.string.bridge_base_url)).trimEnd('/')
            if (root.isBlank()) return@withContext Result.success()
            val grants = JSONObject().put("pairing_revision", snapshot.pairingRevision)
                .put("evidence_id", UUID.randomUUID().toString())
                .put("grants", JSONObject(identity.observeCapabilityGrants()))
            send(root, snapshot.credential, "/grants", grants)
            replayResults(root, snapshot.credential, snapshot.pairingRevision)
            drainOutbox(root, snapshot.credential, snapshot.pairingRevision)
            poll(root, snapshot.credential, snapshot.pairingRevision)
            Result.success()
        } catch (cancelled: CancellationException) {
            throw cancelled
        } catch (_: IOException) {
            Result.retry()
        } catch (_: Exception) {
            Result.retry()
        } finally { syncMutex.unlock() }
    }

    private fun send(root: String, credential: String, path: String, body: JSONObject? = null,
                     timeoutMillis: Long? = null): JSONObject {
        val builder = Request.Builder().url("$root/api/mobile/v1$path")
            .header("Authorization", "Bearer $credential")
        if (body != null) builder.post(body.toString().toRequestBody(JSON)) else builder.get()
        val client = if(timeoutMillis == null) http else http.newBuilder()
            .callTimeout(timeoutMillis, TimeUnit.MILLISECONDS).build()
        client.newCall(builder.build()).execute().use { response ->
            if (response.code == 401) {
                identity.forget()
                throw IOException("Phone credential revoked or stale")
            }
            if (!response.isSuccessful) throw IOException("Mobile endpoint $path returned ${response.code}")
            return JSONObject(response.body?.string().orEmpty().ifBlank { "{}" })
        }
    }

    private fun save(id: String, entry: JSONObject) {
        check(journal.edit().putString(id, entry.toString()).commit()) { "Cannot persist command outcome" }
    }
    private fun replayResults(root: String, credential: String, revision: Long) {
        for ((id, value) in journal.all) {
            val entry = JSONObject(value as? String ?: continue)
            // Keep old-revision evidence for reconciliation, never replay it with new authority.
            if (entry.optLong("pairing_revision") != revision) continue
            if (!entry.has("result") && !entry.has("error")) {
                entry.put("error", "Execution interrupted after durable dispatch marker; outcome unknown, do not replay")
                entry.put("review_required", true)
                save(id, entry)
            }
            if (entry.optBoolean("acknowledged")) continue
            finishScreenUpload(root, credential, id, entry)
            acknowledge(root, credential, id, entry)
        }
    }
    private fun acknowledge(root: String, credential: String, id: String, entry: JSONObject) {
        val body = JSONObject().put("owner", entry.getString("owner"))
            .put("lease_token", entry.getString("lease_token"))
            .put("pairing_revision", entry.getLong("pairing_revision"))
        if (entry.has("result")) body.put("result", entry.getJSONObject("result"))
        else body.put("error", entry.get("error"))
        val ack = send(root, credential, "/commands/$id/result", body)
        val state = ack.optJSONObject("command")?.optString("state")
        // Terminal duplicate acknowledgement is reconciliation, not proof the effect succeeded.
        if (ack.optBoolean("acknowledged") || state in setOf("completed", "done", "failed", "interrupted", "expired", "cancelled")) {
            entry.put("acknowledged", true).put("authoritative_state", state)
            save(id, entry)
        } else throw IOException("Command result not acknowledged")
    }

    private suspend fun poll(root: String, credential: String, revision: Long) {
        val commands = send(root, credential, "/commands?pairing_revision=$revision").optJSONArray("commands") ?: JSONArray()
        for (i in 0 until commands.length()) {
            if (isStopped) return
            val observed = commands.getJSONObject(i)
            val id = observed.getString("id")
            // Even if a server redelivers a queued record, never repeat our dispatch.
            if (journal.contains(id)) continue
            if (observed.optString("target_device") != DeviceIdentity.current(applicationContext) || observed.optBoolean("cancel_requested")) continue
            if (observed.optJSONObject("arguments")?.optLong("pairing_revision") != revision) continue
            if (System.currentTimeMillis() / 1000.0 >= observed.optDouble("expires_at", 0.0)) continue
            val owner = "${DeviceIdentity.current(applicationContext)}:worker-${UUID.randomUUID()}"
            val accepted = send(root, credential, "/commands/$id/accept", JSONObject()
                .put("owner", owner).put("pairing_revision", revision).put("lease_seconds", 60))
                .optJSONObject("command") ?: continue
            val token = accepted.optString("lease_token")
            if (token.isBlank() || accepted.optString("owner") != owner) continue
            val heartbeat = send(root, credential, "/commands/$id/heartbeat", JSONObject()
                .put("owner", owner).put("lease_token", token).put("pairing_revision", revision)
                .put("lease_seconds", 60).put("running", true)
                .put("checkpoint", JSONObject().put("phase", "dispatching").put("side_effect_dispatched", true)))
            val current = heartbeat.optJSONObject("command") ?: continue
            if (!heartbeat.optBoolean("heartbeated") || current.optBoolean("cancel_requested") ||
                current.optString("target_device") != DeviceIdentity.current(applicationContext) ||
                current.optJSONObject("arguments")?.optLong("pairing_revision") != revision ||
                System.currentTimeMillis() / 1000.0 >= minOf(current.optDouble("expires_at", 0.0), current.optDouble("lease_until", 0.0))) continue
            val entry = JSONObject().put("owner", owner).put("lease_token", token)
                .put("pairing_revision", revision).put("phase", "dispatching")
            save(id, entry)
            if (isStopped || identity.pairingRevision() != revision) return
            val args = current.optJSONObject("arguments") ?: JSONObject()
            val map = mutableMapOf<String, Any?>()
            args.keys().forEach { key -> map[key] = args.opt(key) }
            // Android services/UI/clipboard APIs execute on the platform main thread.
            val command = current.getString("command")
            val correlation = current.optString("correlation_id")
            if (command == "phone.announcement") {
                val invalid = validateAnnouncement(current, args, revision, correlation)
                if (invalid != null) {
                    val rejected = CommandDispatcher.ExecutionResult(false,
                        result = mapOf("command_id" to id, "delivery_state" to "rejected"),
                        error = invalid, evidence = identity.executionEvidence())
                    entry.put("error", JSONObject(rejected.toMap())).put("phase", "result_pending")
                    save(id, entry)
                    acknowledge(root, credential, id, entry)
                    continue
                }
                val deliveryKey = "${args.getString("record_id")}:${args.getLong("record_revision")}:$correlation"
                if (announcementJournal.contains(deliveryKey)) {
                    val duplicate = CommandDispatcher.ExecutionResult(true, mapOf(
                        "record_id" to args.getString("record_id"),
                        "record_revision" to args.getLong("record_revision"),
                        "correlation_id" to correlation, "delivery_state" to "deduplicated"),
                        evidence = identity.executionEvidence())
                    entry.put("result", JSONObject(duplicate.toMap())).put("phase", "result_pending")
                    save(id, entry)
                    acknowledge(root, credential, id, entry)
                    continue
                }
                // At-most-once fence: persist before TTS. A process death may miss this
                // announcement, but redelivery and a lost acknowledgement cannot repeat it.
                check(announcementJournal.edit().putString(deliveryKey,
                    JSONObject().put("command_id", id).put("state", "dispatching").toString()).commit()) {
                    "Cannot persist announcement delivery fence"
                }
                map["_command_id"] = id
                map["_command_revision"] = current.getLong("revision")
                map["_command_correlation_id"] = correlation
                // Playback may not outlive either the command expiry or this accepted lease.
                map["_command_expires_at"] = minOf(current.getDouble("expires_at"), current.getDouble("lease_until"))
                map["_command_target_device"] = current.getString("target_device")
                map["_command_owner"] = owner
                map["_command_lease_token"] = token
            }
            val privileged = command in setOf("privileged_shell", "privileged_files")
            val deadlineMillis = (minOf(current.optDouble("expires_at",0.0),
                current.optDouble("lease_until",0.0)) * 1000.0).toLong()
            val authority: (() -> Unit)? = if(!privileged) null else {
                {
                    check(!isStopped && identity.pairingRevision() == revision) { "Phone worker stopped or pairing changed" }
                    val remaining = deadlineMillis - System.currentTimeMillis()
                    check(remaining > 0) { "Privileged command lease or expiry elapsed" }
                    val renewed = send(root, credential, "/commands/$id/heartbeat", JSONObject()
                        .put("owner",owner).put("lease_token",token).put("pairing_revision",revision)
                        .put("lease_seconds",60).put("running",true)
                        .put("checkpoint",JSONObject().put("phase","privileged_running")
                            .put("side_effect_dispatched",true)), minOf(3000L,remaining))
                    val observedCommand = renewed.optJSONObject("command")
                    check(renewed.optBoolean("heartbeated") && observedCommand != null &&
                        !observedCommand.optBoolean("cancel_requested") &&
                        observedCommand.optString("target_device") == DeviceIdentity.current(applicationContext) &&
                        observedCommand.optString("owner") == owner &&
                        observedCommand.optString("lease_token") == token &&
                        observedCommand.optJSONObject("arguments")?.optLong("pairing_revision") == revision &&
                        System.currentTimeMillis()/1000.0 < minOf(observedCommand.optDouble("expires_at",0.0),
                            observedCommand.optDouble("lease_until",0.0))) { "Privileged command authority was cancelled, revoked, expired or transferred" }
                }
            }
            val result = try {
                CommandDispatcher(applicationContext).dispatchAwait(command, map,
                    if(privileged) deadlineMillis else null, authority)
            } catch(cancelled: CancellationException) {
                // Dispatch was durably marked before the OS call. Cancellation
                // alone cannot establish whether its effect happened.
                withContext(NonCancellable) {
                    val unknown = CommandDispatcher.ExecutionResult(false,
                        result = mapOf("outcome_unknown" to true, "worker_cancelled" to true),
                        error = "Phone worker cancelled after dispatch; execution outcome unknown",
                        evidence = identity.executionEvidence())
                    entry.put("error", JSONObject(unknown.toMap())).put("phase", "result_pending")
                    save(id, entry)
                }
                throw cancelled
            }
            withContext(NonCancellable) {
                if (command == "phone.announcement") {
                    val deliveryKey = "${args.getString("record_id")}:${args.getLong("record_revision")}:$correlation"
                    announcementJournal.edit().putString(deliveryKey, JSONObject()
                        .put("command_id", id)
                        .put("state", if (result.success) "spoken" else "outcome_unknown")
                        .toString()).commit()
                }
                if (result.success) entry.put("result", JSONObject(result.toMap()))
                else entry.put("error", JSONObject(result.toMap()))
                entry.put("phase", "result_pending")
                save(id, entry)
            }
            // Journal survives cancelled coroutines; the next connected worker
            // reconciles/acknowledges it. Never run HTTP ack in cancelled work.
            if(isStopped || !currentCoroutineContext().isActive)
                throw CancellationException("Phone command outcome saved for reconciliation")
            finishScreenUpload(root, credential, id, entry)
            acknowledge(root, credential, id, entry)
        }
    }

    private fun validateAnnouncement(command: JSONObject, args: JSONObject, revision: Long,
                                     correlation: String): String? {
        if (command.optString("command") != "phone.announcement") return "Invalid announcement command kind"
        if (command.optString("target_device") != DeviceIdentity.current(applicationContext) ||
            args.optString("protocol") != "phone.v1" ||
            args.optLong("pairing_revision") != revision) return "Invalid announcement pairing authority"
        if (command.optLong("revision") <= 0 || args.optLong("record_revision") <= 0 ||
            args.optString("record_id").isBlank() || args.optString("record_id").length > 256)
            return "Invalid announcement record identity"
        if (correlation.isBlank() || correlation.length > 256 || args.optString("correlation_id") != correlation)
            return "Invalid announcement correlation"
        val now = System.currentTimeMillis() / 1000.0
        val expiry = command.optDouble("expires_at", 0.0)
        if (!expiry.isFinite() || now >= expiry) return "Announcement expired before dispatch"
        val text = args.optString("text").trim()
        if (text.isEmpty() || text.length > 2_000) return "Announcement content is empty or exceeds the speakable bound"
        return null
    }

    /** Persist captured bytes before upload; a lost connection retries upload, not capture. */
    private fun finishScreenUpload(root: String, credential: String, id: String, entry: JSONObject) {
        val payload = entry.optJSONObject("result")?.optJSONObject("result") ?: return
        val capture = payload.optJSONObject("capture_file") ?: return
        val local = File(capture.getString("local_path")).canonicalFile
        val directory = File(applicationContext.filesDir, "screen-captures").canonicalFile
        val shared = File(applicationContext.filesDir, "shared-captures").canonicalFile
        check(local.parentFile == directory || local.parentFile == shared) { "Capture must belong to private storage" }
        val reference = uploadCapture(root, credential, capture)
        payload.remove("capture_file")
        payload.put("attachment", reference).put("attachments", JSONArray().put(reference))
        entry.put("phase", "result_pending")
        save(id, entry)
        // Only delete after authenticated server source identity is durable in the journal.
        local.delete()
    }

    private fun uploadCapture(root: String, credential: String, file: JSONObject): JSONObject {
        val local = File(file.getString("local_path"))
        check(local.isFile && local.length() <= 64L * 1024 * 1024) { "Captured bytes unavailable or exceed 64 MB" }
        val type = file.optString("media_type", "application/octet-stream")
        val url = "$root/api/mobile/v1/files/${file.getString("file_id")}".toHttpUrl().newBuilder()
            .addQueryParameter("name", file.optString("name", "shared-file"))
            .addQueryParameter("media_type", type).build()
        val request = Request.Builder().url(url).header("Authorization", "Bearer $credential")
            .post(local.asRequestBody(type.toMediaType())).build()
        http.newCall(request).execute().use { response ->
            if (response.code == 401) identity.forget()
            if (!response.isSuccessful) throw IOException("File upload returned ${response.code}")
            val reference = JSONObject(response.body?.string().orEmpty())
            check(reference.optString("kind") == "source" && reference.optString("source_id").isNotBlank() &&
                reference.optLong("revision") > 0) { "Upload returned no versioned source identity" }
            return reference
        }
    }

    private fun drainOutbox(root: String, credential: String, revision: Long) {
        val bridge = BridgeClient(applicationContext, identity, root)
        try {
            var failed = false
            for (item in ShareReceiverActivity.getOutboxItems(applicationContext)) {
                if (isStopped) break
                if (item.optLong("pairing_revision", revision) != revision) continue
                try {
                    val attachments = JSONArray()
                    val files = item.optJSONArray("files") ?: JSONArray()
                    if ((item.has("uri") || item.has("uris")) && files.length() == 0)
                        throw IOException("Legacy share has no retained bytes; share the file again")
                    for (index in 0 until files.length()) {
                        val file = files.getJSONObject(index)
                        if (!file.has("attachment")) {
                            if (file.has("capture_error")) throw IOException(file.getString("capture_error"))
                            file.put("attachment", uploadCapture(root, credential, file))
                            // Persist each successful upload before submitting/retrying conversation.
                            ShareReceiverActivity.updateOutboxItem(applicationContext, item)
                        }
                        attachments.put(file.getJSONObject("attachment"))
                    }
                    val content = item.optString("text").ifBlank {
                        item.optString("url").ifBlank { item.optString("subject").ifBlank { "Shared ${files.length()} file(s)" } }
                    }
                    val conversation = item.optString("conversation_id").ifBlank {
                        applicationContext.getSharedPreferences("conversation_ui", Context.MODE_PRIVATE)
                            .getString("conversation_id", "personal") ?: "personal"
                    }
                    if (bridge.enqueueCapturedMessage(item.getString("capture_id"), conversation, content, attachments)) {
                        ShareReceiverActivity.removeOutboxItems(applicationContext, setOf(item.getString("capture_id")))
                        // Source bytes are already durable server-side and the shared outbox owns delivery.
                        for (index in 0 until files.length()) {
                            val path = files.getJSONObject(index).optString("local_path")
                            if (path.isNotBlank()) {
                                val local = File(path).canonicalFile
                                val spool = File(applicationContext.filesDir, "shared-captures").canonicalFile
                                if (local.parentFile == spool) local.delete()
                            }
                        }
                    }
                } catch (error: Exception) {
                    item.put("upload_error", error.message ?: "Capture upload failed")
                    ShareReceiverActivity.updateOutboxItem(applicationContext, item)
                    failed = true
                    if (identity.snapshot() == null) throw IOException("Phone credential unavailable")
                }
            }
            val result = bridge.replayAwait()
            result.unavailable?.let { throw IOException(it.message) }
            if (failed) throw IOException("Shared uploads remain pending; see retained capture errors")
        } finally { bridge.close() }
    }
}
