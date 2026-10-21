package io.github.kianjross.herald.assistant

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.inputmethod.EditorInfo
import android.webkit.JavascriptInterface
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayInputStream

/** Adaptation of ultimate_assistant/ui/index.html's actual conversation design.
 * Packaged, network-isolated message renderer; credentials and transport remain native.
 * The native multiline composer provides the phone keyboard's genuine Send action.
 */
@SuppressLint("SetJavaScriptEnabled")
class AndroidConversationView(private val context: Context, private val callbacks: Callbacks) {
    data class Callbacks(
        val onSend: (String) -> Unit,
        val onVoice: () -> Unit,
        val onAttach: () -> Unit = {},
        val onSettings: () -> Unit,
        val onRetry: () -> Unit,
        val onSpeak: (String) -> Unit,
        val onComposerFocusIME: () -> Unit = {}
    )
    private val backgroundColor = Color.rgb(16, 19, 25)
    private val root = LinearLayout(context).apply {
        orientation = LinearLayout.VERTICAL
        setBackgroundColor(backgroundColor)
    }
    val view: View get() = root
    val composer: EditText = object : androidx.appcompat.widget.AppCompatEditText(context) {
        override fun onCreateInputConnection(outAttrs: EditorInfo): android.view.inputmethod.InputConnection? {
            val connection = super.onCreateInputConnection(outAttrs)
            outAttrs.imeOptions = (outAttrs.imeOptions and EditorInfo.IME_MASK_ACTION.inv() and
                EditorInfo.IME_FLAG_NO_ENTER_ACTION.inv()) or EditorInfo.IME_ACTION_SEND
            return connection
        }
    }
    private val web = WebView(context)
    private val retry = Button(context)
    private var loaded = false
    private var disposed = false
    private val scripts = mutableListOf<String>()
    private fun dp(value: Int): Int = (value * context.resources.displayMetrics.density).toInt()

    init {
        web.setBackgroundColor(backgroundColor)
        web.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = false
            allowFileAccess = true
            allowContentAccess = false
            @Suppress("DEPRECATION")
            allowFileAccessFromFileURLs = false
            @Suppress("DEPRECATION")
            allowUniversalAccessFromFileURLs = false
            mixedContentMode = android.webkit.WebSettings.MIXED_CONTENT_NEVER_ALLOW
        }
        web.addJavascriptInterface(object {
            @JavascriptInterface fun settings() { root.post { if (!disposed) callbacks.onSettings() } }
            @JavascriptInterface fun speak(text: String) { root.post { if (!disposed) callbacks.onSpeak(text) } }
        }, "ConversationNative")
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean = true
            override fun shouldInterceptRequest(view: WebView?, request: WebResourceRequest?): WebResourceResponse? {
                val url = request?.url?.toString().orEmpty()
                // Only bundled conversation resources; remote and arbitrary local URLs stay blocked.
                if (url == ASSET || (url.startsWith("file:///android_asset/vendor/") &&
                    !url.contains("..") && !url.contains("%", ignoreCase = true))) return null
                return WebResourceResponse("text/plain", "utf-8", ByteArrayInputStream(ByteArray(0)))
            }
            override fun onPageFinished(view: WebView?, url: String?) {
                if (disposed || url != ASSET) return
                loaded = true
                scripts.toList().forEach { web.evaluateJavascript(it, null) }
                scripts.clear()
            }
        }
        root.addView(web, LinearLayout.LayoutParams(-1, 0, 1f))
        val bottom = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), dp(8), dp(12), dp(12))
            background = GradientDrawable().apply { setColor(Color.rgb(25,30,40)); cornerRadius = dp(20).toFloat() }
        }
        composer.apply {
            hint = "Message your assistant..."
            setTextColor(Color.rgb(238,241,247)); setHintTextColor(Color.rgb(127,138,157))
            textSize = 16f
            minLines = 1; maxLines = 6
            setSingleLine(false)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE or InputType.TYPE_TEXT_FLAG_CAP_SENTENCES
            imeOptions = EditorInfo.IME_ACTION_SEND
            setPadding(dp(8), dp(8), dp(8), dp(8))
            background = null
            setOnFocusChangeListener { _, focused -> if (focused) callbacks.onComposerFocusIME() }
            setOnEditorActionListener { _, actionId, _ ->
                if (actionId == EditorInfo.IME_ACTION_SEND) { send(); true } else false
            }
        }
        bottom.addView(composer, LinearLayout.LayoutParams(-1, -2))
        val actions = LinearLayout(context).apply { gravity = Gravity.END; orientation = LinearLayout.HORIZONTAL }
        fun styleButton(button: Button, label: String, primary: Boolean = false, click: () -> Unit): Button = button.apply {
            text = label
            isAllCaps = false
            textSize = 14f
            setTextColor(Color.WHITE)
            minWidth = dp(64); minimumHeight = dp(48)
            setPadding(dp(14), 0, dp(14), 0)
            backgroundTintList = null
            val shape = GradientDrawable().apply {
                cornerRadius = dp(18).toFloat()
                setColor(if (primary) Color.rgb(109,123,240) else Color.rgb(40,47,62))
                if (!primary) setStroke(dp(1), Color.rgb(58,67,86))
            }
            background = android.graphics.drawable.RippleDrawable(
                android.content.res.ColorStateList.valueOf(Color.argb(45,255,255,255)), shape, null)
            layoutParams = LinearLayout.LayoutParams(-2, dp(48)).apply { marginStart = dp(8) }
            setOnClickListener { click() }
        }
        styleButton(retry, "Retry", click = callbacks.onRetry)
        retry.visibility = View.GONE
        actions.setPadding(0, dp(6), 0, 0)
        actions.addView(styleButton(Button(context), "Attach", click = callbacks.onAttach))
        actions.addView(styleButton(Button(context), "Voice", click = callbacks.onVoice))
        actions.addView(retry)
        actions.addView(styleButton(Button(context), "Send", true) { send() })
        bottom.addView(actions)
        root.addView(bottom, LinearLayout.LayoutParams(-1, -2))
        web.loadUrl(ASSET)
    }

    private fun send() {
        val text = composer.text.toString().trim()
        if (text.isNotEmpty()) { callbacks.onSend(text); composer.text.clear() }
    }
    private fun script(value: String) {
        root.post {
            if (disposed) return@post
            if (loaded) web.evaluateJavascript(value, null) else scripts.add(value)
        }
    }
    // JSONObject encoding is data; the packaged asset sanitizes assistant Markdown.
    fun appendMessage(message: JSONObject) = script("PhoneConversation.append(${message});")
    fun renderHistory(messages: List<JSONObject>) = script("PhoneConversation.history(${JSONArray(messages)});")
    fun setStatus(text: String) = script("PhoneConversation.status(${JSONObject.quote(text)});")
    fun setActivity(activity: JSONObject) = script("PhoneConversation.activity(${activity});")
    fun setPresentation(presentation: AssistantPresentation?) = script(if (presentation == null)
        "PhoneConversation.presentation(null);" else "PhoneConversation.presentation(${JSONObject().put("title", presentation.title).put("markdown", presentation.markdown).put("kind", presentation.kind)});")
    fun setComposer(text: String) { root.post { if (!disposed) { composer.setText(text); composer.setSelection(composer.text.length) } } }
    fun showRetry(show: Boolean) { root.post { if (!disposed) retry.visibility = if (show) View.VISIBLE else View.GONE } }
    fun dispose() {
        disposed = true
        scripts.clear()
        web.stopLoading()
        web.removeJavascriptInterface("ConversationNative")
        root.removeView(web)
        web.destroy()
    }
    companion object { private const val ASSET = "file:///android_asset/conversation.html" }
}
