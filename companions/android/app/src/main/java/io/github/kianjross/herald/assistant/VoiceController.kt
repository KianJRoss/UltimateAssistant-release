package io.github.kianjross.herald.assistant

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.MediaPlayer
import android.media.MediaRecorder
import android.os.Handler
import android.os.Looper
import java.io.File

/** User-activated microphone capture; recognition and voice use the shared custom providers. */
class VoiceController(
    private val context: Context,
    private val onSpeechRecognized: (String) -> Unit,
    private val onError: (String) -> Unit,
    private val onStatus: (String) -> Unit = {},
    private val onPartial: (String) -> Unit = {},
    private val launchRecognition: ((Intent) -> Boolean)? = null,
    private val onSpeechFinished: (cancelled: Boolean) -> Unit = {}
) {
    companion object {
        private const val TAG = "VoiceController"
        const val VOICE_REQUEST_CODE = 7110

        /**
         * Converts rich assistant content (markdown, formatting, code fences, URLs)
         * into clean, natural plain language suitable for speech output.
         */
        fun toSpeechFriendly(content: String): String {
            if (content.isBlank()) return ""
            var text = content

            // 1. Unwrap or strip fenced code blocks
            text = text.replace(Regex("```[a-zA-Z0-9_-]*\\n?([\\s\\S]*?)```"), "$1")

            // 2. Convert markdown image syntax ![alt](url) -> alt
            text = text.replace(Regex("!\\[([^\\]]*)\\]\\([^)]+\\)"), "$1")

            // 3. Convert markdown link syntax [text](url) -> text
            text = text.replace(Regex("\\[([^\\]]+)\\]\\([^)]+\\)"), "$1")

            // 4. Strip footnote and citation references [^1] or [1]
            text = text.replace(Regex("\\[\\^?\\d+\\]"), "")

            // 5. Simplify bare URLs (domain only for speech)
            text = text.replace(Regex("https?://([a-zA-Z0-9.-]+)(/[^\\s]*)?"), "$1")

            // 6. Remove HTML tags
            text = text.replace(Regex("<[^>]+>"), " ")

            // 7. Strip inline code backticks `code` -> code
            text = text.replace(Regex("`([^`]+)`"), "$1")

            // 8. Strip bold and italic markdown (**text**, *text*, __text__, _text_)
            text = text.replace(Regex("\\*\\*([^*]+)\\*\\*"), "$1")
            text = text.replace(Regex("\\*([^*]+)\\*"), "$1")
            text = text.replace(Regex("__([^_]+)__"), "$1")
            text = text.replace(Regex("\\b_([^_]+)_\\b"), "$1")

            // 9. Strip strikethrough ~~text~~ -> text
            text = text.replace(Regex("~~([^~]+)~~"), "$1")

            // 10. Strip headers (# Header -> Header)
            text = text.replace(Regex("(?m)^#{1,6}\\s+"), "")

            // 11. Strip blockquote markers (> text -> text)
            text = text.replace(Regex("(?m)^>\\s*"), "")

            // 12. Strip horizontal rules
            text = text.replace(Regex("(?m)^[-*_]{3,}\\s*$"), "")

            // 13. Strip list markers (* Item, - Item, + Item, • Item, 1. Item)
            text = text.replace(Regex("(?m)^\\s*[•\\-*+]\\s+"), "")
            text = text.replace(Regex("(?m)^\\s*\\d+\\.\\s+"), "")

            // 14. Strip table borders and formatting
            text = text.replace(Regex("(?m)^\\s*\\|[-:| ]+\\|\\s*$"), "")
            text = text.replace(Regex("(?m)^\\s*\\|"), "")
            text = text.replace(Regex("(?m)\\|\\s*$"), "")
            text = text.replace("|", ", ")

            // 15. Clean math / LaTeX delimiters ($x^2$ or $$x^2$$ -> x^2)
            text = text.replace(Regex("\\x24{1,2}([^\\x24]+)\\x24{1,2}"), "$1")

            // 16. Normalize multiple whitespace and linebreaks to single spaces
            text = text.replace(Regex("\\s+"), " ").trim()

            return text
        }
    }
    private val main = Handler(Looper.getMainLooper())
    private val bridge = BridgeClient(context.applicationContext, DeviceIdentity(context.applicationContext), context.getString(R.string.bridge_base_url))
    private var recorder: MediaRecorder? = null
    private var recordingFile: File? = null
    private var player: MediaPlayer? = null
    private var playbackFile: File? = null
    private var destroyed = false
    private var listening = false
    private var speaking = false
    private var generation = 0L
    private var speechGeneration = 0L
    private var beganAt = 0L
    private var lastSpeechAt = 0L
    private var heardSpeech = false
    private val meter = object : Runnable {
        override fun run() {
            if (!listening || destroyed) return
            val now = android.os.SystemClock.elapsedRealtime()
            val amplitude = runCatching { recorder?.maxAmplitude ?: 0 }.getOrDefault(0)
            if (amplitude > 900) { heardSpeech = true; lastSpeechAt = now }
            if ((heardSpeech && now - lastSpeechAt > 1300) || now - beganAt > 45000 || (!heardSpeech && now - beganAt > 10000)) {
                if (!heardSpeech) { finishRecording(false); onError("No speech recognized; try again") }
                else finishRecording(true)
                return
            }
            main.postDelayed(this, 100)
        }
    }
    private fun onMain(action: () -> Unit) {
        if (Looper.myLooper() == Looper.getMainLooper()) action() else main.post(action)
    }
    fun isSpeaking() = speaking
    fun isListening() = listening
    @Suppress("DEPRECATION")
    fun startListening(prompt: String? = null) = onMain {
        if (destroyed || listening || speaking) return@onMain
        if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            onError("Microphone permission is required"); return@onMain
        }
        generation++
        val file = File.createTempFile("assistant-mic-", ".m4a", context.cacheDir)
        val capture = MediaRecorder()
        try {
            capture.setAudioSource(MediaRecorder.AudioSource.VOICE_RECOGNITION)
            capture.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            capture.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            capture.setAudioSamplingRate(16000)
            capture.setAudioEncodingBitRate(64000)
            capture.setOutputFile(file.absolutePath)
            capture.prepare(); capture.start()
            recorder = capture; recordingFile = file; listening = true
            beganAt = android.os.SystemClock.elapsedRealtime(); lastSpeechAt = beganAt; heardSpeech = false
            onStatus("Listening..."); main.postDelayed(meter, 100)
        } catch (e: Exception) {
            capture.release(); file.delete(); onError("Microphone recording failed: ${e.message}")
        }
    }
    private fun finishRecording(submit: Boolean) {
        main.removeCallbacks(meter)
        val capture = recorder; recorder = null
        val file = recordingFile; recordingFile = null
        listening = false
        val recorded = runCatching { capture?.stop() }.isSuccess
        capture?.release()
        if (!submit || !recorded || file == null || file.length() == 0L) { file?.delete(); return }
        val attempt = generation
        val bytes = file.readBytes(); file.delete()
        onStatus("Transcribing...")
        bridge.transcribeAudio(bytes) { result -> onMain {
            if (destroyed || attempt != generation) return@onMain
            val text = result.body?.optString("text").orEmpty()
            if (text.isNotBlank()) { onStatus("Voice input ready"); onSpeechRecognized(text) }
            else onError(result.unavailable?.message ?: "No speech recognized; try again")
        } }
    }
    fun stopListening() = onMain { if (listening) finishRecording(true) }
    fun cancelListening() = onMain { generation++; finishRecording(false) }
    fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?): Boolean = false
    fun speak(text: String, queueMode: Int = 0): Boolean {
        if (text.isBlank() || destroyed) return false
        onMain {
            cancelListening(); stopSpeaking()
            val attempt = ++speechGeneration
            speaking = true; onStatus("Preparing speech output...")
            bridge.synthesizeSpeech(toSpeechFriendly(text)) { result -> onMain {
                if (destroyed || attempt != speechGeneration) return@onMain
                val bytes = result.audio
                if (bytes == null) {
                    speaking = false; onError(result.error ?: "Custom speech unavailable"); onSpeechFinished(true)
                    return@onMain
                }
                val file = File.createTempFile("assistant-voice-", ".audio", context.cacheDir)
                playbackFile = file; file.writeBytes(bytes)
                val playback = MediaPlayer(); player = playback
                try {
                    playback.setDataSource(file.absolutePath)
                    playback.setOnCompletionListener { completePlayback(attempt, false) }
                    playback.setOnErrorListener { _, _, _ -> completePlayback(attempt, true); true }
                    playback.setOnPreparedListener { if (attempt == speechGeneration && !destroyed) { onStatus("Preparing speech output..."); it.start() } }
                    playback.prepareAsync()
                } catch (e: Exception) { onError("Voice playback failed"); completePlayback(attempt, true) }
            } }
        }
        return true
    }
    private fun completePlayback(attempt: Long, cancelled: Boolean) {
        if (attempt != speechGeneration || destroyed) return
        stopSpeaking(); onSpeechFinished(cancelled)
    }
    fun stopSpeaking() = onMain {
        speechGeneration++; speaking = false
        player?.release(); player = null
        playbackFile?.delete(); playbackFile = null
    }
    fun destroy() = onMain {
        destroyed = true; cancelListening(); stopSpeaking(); bridge.close()
    }
}
