package io.github.kianjross.herald.assistant

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.media.AudioManager
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.Looper
import java.util.concurrent.FutureTask
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull
import android.provider.Settings
import org.json.JSONArray
import org.json.JSONObject

/**
 * CommandDispatcher handles phone-local execution for allowlisted actions:
 * - App launches / explicit intents
 * - Web URLs / browsing
 * - File opening / viewing
 * - System share sheet triggering
 * - Clipboard operations (read / write)
 * - Android settings navigation (general, accessibility, notification listener, wifi, bluetooth)
 *
 * All operations enforce strict allowlisting and return structured results or errors
 * compatible with phone.v1 command acknowledgement.
 */
class CommandDispatcher(private val context: Context) {

    data class ExecutionResult(
        val success: Boolean,
        val result: Map<String, Any?>? = null,
        val error: String? = null,
        val evidence: Map<String, Any?> = emptyMap()
    ) {
        fun toMap(): Map<String, Any?> {
            return if (success) {
                mapOf("status" to "success", "result" to (result ?: emptyMap<String, Any?>()), "phone_evidence" to evidence)
            } else {
                mapOf("status" to "failed", "error" to (error ?: "Unknown error"), "result" to result, "phone_evidence" to evidence)
            }
        }
    }

    companion object {
        // Allowlisted settings action identifiers
        val ALLOWED_SETTINGS_ACTIONS = setOf(
            Settings.ACTION_SETTINGS,
            Settings.ACTION_ACCESSIBILITY_SETTINGS,
            Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS,
            Settings.ACTION_WIFI_SETTINGS,
            Settings.ACTION_BLUETOOTH_SETTINGS,
            Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
            Settings.ACTION_DATE_SETTINGS,
            Settings.ACTION_SOUND_SETTINGS
        )

        // Allowlisted URL schemes
        val ALLOWED_URL_SCHEMES = setOf("http", "https", "mailto", "tel")
        private const val ANNOUNCEMENT_COMMAND = "phone.announcement"
        private const val MAX_ANNOUNCEMENT_CHARS = 2_000
        private const val ANNOUNCEMENT_CHANNEL = "ua_assistant_announcements"
        private val announcementSpeech = Mutex()
    }

    /**
     * Dispatch an incoming command by name and arguments.
     */
    fun dispatch(command: String, arguments: Map<String, Any?>): ExecutionResult =
        dispatchInternal(command, arguments).copy(evidence = DeviceIdentity(context).executionEvidence())

    private fun dispatchInternal(command: String, arguments: Map<String, Any?>): ExecutionResult {
        if (Looper.myLooper() == Looper.getMainLooper()) return dispatchOnMain(command, arguments)
        val task = FutureTask { dispatchOnMain(command, arguments) }
        if (!Handler(Looper.getMainLooper()).post(task))
            return ExecutionResult(false, error = "Android main thread is unavailable")
        return try { task.get() } catch (interrupted: InterruptedException) {
            task.cancel(false)
            Thread.currentThread().interrupt()
            ExecutionResult(false, error = "Platform dispatch interrupted; outcome may be unknown")
        }
    }

    suspend fun dispatchAwait(command: String, arguments: Map<String, Any?>,
                              privilegedDeadlineMillis: Long? = null,
                              privilegedAuthority: (() -> Unit)? = null): ExecutionResult =
        dispatchAwaitInternal(command, arguments, privilegedDeadlineMillis, privilegedAuthority)
            .copy(evidence = DeviceIdentity(context).executionEvidence())

    private suspend fun dispatchAwaitInternal(command: String, arguments: Map<String, Any?>,
                                             privilegedDeadlineMillis: Long? = null,
                                             privilegedAuthority: (() -> Unit)? = null): ExecutionResult {
        if (command == ANNOUNCEMENT_COMMAND) return handleAnnouncement(arguments)
        if (command in setOf("privileged_status", "privileged_shell", "privileged_files")) {
            return try {
                if(command == "privileged_status") ExecutionResult(true, PrivilegedControl.status())
                else {
                    val result = PrivilegedControl.execute(context, command, arguments,
                        privilegedDeadlineMillis, privilegedAuthority)
                    val success = result["success"] == true
                    ExecutionResult(success, result, if(success) null else
                        ((result["error"] as? String)?.takeIf { it.isNotBlank() }
                            ?: (result["stderr"] as? String)?.takeIf { it.isNotBlank() }
                            ?: if(result["timed_out"] == true) "Privileged command timed out; execution outcome is unknown"
                            else "Privileged command failed (exit code ${result["exit_code"] ?: "unknown"})"))
                }
            } catch(cancelled: kotlinx.coroutines.CancellationException) {
                // WorkManager cancellation is not evidence an OS effect failed.
                throw cancelled
            } catch(error: Exception) { ExecutionResult(false, error = error.message ?: "Privileged execution failed") }
        }
        if (command == "accessibility_action" && arguments["action_type"] in setOf("capture_screen", "take_screenshot")) {
            val service = PhoneAccessibilityService.instance
                ?: return ExecutionResult(false, error = "Android accessibility service is disconnected; reported grant and service connection are separate observations")
            val result = service.captureScreen(arguments)
            return ExecutionResult(result["status"] == "success", result, result["error"] as? String)
        }
        if (command in setOf("list_file_roots", "list_files", "read_file", "upload_file")) {
            return kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.IO) {
                try {
                    val files = PhoneFileAccess(context)
                    val uri = arguments["uri"] as? String ?: ""
                    val result = when(command) {
                        "list_file_roots" -> files.roots()
                        "list_files" -> files.list(uri, (arguments["limit"] as? Number)?.toInt() ?: 100)
                        "read_file" -> files.read(uri, (arguments["max_bytes"] as? Number)?.toInt() ?: 65536)
                        else -> mapOf("capture_file" to files.capture(uri))
                    }
                    ExecutionResult(true, result)
                } catch(error: Exception) { ExecutionResult(false, error = error.message ?: "File access failed") }
            }
        }
        return kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.Main) { dispatchInternal(command, arguments) }
    }

    private fun dispatchOnMain(command: String, arguments: Map<String, Any?>): ExecutionResult {
        return try {
            when (command) {
                "device_status", "get_device_status" -> ExecutionResult(true, DeviceIdentity(context).executionEvidence())
                "launch_app", "open_app" -> handleLaunchApp(arguments)
                "open_url" -> handleOpenUrl(arguments)
                "open_file" -> handleOpenFile(arguments)
                "share_text", "share_content" -> handleShare(arguments)
                "set_clipboard" -> handleSetClipboard(arguments)
                "get_clipboard" -> handleGetClipboard()
                "open_settings" -> handleOpenSettings(arguments)
                "accessibility_action" -> handleAccessibilityAction(arguments)
                "notification_action" -> handleNotificationAction(arguments)
                // Speech is suspendable and deliberately handled only by dispatchAwaitInternal.
                ANNOUNCEMENT_COMMAND -> ExecutionResult(false, error = "Announcement requires leased asynchronous dispatch")
                else -> ExecutionResult(success = false, error = "Unsupported command: '$command'")
            }
        } catch (t: Throwable) {
            ExecutionResult(success = false, error = "Execution failed with exception: ${t.message ?: t.javaClass.simpleName}")
        }
    }

    /**
     * Play one authenticated proactive record without creating a conversation or opening a mic.
     * SyncWorker supplies the immutable leased-record fields prefixed with `_command_`.
     */
    private suspend fun handleAnnouncement(arguments: Map<String, Any?>): ExecutionResult =
        announcementSpeech.withLock {
            fun bounded(name: String, limit: Int = 256): String? =
                (arguments[name] as? String)?.trim()?.takeIf { it.isNotEmpty() && it.length <= limit }
            val recordId = bounded("record_id")
                ?: return@withLock ExecutionResult(false, error = "Invalid announcement record identity")
            val recordRevision = (arguments["record_revision"] as? Number)?.toLong()
            val pairingRevision = (arguments["pairing_revision"] as? Number)?.toLong()
            val correlation = bounded("correlation_id")
            val text = bounded("text", MAX_ANNOUNCEMENT_CHARS)
            val expiry = (arguments["_command_expires_at"] as? Number)?.toDouble()
            val envelopeRevision = (arguments["_command_revision"] as? Number)?.toLong()
            val envelopeCorrelation = bounded("_command_correlation_id")
            val commandId = bounded("_command_id")
            val owner = bounded("_command_owner")
            val leaseToken = bounded("_command_lease_token")
            val target = bounded("_command_target_device")
            if (arguments["protocol"] != "phone.v1" ||
                target != DeviceIdentity.current(context) || commandId == null || owner == null || leaseToken == null ||
                recordRevision == null || recordRevision <= 0 || envelopeRevision == null || envelopeRevision <= 0 ||
                pairingRevision == null || pairingRevision <= 0 || pairingRevision != DeviceIdentity(context).pairingRevision() ||
                correlation == null || correlation != envelopeCorrelation || text == null ||
                expiry == null || !expiry.isFinite() || System.currentTimeMillis() / 1000.0 >= expiry
            ) return@withLock ExecutionResult(false, error = "Announcement authority or envelope is invalid")

            val audio = context.getSystemService(AudioManager::class.java)
            if (audio?.activeRecordingConfigurations?.isNotEmpty() == true) {
                return@withLock ExecutionResult(false, error = "Announcement unavailable while microphone audio is owned")
            }
            val finished = CompletableDeferred<String>()
            val controller = kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.Main) {
                VoiceController(context.applicationContext,
                    onSpeechRecognized = {},
                    onError = { finished.complete("tts_unavailable") },
                    onStatus = {},
                    onSpeechFinished = { finished.complete("spoken") })
            }
            val accepted = controller.speak(text)
            if (!accepted) {
                controller.destroy()
                return@withLock ExecutionResult(false, error = "Announcement speech output is unavailable")
            }
            val remaining = ((expiry * 1000.0).toLong() - System.currentTimeMillis()).coerceAtMost(120_000L)
            val state = if (remaining > 0) withTimeoutOrNull(remaining) { finished.await() } else null
            controller.destroy()
            if (state != "spoken") return@withLock ExecutionResult(false,
                result = mapOf("record_id" to recordId, "record_revision" to recordRevision,
                    "correlation_id" to correlation, "delivery_state" to (state ?: "outcome_unknown"),
                    "outcome_unknown" to (state == null)),
                error = if (state == null) "Announcement playback completion is unknown" else "Announcement speech output is unavailable")
            val actionAvailable = publishAssistantAction(recordId, recordRevision)
            ExecutionResult(true, mapOf(
                "record_id" to recordId, "record_revision" to recordRevision,
                "correlation_id" to correlation, "delivery_state" to "spoken",
                "assistant_action" to "android.intent.action.VOICE_ASSIST",
                "assistant_available" to DefaultAssistant.status(context)["active"],
                "assistant_action_exposed" to actionAvailable
            ))
        }

    /** A user gesture may enter the native assistant after playback; this never opens it itself. */
    private fun publishAssistantAction(recordId: String, revision: Long): Boolean {
        if (!androidx.core.app.NotificationManagerCompat.from(context).areNotificationsEnabled()) return false
        val manager = context.getSystemService(NotificationManager::class.java) ?: return false
        manager.createNotificationChannel(NotificationChannel(ANNOUNCEMENT_CHANNEL,
            "Assistant announcements", NotificationManager.IMPORTANCE_DEFAULT).apply {
            description = "Permitted proactive assistant announcements and a reply action"
        })
        val invoke = Intent(context, AssistantVoiceInteractionService::class.java).apply {
            action = "io.github.kianjross.herald.assistant.RESUME_CONVERSATION"
        }
        val requestCode = 31 * recordId.hashCode() + revision.hashCode()
        val pending = PendingIntent.getService(context, requestCode, invoke,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val notification = Notification.Builder(context, ANNOUNCEMENT_CHANNEL)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("Personal Assistant")
            .setContentText("Tap to respond")
            .setContentIntent(pending).setAutoCancel(true)
            .setCategory(Notification.CATEGORY_REMINDER)
            .addAction(Notification.Action.Builder(null, "Respond", pending).build())
            .build()
        manager.notify(requestCode, notification)
        return true
    }

    private fun handleLaunchApp(arguments: Map<String, Any?>): ExecutionResult {
        val packageName = arguments["package_name"] as? String
            ?: return ExecutionResult(success = false, error = "Missing 'package_name' argument")

        val packageManager = context.packageManager
        val launchIntent = packageManager.getLaunchIntentForPackage(packageName)
            ?: return ExecutionResult(
                success = false,
                error = "Application '$packageName' is not installed or has no launcher intent"
            )

        launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(launchIntent)
        return ExecutionResult(
            success = true,
            result = mapOf("package_name" to packageName, "dispatched" to "launch_activity")
        )
    }

    private fun handleOpenUrl(arguments: Map<String, Any?>): ExecutionResult {
        val urlString = arguments["url"] as? String
            ?: return ExecutionResult(success = false, error = "Missing 'url' argument")

        val uri = Uri.parse(urlString)
        val scheme = uri.scheme?.lowercase()
        if (scheme == null || scheme !in ALLOWED_URL_SCHEMES) {
            return ExecutionResult(
                success = false,
                error = "URL scheme '${scheme ?: ""}' is not in the allowlist $ALLOWED_URL_SCHEMES"
            )
        }

        val intent = Intent(Intent.ACTION_VIEW, uri).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        if (intent.resolveActivity(context.packageManager) == null) {
            return ExecutionResult(success = false, error = "No activity available to handle URL: $urlString")
        }

        context.startActivity(intent)
        return ExecutionResult(
            success = true,
            result = mapOf("url" to urlString, "scheme" to scheme, "dispatched" to "action_view")
        )
    }

    private fun handleOpenFile(arguments: Map<String, Any?>): ExecutionResult {
        val uriString = arguments["uri"] as? String
            ?: return ExecutionResult(success = false, error = "Missing 'uri' argument")
        val mimeType = arguments["mime_type"] as? String ?: "*/*"

        val uri = Uri.parse(uriString)
        if (uri.scheme != "content") return ExecutionResult(false, error = "File opening requires a granted content URI")
        val intent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, mimeType)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }

        if (intent.resolveActivity(context.packageManager) == null) {
            return ExecutionResult(success = false, error = "No viewer available for file URI: $uriString ($mimeType)")
        }

        context.startActivity(intent)
        return ExecutionResult(
            success = true,
            result = mapOf("uri" to uriString, "mime_type" to mimeType, "dispatched" to "open_file")
        )
    }

    private fun handleShare(arguments: Map<String, Any?>): ExecutionResult {
        val text = arguments["text"] as? String
        val title = arguments["title"] as? String ?: "Share via Personal Assistant"
        val uriString = arguments["uri"] as? String

        if (text.isNullOrBlank() && uriString.isNullOrBlank()) {
            return ExecutionResult(success = false, error = "At least one of 'text' or 'uri' must be provided for sharing")
        }

        val shareIntent = Intent(Intent.ACTION_SEND).apply {
            if (!text.isNullOrBlank()) {
                putExtra(Intent.EXTRA_TEXT, text)
                type = "text/plain"
            }
            if (!uriString.isNullOrBlank()) {
                val uri = Uri.parse(uriString)
                if (uri.scheme != "content") return ExecutionResult(false, error = "File sharing requires a granted content URI")
                putExtra(Intent.EXTRA_STREAM, uri)
                val mime = arguments["mime_type"] as? String ?: "*/*"
                type = mime
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
        }

        val chooserIntent = Intent.createChooser(shareIntent, title).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(chooserIntent)
        return ExecutionResult(
            success = true,
            result = mapOf("shared" to true, "has_text" to !text.isNullOrBlank(), "has_uri" to !uriString.isNullOrBlank())
        )
    }

    private fun handleSetClipboard(arguments: Map<String, Any?>): ExecutionResult {
        val text = arguments["text"] as? String
            ?: return ExecutionResult(success = false, error = "Missing 'text' argument")
        val label = arguments["label"] as? String ?: "PersonalAssistant"

        val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
            ?: return ExecutionResult(success = false, error = "Clipboard service is unavailable")

        val clip = ClipData.newPlainText(label, text)
        clipboard.setPrimaryClip(clip)

        return ExecutionResult(
            success = true,
            result = mapOf("clipboard_updated" to true, "length" to text.length)
        )
    }

    private fun handleGetClipboard(): ExecutionResult {
        val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
            ?: return ExecutionResult(success = false, error = "Clipboard service is unavailable")

        val clip = clipboard.primaryClip
        if (clip == null || clip.itemCount == 0) {
            return ExecutionResult(
                success = true,
                result = mapOf("has_content" to false, "text" to "")
            )
        }

        val text = clip.getItemAt(0).coerceToText(context).toString()
        return ExecutionResult(
            success = true,
            result = mapOf("has_content" to true, "text" to text, "length" to text.length)
        )
    }

    private fun handleOpenSettings(arguments: Map<String, Any?>): ExecutionResult {
        val settingType = arguments["setting"] as? String ?: "main"
        val action = when (settingType.lowercase()) {
            "main", "settings" -> Settings.ACTION_SETTINGS
            "accessibility" -> Settings.ACTION_ACCESSIBILITY_SETTINGS
            "notification_listener", "notifications" -> Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS
            "wifi" -> Settings.ACTION_WIFI_SETTINGS
            "bluetooth" -> Settings.ACTION_BLUETOOTH_SETTINGS
            "app_details" -> Settings.ACTION_APPLICATION_DETAILS_SETTINGS
            "date" -> Settings.ACTION_DATE_SETTINGS
            "sound" -> Settings.ACTION_SOUND_SETTINGS
            else -> return ExecutionResult(
                success = false,
                error = "Unknown setting type: '$settingType'. Allowed: main, accessibility, notifications, wifi, bluetooth, app_details, date, sound"
            )
        }

        if (action !in ALLOWED_SETTINGS_ACTIONS) {
            return ExecutionResult(success = false, error = "Setting action is not in the allowlist: $action")
        }

        val intent = Intent(action).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            if (action == Settings.ACTION_APPLICATION_DETAILS_SETTINGS) {
                data = Uri.fromParts("package", context.packageName, null)
            }
        }

        context.startActivity(intent)
        return ExecutionResult(
            success = true,
            result = mapOf("setting" to settingType, "action" to action, "opened" to true)
        )
    }

    private fun handleAccessibilityAction(arguments: Map<String, Any?>): ExecutionResult {
        val service = PhoneAccessibilityService.instance
            ?: return ExecutionResult(
                success = false,
                error = "PhoneAccessibilityService is not enabled or active. Grant accessibility permission in Android settings."
            )
        val actionType = arguments["action_type"] as? String
            ?: return ExecutionResult(success = false, error = "Missing 'action_type' for accessibility action")

        val res = service.performScopedAction(actionType, arguments)
        return ExecutionResult(
            success = res["status"] == "success",
            result = res,
            error = res["error"] as? String
        )
    }

    private fun handleNotificationAction(arguments: Map<String, Any?>): ExecutionResult {
        val service = PhoneNotificationService.instance
            ?: return ExecutionResult(
                success = false,
                error = "PhoneNotificationService is not connected. Grant notification listener permission in Android settings."
            )

        val key = arguments["key"] as? String
            ?: return ExecutionResult(success = false, error = "Missing 'key' for notification action")
        val actionTitle = arguments["action_title"] as? String
            ?: return ExecutionResult(success = false, error = "Missing 'action_title' for notification action")

        val success = service.executeNotificationAction(key, actionTitle)
        return if (success) {
            ExecutionResult(
                success = true,
                result = mapOf("action_executed" to true, "key" to key, "action_title" to actionTitle)
            )
        } else {
            ExecutionResult(
                success = false,
                error = "Notification action '$actionTitle' could not be found or executed for notification '$key'"
            )
        }
    }
}
