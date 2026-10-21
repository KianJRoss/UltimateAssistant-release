package io.github.kianjross.herald.assistant

import android.content.Context
import android.content.Intent
import android.graphics.Region
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.view.ViewGroup
import android.widget.FrameLayout
import org.json.JSONArray
import org.json.JSONObject

/** Owns only the canvas projection lifecycle; voice/session and domain state remain elsewhere. */
class AssistantCanvasController(
    private val regionChanged: () -> Unit,
    root: FrameLayout,
    external: AssistantCanvasView.ExternalCards? = null
) : CanvasTouchDelegate {
    private val context: Context = root.context
    private val main = Handler(Looper.getMainLooper())
    private val identity = DeviceIdentity(context)
    private val bridge = BridgeClient(context, identity, context.getString(R.string.bridge_base_url))
    private val preferences = context.getSharedPreferences("assistant_canvas", Context.MODE_PRIVATE)
    private val conversationId = context.getSharedPreferences("conversation_ui", Context.MODE_PRIVATE)
        .getString("conversation_id", null) ?: "personal"
    private val renderer = AssistantAssetRenderer(context)
    private val presentationRevisions = linkedMapOf<String, Long>()
    private val envelopes = linkedMapOf<String, JSONObject>()
    private val elementRevisions = linkedMapOf<String, Long>()
    private val expiry = linkedMapOf<String, Runnable>()
    private var eventRevision = preferences.getLong("event_revision", 0)
    private var active = false
    private var disposed = false
    private var streaming = false

    private val cardHost: AssistantCardView.Host = object : AssistantCardView.Host {
        override fun card(cardId: String, callback: (JSONObject?) -> Unit) {
            bridge.canvasCard(cardId) { result -> main.post { if (!disposed) callback(result.body?.optJSONObject("card")) } }
        }
        override fun state(cardId: String, state: JSONArray) { bridge.canvasCardState(cardId, state) }
        override fun event(cardId: String, event: JSONObject) { bridge.canvasCardEvent(cardId, event) }
        override fun dismiss(cardId: String) {
            canvas.remove(cardId)
            regionChanged()
        }
        override fun heightChanged() { canvas.relayout() }
        override fun openUrl(url: String) {
            runCatching {
                context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            }
        }
    }

    private val canvas: AssistantCanvasView = AssistantCanvasView(context, renderer,
        onAction = { element, action -> handleAction(element, action) },
        onDismiss = { element -> removeElement(element.id, element.revision) },
        onRegionChanged = { regionChanged() },
        cards = cardHost,
        external = external
    )

    init {
        root.addView(canvas, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        restore()
    }

    override fun computeTouchableRegion(outRegion: Region) = canvas.computeTouchableRegion(outRegion)

    override fun onSessionShow() {
        active = true
        startStream()
    }

    override fun onSessionHide() {
        // Visual window may hide; projection and voice continuity stay intact for reconstruction.
        active = false
    }

    override fun onBackPressed(): Boolean {
        if (!canvas.hasElements()) return false
        canvas.clearElements()
        expiry.values.forEach(main::removeCallbacks); expiry.clear()
        envelopes.clear()
        elementRevisions.clear()
        persist()
        regionChanged()
        return true
    }

    override fun onSessionDestroy() {
        disposed = true
        active = false
        main.removeCallbacksAndMessages(null)
        renderer.close()
        bridge.close()
    }

    private fun startStream() {
        if (disposed || !active || streaming) return
        if (identity.pairingRevision() <= 0) {
            main.postDelayed(::startStream, 1000)
            return
        }
        streaming = true
        bridge.streamEvents(conversationId, eventRevision,
            onEvent = { revision, event -> main.post {
                if (disposed || revision <= eventRevision) return@post
                eventRevision = revision
                val envelope = event.optJSONObject("presentation") ?: event
                if (event.optString("_event_kind") == "conversation.presentation" || envelope.has("schema")) applyEnvelope(envelope)
                preferences.edit().putLong("event_revision", eventRevision).apply()
            } },
            onClosed = { unavailable -> main.post {
                streaming = false
                if (!disposed && active && (unavailable == null || unavailable.retryable)) main.postDelayed(::startStream, 2000)
            } })
    }

    fun applyEnvelope(envelope: JSONObject) {
        val presentation = runCatching { AssistantPresentation.parse(envelope) }.getOrNull() ?: return
        if (presentation.conversationId != null && presentation.conversationId != conversationId) return
        val previous = presentationRevisions[presentation.id] ?: 0L
        if (presentation.revision <= previous) return
        // The shared canvas publishes full snapshots; other presentation producers send deltas.
        if (presentation.id == "assistant-canvas") {
            val previousElements = envelopes[presentation.id]?.optJSONArray("elements")
            val incomingIds = presentation.elements.map { it.id }.toSet()
            if (previousElements != null) for (index in 0 until previousElements.length()) {
                val id = previousElements.optJSONObject(index)?.optString("id") ?: continue
                if (id !in incomingIds) {
                    expiry.remove(id)?.let(main::removeCallbacks)
                    elementRevisions.remove(id)
                    canvas.remove(id)
                }
            }
        }
        presentationRevisions[presentation.id] = presentation.revision
        envelopes[presentation.id] = JSONObject(envelope.toString())
        if (!presentation.isAvailable() || presentation.emptyCanvas || presentation.revoked) {
            if (presentation.id != "assistant-canvas") canvas.clearElements()
            if (presentation.id != "assistant-canvas") {
                expiry.values.forEach(main::removeCallbacks)
                expiry.clear()
            }
            if (presentation.emptyCanvas) {
                if (presentation.id != "assistant-canvas") { envelopes.clear(); elementRevisions.clear() }
                envelopes[presentation.id] = JSONObject(envelope.toString())
            } else if (presentation.revoked || !presentation.isAvailable()) {
                envelopes.remove(presentation.id)
            }
        }
        presentation.elements.sortedBy { it.zOrder }.forEach { element ->
            val known = elementRevisions[element.id] ?: 0L
            if (element.revision <= known) return@forEach
            elementRevisions[element.id] = element.revision
            if (element.operation == "remove" || !presentation.isAvailable() || presentation.revoked) removeElement(element.id, element.revision)
            else {
                canvas.apply(element)
                scheduleExpiry(element.id, element.expiresAtMillis ?: presentation.expiresAtMillis, element.revision)
            }
        }
        persist()
        regionChanged()
    }

    private fun scheduleExpiry(id: String, at: Long?, revision: Long) {
        expiry.remove(id)?.let(main::removeCallbacks)
        if (at == null) return
        val task = Runnable { removeElement(id, revision) }
        expiry[id] = task
        main.postDelayed(task, (at - System.currentTimeMillis()).coerceAtLeast(1L))
    }

    private fun removeElement(id: String, revision: Long) {
        if ((elementRevisions[id] ?: 0L) > revision) return
        elementRevisions[id] = revision
        expiry.remove(id)?.let(main::removeCallbacks)
        canvas.remove(id)
        regionChanged()
    }

    private fun handleAction(element: AssistantPresentation.Element, action: AssistantPresentation.Action) {
        // The v1 presentation contract describes intent but exposes no authoritative action route.
        // Only projection-local dismissal is safe; domain acknowledgements/snoozes remain server-owned.
        if (action.kind == "dismiss") removeElement(element.id, element.revision)
    }

    private fun restore() {
        runCatching {
            val stored = JSONArray(preferences.getString("presentations", "[]"))
            for (index in 0 until stored.length()) applyEnvelope(stored.getJSONObject(index))
        }
    }

    private fun persist() {
        val array = JSONArray(envelopes.values)
        preferences.edit().putString("presentations", array.toString()).putLong("event_revision", eventRevision).apply()
    }
}
