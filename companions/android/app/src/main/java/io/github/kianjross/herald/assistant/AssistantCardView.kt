package io.github.kianjross.herald.assistant

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Color
import android.os.Handler
import android.os.Looper
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayInputStream
import kotlin.math.roundToInt

/**
 * One interactive canvas card ("card" element), drawn on the phone itself: the renderer
 * (genui.js), its libraries and the card page ship in the app (assets/canvas) and are served to the
 * WebView from a private origin; the assistant only sends the card. Taps and state go back through
 * [host], which holds the device credential (the WebView never has it). Closing is local: the card
 * disappears at once and the assistant is told afterwards. Links open in the user's browser.
 */
@SuppressLint("SetJavaScriptEnabled", "ViewConstructor")
class AssistantCardView(context: Context, private val host: Host) : FrameLayout(context) {
    interface Host {
        fun card(cardId: String, callback: (JSONObject?) -> Unit)
        fun state(cardId: String, state: JSONArray)
        fun event(cardId: String, event: JSONObject)
        /** Remove the card from this phone now (the user closed it). */
        fun dismiss(cardId: String)
        fun heightChanged()
        fun openUrl(url: String)
    }

    private val main = Handler(Looper.getMainLooper())
    private val web = WebView(context)
    private var cardId: String? = null
    private var shownRevision = 0L
    private var ready = false
    private var pending: JSONObject? = null
    private var contentHeight = 0
    /** The card's content height in pixels (0 until it has rendered). */
    val contentHeightPx: Int get() = contentHeight

    init {
        setBackgroundColor(Color.TRANSPARENT)
        web.setBackgroundColor(Color.TRANSPARENT)
        web.isVerticalScrollBarEnabled = false
        web.isHorizontalScrollBarEnabled = false
        with(web.settings) {
            javaScriptEnabled = true
            allowFileAccess = false
            allowContentAccess = false
            setSupportMultipleWindows(false)  // window.open navigates, and navigation opens the browser
        }
        web.addJavascriptInterface(Bridge(), "UAHost")
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                val url = request?.url?.toString().orEmpty()
                if (url.startsWith("https://") || url.startsWith("http://")) host.openUrl(url)
                return true
            }

            override fun shouldInterceptRequest(view: WebView?, request: WebResourceRequest?): WebResourceResponse? {
                val url = request?.url ?: return null
                if (url.host != HOST) return null  // pictures and videos the card shows come from the web
                return bundled(url.path.orEmpty().trimStart('/'))
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                ready = true
                pending?.let(::push)
                pending = null
            }
        }
        web.webChromeClient = WebChromeClient()  // video playback
        addView(web, LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT))
        web.loadUrl("https://$HOST/card.html")
    }

    /** Show (or refresh) the card this element points at. */
    fun show(element: AssistantPresentation.Element) {
        val id = element.cardId ?: return
        if (id == cardId && element.cardRevision <= shownRevision) return
        cardId = id
        shownRevision = element.cardRevision
        contentDescription = element.title ?: context.getString(R.string.canvas_card)
        host.card(id) { card -> if (card != null && id == cardId) { if (ready) push(card) else pending = card } }
    }

    fun destroy() {
        cardId = null
        web.stopLoading()
        web.removeJavascriptInterface("UAHost")
        web.destroy()
    }

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val height = if (contentHeight > 0) contentHeight else dp(72)
        super.onMeasure(widthMeasureSpec, MeasureSpec.makeMeasureSpec(height, MeasureSpec.EXACTLY))
    }

    private fun push(card: JSONObject) {
        web.evaluateJavascript("window.uaShow(${JSONObject.quote(card.toString())})", null)
    }

    /** A file from assets/canvas, or a 404. Paths can't leave that folder. */
    private fun bundled(path: String): WebResourceResponse {
        val type = when (path.substringAfterLast('.', "")) {
            "html" -> "text/html"; "js" -> "text/javascript"; "css" -> "text/css"; "woff2" -> "font/woff2"
            else -> "application/octet-stream"
        }
        val stream = if (path.isEmpty() || ".." in path.split('/')) null
                     else runCatching { context.assets.open("canvas/$path") }.getOrNull()
        return if (stream != null) WebResourceResponse(type, "utf-8", stream)
               else WebResourceResponse("text/plain", "utf-8", 404, "Not found", emptyMap(), ByteArrayInputStream(ByteArray(0)))
    }

    private inner class Bridge {
        @JavascriptInterface
        fun event(json: String) {
            val id = cardId ?: return
            val event = runCatching { JSONObject(json) }.getOrNull() ?: return
            main.post {
                if (event.optString("type") == "close") host.dismiss(id)
                host.event(id, event)
            }
        }

        @JavascriptInterface
        fun state(json: String) {
            val id = cardId ?: return
            runCatching { JSONArray(json) }.getOrNull()?.let { main.post { host.state(id, it) } }
        }

        @JavascriptInterface
        fun height(cssPixels: Int) {
            main.post {
                val px = (cssPixels * resources.displayMetrics.density).roundToInt()
                if (px > 0 && px != contentHeight) {
                    contentHeight = px
                    requestLayout()
                    host.heightChanged()
                }
            }
        }
    }

    private fun dp(value: Int) = (value * resources.displayMetrics.density).roundToInt()

    private companion object {
        const val HOST = "canvas.ultimate-assistant.invalid"
    }
}
