package io.github.kianjross.herald.assistant

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import android.widget.Toast
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.UUID

/**
 * ShareReceiverActivity captures shared tasks, text, URLs, and content-URIs from Android's
 * system share sheet and deep links.
 *
 * It persists capture identity (idempotency key / capture ID) and enqueues items into the
 * local offline outbox for SyncWorker or BridgeClient to upload to the assistant stream.
 */
class ShareReceiverActivity : Activity() {

    companion object {
        const val PREFS_NAME = "ua_offline_outbox"
        const val KEY_OUTBOX = "outbox_items"

        /**
         * Enqueue a capture item directly into persistent offline storage.
         */
        @Synchronized
        fun enqueueOutboxItem(context: Context, item: JSONObject) {
            val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            val existingStr = prefs.getString(KEY_OUTBOX, "[]") ?: "[]"
            val array = try {
                JSONArray(existingStr)
            } catch (_: Throwable) {
                JSONArray()
            }
            DeviceIdentity(context).snapshot()?.let { item.put("pairing_revision", it.pairingRevision) }
            array.put(item)
            check(prefs.edit().putString(KEY_OUTBOX, array.toString()).commit()) { "Cannot persist shared capture" }
        }

        /**
         * Get all pending items in the offline outbox.
         */
        @Synchronized
        fun getOutboxItems(context: Context): List<JSONObject> {
            val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            val existingStr = prefs.getString(KEY_OUTBOX, "[]") ?: "[]"
            val result = mutableListOf<JSONObject>()
            try {
                val array = JSONArray(existingStr)
                for (i in 0 until array.length()) {
                    result.add(array.getJSONObject(i))
                }
            } catch (_: Throwable) {
            }
            return result
        }

        /**
         * Clear or replace the outbox items after successful drainage.
         */
        @Synchronized
        fun setOutboxItems(context: Context, items: List<JSONObject>) {
            val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            val array = JSONArray()
            for (item in items) {
                array.put(item)
            }
            check(prefs.edit().putString(KEY_OUTBOX, array.toString()).commit()) { "Cannot persist shared capture" }
        }

        /** Update only one capture, retaining concurrent shares and uploaded identities. */
        @Synchronized
        fun updateOutboxItem(context: Context, item: JSONObject) {
            val items = getOutboxItems(context).map {
                if (it.optString("capture_id") == item.getString("capture_id")) item else it
            }
            setOutboxItems(context, items)
        }

        /** Remove acknowledged captures without overwriting items enqueued during a sync. */
        @Synchronized
        fun removeOutboxItems(context: Context, captureIds: Set<String>) {
            if (captureIds.isEmpty()) return
            val retained = getOutboxItems(context).filterNot {
                it.optString("capture_id") in captureIds
            }
            setOutboxItems(context, retained)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val intent = intent
        val action = intent?.action
        val type = intent?.type

        if (Intent.ACTION_SEND == action && type != null) {
            handleSend(intent, type)
        } else if (Intent.ACTION_SEND_MULTIPLE == action && type != null) {
            handleSendMultiple(intent, type)
        } else if (Intent.ACTION_VIEW == action) {
            handleDeepLink(intent)
        } else {
            finish()
        }
    }

    private fun handleSend(intent: Intent, type: String) {
        val sharedText = intent.getStringExtra(Intent.EXTRA_TEXT)
        val sharedSubject = intent.getStringExtra(Intent.EXTRA_SUBJECT)
        val streamUri = intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)

        val captureId = UUID.randomUUID().toString()
        val now = System.currentTimeMillis() / 1000.0

        val item = JSONObject().apply {
            put("type", "share_capture")
            put("capture_id", captureId)
            put("created_at", now)
            put("mime_type", type)
            if (!sharedSubject.isNullOrBlank()) {
                put("subject", sharedSubject)
            }
            if (!sharedText.isNullOrBlank()) {
                put("text", sharedText)
            }
            if (streamUri != null) {
                put("uri", streamUri.toString())
                // Cache persistent URI permission if grantable
                try {
                    contentResolver.takePersistableUriPermission(
                        streamUri,
                        Intent.FLAG_GRANT_READ_URI_PERMISSION
                    )
                } catch (_: Throwable) {
                }
            }
        }

        persistCapture(item, listOfNotNull(streamUri))
    }

    private fun handleSendMultiple(intent: Intent, type: String) {
        val streamUris = intent.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM)
        val sharedText = intent.getStringExtra(Intent.EXTRA_TEXT)

        val captureId = UUID.randomUUID().toString()
        val now = System.currentTimeMillis() / 1000.0

        val urisArray = JSONArray()
        streamUris?.forEach { uri ->
            urisArray.put(uri.toString())
            try {
                contentResolver.takePersistableUriPermission(
                    uri,
                    Intent.FLAG_GRANT_READ_URI_PERMISSION
                )
            } catch (_: Throwable) {
            }
        }

        val item = JSONObject().apply {
            put("type", "share_multiple_capture")
            put("capture_id", captureId)
            put("created_at", now)
            put("mime_type", type)
            put("uris", urisArray)
            if (!sharedText.isNullOrBlank()) {
                put("text", sharedText)
            }
        }

        persistCapture(item, streamUris ?: emptyList())
    }

    private fun handleDeepLink(intent: Intent) {
        val data = intent.data ?: run {
            finish()
            return
        }

        val captureId = UUID.randomUUID().toString()
        val now = System.currentTimeMillis() / 1000.0

        val item = JSONObject().apply {
            put("type", "deeplink_capture")
            put("capture_id", captureId)
            put("created_at", now)
            put("url", data.toString())
        }

        persistCapture(item, emptyList())
    }
    /** Hold the activity/grant until bytes are copied, off the UI thread. */
    private fun persistCapture(item: JSONObject, uris: List<Uri>) {
        val conversation = getSharedPreferences("conversation_ui", MODE_PRIVATE)
            .getString("conversation_id", "personal") ?: "personal"
        item.put("conversation_id", conversation)
        Thread {
            val files = JSONArray()
            try {
                for ((index, uri) in uris.withIndex()) {
                    val fileId = "${item.getString("capture_id")}-${index}"
                    val descriptor = JSONObject().put("file_id", fileId).put("uri", uri.toString())
                    files.put(descriptor)
                    var name = "shared-file-$index"
                    contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use {
                        if (it.moveToFirst()) name = it.getString(0) ?: name
                    }
                    descriptor.put("name", name.take(240)).put("media_type", contentResolver.getType(uri) ?: "application/octet-stream")
                    val folder = File(filesDir, "shared-captures").apply { mkdirs() }
                    val cached = File(folder, fileId)
                    val partial = File(folder, "$fileId.partial")
                    try {
                        val source = contentResolver.openInputStream(uri) ?: error("Shared file cannot be opened")
                        source.use { input -> partial.outputStream().use { output ->
                            val buffer = ByteArray(64 * 1024); var total = 0L
                            while (true) {
                                val read = input.read(buffer); if (read < 0) break
                                total += read; check(total <= 64L * 1024 * 1024) { "Shared file exceeds 64 MB" }
                                output.write(buffer, 0, read)
                            }
                        } }
                        check(partial.renameTo(cached)) { "Cannot retain shared file bytes" }
                        descriptor.put("local_path", cached.absolutePath).put("size", cached.length())
                    } catch (failure: Exception) {
                        partial.delete()
                        descriptor.put("capture_error", failure.message ?: "File capture failed")
                    }
                }
                if (uris.isNotEmpty()) item.put("files", files)
                enqueueOutboxItem(this, item)
                SyncWorker.scheduleImmediateSync(this)
                runOnUiThread {
                    Toast.makeText(this, if ((0 until files.length()).any { files.getJSONObject(it).has("capture_error") })
                        "File capture failed; retained for review" else "Queued for Personal Assistant", Toast.LENGTH_LONG).show()
                    finish()
                }
            } catch (failure: Exception) {
                item.put("files", files).put("upload_error", failure.message ?: "File capture failed")
                try { enqueueOutboxItem(this, item) } catch (_: Exception) { /* UI reports failed persistence too. */ }
                runOnUiThread { Toast.makeText(this, "Capture failed: ${failure.message}", Toast.LENGTH_LONG).show(); finish() }
            }
        }.start()
    }

}
