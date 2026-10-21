package io.github.kianjross.herald.assistant

import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.Region
import android.graphics.drawable.ColorDrawable
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.service.voice.VoiceInteractionSession
import android.service.voice.VoiceInteractionSessionService
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.FrameLayout
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

class AssistantVoiceSessionService : VoiceInteractionSessionService() {
    override fun onCreate() {
        super.onCreate()
        AssistantOverlayController.install()
    }

    override fun onNewSession(args: Bundle?): VoiceInteractionSession = AssistantVoiceSession(this)
}

/**
 * Protocol for supplying touchable regions and receiving lifecycle events
 * from an adaptive visual canvas over the transparent assistant window.
 */
interface CanvasTouchDelegate {
    /** Supply interactive bounds in screen/window coordinates. */
    fun computeTouchableRegion(outRegion: Region)
    fun onSessionShow() {}
    fun onSessionHide() {}
    fun onSessionDestroy() {}
    fun onBackPressed(): Boolean = false
}

/**
 * Speech-first native assistant voice session.
 * Replaces the fixed chat overlay panel with a transparent adaptive canvas window.
 * Spoken conversation is primary and operates naturally with or without visible canvas elements.
 */
class AssistantVoiceSession(private val hostService: AssistantVoiceSessionService) : VoiceInteractionSession(hostService) {

    companion object {
        private const val TAG = "AssistantVoiceSession"
        private const val MAX_RECOGNIZER_REARMS = 2
        private val TERMINAL_REQUEST_STATES = setOf("completed", "failed", "cancelled", "expired")
        /**
         * Optional hook for an adaptive canvas controller to attach to newly created voice sessions.
         * The canvas controller can add visual elements (annotations, reference images, reminders)
         * to the root container and supply interactive touch regions.
         */
        @Volatile
        var canvasDelegateFactory: ((AssistantVoiceSession, FrameLayout) -> CanvasTouchDelegate)? = null
    }

    private val main = Handler(Looper.getMainLooper())
    private val identity = DeviceIdentity(hostService)
    private val bridge = BridgeClient(hostService, identity, hostService.getString(R.string.bridge_base_url))
    private val preferences = hostService.getSharedPreferences("conversation_ui", Context.MODE_PRIVATE)

    private val conversationId = preferences.getString("conversation_id", null) ?: "personal"
    private var continuationId: String? = preferences.getString("continuation_id", null)
        ?.takeIf { it.isNotBlank() && it != "null" }
    private var continuationRevision: Long? = if (continuationId != null && preferences.contains("continuation_revision")) preferences.getLong("continuation_revision", 0) else null
    private var lastSequence = preferences.getLong("last_sequence", 0)
    private var eventRevision = preferences.getLong("last_event_revision", 0)

    private var active = false
    private var disposed = false
    private var voiceEnabled = true
    private var busy = false
    private var speaking = false
    private var cueSpeaking = false
    private var speechGeneration = 0L
    private var cueRequestId: String? = null
    private val progressCues = VoiceProgressCues()
    private var loading = false
    private var engaged = false
    private var meaningfulTurn = false
    private var sessionId = ""
    private var turnCorrelationId: String? = null
    private var speakingRequestId: String? = null
    private var speakingCorrelationId: String? = null
    private var awaitingResponseRequestId: String? = null

    private val messages = mutableListOf<JSONObject>()
    private val spokenMessageIds = mutableSetOf<String>()
    private val requestActivity = linkedMapOf<String, JSONObject>()
    /** Requests submitted by this live voice session are the only requests allowed to gate listening. */
    private val ownedActiveRequests = mutableSetOf<String>()
    private val sessionRequestIds = mutableSetOf<String>()
    private val requestCorrelations = mutableMapOf<String, String>()
    private var recognizerRearms = 0

    private var rootContainer: FrameLayout? = null
    private var canvasTouchDelegate: CanvasTouchDelegate? = null
    private var customTouchableRegion: Region? = null

    private lateinit var voice: VoiceController

    private val rearmRunnable = Runnable {
        val eligible = active && engaged && voiceEnabled && !busy && !speaking && !disposed
        Log.i(TAG, "rearm fired eligible=$eligible busy=$busy speaking=$speaking")
        if (eligible) {
            VoiceSessionForeground.updateStatus(hostService, VoiceSessionForeground.State.STARTING)
            voice.startListening()
        }
    }

    init {
        // Load messages for backend transcript continuity
        runCatching {
            val stored = JSONArray(preferences.getString("messages", "[]"))
            for (i in 0 until stored.length()) {
                val msg = stored.getJSONObject(i)
                messages.add(msg)
                if (msg.optString("role") == "assistant") {
                    msg.optString("message_id").takeIf { it.isNotBlank() }?.let { spokenMessageIds.add(it) }
                }
            }
        }
        initVoiceController()
    }

    private fun initVoiceController() {
        voice = VoiceController(
            context = hostService,
            onSpeechRecognized = { text ->
                main.post {
                    if (active && engaged && voiceEnabled && !busy && !speaking && !disposed) {
                        recognizerRearms = 0
                        handleSpokenInput(text)
                    }
                }
            },
            onError = { error ->
                val failedCue = cueSpeaking
                val generation = speechGeneration
                main.post {
                    if (disposed || generation != speechGeneration) return@post
                    if (failedCue) {
                        if (generation == speechGeneration) stopCue()
                        return@post
                    }
                    // VoiceController owns provider fallback. This layer only decides whether a
                    // completed controller attempt should be rearmed.
                    val unavailable = error.contains("permission", ignoreCase = true) ||
                        error.contains("denied", ignoreCase = true) ||
                        error.contains("no Android speech recognition service", ignoreCase = true) ||
                        error.contains("install language data", ignoreCase = true) ||
                        error.contains("unable to start Android speech input", ignoreCase = true)
                    val retryable = !unavailable && (error.contains("busy", ignoreCase = true) ||
                        error.contains("network", ignoreCase = true) ||
                        error.contains("timed out", ignoreCase = true) ||
                        error.contains("no speech", ignoreCase = true) ||
                        error.contains("interrupted", ignoreCase = true) ||
                        error.contains("recording failed", ignoreCase = true) ||
                        error.contains("speech service failed", ignoreCase = true))
                    val numericCode = Regex("(?:error|code)[^0-9]*(\\d+)", RegexOption.IGNORE_CASE)
                        .find(error)?.groupValues?.getOrNull(1)
                    Log.w(TAG, "recognizer error retryable=$retryable unavailable=$unavailable code=${numericCode ?: "none"} rearm=$recognizerRearms")
                    if (speaking) return@post // TTS completion owns cancellation and lifecycle acknowledgement.
                    if (!active || !engaged || !voiceEnabled) return@post
                    // Silence ends microphone engagement, not assistant availability or context.
                    // Android NO_MATCH/SPEECH_TIMEOUT are normal conversation boundaries.
                    if (error.contains("no speech", ignoreCase = true)) {
                        settleIdle("silence")
                        return@post
                    }
                    if (active && voiceEnabled && !speaking && !busy && retryable && recognizerRearms < MAX_RECOGNIZER_REARMS) {
                        recognizerRearms++
                        VoiceSessionForeground.updateStatus(hostService, VoiceSessionForeground.State.STARTING)
                        rearmListening(2000L)
                    } else {
                        VoiceSessionForeground.updateStatus(
                            hostService,
                            if (unavailable) VoiceSessionForeground.State.UNAVAILABLE else VoiceSessionForeground.State.ERROR
                        )
                        settleIdle(if (unavailable) "recognizer_unavailable" else "recognizer_exhausted")
                    }
                }
            },
            onStatus = { status ->
                val generation = speechGeneration
                main.post {
                    if (generation != speechGeneration) return@post
                    // cancelListening() emits a status synchronously, but this callback is
                    // delivered later on the main queue. Do not let that stale status recreate
                    // the ongoing notification after mute/pause stopped foreground operation.
                    if (!disposed && active && engaged && voiceEnabled) {
                        Log.i(TAG, "recognizer status=$status")
                        if (status == "Voice input cancelled" && speaking) {
                            VoiceSessionForeground.updateStatus(hostService, VoiceSessionForeground.State.SPEAKING)
                        } else {
                            VoiceSessionForeground.updateStatus(hostService, status)
                            if (status == "Voice input cancelled" && !busy && !speaking && engaged) {
                                settleIdle("recognition_cancelled")
                            }
                        }
                    }
                }
            },
            onPartial = { /* Partial speech does not populate a hidden composer */ },
            onSpeechFinished = { cancelled ->
                val generation = speechGeneration
                val completedCue = cueSpeaking
                main.post {
                    if (disposed || generation != speechGeneration) return@post
                    if (completedCue) {
                        cueSpeaking = false
                        speaking = false
                        if (active && engaged && voiceEnabled) {
                            VoiceSessionForeground.updateStatus(hostService, VoiceSessionForeground.State.PROCESSING)
                        }
                        return@post
                    }
                    val completedRequest = speakingRequestId
                    val completedCorrelation = speakingCorrelationId
                    speaking = false
                    speakingRequestId = null
                    speakingCorrelationId = null
                    reportLifecycle("speech_finished", completedRequest, completedCorrelation, cancelled = cancelled)
                    // Follow-up is available only after a completed meaningful turn while the
                    // invocation remains engaged. It is never an idle microphone loop.
                    if (!cancelled && active && engaged && meaningfulTurn && voiceEnabled && !busy && !disposed) {
                        rearmListening(300L)
                    } else if (!busy && !speaking) {
                        settleIdle(if (cancelled) "speech_cancelled" else "turn_complete")
                    }
                }
            }
        )
    }

    override fun onCreateContentView(): View {
        val container = FrameLayout(hostService).apply {
            setBackgroundColor(Color.TRANSPARENT)
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
            layoutParams = ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        }
        rootContainer = container
        canvasTouchDelegate = canvasDelegateFactory?.invoke(this, container)
        return container
    }

    private fun setupWindow() {
        val win = window.window ?: return
        win.setGravity(Gravity.FILL)
        win.setLayout(WindowManager.LayoutParams.MATCH_PARENT, WindowManager.LayoutParams.MATCH_PARENT)
        win.setBackgroundDrawable(ColorDrawable(Color.TRANSPARENT))
        win.clearFlags(WindowManager.LayoutParams.FLAG_DIM_BEHIND)
        win.addFlags(WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL)
        win.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_STATE_HIDDEN or WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE)
    }

    override fun onComputeInsets(outInsets: Insets) {
        outInsets.touchableInsets = Insets.TOUCHABLE_INSETS_REGION
        outInsets.contentInsets.set(0, 0, 0, 0)
        outInsets.touchableRegion.setEmpty()
        // If a canvas controller supplies interactive touch bounds, apply them;
        // otherwise touchableRegion remains empty and all touches pass through to underlying app.
        canvasTouchDelegate?.computeTouchableRegion(outInsets.touchableRegion)
        customTouchableRegion?.let {
            if (outInsets.touchableRegion.isEmpty) {
                outInsets.touchableRegion.set(it)
            } else {
                outInsets.touchableRegion.op(it, Region.Op.UNION)
            }
        }
    }

    fun setCanvasTouchDelegate(delegate: CanvasTouchDelegate?) {
        this.canvasTouchDelegate = delegate
        requestTouchableRegionUpdate()
    }

    fun updateTouchableRegion(region: Region?) {
        this.customTouchableRegion = region
        requestTouchableRegionUpdate()
    }

    fun requestTouchableRegionUpdate() {
        main.post {
            window.window?.decorView?.let { decor ->
                decor.requestLayout()
                decor.invalidate()
            }
        }
    }

    fun getCanvasContainer(): FrameLayout? = rootContainer

    override fun onShow(args: Bundle?, showFlags: Int) {
        super.onShow(args, showFlags)
        setupWindow()
        if (speaking && !cueSpeaking) {
            reportLifecycle("speech_finished", speakingRequestId, speakingCorrelationId, cancelled = true)
        }
        voice.stopSpeaking()
        speaking = false
        stopCue()
        speechGeneration++
        progressCues.reset()
        cueRequestId = null
        active = true
        engaged = true
        meaningfulTurn = false
        sessionId = "android-voice:${UUID.randomUUID()}"
        turnCorrelationId = null
        speakingRequestId = null
        speakingCorrelationId = null
        busy = false
        awaitingResponseRequestId = null
        ownedActiveRequests.clear()
        sessionRequestIds.clear()
        requestCorrelations.clear()
        requestActivity.clear()
        Log.i(TAG, "session show flags=$showFlags")
        enableVoice()
        connect()
        reportLifecycle("start")
        canvasTouchDelegate?.onSessionShow()
    }

    override fun onHide() {
        super.onHide()
        // Idle availability is provided by Android's selected assistant service, not by a
        // hidden session retaining the microphone.
        active = false
        bridge.stopStreaming()
        endEngagement()
        canvasTouchDelegate?.onSessionHide()
    }

    fun enableVoice() {
        if (disposed) return
        voiceEnabled = true
        engaged = true
        recognizerRearms = 0
        Log.i(TAG, "voice enable active=$active busy=$busy speaking=$speaking")
        val foregroundStarted = runCatching {
            VoiceSessionForeground.start(hostService)
        }.onFailure {
            Log.e(TAG, "foreground start failed type=${it.javaClass.simpleName}")
        }.isSuccess
        if (!foregroundStarted) {
            // Do not open the microphone if Android cannot establish the visible,
            // user-activated foreground voice session.
            voiceEnabled = false
            voice.cancelListening()
            return
        }
        rearmListening(200L)
    }

    fun pauseVoice() {
        Log.i(TAG, "voice pause")
        voiceEnabled = false
        engaged = false
        main.removeCallbacks(rearmRunnable)
        if (speaking && !cueSpeaking) reportLifecycle("speech_finished", speakingRequestId, speakingCorrelationId, cancelled = true)
        stopCue()
        speechGeneration++
        speaking = false
        speakingRequestId = null
        speakingCorrelationId = null
        reportLifecycle("mute", value = true)
        voice.cancelListening()
        voice.stopSpeaking()
        VoiceSessionForeground.stop(hostService)
    }

    fun mute() {
        pauseVoice()
    }

    fun unmute() {
        reportLifecycle("mute", value = false)
        enableVoice()
    }

    fun isMuted(): Boolean = !voiceEnabled

    fun interrupt() {
        if (disposed) return
        main.removeCallbacks(rearmRunnable)
        reportLifecycle("interrupt", speakingRequestId, speakingCorrelationId)
        speechGeneration++
        cueSpeaking = false
        voice.stopSpeaking()
        speaking = false
        speakingRequestId = null
        speakingCorrelationId = null
        settleIdle("interrupted")
    }

    fun endSession() {
        endEngagement()
        finish()
    }

    private fun rearmListening(delayMs: Long) {
        main.removeCallbacks(rearmRunnable)
        val eligible = active && engaged && voiceEnabled && !busy && !speaking && !disposed
        Log.i(TAG, "rearm scheduled eligible=$eligible delayMs=$delayMs")
        if (eligible) {
            main.postDelayed(rearmRunnable, delayMs)
        }
    }

    private fun stopCue() {
        if (!cueSpeaking) return
        speechGeneration++
        cueSpeaking = false
        speaking = false
        voice.stopSpeaking()
    }

    private fun sayCue(text: String, requestId: String) {
        if (disposed || !active || !engaged || !voiceEnabled || requestId != cueRequestId ||
            requestId !in ownedActiveRequests || (speaking && !cueSpeaking)) return
        stopCue()
        speechGeneration++
        cueSpeaking = true
        speaking = true
        // A cue acknowledges work, not a response; request ownership stays intact.
        val accepted = voice.speak(text)
        Log.i(TAG, "progress cue accepted=$accepted")
        if (!accepted) {
            cueSpeaking = false
            speaking = false
        }
        VoiceSessionForeground.updateStatus(hostService, VoiceSessionForeground.State.PROCESSING)
    }

    private fun say(text: String, requestId: String, correlationId: String?) {
        if (disposed || !engaged || !voiceEnabled || text.isBlank()) return
        awaitingResponseRequestId = null
        stopCue()
        speechGeneration++
        cueSpeaking = false
        // Recognition is cancelled while TTS speaks
        voice.cancelListening()
        speaking = true
        speakingRequestId = requestId
        speakingCorrelationId = correlationId
        reportLifecycle("response_ready", requestId, correlationId)
        reportLifecycle("speak", requestId, correlationId) { result ->
            if (result.unavailable == null) reportLifecycle("speaking_acknowledged", requestId, correlationId)
        }
        val accepted = voice.speak(text)
        if (!accepted) {
            speaking = false
            speakingRequestId = null
            speakingCorrelationId = null
            reportLifecycle("speech_finished", requestId, correlationId, cancelled = true)
            settleIdle("speech_unavailable")
        } else {
            VoiceSessionForeground.updateStatus(hostService, VoiceSessionForeground.State.SPEAKING)
        }
    }

    private fun handleSpokenInput(text: String) {
        val content = text.trim()
        if (disposed || content.isEmpty()) return
        voice.cancelListening()
        speechGeneration++
        cueSpeaking = false
        voice.stopSpeaking()
        speaking = false
        busy = true
        meaningfulTurn = true
        recognizerRearms = 0
        val correlationId = UUID.randomUUID().toString()
        turnCorrelationId = correlationId
        VoiceSessionForeground.updateStatus(hostService, VoiceSessionForeground.State.PROCESSING)

        // Preserve backend transcript continuity without displaying a chat window
        appendMessage(
            JSONObject().put("message_id", "voice-local:${UUID.randomUUID()}")
                .put("role", "user")
                .put("content", content)
                .put("sequence", 0L)
                .put("pending", true),
            speakNew = false
        )

        val requestId = bridge.submitMessage(
            conversationId = conversationId,
            content = content,
            contextId = continuationId,
            expectedContinuationRevision = continuationRevision,
            correlationId = correlationId,
            interactionContext = JSONObject()
                .put("version", 1)
                .put("kind", "assistant_voice_session")
                .put("interface", "voice")
                .put("platform", "android")
                .put("session_id", sessionId)
                .put("correlation_id", correlationId)
        ) { response ->
            main.post { handleBridgeResult(response) }
        }
        ownedActiveRequests.add(requestId)
        sessionRequestIds.add(requestId)
        requestCorrelations[requestId] = correlationId
        requestActivity[requestId] = JSONObject().put("state", "accepted").put("request_id", requestId)
        cueRequestId = requestId
        sayCue(progressCues.startTurn(content), requestId)
        Log.i(TAG, "voice request submitted owned=${ownedActiveRequests.size}")
    }

    private fun handleBridgeResult(response: BridgeClient.Result) {
        if (disposed) return
        response.unavailable?.let { unavailable ->
            // Empty outbox / historical replay status does not belong to this invocation.
            // Only failure of a turn submitted by this voice session can end engagement.
            val failedRequest = unavailable.requestId
            if (failedRequest == null || failedRequest !in sessionRequestIds) {
                Log.i(TAG, "bridge replay unavailable ignored owned=false")
                return
            }
            // A retained request may replay later, but this invocation does not churn the mic.
            unavailable.requestId?.let { ownedActiveRequests.remove(it) }
            updateBusyFromOwnedRequests()
            Log.w(TAG, "voice request unavailable retryable=${unavailable.retryable} owned=${ownedActiveRequests.size}")
            if (!busy) {
                stopCue()
                settleIdle("request_unavailable")
            }
            return
        }
        response.body?.optJSONObject("message")?.let { appendMessage(it, speakNew = true) }
        response.body?.takeIf { it.has("request_id") && it.has("state") }?.let { handleActivityEvent(it) }
    }

    private fun handleActivityEvent(event: JSONObject) {
        if (disposed) return
        val state = event.optString("state")
        val requestId = event.optString("request_id").takeIf { it.isNotBlank() }
        requestId?.let { requestActivity[it] = event }
        val ownedByInvocation = requestId != null && requestId in sessionRequestIds
        if (!ownedByInvocation) {
            Log.i(TAG, "request state=$state owned=false")
            return
        }
        if (state == "acknowledged" && requestId == cueRequestId && busy &&
            active && engaged && voiceEnabled && (!speaking || cueSpeaking)) {
            // The assistant's own first line ("Sure, checking the forecast"), said while it works.
            event.optString("label").takeIf { it.isNotBlank() }?.let {
                progressCues.acknowledged()
                sayCue(it, requestId!!)
            }
        }
        if (state == "tool_started" && requestId == cueRequestId && busy &&
            active && engaged && voiceEnabled && (!speaking || cueSpeaking)) {
            progressCues.toolStarted(event.optString("tool_name"))?.let { sayCue(it, requestId!!) }
        }
        if (state in TERMINAL_REQUEST_STATES) requestId?.let { ownedActiveRequests.remove(it) }
        updateBusyFromOwnedRequests()
        Log.i(TAG, "request state=$state owned=true busy=$busy")
        if (state in setOf("failed", "cancelled", "expired") && !busy && (!speaking || cueSpeaking)) {
            stopCue()
            settleIdle("request_terminal")
        } else if (state == "completed" && !busy && (!speaking || cueSpeaking) && requestId != null) {
            // Completion and the final message are independent stream records. Allow the final
            // response to arrive, but do not leave a processing session engaged indefinitely.
            awaitingResponseRequestId = requestId
            main.postDelayed({
                if (engaged && !busy && (!speaking || cueSpeaking) && awaitingResponseRequestId == requestId) {
                    settleIdle("response_missing")
                }
            }, 5000L)
        }
    }

    private fun updateBusyFromOwnedRequests() {
        ownedActiveRequests.removeAll { requestId ->
            requestActivity[requestId]?.optString("state") in TERMINAL_REQUEST_STATES
        }
        busy = ownedActiveRequests.isNotEmpty()
    }

    private fun appendMessage(message: JSONObject, speakNew: Boolean) {
        if (disposed) return
        val role = message.optString("role")
        if (role !in setOf("user", "assistant")) return
        val id = message.optString("message_id")
        if (id.isNotEmpty() && messages.any { it.optString("message_id") == id }) return
        val sequence = message.optLong("sequence", 0L)
        val isNew = sequence > lastSequence || sequence == 0L
        if (role == "user" && !message.optBoolean("pending")) {
            messages.removeAll { it.optBoolean("pending") && it.optString("content") == message.optString("content") }
        }
        messages.add(message)
        lastSequence = maxOf(lastSequence, sequence)
        if (message.has("continuation_id")) {
            continuationId = if (message.isNull("continuation_id")) null else
                message.optString("continuation_id").takeIf { it.isNotBlank() && it != "null" }
            continuationRevision = if (continuationId != null && !message.isNull("continuation_revision"))
                message.optLong("continuation_revision") else null
        }
        persist()

        val responseRequestId = message.optString("request_id").takeIf { it.isNotBlank() }
        val speechIdentity = id.ifBlank { responseRequestId?.let { "$it:$sequence" }.orEmpty() }
        if (speakNew && isNew && role == "assistant" && responseRequestId != null &&
            responseRequestId in sessionRequestIds && speechIdentity.isNotBlank() &&
            spokenMessageIds.add(speechIdentity)) {
            awaitingResponseRequestId = null
            // Only this invocation's current request owns TTS. Durable history and proactive
            // messages remain visible to the canvas but can never replay through this session.
            ownedActiveRequests.remove(responseRequestId)
            updateBusyFromOwnedRequests()
            val content = message.optString("content")
            val speechFriendly = VoiceController.toSpeechFriendly(content)
            if (speechFriendly.isNotBlank()) {
                say(speechFriendly, responseRequestId, requestCorrelations[responseRequestId])
            } else {
                settleIdle("empty_response")
            }
        }
    }

    private fun connect() {
        if (disposed || loading) return
        if (identity.pairingRevision() == 0L) {
            bridge.autoPair { response ->
                main.post {
                    handleBridgeResult(response)
                    if (identity.pairingRevision() > 0) {
                        reportLifecycle("start")
                        connect()
                    }
                }
            }
            return
        }
        loading = true
        bridge.history(conversationId, 0L) { response ->
            main.post {
                loading = false
                if (disposed) return@post
                val history = response.body?.optJSONArray("messages")
                if (history != null) {
                    for (i in 0 until history.length()) {
                        val msg = history.getJSONObject(i)
                        appendMessage(msg, speakNew = false)
                        msg.optString("message_id").takeIf { it.isNotBlank() }?.let { spokenMessageIds.add(it) }
                    }
                }
                bridge.requestStates(conversationId) { requests ->
                    main.post {
                        val states = requests.body?.optJSONArray("requests")
                        if (states != null) {
                            for (i in 0 until states.length()) handleActivityEvent(states.getJSONObject(i))
                        }
                    }
                }
                startEventStream()
                bridge.replay { replayed -> main.post { handleBridgeResult(replayed) } }
            }
        }
    }

    private fun startEventStream() {
        if (disposed || !active) return
        bridge.streamEvents(
            conversationId = conversationId,
            afterRevision = eventRevision,
            onEvent = { revision, event ->
                main.post {
                    if (disposed || revision <= eventRevision) return@post
                    eventRevision = revision
                    val kind = event.optString("_event_kind")
                    if (kind == "conversation.request" || (event.has("request_id") && !event.has("role"))) {
                        handleActivityEvent(event)
                    } else if (kind == "conversation.message" || event.has("role")) {
                        appendMessage(event, speakNew = true)
                    }
                    persist()
                }
            },
            onClosed = { unavailable ->
                main.post {
                    if (disposed) return@post
                    if (unavailable == null || unavailable.retryable) {
                        main.postDelayed({ startEventStream() }, 2000L)
                    }
                }
            }
        )
    }

    private fun persist() {
        preferences.edit()
            .putString("conversation_id", conversationId)
            .putLong("last_sequence", lastSequence)
            .putLong("last_event_revision", eventRevision)
            .putString("continuation_id", continuationId)
            .putString("messages", JSONArray(messages.takeLast(500)).toString())
            .apply {
                continuationRevision?.let { putLong("continuation_revision", it) }
                    ?: remove("continuation_revision")
            }
            .apply()
    }

    private fun reportLifecycle(
        transition: String,
        requestId: String? = null,
        correlationId: String? = null,
        cancelled: Boolean? = null,
        value: Boolean? = null,
        callback: (BridgeClient.Result) -> Unit = {}
    ) {
        if (sessionId.isBlank() || disposed) return
        bridge.voiceTransition(
            conversationId = conversationId,
            sessionId = sessionId,
            transition = transition,
            requestId = requestId,
            correlationId = correlationId,
            cancelled = cancelled,
            value = value,
            callback = callback
        )
    }

    private fun settleIdle(reason: String) {
        if (!engaged || disposed) return
        Log.i(TAG, "voice idle reason=$reason")
        engaged = false
        stopCue()
        speechGeneration++
        main.removeCallbacks(rearmRunnable)
        voice.cancelListening()
        VoiceSessionForeground.updateStatus(hostService, VoiceSessionForeground.State.IDLE)
        VoiceSessionForeground.stop(hostService)
        reportLifecycle("end")
    }

    private fun endEngagement() {
        if (disposed) return
        main.removeCallbacks(rearmRunnable)
        if (speaking && !cueSpeaking) reportLifecycle("speech_finished", speakingRequestId, speakingCorrelationId, cancelled = true)
        stopCue()
        speechGeneration++
        speaking = false
        speakingRequestId = null
        speakingCorrelationId = null
        voice.cancelListening()
        voice.stopSpeaking()
        engaged = false
        reportLifecycle("end")
        VoiceSessionForeground.stop(hostService)
    }

    /** Keep MainActivity available only for explicit secondary entry. */
    fun openExplicitSecondary(action: String) {
        pauseVoice()
        startAssistantActivity(Intent(hostService, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            putExtra(action, true)
        })
    }

    override fun onBackPressed() {
        if (canvasTouchDelegate?.onBackPressed() == true) {
            return
        }
        finish()
    }

    override fun onCloseSystemDialogs() {
        // Keep coherent on system dialog dismissal
    }

    override fun onDestroy() {
        Log.i(TAG, "session destroy")
        endEngagement()
        disposed = true
        active = false
        main.removeCallbacksAndMessages(null)
        voice.cancelListening()
        voice.stopSpeaking()
        voice.destroy()
        bridge.close()
        VoiceSessionForeground.stop(hostService)
        canvasTouchDelegate?.onSessionDestroy()
        canvasTouchDelegate = null
        super.onDestroy()
    }

}
