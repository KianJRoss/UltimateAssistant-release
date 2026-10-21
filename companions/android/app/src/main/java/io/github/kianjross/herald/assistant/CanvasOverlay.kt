package io.github.kianjross.herald.assistant

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.PixelFormat
import android.os.Build
import android.provider.Settings
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.FrameLayout
import kotlin.math.roundToInt

/**
 * The canvas as an always-on overlay over other apps (Android's "Display over other apps").
 *
 * Passive things (notes, drawings, images) are drawn in one full-screen window that never takes
 * touches; Android only lets touches pass through another app's window when it is at most 80%
 * opaque, hence the alpha. Interactive cards each get their own small window: touches outside it go
 * to the app below, and it becomes focusable only after a tap inside, so typing works without it
 * ever stealing the keyboard or the back button. Hosted by AssistantConnectionService.
 */
class CanvasOverlay(private val context: Context) {
    companion object {
        /** True while the overlay shows the canvas; the voice session then draws no second copy. */
        @Volatile var active = false
        fun permitted(context: Context) = Settings.canDrawOverlays(context)
    }

    private val windows = context.getSystemService(WindowManager::class.java)
    private val root = FrameLayout(context)
    private val assets = AssistantAssetRenderer(context)
    private val cards = Cards()
    private var controller: AssistantCanvasController? = null

    fun start() {
        if (controller != null || !permitted(context)) return
        val params = WindowManager.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            PixelFormat.TRANSLUCENT
        ).apply { gravity = Gravity.TOP or Gravity.START; alpha = 0.8f; title = "Assistant canvas" }
        runCatching { windows.addView(root, params) }.onFailure { return }
        root.addOnLayoutChangeListener { _, _, _, _, _, _, _, _, _ -> cards.relayout() }
        controller = AssistantCanvasController({}, root, cards).also { it.onSessionShow() }
        active = true
    }

    fun stop() {
        active = false
        controller?.onSessionDestroy()
        controller = null
        cards.clear()
        assets.close()
        runCatching { windows.removeView(root) }
    }

    private inner class Cards : AssistantCanvasView.ExternalCards {
        private inner class Card(val element: AssistantPresentation.Element, val view: View,
                                 val frame: Frame, val params: WindowManager.LayoutParams)
        private val shown = linkedMapOf<String, Card>()
        private val userPositions = mutableMapOf<String, Pair<Float, Float>>()
        private val builder = AssistantCanvasView(context, assets, { _, _ -> }, { remove(it.id) }, {})
        private fun release(view: View) { if (view is AssistantCardView) view.destroy() }

        override fun show(element: AssistantPresentation.Element, host: AssistantCardView.Host) {
            val current = shown[element.id]
            if (current != null && (current.element.bounds != element.bounds || !element.draggable)) userPositions.remove(element.id)
            if (current != null && current.view is AssistantCardView && element.kind == "card" && current.element.draggable == element.draggable) {
                current.view.show(element)
                shown[element.id] = Card(element, current.view, current.frame, current.params)
                relayout()
                return
            }
            current?.let {
                release(it.view)
                runCatching { windows.removeView(it.frame) }
                shown.remove(element.id)
            }
            val view: View = if (element.kind == "card") AssistantCardView(context, host).apply { show(element) }
                            else builder.buildChild(element.copy(draggable = false))
            val frame = Frame(context)
            val content = if (element.draggable) CanvasDragHandle(context, view) { dx, dy ->
                shown[element.id]?.let { card ->
                    val (sw, sh) = screen()
                    val maxX = (sw-card.frame.width).coerceAtLeast(0)
                    val minY = dp(32)
                    val maxY = (sh-card.frame.height-dp(32)).coerceAtLeast(minY)
                    card.params.x = (card.params.x+dx).roundToInt().coerceIn(0,maxX)
                    card.params.y = (card.params.y+dy).roundToInt().coerceIn(minY,maxY)
                    userPositions[element.id] = Pair(if(maxX>0) card.params.x.toFloat()/maxX else 0f,
                        if(maxY>minY) (card.params.y-minY).toFloat()/(maxY-minY) else 0f)
                    runCatching { windows.updateViewLayout(card.frame, card.params) }
                }
            } else view
            val height = if (element.kind == "drawing") ((element.bounds?.height ?: 0.3) * screen().second).roundToInt() else ViewGroup.LayoutParams.WRAP_CONTENT
            if (element.kind == "drawing" && content is CanvasDragHandle) view.layoutParams.height = height
            val frameHeight = if (element.kind == "drawing") height else ViewGroup.LayoutParams.WRAP_CONTENT
            frame.addView(content, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, frameHeight))
            val params = WindowManager.LayoutParams(
                width(element), ViewGroup.LayoutParams.WRAP_CONTENT,
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
                    WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH or WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                PixelFormat.TRANSLUCENT
            ).apply { gravity = Gravity.TOP or Gravity.START; title = "Assistant card" }
            frame.params = params
            runCatching { windows.addView(frame, params) }.onFailure { release(view); return }
            shown[element.id] = Card(element, view, frame, params)
            frame.addOnLayoutChangeListener { _, _, _, _, _, _, _, _, _ -> relayout() }
            relayout()
        }

        override fun remove(id: String) {
            userPositions.remove(id)
            assets.cancel(id)
            shown.remove(id)?.let { release(it.view); runCatching { windows.removeView(it.frame) } }
            relayout()
        }

        override fun clear() { shown.keys.toList().forEach(::remove) }

        /** Place cards by their anchor, stacking cards that share one instead of overlapping. */
        override fun relayout() {
            val (screenWidth, screenHeight) = screen()
            val stacks = mutableMapOf<String, Int>()
            shown.values.sortedBy { it.element.zOrder }.forEach { card ->
                val bounds = card.element.bounds
                val anchor = bounds?.anchor ?: "top_right"
                val width = width(card.element)
                val height = card.frame.height.takeIf { it > 0 } ?: ((card.view as? AssistantCardView)?.contentHeightPx?.takeIf { it > 0 } ?: dp(72))
                var x = bounds?.x?.let { (it * (screenWidth - width)).roundToInt() } ?: when {
                    "left" in anchor -> dp(12)
                    "right" in anchor -> screenWidth - width - dp(12)
                    else -> (screenWidth - width) / 2
                }
                val offset = stacks.getOrDefault(anchor, 0)
                var y = bounds?.y?.let { (it * (screenHeight - height)).roundToInt() } ?: when {
                    anchor.startsWith("bottom") -> screenHeight - height - dp(96) - offset
                    anchor.startsWith("top") -> dp(48) + offset
                    else -> (screenHeight - height) / 2 + offset
                }
                stacks[anchor] = offset + height + dp(8)
                val maxX = (screenWidth-width).coerceAtLeast(0)
                val minY = dp(32)
                val maxY = (screenHeight-height-dp(32)).coerceAtLeast(minY)
                userPositions[card.element.id]?.let { (px, py) ->
                    x = (px*maxX).roundToInt(); y = minY+(py*(maxY-minY)).roundToInt()
                }
                x = x.coerceIn(0,maxX); y = y.coerceIn(minY,maxY)
                if (card.params.x != x || card.params.y != y || card.params.width != width) {
                    card.params.x = x; card.params.y = y; card.params.width = width
                    runCatching { windows.updateViewLayout(card.frame, card.params) }
                }
            }
        }

        private fun width(element: AssistantPresentation.Element): Int {
            val screenWidth = screen().first
            val fraction = element.bounds?.width?.takeIf { it in 0.1..1.0 } ?: if (element.kind == "card") 0.94 else 0.78
            return (screenWidth * fraction).roundToInt().coerceAtMost(screenWidth - dp(16))
        }
    }

    /**
     * A card's window: becomes focusable after a tap inside (typing), and not focusable again on a tap
     * outside. The switch happens after the tap ends: changing the window mid-tap swallows the tap.
     */
    private inner class Frame(context: Context) : FrameLayout(context) {
        var params: WindowManager.LayoutParams? = null
        private var downX = 0f
        private var downY = 0f
        private var gripTouch = false

        @SuppressLint("ClickableViewAccessibility")
        override fun dispatchTouchEvent(event: MotionEvent): Boolean {
            if (event.actionMasked == MotionEvent.ACTION_DOWN) {
                downX=event.rawX; downY=event.rawY
                gripTouch = getChildAt(0) is CanvasDragHandle && event.y < dp(36) && event.x < (width - dp(44))
            }
            val handled = super.dispatchTouchEvent(event)
            when (event.actionMasked) {
                MotionEvent.ACTION_UP -> if (!gripTouch && kotlin.math.abs(event.rawX-downX) < dp(8) && kotlin.math.abs(event.rawY-downY) < dp(8)) post { focusable(true) }
                MotionEvent.ACTION_OUTSIDE -> focusable(false)
            }
            return handled
        }

        private fun focusable(wanted: Boolean) {
            val p = params ?: return
            if (!isAttachedToWindow) return
            val now = p.flags and WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE == 0
            if (now == wanted) return
            p.flags = if (wanted) p.flags and WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE.inv()
                      else p.flags or WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
            runCatching { windows.updateViewLayout(this, p) }
        }
    }

    private fun screen(): Pair<Int, Int> =
        if (Build.VERSION.SDK_INT >= 30) windows.currentWindowMetrics.bounds.let { it.width() to it.height() }
        else context.resources.displayMetrics.let { it.widthPixels to it.heightPixels }

    private fun dp(value: Int) = (value * context.resources.displayMetrics.density).roundToInt()
}
