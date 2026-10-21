package io.github.kianjross.herald.assistant

import android.content.Context
import android.net.Uri
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStream
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL
import java.util.UUID
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/** Transport for the shared phone.v1 API. ControlStore remains command-state authority. */
class BridgeClient(
    context: Context,
    private val identity: DeviceIdentity,
    baseUrl: String,
    private val executor: ExecutorService = Executors.newSingleThreadExecutor()
) {
    data class Unavailable(
        val code: String,
        val message: String,
        val retryable: Boolean,
        val requestId: String? = null
    )

    data class Result(val body: JSONObject? = null, val unavailable: Unavailable? = null)

    data class AttachmentReference(
        val kind: String,
        val identity: String,
        val revision: Long,
        val name: String? = null,
        val mediaType: String? = null,
        val uri: Uri? = null
    ) {
        fun toJson(): JSONObject {
            require(kind == "source" || kind == "artifact")
            require(revision > 0)
            return JSONObject().put("kind", kind)
                .put(if (kind == "source") "source_id" else "artifact_id", identity)
                .put("revision", revision)
                .apply {
                    name?.let { put("name", it) }
                    mediaType?.let { put("media_type", it) }
                    uri?.let { put("uri", it.toString()) }
                }
        }
    }

    private val appContext = context.applicationContext
    private val prefs = appContext.getSharedPreferences("phone_bridge_outbox", Context.MODE_PRIVATE)
    private val fallbackRoot = baseUrl.trimEnd('/')
    private val root: String get() = identity.backendUrl(fallbackRoot).trimEnd('/')
    private val streamExecutor = Executors.newSingleThreadExecutor()
    @Volatile private var streamConnection: HttpURLConnection? = null
    @Volatile private var streamGeneration = 0L

    fun stopStreaming() {
        streamGeneration += 1
        streamConnection?.disconnect()
        streamConnection = null
    }

    fun close() {
        stopStreaming()
        streamExecutor.shutdownNow()
        executor.shutdownNow()
    }

    fun autoPair(callback: (Result) -> Unit) {
        if (!identity.autoPairingAllowed()) {
            callback(Result(unavailable = Unavailable("pairing_paused", "Phone connection paused; choose Reconnect in Phone setup", false)))
            return
        }
        pairingRequest(root, "/api/mobile/v1/pairing/auto", JSONObject().put("installation_id", identity.installationId()), callback)
    }

    fun redeemPairing(endpoint: String, challenge: String, callback: (Result) -> Unit) {
        if (challenge.isBlank()) {
            callback(Result(unavailable = Unavailable("invitation_required", "A one-time invitation is required", false)))
            return
        }
        pairingRequest(endpoint, "/api/mobile/v1/pairing/redeem", JSONObject().put("challenge", challenge), callback)
    }

    private fun pairingRequest(endpoint: String, path: String, payload: JSONObject, callback: (Result) -> Unit) {
        executor.execute {
            var connection: HttpURLConnection? = null
            val result = try {
                // Validate before connecting; retain the endpoint only after redemption succeeds.
                val uri = java.net.URI(endpoint.trim())
                require(uri.scheme.equals("https", true) && !uri.host.isNullOrBlank() &&
                    uri.rawUserInfo == null && uri.rawQuery == null && uri.rawFragment == null) { "Use an HTTPS backend URL" }
                val redeemConnection = URL(endpoint.trim().trimEnd('/') + path).openConnection() as HttpURLConnection
                connection = redeemConnection
                redeemConnection.apply {
                    requestMethod = "POST"
                    instanceFollowRedirects = false
                    connectTimeout = 15_000
                    readTimeout = 30_000
                    setRequestProperty("Content-Type", "application/json")
                    doOutput = true
                    outputStream.bufferedWriter().use { it.write(payload.toString()) }
                }
                if (redeemConnection.responseCode !in 200..299) throw HttpStatus(redeemConnection.responseCode, errorText(redeemConnection))
                val body = JSONObject(redeemConnection.inputStream.bufferedReader().use { it.readText() })
                stopStreaming()
                synchronized(OUTBOX_LOCK) { prefs.edit().remove(OUTBOX).commit() }
                identity.installPairing(body.getString("device_credential"), body.getLong("pairing_revision"), body.getString("device_id"))
                identity.setBackendUrl(endpoint)
                Result(body)
            } catch (e: Exception) {
                val unavailable = asUnavailable(e)
                if (unavailable.code == "credential_rejected" || (path.endsWith("/pairing/auto") && e is HttpStatus && e.status == 403)) identity.forget()
                Result(unavailable = unavailable)
            }
            finally { connection?.disconnect() }
            callback(result)
        }
    }

    fun submitMessage(
        conversationId: String,
        content: String,
        contextId: String? = null,
        expectedContinuationRevision: Long? = null,
        attachments: List<AttachmentReference> = emptyList(),
        expiresAt: Double? = null,
        correlationId: String? = null,
        interactionContext: JSONObject? = null,
        callback: (Result) -> Unit
    ): String {
        val snapshot = identity.snapshot()
        val requestId = UUID.randomUUID().toString()
        if (snapshot == null) {
            callback(Result(unavailable = Unavailable("not_paired", "This phone is not paired", false, requestId)))
            return requestId
        }
        val envelope = JSONObject()
            .put("request_id", requestId)
            .put("conversation_id", conversationId)
            .put("content", content)
            .put("expected_pairing_revision", snapshot.pairingRevision)
            .put("attachments", JSONArray().apply { attachments.forEach { put(it.toJson()) } })
        correlationId?.takeIf { it.isNotBlank() }?.let { envelope.put("correlation_id", it) }
        interactionContext?.let {
            // Capture an immutable copy in the durable envelope. Replays must retain the
            // invocation identity and must never inherit a newer live session.
            envelope.put("interaction_context", JSONObject(it.toString()))
        }
        expiresAt?.let { envelope.put("expires_at", it) }
        if (!contextId.isNullOrBlank() && contextId != "null" && expectedContinuationRevision != null) {
            envelope.put("continuation", JSONObject()
                .put("context_id", contextId)
                .put("expected_revision", expectedContinuationRevision))
        }
        try {
            enqueue(OutboxItem(requestId, conversationId, snapshot.pairingRevision, envelope))
        } catch (failure: Exception) {
            callback(Result(unavailable = Unavailable("outbox_unavailable", failure.message ?: "Cannot retain request", false, requestId)))
            return requestId
        }
        replay { result ->
            val failure = result.unavailable
            callback(result)
            // A retained earlier item may block this submission. Report that queue outcome
            // against the current request too, so its live voice session cannot stay busy.
            if (failure != null && failure.requestId != requestId &&
                (failure.retryable || failure.requestId == null)) {
                callback(Result(unavailable = Unavailable(
                    "queue_blocked", "Request retained until phone delivery is available",
                    failure.retryable, requestId
                )))
            }
        }
        return requestId
    }

    fun voiceTransition(
        conversationId: String,
        sessionId: String,
        transition: String,
        requestId: String? = null,
        correlationId: String? = null,
        cancelled: Boolean? = null,
        value: Boolean? = null,
        callback: (Result) -> Unit = {}
    ) {
        val revision = identity.pairingRevision()
        val body = JSONObject()
            .put("expected_pairing_revision", revision)
            .put("session_id", sessionId)
            .put("transition", transition)
        requestId?.let { body.put("request_id", it) }
        correlationId?.let { body.put("correlation_id", it) }
        cancelled?.let { body.put("cancelled", it) }
        value?.let { body.put("value", it) }
        authenticated { credential ->
            request("POST", "/api/mobile/v1/conversations/${encode(conversationId)}/voice/session", body, credential)
        }.alsoResult(callback)
    }

    /** Retains captured envelopes in insertion order for the existing replay worker. */
    fun enqueueCapturedMessage(requestId: String, conversationId: String, content: String, attachments: JSONArray = JSONArray()): Boolean {
        val snapshot = identity.snapshot() ?: return false
        if (requestId.isBlank() || content.isBlank()) return false
        synchronized(OUTBOX_LOCK) {
            try {
                if (readOutbox().any { it.requestId == requestId }) return true
                enqueue(OutboxItem(requestId, conversationId, snapshot.pairingRevision, JSONObject()
                .put("request_id", requestId).put("conversation_id", conversationId)
                .put("content", content).put("expected_pairing_revision", snapshot.pairingRevision)
                .put("attachments", attachments))) } catch (_: Exception) { return false }
        }
        return true
    }

    /** Worker-thread barrier: the SSE reader never occupies this request executor. */
    fun replayAwait(timeoutSeconds: Long = 120): Result {
        val done = java.util.concurrent.CountDownLatch(1)
        var outcome = Result(JSONObject())
        replay(onComplete = { done.countDown() }) { outcome = it }
        if (!done.await(timeoutSeconds, java.util.concurrent.TimeUnit.SECONDS))
            return Result(unavailable = Unavailable("replay_timeout", "Offline replay is still pending", true))
        return outcome
    }

    fun replay(onComplete: () -> Unit = {}, callback: (Result) -> Unit = {}) {
        executor.execute {
            var replayRequestId: String? = null
            try {
            val currentRevision = identity.pairingRevision()
            val credential = identity.snapshot()?.credential
            if (credential == null) {
                callback(Result(unavailable = Unavailable("not_paired", "This phone is not paired", false)))
                return@execute
            }
            while (true) {
                val item = synchronized(OUTBOX_LOCK) { readOutbox().firstOrNull() } ?: return@execute
                replayRequestId = item.requestId
                if (item.pairingRevision != currentRevision) {
                    val failure = Unavailable("stale_pairing_revision", "Queued request belongs to an old pairing", false, item.requestId)
                    synchronized(OUTBOX_LOCK) { reject(item, failure) }
                    callback(Result(unavailable = failure))
                    continue
                }
                // Older clients persisted JSON null as a literal context ID. Repair only
                // that absent-context sentinel, preserving real continuation revisions.
                val continuation = item.envelope.optJSONObject("continuation")
                if (continuation != null && (continuation.isNull("context_id") ||
                    continuation.optString("context_id").isBlank() ||
                    continuation.optString("context_id") == "null")) {
                    item.envelope.remove("continuation")
                    synchronized(OUTBOX_LOCK) {
                        writeOutbox(readOutbox().map { if (it.requestId == item.requestId) item else it })
                    }
                }
                val result = request(
                    "POST",
                    "/api/mobile/v1/conversations/${encode(item.conversationId)}/messages",
                    item.envelope,
                    credential
                )
                val failure = result.unavailable?.copy(requestId = item.requestId)
                if (failure != null) {
                    if (!failure.retryable) synchronized(OUTBOX_LOCK) { reject(item, failure) }
                    callback(Result(unavailable = failure))
                    if (failure.retryable) return@execute
                    continue
                }
                callback(result)
                synchronized(OUTBOX_LOCK) { remove(item.requestId) }
            }
            } catch (failure: Exception) {
                callback(Result(unavailable = Unavailable("outbox_unavailable", failure.message ?: "Offline replay cannot read or persist its queue", true, replayRequestId)))
            } finally { onComplete() }
        }
    }

    /** Cancellation is definitive only while still queued locally. */
    fun cancelQueued(requestId: String): Boolean = synchronized(OUTBOX_LOCK) {
        val found = readOutbox().any { it.requestId == requestId }
        if (found) remove(requestId)
        found
    }

    fun history(conversationId: String, afterSequence: Long, callback: (Result) -> Unit) = authenticated {
        request("GET", "/api/mobile/v1/conversations/${encode(conversationId)}/history?after_sequence=$afterSequence", null, it)
    }.alsoResult(callback)

    /** Reads phone.v1 SSE records incrementally and resumes from the last authoritative revision. */
    fun streamEvents(
        conversationId: String,
        afterRevision: Long,
        onEvent: (revision: Long, event: JSONObject) -> Unit,
        onClosed: (Unavailable?) -> Unit
    ) {
        stopStreaming()
        val generation = streamGeneration
        streamExecutor.execute {
            val credential = identity.snapshot()?.credential
            if (credential == null) {
                onClosed(Unavailable("not_paired", "This phone is not paired", false)); return@execute
            }
            var connection: HttpURLConnection? = null
            try {
                connection = open("GET", "/api/mobile/v1/conversations/${encode(conversationId)}/events?after_revision=$afterRevision", credential)
                if (generation != streamGeneration) return@execute
                streamConnection = connection
                connection.setRequestProperty("Accept", "text/event-stream")
                if (connection.responseCode !in 200..299) throw HttpStatus(connection.responseCode, errorText(connection))
                val reader = BufferedReader(InputStreamReader(connection.inputStream))
                var revision: Long? = null
                var eventKind = "conversation.message"
                val data = StringBuilder()
                reader.forEachLine { line ->
                    if (generation != streamGeneration) return@forEachLine
                    when {
                        line.startsWith("id:") -> revision = line.substringAfter(':').trim().toLongOrNull()
                        line.startsWith("event:") -> eventKind = line.substringAfter(':').trim()
                        line.startsWith("data:") -> data.append(line.substringAfter(':').trim())
                        line.isBlank() && data.isNotEmpty() -> {
                            onEvent(revision ?: 0, JSONObject(data.toString()).put("_event_kind", eventKind))
                            revision = null; eventKind = "conversation.message"; data.setLength(0)
                        }
                    }
                }
                if (generation == streamGeneration) onClosed(null)
            } catch (e: Exception) {
                if (generation == streamGeneration) onClosed(asUnavailable(e))
            } finally {
                connection?.disconnect()
            }
        }
    }

    fun pendingCommands(callback: (Result) -> Unit) = authenticated { credential ->
        request("GET", "/api/mobile/v1/commands?pairing_revision=${identity.pairingRevision()}", null, credential)
    }.alsoResult(callback)

    fun requestStates(conversationId: String, callback: (Result) -> Unit) = authenticated { credential ->
        request("GET", "/api/mobile/v1/conversations/${encode(conversationId)}/requests", null, credential)
    }.alsoResult(callback)

    fun acceptCommand(commandId: String, owner: String, leaseSeconds: Int, callback: (Result) -> Unit) =
        postCommand(commandId, "accept", JSONObject().put("owner", owner)
            .put("lease_seconds", leaseSeconds).put("pairing_revision", identity.pairingRevision()), callback)

    /** Reports an observed terminal outcome; acceptance is never represented as completion. */
    fun completeCommand(
        commandId: String,
        owner: String,
        leaseToken: String,
        result: JSONObject? = null,
        error: JSONObject? = null,
        callback: (Result) -> Unit
    ) {
        require((result == null) != (error == null)) { "exactly one of result or error is required" }
        val body = JSONObject().put("owner", owner).put("lease_token", leaseToken)
            .put("pairing_revision", identity.pairingRevision())
            .put(if (result != null) "result" else "error", result ?: error)
        postCommand(commandId, "result", body, callback)
    }

    fun reportGrantEvidence(callback: (Result) -> Unit) {
        val body = identity.grantEvidenceEnvelope()
        if (body == null) callback(Result(unavailable = Unavailable("not_paired", "This phone is not paired", false)))
        else authenticated { request("POST", "/api/mobile/v1/grants", body, it) }.alsoResult(callback)
    }

    /** Streams a shared content URI without loading the attachment into memory. */
    fun streamAttachment(uri: Uri, consume: (ByteArray, Int) -> Unit) {
        appContext.contentResolver.openInputStream(uri).use { input ->
            requireNotNull(input) { "attachment is unavailable" }
            stream(input, consume)
        }
    }

    private fun stream(input: InputStream, consume: (ByteArray, Int) -> Unit) {
        val buffer = ByteArray(64 * 1024)
        while (true) {
            val read = input.read(buffer)
            if (read < 0) return
            consume(buffer, read)
        }
    }

    // Interactive canvas cards: fetched and answered with this phone's credential; the
    // WebView that renders them never holds it (and its renderer ships in the app).
    fun canvasCard(cardId: String, callback: (Result) -> Unit) =
        authenticated { request("GET", "/api/mobile/v1/canvas/cards/${encode(cardId)}", null, it) }.alsoResult(callback)

    fun canvasCardState(cardId: String, state: JSONArray, callback: (Result) -> Unit = {}) =
        authenticated { request("POST", "/api/mobile/v1/canvas/cards/${encode(cardId)}/state", JSONObject().put("state", state), it) }
            .alsoResult(callback)

    fun canvasCardEvent(cardId: String, event: JSONObject, callback: (Result) -> Unit = {}) =
        authenticated { request("POST", "/api/mobile/v1/canvas/cards/${encode(cardId)}/events", event, it) }.alsoResult(callback)

    private fun postCommand(commandId: String, action: String, body: JSONObject, callback: (Result) -> Unit) =
        authenticated { request("POST", "/api/mobile/v1/commands/${encode(commandId)}/$action", body, it) }.alsoResult(callback)

    data class SpeechAudio(val audio: ByteArray? = null, val error: String? = null)

    fun transcribeAudio(audio: ByteArray, callback: (Result) -> Unit) {
        executor.execute {
            var connection: HttpURLConnection? = null
            val result = try {
                val credential = identity.snapshot()?.credential ?: error("This phone is not paired")
                connection = open("POST", "/api/mobile/v1/voice/transcribe", credential)
                connection.readTimeout = 250_000
                connection.setRequestProperty("Content-Type", "audio/mp4")
                connection.doOutput = true
                connection.outputStream.use { it.write(audio) }
                if (connection.responseCode !in 200..299) throw HttpStatus(connection.responseCode, errorText(connection))
                Result(JSONObject(connection.inputStream.bufferedReader().use { it.readText() }))
            } catch (e: Exception) { Result(unavailable = asUnavailable(e)) }
            finally { connection?.disconnect() }
            callback(result)
        }
    }

    fun synthesizeSpeech(text: String, callback: (SpeechAudio) -> Unit) {
        executor.execute {
            var connection: HttpURLConnection? = null
            val result = try {
                val credential = identity.snapshot()?.credential ?: error("This phone is not paired")
                connection = open("POST", "/api/mobile/v1/voice/speech", credential)
                connection.readTimeout = 190_000
                connection.setRequestProperty("Content-Type", "application/json")
                connection.doOutput = true
                connection.outputStream.bufferedWriter().use { it.write(JSONObject().put("text", text).toString()) }
                if (connection.responseCode !in 200..299) throw HttpStatus(connection.responseCode, errorText(connection))
                SpeechAudio(connection.inputStream.use { it.readBytes() })
            } catch (e: Exception) { SpeechAudio(error = e.message ?: "Custom speech unavailable") }
            finally { connection?.disconnect() }
            callback(result)
        }
    }

    private fun authenticated(block: (String) -> Result): Pending {
        val credential = identity.snapshot()?.credential
        val operation: (() -> Result)? = if (credential == null) null else ({ block(credential) })
        return Pending(operation)
    }

    private inner class Pending(private val operation: (() -> Result)?) {
        fun alsoResult(callback: (Result) -> Unit) {
            if (operation == null) callback(Result(unavailable = Unavailable("not_paired", "This phone is not paired", false)))
            else executor.execute { callback(operation.invoke()) }
        }
    }

    private fun request(method: String, path: String, body: JSONObject?, credential: String): Result {
        var connection: HttpURLConnection? = null
        return try {
            connection = open(method, path, credential)
            body?.let {
                connection.setRequestProperty("Content-Type", "application/json")
                connection.doOutput = true
                connection.outputStream.bufferedWriter(Charsets.UTF_8).use { writer -> writer.write(it.toString()) }
            }
            val status = connection.responseCode
            if (status !in 200..299) throw HttpStatus(status, errorText(connection))
            val text = connection.inputStream.bufferedReader().use { it.readText() }
            Result(if (text.isBlank()) JSONObject() else JSONObject(text))
        } catch (e: Exception) {
            val unavailable = asUnavailable(e)
            if (unavailable.code == "credential_rejected") {
                identity.forget()
                // Queue replay retains each terminal envelope before removing it.
                // A rejected credential must not erase unrelated pending requests.
            }
            Result(unavailable = unavailable)
        } finally {
            connection?.disconnect()
        }
    }

    private fun open(method: String, path: String, credential: String) =
        (URL(root + path).openConnection() as HttpURLConnection).apply {
            instanceFollowRedirects = false
            requestMethod = method
            connectTimeout = 15_000
            readTimeout = 60_000
            setRequestProperty("Authorization", "Bearer $credential")
            setRequestProperty("Accept", "application/json")
        }

    private fun errorText(connection: HttpURLConnection): String =
        connection.errorStream?.bufferedReader()?.use { it.readText() }.orEmpty()

    private fun asUnavailable(error: Exception): Unavailable = when (error) {
        is HttpStatus -> when (error.status) {
            401 -> Unavailable("credential_rejected", error.body.ifBlank { "Device credential was rejected" }, false)
            403 -> Unavailable("capability_unavailable", error.body.ifBlank { "Phone permission or capability is unavailable" }, false)
            409 -> Unavailable("revision_conflict", error.body.ifBlank { "Revision conflict" }, false)
            410 -> Unavailable("expired", error.body.ifBlank { "Request expired" }, false)
            in 500..599 -> Unavailable("backend_unavailable", error.body.ifBlank { "Assistant backend unavailable" }, true)
            else -> Unavailable("http_${error.status}", error.body.ifBlank { "Request failed" }, false)
        }
        else -> Unavailable("transport_unavailable", error.message ?: "Network unavailable", true)
    }

    private fun enqueue(item: OutboxItem) = synchronized(OUTBOX_LOCK) {
        val items = readOutbox().toMutableList()
        items.removeAll { it.requestId == item.requestId }
        items.add(item)
        check(items.size <= MAX_OUTBOX) { "Offline outbox is full; pending requests are retained" }
        writeOutbox(items)
    }

    /** Keep terminal delivery evidence before letting the next queued request proceed. */
    private fun reject(item: OutboxItem, failure: Unavailable) {
        val rejected = JSONArray(prefs.getString(REJECTED_OUTBOX, "[]"))
        if ((0 until rejected.length()).none {
                rejected.getJSONObject(it).optString("request_id") == item.requestId
            }) {
            rejected.put(item.toJson().put("failure", JSONObject()
                .put("code", failure.code).put("message", failure.message)))
            check(prefs.edit().putString(REJECTED_OUTBOX, rejected.toString()).commit()) {
                "Cannot retain rejected request"
            }
        }
        remove(item.requestId)
    }

    private fun remove(requestId: String) = synchronized(OUTBOX_LOCK) {
        writeOutbox(readOutbox().filterNot { it.requestId == requestId })
    }

    private fun readOutbox(): List<OutboxItem> = try {
        val array = JSONArray(prefs.getString(OUTBOX, "[]"))
        (0 until array.length()).map { OutboxItem.fromJson(array.getJSONObject(it)) }
    } catch (failure: Exception) { throw IllegalStateException("Offline outbox cannot be read; retained for recovery", failure) }

    private fun writeOutbox(items: List<OutboxItem>) {
        check(prefs.edit().putString(OUTBOX, JSONArray().apply { items.forEach { put(it.toJson()) } }.toString()).commit()) { "Cannot persist offline request" }
    }

    private data class OutboxItem(
        val requestId: String,
        val conversationId: String,
        val pairingRevision: Long,
        val envelope: JSONObject
    ) {
        fun toJson() = JSONObject().put("request_id", requestId).put("conversation_id", conversationId)
            .put("pairing_revision", pairingRevision).put("envelope", envelope)
        companion object {
            fun fromJson(value: JSONObject) = OutboxItem(value.getString("request_id"),
                value.getString("conversation_id"), value.getLong("pairing_revision"), value.getJSONObject("envelope"))
        }
    }

    private class HttpStatus(val status: Int, val body: String) : Exception("HTTP $status")

    companion object {
        private val OUTBOX_LOCK = Any()
        private const val OUTBOX = "ordered_requests"
        private const val REJECTED_OUTBOX = "rejected_requests"
        private const val MAX_OUTBOX = 128
        private fun encode(value: String): String = java.net.URLEncoder.encode(value, "UTF-8")
    }
}
