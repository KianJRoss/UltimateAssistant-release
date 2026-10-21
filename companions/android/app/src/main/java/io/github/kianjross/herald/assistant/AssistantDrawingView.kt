package io.github.kianjross.herald.assistant

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Rect
import android.graphics.RectF
import android.view.MotionEvent
import android.view.View
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.sin

/**
 * Transparent view for rendering task-anchored drawing primitives
 * (strokes, shapes, arrows, labels) in normalized coordinates.
 * Touch events pass through to underlying applications unless explicitly made clickable.
 */
class AssistantDrawingView(context: Context) : View(context) {

    private var primitives: List<CanvasGeometry.Primitive> = emptyList()

    private val strokePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
        strokeJoin = Paint.Join.ROUND
    }

    private val fillPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
    }

    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val textBgPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = 0xD91F2430.toInt() // dark translucent rounded pill for label legibility
        style = Paint.Style.FILL
    }

    init {
        setBackgroundColor(Color.TRANSPARENT)
        isClickable = false
        isFocusable = false
        importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_NO
    }

    override fun onTouchEvent(event: MotionEvent): Boolean {
        if (isClickable) {
            return super.onTouchEvent(event)
        }
        return false
    }

    fun setPrimitives(newPrimitives: List<CanvasGeometry.Primitive>) {
        primitives = newPrimitives
        invalidate()
    }

    fun updateDrawing(drawing: CanvasGeometry.Drawing) {
        primitives = CanvasGeometry.applyDrawing(primitives, drawing)
        invalidate()
    }

    fun clear() {
        primitives = emptyList()
        invalidate()
    }

    fun getPrimitives(): List<CanvasGeometry.Primitive> = primitives

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val w = width.toFloat()
        val h = height.toFloat()
        if (w <= 0f || h <= 0f || primitives.isEmpty()) return

        for (p in primitives) {
            when (p) {
                is CanvasGeometry.Primitive.Stroke -> drawStroke(canvas, p, w, h)
                is CanvasGeometry.Primitive.Shape -> drawShape(canvas, p, w, h)
                is CanvasGeometry.Primitive.Arrow -> drawArrow(canvas, p, w, h)
                is CanvasGeometry.Primitive.Label -> drawLabel(canvas, p, w, h)
                is CanvasGeometry.Primitive.Remove -> Unit
            }
        }
    }

    private fun drawStroke(canvas: Canvas, stroke: CanvasGeometry.Primitive.Stroke, w: Float, h: Float) {
        val points = stroke.points
        if (points.size < 2) return

        val paint = configureStrokePaint(stroke.style, defaultColor = 0xFF4A90E2.toInt(), defaultWidthDp = 3f)
        val path = Path()
        path.moveTo(points[0].x * w, points[0].y * h)
        for (i in 1 until points.size) {
            path.lineTo(points[i].x * w, points[i].y * h)
        }
        canvas.drawPath(path, paint)
    }

    private fun drawShape(canvas: Canvas, shape: CanvasGeometry.Primitive.Shape, w: Float, h: Float) {
        if (shape.shape == "line") {
            val from = shape.from ?: return
            val to = shape.to ?: return
            val paint = configureStrokePaint(shape.style, defaultColor = 0xFF50E3C2.toInt(), defaultWidthDp = 2.5f)
            canvas.drawLine(from.x * w, from.y * h, to.x * w, to.y * h, paint)
            return
        }

        val x = shape.x ?: 0f
        val y = shape.y ?: 0f
        val shapeW = shape.width ?: 0f
        val shapeH = shape.height ?: 0f
        val left = x * w
        val top = y * h
        val right = left + shapeW * w
        val bottom = top + shapeH * h

        val isFilled = shape.style?.filled == true
        val paint = if (isFilled) {
            configureFillPaint(shape.style, defaultColor = 0x8050E3C2.toInt())
        } else {
            configureStrokePaint(shape.style, defaultColor = 0xFF50E3C2.toInt(), defaultWidthDp = 2.5f)
        }

        when (shape.shape) {
            "rect" -> canvas.drawRect(left, top, right, bottom, paint)
            "ellipse" -> canvas.drawOval(RectF(left, top, right, bottom), paint)
        }
    }

    private fun drawArrow(canvas: Canvas, arrow: CanvasGeometry.Primitive.Arrow, w: Float, h: Float) {
        val x1 = arrow.from.x * w
        val y1 = arrow.from.y * h
        val x2 = arrow.to.x * w
        val y2 = arrow.to.y * h

        val paint = configureStrokePaint(arrow.style, defaultColor = 0xFFFF5252.toInt(), defaultWidthDp = 3f)
        val dx = x2 - x1
        val dy = y2 - y1
        val length = Math.hypot(dx.toDouble(), dy.toDouble()).toFloat()
        if (length <= 0.001f) return

        canvas.drawLine(x1, y1, x2, y2, paint)

        val angle = atan2(dy.toDouble(), dx.toDouble())
        val headLength = (paint.strokeWidth * 3.5f).coerceIn(dp(10).toFloat(), dp(28).toFloat())
        val arrowAngle = Math.toRadians(25.0)

        val headPath = Path().apply {
            moveTo(x2, y2)
            lineTo(
                (x2 - headLength * cos(angle - arrowAngle)).toFloat(),
                (y2 - headLength * sin(angle - arrowAngle)).toFloat()
            )
            lineTo(
                (x2 - headLength * cos(angle + arrowAngle)).toFloat(),
                (y2 - headLength * sin(angle + arrowAngle)).toFloat()
            )
            close()
        }

        val arrowHeadPaint = Paint(paint).apply {
            style = Paint.Style.FILL_AND_STROKE
        }
        canvas.drawPath(headPath, arrowHeadPaint)
    }

    private fun drawLabel(canvas: Canvas, label: CanvasGeometry.Primitive.Label, w: Float, h: Float) {
        val x = label.at.x * w
        val y = label.at.y * h
        val text = label.text
        if (text.isEmpty()) return

        val textSizePx = dp((label.style?.width?.toInt() ?: 13).coerceIn(10, 32)).toFloat()
        val textColor = parseHexColor(label.style?.color, Color.WHITE)
        val opacity = label.style?.opacity ?: 1.0f

        textPaint.apply {
            textSize = textSizePx
            color = textColor
            alpha = (opacity * (Color.alpha(textColor) / 255f) * 255).toInt().coerceIn(0, 255)
        }

        val textBounds = Rect()
        textPaint.getTextBounds(text, 0, text.length, textBounds)
        val padX = dp(6).toFloat()
        val padY = dp(4).toFloat()

        val pillRect = RectF(
            x - padX,
            y + textBounds.top - padY,
            x + textBounds.width() + padX,
            y + textBounds.bottom + padY
        )

        textBgPaint.alpha = (opacity * 217).toInt().coerceIn(0, 255)
        canvas.drawRoundRect(pillRect, dp(4).toFloat(), dp(4).toFloat(), textBgPaint)
        canvas.drawText(text, x, y, textPaint)
    }

    private fun configureStrokePaint(style: CanvasGeometry.Style?, defaultColor: Int, defaultWidthDp: Float): Paint {
        val color = parseHexColor(style?.color, defaultColor)
        val widthDp = style?.width ?: defaultWidthDp
        val opacity = style?.opacity ?: 1.0f
        return strokePaint.apply {
            this.color = color
            this.strokeWidth = dp(widthDp.toInt().coerceAtLeast(1)).toFloat()
            this.alpha = (opacity * (Color.alpha(color) / 255f) * 255).toInt().coerceIn(0, 255)
        }
    }

    private fun configureFillPaint(style: CanvasGeometry.Style?, defaultColor: Int): Paint {
        val color = parseHexColor(style?.color, defaultColor)
        val opacity = style?.opacity ?: 1.0f
        return fillPaint.apply {
            this.color = color
            this.alpha = (opacity * (Color.alpha(color) / 255f) * 255).toInt().coerceIn(0, 255)
        }
    }

    private fun parseHexColor(colorStr: String?, defaultColor: Int): Int {
        if (colorStr.isNullOrBlank()) return defaultColor
        return try {
            if (colorStr.startsWith("#") && colorStr.length == 9) {
                // #RRGGBBAA format: convert to Android's #AARRGGBB
                val rr = colorStr.substring(1, 3)
                val gg = colorStr.substring(3, 5)
                val bb = colorStr.substring(5, 7)
                val aa = colorStr.substring(7, 9)
                Color.parseColor("#$aa$rr$gg$bb")
            } else {
                Color.parseColor(colorStr)
            }
        } catch (_: Exception) {
            defaultColor
        }
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()
}
