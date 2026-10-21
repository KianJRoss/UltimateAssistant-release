package io.github.kianjross.herald.assistant

import android.widget.FrameLayout

/** Voice-window adapter. It deliberately contains no transcript, composer, or chat controls. */
class AssistantOverlayController private constructor(
    private val controller: AssistantCanvasController
) : CanvasTouchDelegate by controller {
    companion object {
        /** Frozen integration seam exposed by AssistantVoiceSession. */
        fun attach(session: AssistantVoiceSession, root: FrameLayout): CanvasTouchDelegate =
            // With the always-on overlay up, the canvas is already on screen: don't draw it twice.
            if (CanvasOverlay.active) object : CanvasTouchDelegate {
                override fun computeTouchableRegion(outRegion: android.graphics.Region) = outRegion.setEmpty()
            }
            else AssistantOverlayController(AssistantCanvasController({ session.requestTouchableRegionUpdate() }, root))

        /** Called by the application/service composition root before a voice session is shown. */
        fun install() {
            AssistantVoiceSession.canvasDelegateFactory = ::attach
        }
    }
}
