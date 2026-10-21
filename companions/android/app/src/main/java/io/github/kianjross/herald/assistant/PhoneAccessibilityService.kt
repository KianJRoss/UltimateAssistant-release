package io.github.kianjross.herald.assistant

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.graphics.Bitmap
import android.view.Display
import java.io.File
import java.util.UUID
import java.util.concurrent.Executor
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import kotlin.coroutines.resume
import android.graphics.Rect
import android.os.Build
import androidx.annotation.RequiresApi
import android.os.Bundle
import android.os.Looper
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

/**
 * PhoneAccessibilityService provides narrowly scoped, user-enabled interaction
 * with active applications when the explicit accessibility permission grant is active.
 *
 * It reports node/action evidence, allows inspecting visible text nodes, and executing
 * bounded interactions (click by id/text, scroll, back, home).
 * If the service is disabled or disconnected, callers immediately receive an unavailable result.
 */
class PhoneAccessibilityService : AccessibilityService() {

    companion object {
        @Volatile
        var instance: PhoneAccessibilityService? = null
            private set

        val isAvailable: Boolean
            get() = instance != null
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
    }

    override fun onDestroy() {
        if (instance == this) {
            instance = null
        }
        super.onDestroy()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // Passive observation if needed for contextual cues; no background input injection.
    }

    override fun onInterrupt() {
        // Called when the system wants to interrupt the feedback.
    }

    /** Actual pixel capture, independent of the global screenshot UI action. */
    suspend fun captureScreen(arguments: Map<String, Any?>): Map<String, Any?> {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) return mapOf("status" to "unavailable", "error" to
            "Screen capture from the app needs Android 11 or newer. On this phone the computer can capture it over " +
            "USB or wireless debugging (Android control module), or with Shizuku (privileged_shell: screencap -p).")
        if (instance !== this) return mapOf("status" to "failed", "error" to "Accessibility service is disconnected")
        return capturePixels(arguments)
    }

    @RequiresApi(Build.VERSION_CODES.R)
    private suspend fun capturePixels(arguments: Map<String, Any?>): Map<String, Any?> {
        val maxEdge = ((arguments["max_edge"] as? Number)?.toInt() ?: 1600).coerceIn(320, 2048)
        val executor = Executor { runnable -> Dispatchers.IO.dispatch(kotlin.coroutines.EmptyCoroutineContext, runnable) }
        return withTimeoutOrNull(15000) {
            withContext(Dispatchers.Main) {
                val packageName = rootInActiveWindow?.packageName?.toString().orEmpty()
                suspendCancellableCoroutine<Map<String, Any?>> { continuation ->
                    fun finish(value: Map<String, Any?>) {
                        if (continuation.isActive) continuation.resume(value)
                        else (value["capture_file"] as? Map<*, *>)?.get("local_path")?.let { File(it.toString()).delete() }
                    }
                    try {
                        takeScreenshot(Display.DEFAULT_DISPLAY, executor, object : TakeScreenshotCallback {
                            override fun onFailure(errorCode: Int) {
                                val reason = when (errorCode) {
                                    2 -> "Accessibility screenshot access is unavailable"
                                    3 -> "Android screenshot rate limit; wait before retrying"
                                    4 -> "Screenshot display is unavailable"
                                    6 -> "Android secure window prevents capture"
                                    else -> "Android screenshot failed"
                                }
                                finish(mapOf("status" to "failed", "error" to reason, "android_error_code" to errorCode))
                            }
                            override fun onSuccess(screenshot: ScreenshotResult) {
                                val buffer = screenshot.hardwareBuffer
                                var hardware: Bitmap? = null
                                var pixels: Bitmap? = null
                                var scaled: Bitmap? = null
                                var file: File? = null
                                try {
                                    hardware = Bitmap.wrapHardwareBuffer(buffer, screenshot.colorSpace)
                                        ?: error("Android returned no screenshot bitmap")
                                    pixels = hardware!!.copy(Bitmap.Config.ARGB_8888, false)
                                        ?: error("Cannot copy screenshot pixels")
                                    val originalWidth = pixels!!.width
                                    val originalHeight = pixels!!.height
                                    val ratio = minOf(1.0, maxEdge.toDouble() / maxOf(originalWidth, originalHeight))
                                    scaled = if (ratio < 1.0) Bitmap.createScaledBitmap(pixels!!,
                                        (originalWidth * ratio).toInt().coerceAtLeast(1),
                                        (originalHeight * ratio).toInt().coerceAtLeast(1), true) else pixels
                                    val directory = File(filesDir, "screen-captures").apply { mkdirs() }
                                    val id = UUID.randomUUID().toString()
                                    file = File(directory, "$id.png")
                                    file!!.outputStream().use { check(scaled!!.compress(Bitmap.CompressFormat.PNG, 100, it)) }
                                    check(file!!.length() <= 16L * 1024 * 1024) { "Screenshot exceeds 16 MB" }
                                    finish(mapOf("status" to "success", "package_name" to packageName,
                                        "width" to scaled!!.width, "height" to scaled!!.height,
                                        "original_width" to originalWidth, "original_height" to originalHeight,
                                        "captured_at_ms" to System.currentTimeMillis(),
                                        "capture_file" to mapOf("local_path" to file!!.absolutePath,
                                            "file_id" to id, "name" to "phone-screen.png", "media_type" to "image/png")))
                                } catch (error: Exception) {
                                    file?.delete()
                                    finish(mapOf("status" to "failed", "error" to (error.message ?: "Screenshot encoding failed")))
                                } finally {
                                    if (scaled !== pixels) scaled?.recycle()
                                    pixels?.recycle()
                                    hardware?.recycle()
                                    buffer.close()
                                }
                            }
                        })
                    } catch (error: Exception) {
                        finish(mapOf("status" to "failed", "error" to (error.message ?: "Screenshot unavailable")))
                    }
                }
            }
        } ?: mapOf("status" to "failed", "error" to "Android screenshot callback timed out")
    }

    /**
     * Perform a scoped accessibility action.
     * Enforces evidence collection and bounds on interaction.
     */
    fun performScopedAction(actionType: String, arguments: Map<String, Any?>): Map<String, Any?> {
        if (Looper.myLooper() != Looper.getMainLooper())
            return mapOf("status" to "failed", "error" to "Accessibility action requires Android main thread")
        if (instance !== this)
            return mapOf("status" to "failed", "error" to "Accessibility service is disconnected")
        val root = rootInActiveWindow
            ?: return mapOf(
                "status" to "failed",
                "error" to "No active window available from AccessibilityService"
            )

        return when (actionType.lowercase()) {
            "inspect_window", "get_tree" -> {
                val maxDepth = (arguments["max_depth"] as? Number)?.toInt() ?: 5
                val nodes = mutableListOf<Map<String, Any?>>()
                collectNodeEvidence(root, nodes, currentDepth = 0, maxDepth = maxDepth)
                mapOf(
                    "status" to "success",
                    "package_name" to (root.packageName?.toString() ?: ""),
                    "node_count" to nodes.size,
                    "nodes" to nodes
                )
            }
            "find_nodes" -> {
                val text = arguments["text"] as? String
                val viewId = arguments["view_id"] as? String
                val matches = mutableListOf<Map<String, Any?>>()

                if (!text.isNullOrBlank()) {
                    val found = root.findAccessibilityNodeInfosByText(text)
                    for (node in found) {
                        matches.add(nodeToEvidence(node))
                    }
                } else if (!viewId.isNullOrBlank()) {
                    val found = root.findAccessibilityNodeInfosByViewId(viewId)
                    for (node in found) {
                        matches.add(nodeToEvidence(node))
                    }
                } else {
                    return mapOf("status" to "failed", "error" to "Either 'text' or 'view_id' must be specified for find_nodes")
                }

                mapOf(
                    "status" to "success",
                    "match_count" to matches.size,
                    "matches" to matches
                )
            }
            "click_node" -> {
                val text = arguments["text"] as? String
                val viewId = arguments["view_id"] as? String

                val targetNode = when {
                    !viewId.isNullOrBlank() -> root.findAccessibilityNodeInfosByViewId(viewId).firstOrNull()
                    !text.isNullOrBlank() -> root.findAccessibilityNodeInfosByText(text).firstOrNull()
                    else -> null
                } ?: return mapOf(
                    "status" to "failed",
                    "error" to "Target node not found for click (view_id=$viewId, text=$text)"
                )

                var clickableNode: AccessibilityNodeInfo? = targetNode
                while (clickableNode != null && !clickableNode.isClickable) {
                    clickableNode = clickableNode.parent
                }

                val clicked = (clickableNode ?: targetNode).performAction(AccessibilityNodeInfo.ACTION_CLICK)
                mapOf(
                    "status" to if (clicked) "success" else "failed",
                    "clicked" to clicked,
                    "node_info" to nodeToEvidence(clickableNode ?: targetNode)
                )
            }
            "set_text" -> {
                val viewId = arguments["view_id"] as? String
                val textToSet = arguments["text"] as? String
                    ?: return mapOf("status" to "failed", "error" to "Missing 'text' argument")

                val targetNode = if (!viewId.isNullOrBlank()) {
                    root.findAccessibilityNodeInfosByViewId(viewId).firstOrNull()
                } else {
                    root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
                } ?: return mapOf("status" to "failed", "error" to "Target editable node not found")

                val argumentsBundle = Bundle().apply {
                    putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, textToSet)
                }
                val result = targetNode.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, argumentsBundle)
                mapOf(
                    "status" to if (result) "success" else "failed",
                    "text_set" to result
                )
            }
            "global_action" -> {
                val globalActionName = arguments["action"] as? String ?: ""
                val actionId = when (globalActionName.lowercase()) {
                    "back" -> GLOBAL_ACTION_BACK
                    "home" -> GLOBAL_ACTION_HOME
                    "recents" -> GLOBAL_ACTION_RECENTS
                    "notifications" -> GLOBAL_ACTION_NOTIFICATIONS
                    "quick_settings" -> GLOBAL_ACTION_QUICK_SETTINGS
                    "lock_screen" -> if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) GLOBAL_ACTION_LOCK_SCREEN else -1
                    "take_screenshot" -> if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) GLOBAL_ACTION_TAKE_SCREENSHOT else -1
                    else -> -1
                }

                if (actionId == -1) {
                    return mapOf("status" to "failed", "error" to "Unsupported or unavailable global action: $globalActionName")
                }

                val performed = performGlobalAction(actionId)
                mapOf("status" to if (performed) "success" else "failed", "global_action" to globalActionName, "result" to performed)
            }
            "scroll" -> {
                val direction = arguments["direction"] as? String ?: "forward"
                val scrollAction = if (direction.lowercase() == "backward") {
                    AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
                } else {
                    AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
                }
                val scrolled = root.performAction(scrollAction)
                mapOf("status" to if (scrolled) "success" else "failed", "scrolled" to scrolled, "direction" to direction)
            }
            else -> mapOf("status" to "failed", "error" to "Unknown accessibility action type: '$actionType'")
        }
    }

    private fun collectNodeEvidence(
        node: AccessibilityNodeInfo?,
        outList: MutableList<Map<String, Any?>>,
        currentDepth: Int,
        maxDepth: Int
    ) {
        if (node == null || currentDepth > maxDepth) return

        outList.add(nodeToEvidence(node))

        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            collectNodeEvidence(child, outList, currentDepth + 1, maxDepth)
        }
    }

    private fun nodeToEvidence(node: AccessibilityNodeInfo): Map<String, Any?> {
        val bounds = Rect()
        node.getBoundsInScreen(bounds)
        return mapOf(
            "view_id" to (node.viewIdResourceName ?: ""),
            "class_name" to (node.className?.toString() ?: ""),
            "package_name" to (node.packageName?.toString() ?: ""),
            "text" to (node.text?.toString() ?: ""),
            "content_description" to (node.contentDescription?.toString() ?: ""),
            "is_clickable" to node.isClickable,
            "is_enabled" to node.isEnabled,
            "is_focused" to node.isFocused,
            "is_scrollable" to node.isScrollable,
            "bounds" to mapOf(
                "left" to bounds.left,
                "top" to bounds.top,
                "right" to bounds.right,
                "bottom" to bounds.bottom
            )
        )
    }
}
