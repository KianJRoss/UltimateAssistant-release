package io.github.kianjross.herald.assistant

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Color
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.widget.FrameLayout
import kotlin.math.abs

/** A transparent drag region overlaying the top of the element to allow repositioning. */
@SuppressLint("ClickableViewAccessibility")
class CanvasDragHandle(context: Context, val content: View, move: (Float, Float) -> Unit) : FrameLayout(context) {
    init {
        val contentParams = (content.layoutParams as? LayoutParams)
            ?: LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.WRAP_CONTENT)
        addView(content, contentParams)

        val density = resources.displayMetrics.density
        val gripHeight = (36 * density).toInt()
        val closeMargin = (44 * density).toInt()

        val grip = View(context).apply {
            setBackgroundColor(Color.TRANSPARENT)
            contentDescription = "Drag to move canvas item"
        }
        val gripParams = LayoutParams(LayoutParams.MATCH_PARENT, gripHeight).apply {
            gravity = Gravity.TOP
            marginEnd = closeMargin
            rightMargin = closeMargin
        }
        addView(grip, gripParams)

        var startX = 0f
        var startY = 0f
        var lastX = 0f
        var lastY = 0f
        var dragging = false
        val slop = ViewConfiguration.get(context).scaledTouchSlop

        grip.setOnTouchListener { _, event ->
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    startX = event.rawX
                    startY = event.rawY
                    lastX = startX
                    lastY = startY
                    dragging = false
                    parent?.requestDisallowInterceptTouchEvent(true)
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    if (!dragging && (abs(event.rawX - startX) > slop || abs(event.rawY - startY) > slop)) {
                        dragging = true
                    }
                    if (dragging) {
                        move(event.rawX - lastX, event.rawY - lastY)
                        lastX = event.rawX
                        lastY = event.rawY
                    }
                    true
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    parent?.requestDisallowInterceptTouchEvent(false)
                    if (!dragging && event.actionMasked == MotionEvent.ACTION_UP) {
                        grip.performClick()
                    }
                    dragging = false
                    true
                }
                else -> true
            }
        }
    }
}
