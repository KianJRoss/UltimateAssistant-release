package io.github.kianjross.herald.assistant

import android.Manifest
import android.app.AlertDialog
import android.content.pm.PackageManager
import android.os.Build
import android.provider.Settings
import android.app.Activity
import android.content.Intent
import android.graphics.Typeface
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

/** The companion's sole product surface: the shared assistant conversation. */
class MainActivity : Activity() {
    private lateinit var voice: VoiceController
    private var pairingAttempted = false
    private var pairingInProgress = false
    private var resumed = false
    private var pendingAssistantVoice = false
    private var pendingAssistantSettings = false
    private var pendingAssistantAttachment = false
    private val foregroundSyncHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private val foregroundSyncTick = object : Runnable {
        override fun run() {
            if (!resumed || isFinishing || isDestroyed) return
            if (identity.pairingRevision() > 0) SyncWorker.scheduleImmediateSync(this@MainActivity)
            foregroundSyncHandler.postDelayed(this, 5_000L)
        }
    }

    private fun startForegroundSync() {
        foregroundSyncHandler.removeCallbacks(foregroundSyncTick)
        foregroundSyncHandler.post(foregroundSyncTick)
    }
    private var setupDialog: AlertDialog? = null
    private lateinit var identity: DeviceIdentity
    private lateinit var bridge: BridgeClient
    private lateinit var conversationView: AndroidConversationView
    private lateinit var permissions: PhonePermissionController
    private val requestActivity = linkedMapOf<String, JSONObject>()
    private var lastStatus = ""
    private lateinit var composer: EditText
    private val messages = mutableListOf<JSONObject>()
    private var conversationId = ""
    private var lastSequence = 0L
    private var lastEventRevision = 0L
    private var continuationId: String? = null
    private var continuationRevision: Long? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        identity = DeviceIdentity(this)
        bridge = BridgeClient(this, identity, getString(R.string.bridge_base_url))
        restoreState()
        setContentView(createConversationView())
        permissions = PhonePermissionController(this,
            onStatus = { text -> updateStatus(text) },
            onGrantsChanged = { if (identity.pairingRevision() > 0) bridge.reportGrantEvidence { } })
        permissions.observePrivileged()
        voice = VoiceController(this,
            onSpeechRecognized = { text -> runOnUiThread { conversationView.setComposer(text) } },
            onError = { error -> runOnUiThread { updateStatus(error) } },
            onStatus = { text -> runOnUiThread { updateStatus(text) } },
            onPartial = { text -> runOnUiThread { conversationView.setComposer(text) } },
            launchRecognition = { request ->
                try { startActivityForResult(request, VoiceController.VOICE_REQUEST_CODE); true }
                catch (_: android.content.ActivityNotFoundException) { false }
            })
        render()
        consumeIntent(intent)
        if (identity.pairingRevision() == 0L && setupDialog == null) connectAutomatically()
        if (identity.pairingRevision() > 0) loadHistory()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        if (consumeIntent(intent)) {
            loadHistory()
            beginStreaming()
        }
    }

    override fun onResume() {
        super.onResume()
        resumed = true
        startForegroundSync()
        permissions.onResume()
        openExplicitAssistantAction()
        if(pendingAssistantVoice) {
            pendingAssistantVoice = false
            composer.post { requestVoiceInput() }
        }
        permissions.ensureNotificationPermission()
        if (identity.pairingRevision() == 0L) { connectAutomatically(); return }
        startBackgroundConversationConnection()
        showCaptureStatus()
        bridge.replay { result -> runOnUiThread { showResult(result) } }
        bridge.reportGrantEvidence { result ->
            result.unavailable?.let { unavailable -> runOnUiThread { showUnavailable(unavailable) } }
        }
        beginStreaming()
    }

    override fun onPause() {
        super.onPause()
        resumed = false
        foregroundSyncHandler.removeCallbacks(foregroundSyncTick)
        bridge.stopStreaming()
        voice.stopSpeaking()
        persistState()
    }

    override fun onDestroy() {
        permissions.close()
        resumed = false
        foregroundSyncHandler.removeCallbacks(foregroundSyncTick)
        setupDialog?.dismiss()
        bridge.close()
        voice.destroy()
        conversationView.dispose()
        super.onDestroy()
    }

    private fun connectAutomatically(force: Boolean = false) {
        if (pairingAttempted && !force) return
        if (pairingInProgress || identity.pairingRevision() > 0) return
        if (!identity.autoPairingAllowed()) {
            updateStatus("Phone connection paused; choose Reconnect in Phone setup")
            return
        }
        if (identity.backendUrl(getString(R.string.bridge_base_url)).isBlank()) {
            // Not paired with any assistant yet: the computer shows a QR code (ask it to pair your
            // phone); the phone's camera opens it here with the address and invitation filled in.
            pairingAttempted = true
            updateStatus("Pair with your assistant: ask it on your computer to pair your phone, then scan the code")
            if (setupDialog?.isShowing != true) showPairingSetup("")  // a pairing link may have opened it already
            return
        }
        pairingAttempted = true
        pairingInProgress = true
        updateStatus("Connecting to your assistant...")
        bridge.autoPair { result -> runOnUiThread {
            pairingInProgress = false
            if (isFinishing || isDestroyed) return@runOnUiThread
            if (result.unavailable != null) { showUnavailable(result.unavailable); return@runOnUiThread }
            SyncWorker.schedulePeriodicSync(this)
            SyncWorker.scheduleImmediateSync(this)
            bridge.reportGrantEvidence { }
            loadHistory()
            AssistantConnectionService.setConnectionEnabled(this, true)
            if (resumed) { startBackgroundConversationConnection(); beginStreaming() }
            updateStatus("Connected to your assistant")
        } }
    }

    private fun showPairingSetup(endpoint: String = identity.backendUrl(getString(R.string.bridge_base_url)), challenge: String = "") {
        setupDialog?.dismiss()
        val fields = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(24, 8, 24, 8) }
        val url = EditText(this).apply { hint = "HTTPS assistant backend URL"; setText(endpoint); inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_URI }
        val invitation = EditText(this).apply { hint = "One-time pairing invitation"; setText(challenge); inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD }
        fields.addView(url); fields.addView(invitation)
        val dialog = AlertDialog.Builder(this).setTitle("Connect this phone")
            .setMessage("On your computer, ask the assistant to pair your phone. Scan the code it shows with this " +
                "phone's camera, or enter its address and invitation here. The phone reaches your computer through " +
                "Tailscale, so have the Tailscale app connected with the same account.")
            .setView(fields).setNegativeButton("Cancel", null).setPositiveButton("Pair", null).create()
        setupDialog = dialog
        dialog.setOnDismissListener { if (setupDialog === dialog) setupDialog = null }
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = false
                bridge.redeemPairing(url.text.toString(), invitation.text.toString().trim()) { result -> runOnUiThread {
                    if (isFinishing || isDestroyed) return@runOnUiThread
                    dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = true
                    if (result.unavailable != null) { updateStatus(result.unavailable.message); return@runOnUiThread }
                    invitation.text.clear()
                    dialog.dismiss()
                    SyncWorker.schedulePeriodicSync(this)
                    SyncWorker.scheduleImmediateSync(this)
                    bridge.reportGrantEvidence { }
                    loadHistory()
                    if (resumed) {
                        AssistantConnectionService.setConnectionEnabled(this, true)
                        startBackgroundConversationConnection()
                        beginStreaming()
                    }
                    updateStatus("Phone paired to your assistant")
                } }
            }
        }
        dialog.show()
    }

    private fun openFilePicker() {
        startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "*/*"
            putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION)
        }, 7120)
    }

    private fun requestVoiceInput() {
        permissions.requestVoice { voice.startListening() }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        this.permissions.onRequestPermissionsResult(requestCode, permissions, grantResults)
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == 7120 || requestCode == 7121) {
            if (resultCode != RESULT_OK || data == null) return
            if (requestCode == 7121) {
                data.data?.let { uri ->
                    try {
                        val write = data.flags and Intent.FLAG_GRANT_WRITE_URI_PERMISSION != 0
                        contentResolver.takePersistableUriPermission(uri, if (write)
                            Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                        else Intent.FLAG_GRANT_READ_URI_PERMISSION)
                        updateStatus("Folder connected; your assistant can browse it")
                    } catch (error: Exception) { updateStatus("Folder access unavailable: ${error.message}") }
                }
                return
            }
            val uris = mutableListOf<android.net.Uri>()
            data.clipData?.let { clips -> for (i in 0 until clips.itemCount) uris.add(clips.getItemAt(i).uri) }
            data.data?.let { if (it !in uris) uris.add(it) }
            val share = Intent(this, ShareReceiverActivity::class.java).apply {
                action = Intent.ACTION_SEND_MULTIPLE; type = "*/*"
                putParcelableArrayListExtra(Intent.EXTRA_STREAM, ArrayList(uris))
                putExtra(Intent.EXTRA_TEXT, composer.text.toString().trim().ifBlank { "Use these files in our conversation" })
                flags = data.flags and (Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION)
                clipData = data.clipData ?: uris.firstOrNull()?.let { android.content.ClipData.newRawUri("Attachment", it) }
            }
            startActivity(share)
            return
        }
        if(requestCode == DefaultAssistant.REQUEST_ROLE) {
            if(DefaultAssistant.status(this)["selected"] != true) DefaultAssistant.openSettings(this)
            else updateStatus("UltimateAssistant is your default digital assistant")
            return
        }
        voice.onActivityResult(requestCode, resultCode, data)
    }

    private fun updateStatus(text: String) {
        lastStatus = text
        if (::conversationView.isInitialized) conversationView.setStatus(text)
    }

    private fun handleActivity(event: JSONObject) {
        val id = event.optString("request_id")
        if (id.isBlank()) return
        val state = event.optString("state")
        if (state in setOf("completed", "failed", "cancelled")) requestActivity.remove(id)
        else requestActivity[id] = event
        val active = requestActivity.values.lastOrNull {
            it.optString("state") !in setOf("accepted", "queued")
        } ?: requestActivity.values.firstOrNull()
        conversationView.setActivity(active ?: JSONObject().put("state", "completed"))
    }

    private fun startBackgroundConversationConnection() {
        if (!resumed || identity.pairingRevision() == 0L) return
        try { AssistantConnectionService.startVisible(this) }
        catch (error: Exception) { updateStatus("Background connection unavailable: ${error.message}. Periodic sync remains scheduled.") }
    }

    private fun showCaptureStatus() {
        val captures = ShareReceiverActivity.getOutboxItems(this)
        val failures = mutableListOf<String>()
        for (capture in captures) {
            capture.optString("upload_error").takeIf { it.isNotBlank() }?.let { failures.add(it) }
            val files = capture.optJSONArray("files") ?: continue
            for (i in 0 until files.length()) files.optJSONObject(i)?.optString("capture_error")
                ?.takeIf { it.isNotBlank() }?.let { failures.add(it) }
        }
        if (failures.isNotEmpty()) {
            updateStatus("Shared content retained for retry: ${failures.first()}")
            android.widget.Toast.makeText(this, lastStatus, android.widget.Toast.LENGTH_LONG).show()
        }
    }

    private fun capabilitySetup() {
        val grants = identity.observeCapabilityGrants()
        val backgroundEnabled = AssistantConnectionService.connectionEnabled(this)
        PhoneSettingsDialog(this,
            paired = identity.pairingRevision() > 0,
            backgroundEnabled = backgroundEnabled,
            grants = grants,
            defaultAssistant = DefaultAssistant.status(this)["selected"] == true,
            onAction = { action ->
                when (action) {
                    "default_assistant" -> DefaultAssistant.choose(this)
                    "notification_access" -> startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
                    "accessibility" -> startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
                    "privileged_control" -> permissions.requestPrivileged()
                    "microphone" -> permissions.requestVoice { updateStatus("Microphone enabled") }
                    "notifications" -> permissions.ensureNotificationPermission()
                    "reconnect" -> { identity.allowAutoPairing(); connectAutomatically(force = true) }
                    "sync" -> { SyncWorker.scheduleImmediateSync(this); showCaptureStatus() }
                    "background" -> if (backgroundEnabled) {
                        AssistantConnectionService.pause(this)
                        updateStatus("Background connection paused; periodic sync remains available")
                    } else {
                        AssistantConnectionService.setConnectionEnabled(this, true)
                        startBackgroundConversationConnection()
                    }
                    "files" -> openFilePicker()
                    "folder" -> startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT_TREE).apply {
                        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION)
                    }, 7121)
                    "backend" -> showPairingSetup()
                    "unpair" -> {
                        AssistantConnectionService.pause(this)
                        bridge.stopStreaming()
                        identity.forget()
                        updateStatus("Phone credential removed; pair again to reconnect")
                    }
                }
            }).show()
    }

    private fun createConversationView(): View {
        conversationView = AndroidConversationView(this, AndroidConversationView.Callbacks(
            onSend = { text -> composer.setText(text); submit() },
            onAttach = { openFilePicker() },
            onVoice = { requestVoiceInput() },
            onSettings = { capabilitySetup() },
            onRetry = {
                conversationView.showRetry(false)
                if (identity.pairingRevision() == 0L) connectAutomatically(force = true)
                else { beginStreaming(); bridge.replay { result -> runOnUiThread { showResult(result) } } }
            },
            onSpeak = { text -> voice.speak(text) }))
        composer = conversationView.composer
        conversationView.view.fitsSystemWindows = true
        updateStatus(getString(R.string.conversation_connecting))
        return conversationView.view
    }

    private fun submit() {
        val content = composer.text.toString().trim()
        if (content.isEmpty()) return
        composer.text.clear()
        appendMessage(JSONObject().put("message_id", "local:${UUID.randomUUID()}")
            .put("role", "user").put("content", content).put("sequence", 0)
            .put("pending", true))
        updateStatus(getString(R.string.conversation_sending)); conversationView.setActivity(JSONObject().put("state", "accepted").put("label", "Sending your message..."))
        bridge.submitMessage(
            conversationId,
            content,
            continuationId,
            continuationRevision
        ) { result -> runOnUiThread { showResult(result) } }
    }

    private fun loadHistory() {
        bridge.history(conversationId, lastSequence) { result ->
            runOnUiThread {
                val history = result.body?.optJSONArray("messages")
                if (history != null) for (index in 0 until history.length()) appendMessage(history.getJSONObject(index))
                showResult(result)
                bridge.requestStates(conversationId) { activity -> runOnUiThread {
                    val requests = activity.body?.optJSONArray("requests")
                    if (requests != null) for (i in 0 until requests.length()) handleActivity(requests.getJSONObject(i))
                } }
            }
        }
    }

    private fun beginStreaming() {
        if (!resumed || identity.pairingRevision() == 0L) return
        val streamedConversationId = conversationId
        bridge.streamEvents(streamedConversationId, lastEventRevision,
            onEvent = { revision, event ->
                runOnUiThread {
                    if (streamedConversationId != conversationId) return@runOnUiThread
                    if (revision <= lastEventRevision) return@runOnUiThread
                    lastEventRevision = revision
                    if (event.optString("_event_kind") == "conversation.request" || (event.has("request_id") && !event.has("role"))) handleActivity(event)
                    else appendMessage(event)
                    updateStatus(getString(R.string.conversation_connected))
                }
            },
            onClosed = { unavailable -> runOnUiThread {
                unavailable?.let { showUnavailable(it) }
                if (resumed && identity.pairingRevision() > 0 &&
                    (unavailable == null || unavailable.retryable)) {
                    foregroundSyncHandler.postDelayed({
                        if (resumed && !isFinishing && !isDestroyed) beginStreaming()
                    }, 2_000L)
                }
            } })
    }

    private fun showResult(result: BridgeClient.Result) {
        result.unavailable?.let { showUnavailable(it); return }
        result.body?.optJSONObject("message")?.let { appendMessage(it) }
        result.body?.takeIf { it.has("state") && it.has("request_id") }?.let { handleActivity(it) }
        updateStatus(getString(R.string.conversation_connected))
        conversationView.showRetry(false)
    }

    private fun showUnavailable(unavailable: BridgeClient.Unavailable) {
        updateStatus(if (unavailable.retryable) "${unavailable.message} - delivery pending; retry when connected." else unavailable.message)
        conversationView.showRetry(unavailable.retryable)
    }

    private fun appendMessage(message: JSONObject) {
        if (message.optString("role") !in setOf("user", "assistant")) return
        val id = message.optString("message_id")
        if (id.isNotEmpty() && messages.any { it.optString("message_id") == id }) return
        var replacedPending = false
        if (message.optString("role") == "user" && !message.optBoolean("pending")) {
            val local = messages.indexOfFirst { it.optBoolean("pending") && it.optString("content") == message.optString("content") }
            if (local >= 0) { messages.removeAt(local); replacedPending = true }
        }
        val sequence = message.optLong("sequence", 0)
        if (sequence > 0) lastSequence = maxOf(lastSequence, sequence)
        if (message.has("continuation_id")) {
            continuationId = if (message.isNull("continuation_id")) null else
                message.optString("continuation_id").takeIf { it.isNotBlank() && it != "null" }
            continuationRevision = if (continuationId != null && !message.isNull("continuation_revision"))
                message.optLong("continuation_revision") else null
        }
        messages.add(message)
        if (replacedPending) conversationView.renderHistory(messages) else conversationView.appendMessage(message)
        persistState()
    }

    private fun render() {
        val retained = messages.filter { it.optString("role") in setOf("user", "assistant") }
            .sortedWith(compareBy { it.optLong("sequence", Long.MAX_VALUE) })
        messages.clear(); messages.addAll(retained)
        conversationView.renderHistory(messages)
    }

    private fun consumeIntent(incoming: Intent): Boolean {
        var conversationChanged = false
        pendingAssistantSettings = incoming.getBooleanExtra("assistant_open_settings", false) || pendingAssistantSettings
        pendingAssistantAttachment = incoming.getBooleanExtra("assistant_open_attachment", false) || pendingAssistantAttachment
        incoming.removeExtra("assistant_open_settings")
        incoming.removeExtra("assistant_open_attachment")
        if(resumed) openExplicitAssistantAction()
        if(incoming.action == Intent.ACTION_ASSIST || incoming.action == "android.intent.action.VOICE_ASSIST" ||
            incoming.getBooleanExtra("assistant_invocation",false)) {
            composer.requestFocus()
            pendingAssistantVoice = incoming.action == "android.intent.action.VOICE_ASSIST" || incoming.getBooleanExtra("assistant_start_voice",false)
            if(resumed && pendingAssistantVoice) {
                pendingAssistantVoice = false
                composer.post { requestVoiceInput() }
            }
            incoming.removeExtra("assistant_start_voice")
        }
        if (incoming.action == Intent.ACTION_SEND) {
            incoming.getStringExtra(Intent.EXTRA_TEXT)?.let { composer.setText(it) }
            if (incoming.clipData != null || incoming.getParcelableExtra<android.net.Uri>(Intent.EXTRA_STREAM) != null) {
                updateStatus(getString(R.string.attachment_requires_ingestion))
            }
        }
        incoming.data?.let { uri ->
            if (uri.scheme != "ultimateassistant" || uri.host != "conversation") return@let
            val endpoint = uri.getQueryParameter("backend_url")
            val challenge = uri.getQueryParameter("challenge")
            if (endpoint != null || challenge != null) showPairingSetup(endpoint ?: identity.backendUrl(getString(R.string.bridge_base_url)), challenge.orEmpty())
            uri.getQueryParameter("conversation_id")?.takeIf { it.isNotBlank() }?.let {
                if (it != conversationId) {
                    conversationId = it
                    messages.clear()
                    lastSequence = 0
                    lastEventRevision = 0
                    continuationId = null
                    continuationRevision = null
                    render()
                    persistState()
                    conversationChanged = true
                }
            }
            uri.getQueryParameter("text")?.let { composer.setText(it) }
        }
        return conversationChanged
    }

    private fun openExplicitAssistantAction() {
        if(pendingAssistantSettings) {
            pendingAssistantSettings = false
            composer.post { capabilitySetup() }
        }
        if(pendingAssistantAttachment) {
            pendingAssistantAttachment = false
            composer.post { openFilePicker() }
        }
    }

    private fun restoreState() {
        val prefs = getSharedPreferences("conversation_ui", MODE_PRIVATE)
        conversationId = prefs.getString("conversation_id", null) ?: "personal"
        lastSequence = prefs.getLong("last_sequence", 0)
        lastEventRevision = prefs.getLong("last_event_revision", 0)
        continuationId = prefs.getString("continuation_id", null)
            ?.takeIf { it.isNotBlank() && it != "null" }
        continuationRevision = if (continuationId != null && prefs.contains("continuation_revision")) prefs.getLong("continuation_revision", 0) else null
        try {
            val stored = JSONArray(prefs.getString("messages", "[]"))
            for (index in 0 until stored.length()) messages.add(stored.getJSONObject(index))
        } catch (_: Exception) { messages.clear() }
    }

    private fun persistState() {
        val array = JSONArray().apply { messages.takeLast(500).forEach { put(it) } }
        getSharedPreferences("conversation_ui", MODE_PRIVATE).edit()
            .putString("conversation_id", conversationId)
            .putLong("last_sequence", lastSequence)
            .putLong("last_event_revision", lastEventRevision)
            .putString("continuation_id", continuationId)
            .putString("messages", array.toString())
            .apply {
                continuationRevision?.let { putLong("continuation_revision", it) }
                    ?: remove("continuation_revision")
            }.apply()
    }
}
