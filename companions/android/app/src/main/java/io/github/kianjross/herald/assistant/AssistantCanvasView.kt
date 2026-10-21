package io.github.kianjross.herald.assistant

import android.content.Context
import android.graphics.Color
import android.graphics.Rect
import android.graphics.Region
import android.graphics.drawable.GradientDrawable
import android.view.MotionEvent
import android.view.View
import android.view.accessibility.AccessibilityEvent
import android.widget.Button
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import kotlin.math.roundToInt

/** Transparent spatial host. Only action-bearing/dismissible children enter the touch region. */
class AssistantCanvasView(
    context: Context,
    private val assets: AssistantAssetRenderer,
    private val onAction: (AssistantPresentation.Element, AssistantPresentation.Action) -> Unit,
    private val onDismiss: (AssistantPresentation.Element) -> Unit,
    private val onRegionChanged: () -> Unit,
    private val cards: AssistantCardView.Host? = null,
    /** Where interactive cards go when they need their own touchable windows (the always-on overlay). */
    private val external: ExternalCards? = null
) : FrameLayout(context) {
    interface ExternalCards {
        fun show(element: AssistantPresentation.Element, host: AssistantCardView.Host)
        fun remove(id: String)
        fun clear()
        fun relayout()
    }
    private val externalIds = mutableSetOf<String>()
    private val userPositions = mutableMapOf<String, Pair<Float, Float>>()
    private fun body(view: View): View = if (view is CanvasDragHandle) view.content else view
    private val records = linkedMapOf<String, Record>()
    private data class Record(val element: AssistantPresentation.Element, val view: View)

    init {
        setBackgroundColor(Color.TRANSPARENT)
        clipChildren = true
        clipToPadding = true
        importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_NO
        addOnLayoutChangeListener { _, _, _, _, _, _, _, _, _ -> layoutElements(); onRegionChanged() }
    }

    override fun onTouchEvent(event: MotionEvent): Boolean = false

    fun apply(element: AssistantPresentation.Element) {
        val current = records[element.id]
        if (current != null && element.revision <= current.element.revision) return
        if (element.operation == "remove") { remove(element.id); return }
        if (current != null && (current.element.bounds != element.bounds || !element.draggable)) userPositions.remove(element.id)
        if ((element.kind == "card" || element.draggable) && external != null && cards != null) {
            current?.let { release(it.view); removeView(it.view); records.remove(element.id) }
            externalIds += element.id
            external.show(element, cards)
            return
        }

        if (externalIds.remove(element.id)) external?.remove(element.id)

        // If an existing drawing view receives an updated revision, merge primitives in-place
        if ((element.kind == "drawing" || element.drawing != null) && current != null && body(current.view) is AssistantDrawingView && (current.view is CanvasDragHandle) == element.draggable && element.drawing != null) {
            (body(current.view) as AssistantDrawingView).updateDrawing(element.drawing)
            records[element.id] = Record(element, current.view)
            current.view.elevation = element.zOrder.toFloat()
            if (isLaidOut) layoutElement(current.view, element)
            onRegionChanged()
            return
        }

        // An interactive card keeps its WebView and state; it just refreshes.
        if (element.kind == "card" && current != null && body(current.view) is AssistantCardView && (current.view is CanvasDragHandle) == element.draggable) {
            (body(current.view) as AssistantCardView).show(element)
            records[element.id] = Record(element, current.view)
            current.view.elevation = element.zOrder.toFloat()
            if (isLaidOut) layoutElement(current.view, element)
            onRegionChanged()
            return
        }

        current?.let { release(it.view); removeView(it.view); assets.cancel(element.id) }
        val content = buildChild(element)
        val child = if (element.draggable) CanvasDragHandle(context, content) { dx, dy ->
            records[element.id]?.view?.let { view ->
                val maxX = (width - view.width).coerceAtLeast(0).toFloat()
                val maxY = (height - view.height).coerceAtLeast(0).toFloat()
                view.x = (view.x + dx).coerceIn(0f, maxX)
                view.y = (view.y + dy).coerceIn(0f, maxY)
                userPositions[element.id] = Pair(if (maxX > 0) view.x/maxX else 0f, if (maxY > 0) view.y/maxY else 0f)
                onRegionChanged()
            }
        } else content
        records[element.id] = Record(element, child)
        val initialWidth = if (element.kind == "drawing" || element.drawing != null) LayoutParams.MATCH_PARENT else dp(280)
        val initialHeight = if (element.kind == "drawing" || element.drawing != null) LayoutParams.MATCH_PARENT else LayoutParams.WRAP_CONTENT
        addView(child, LayoutParams(initialWidth, initialHeight))
        child.elevation = element.zOrder.toFloat()
        if (isLaidOut) layoutElement(child, element)
        onRegionChanged()
    }

    fun remove(id: String) {
        userPositions.remove(id)
        if (externalIds.remove(id)) external?.remove(id)
        records.remove(id)?.let { release(it.view); removeView(it.view); assets.cancel(id); onRegionChanged() }
    }

    /** Re-place everything after a card's content height changed. */
    fun relayout() {
        if (isLaidOut) layoutElements()
        external?.relayout()
        onRegionChanged()
    }

    private fun release(view: View) {
        val content = body(view)
        if (content is AssistantCardView) content.destroy()
    }

    fun clearElements() {
        records.keys.toList().forEach(::remove)
        if (externalIds.isNotEmpty()) { externalIds.clear(); external?.clear() }
    }
    fun hasElements(): Boolean = records.isNotEmpty() || externalIds.isNotEmpty()

    fun computeTouchableRegion(out: Region) {
        out.setEmpty()
        records.values.forEach { record ->
            val policy = record.element.interactionPolicy
            if (!record.element.draggable && policy in setOf("none", "touch_through")) return@forEach
            if (record.element.draggable || policy in setOf("interactive", "dismissible") ||
                record.element.actions.isNotEmpty() || record.element.kind == "question") {
                val rect = Rect()
                if (record.view.visibility == VISIBLE && record.view.getGlobalVisibleRect(rect)) {
                    val root = IntArray(2); getLocationOnScreen(root); rect.offset(-root[0], -root[1])
                    out.op(rect, Region.Op.UNION)
                }
            }
        }
    }

    internal fun buildChild(element: AssistantPresentation.Element): View {
        if (element.kind == "card" && cards != null) return AssistantCardView(context, cards).apply { show(element) }

        // Task-anchored drawings are rendered with AssistantDrawingView (not plain-text annotations)
        if (element.kind == "drawing" || element.drawing != null) {
            val drawingView = AssistantDrawingView(context).apply {
                contentDescription = element.alt ?: element.title ?: "Drawing"
                element.drawing?.let { updateDrawing(it) }
            }
            if (element.interactionPolicy == "dismissible") {
                drawingView.setOnClickListener { onDismiss(element) }
            }
            return drawingView
        }

        // Standalone reference imagery without card chrome when no title, actions, or text exist
        if (element.kind in setOf("image", "artifact") && element.title.isNullOrBlank() && element.actions.isEmpty() && element.content.isNullOrBlank()) {
            val host = FrameLayout(context).apply {
                contentDescription = element.asset?.alt ?: element.alt ?: context.getString(R.string.canvas_asset)
                importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_YES
            }
            val image = ImageView(context).apply {
                adjustViewBounds = true
                scaleType = ImageView.ScaleType.FIT_CENTER
                contentDescription = host.contentDescription
            }
            val status = label(element.asset?.alt ?: element.alt ?: "", 12f, Color.LTGRAY).apply {
                visibility = GONE
            }
            host.addView(image, FrameLayout.LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT))
            host.addView(status, FrameLayout.LayoutParams(LayoutParams.WRAP_CONTENT, LayoutParams.WRAP_CONTENT))
            element.asset?.let { assets.render(element.id, it, image, status) }
            if (element.interactionPolicy == "dismissible") host.setOnClickListener { onDismiss(element) }
            return host
        }

        val host = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), dp(10), dp(12), dp(10))
            background = GradientDrawable().apply { setColor(0xE6212630.toInt()); cornerRadius = dp(14).toFloat() }
            contentDescription = listOfNotNull(element.title, element.content, element.prompt, element.asset?.alt, element.alt).joinToString(". ")
            importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_YES
        }
        element.title?.let { host.addView(label(it, 16f, Color.WHITE)) }
        when (element.kind) {
            "image", "artifact" -> {
                val status = label(element.asset?.alt ?: element.alt ?: context.getString(R.string.canvas_asset), 12f, Color.LTGRAY)
                val image = ImageView(context).apply { adjustViewBounds = true; scaleType = ImageView.ScaleType.FIT_CENTER; contentDescription = status.text }
                host.addView(image, LinearLayout.LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.WRAP_CONTENT))
                host.addView(status)
                element.asset?.let { assets.render(element.id, it, image, status) }
            }
            "question" -> {
                host.addView(label(element.prompt.orEmpty(), 15f, Color.WHITE))
                element.choices.forEach { choice -> host.addView(Button(context).apply {
                    text = choice.second; isAllCaps = false
                    setOnClickListener { onAction(element, AssistantPresentation.Action(choice.first, "answer", choice.second)) }
                }) }
            }
            "flashcard" -> host.addView(label(listOfNotNull(element.front, element.back).joinToString("\n\n"), 15f, Color.WHITE))
            else -> host.addView(label(element.content.orEmpty(), 15f, Color.WHITE))
        }
        element.actions.forEach { action -> host.addView(Button(context).apply {
            text = action.label; isAllCaps = false; setOnClickListener { onAction(element, action) }
        }) }
        if (element.interactionPolicy == "dismissible") host.setOnClickListener { onDismiss(element) }
        return host
    }

    private fun label(value: String, size: Float, color: Int) = TextView(context).apply {
        text = value; textSize = size; setTextColor(color); maxLines = 12
    }

    private fun layoutElements() = records.values.forEach { layoutElement(it.view, it.element) }
    private fun layoutElement(view: View, element: AssistantPresentation.Element) {
        val bounds = element.bounds
        val availableWidth = width.coerceAtLeast(1)
        val availableHeight = height.coerceAtLeast(1)

        val isDrawing = element.kind == "drawing" || element.drawing != null
        val desiredWidth = coordinate(bounds?.width, availableWidth) ?: if (isDrawing) availableWidth else dp(280)
        val desiredHeight = coordinate(bounds?.height, availableHeight) ?: if (isDrawing) availableHeight else LayoutParams.WRAP_CONTENT

        val targetWidth = if (isDrawing && bounds?.width == null) availableWidth else desiredWidth.coerceIn(dp(48), availableWidth)
        val targetHeight = if (isDrawing && bounds?.height == null) availableHeight else (if (desiredHeight == LayoutParams.WRAP_CONTENT) desiredHeight else desiredHeight.coerceIn(dp(32), availableHeight))

        view.layoutParams = (view.layoutParams as LayoutParams).apply {
            width = targetWidth
            height = targetHeight
        }

        val x = coordinate(bounds?.x ?: bounds?.left, availableWidth) ?: if (isDrawing) 0 else anchorX(bounds?.anchor, availableWidth, targetWidth)
        val y = coordinate(bounds?.y ?: bounds?.top, availableHeight) ?: if (isDrawing) 0 else anchorY(bounds?.anchor, availableHeight, if (view.height > 0) view.height else dp(120))

        view.x = x.coerceIn(0, (availableWidth - targetWidth).coerceAtLeast(0)).toFloat()
        val maxY = (availableHeight - (if (view.height > 0) view.height else dp(32))).coerceAtLeast(0)
        view.y = y.coerceIn(0, maxY).toFloat()
        userPositions[element.id]?.let { (px, py) ->
            view.x = px * (availableWidth - targetWidth).coerceAtLeast(0)
            view.y = py * maxY
        }
    }

    private fun coordinate(value: Double?, extent: Int): Int? = value?.let { if (it in 0.0..1.0) (it * extent).roundToInt() else dp(it.roundToInt()) }
    private fun anchorX(anchor: String?, extent: Int, child: Int) = if (anchor?.contains("right") == true) extent - child - dp(16) else if (anchor?.contains("center") == true) (extent - child) / 2 else dp(16)
    private fun anchorY(anchor: String?, extent: Int, child: Int) = if (anchor?.contains("bottom") == true) extent - child - dp(16) else if (anchor?.contains("center") == true) (extent - child) / 2 else dp(16)
    private fun dp(value: Int) = (value * resources.displayMetrics.density).roundToInt()

    override fun onRequestSendAccessibilityEvent(child: View, event: AccessibilityEvent): Boolean =
        super.onRequestSendAccessibilityEvent(child, event)
}
