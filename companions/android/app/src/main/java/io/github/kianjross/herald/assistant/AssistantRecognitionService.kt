package io.github.kianjross.herald.assistant

import android.content.ComponentName
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.speech.RecognitionListener
import android.speech.RecognitionService
import android.speech.SpeechRecognizer
import android.util.Log

/** Reuses an installed Android speech engine; no recognition engine or background mic is added. */
class AssistantRecognitionService : RecognitionService() {
    private var recognizer: SpeechRecognizer? = null
    private var provider: ComponentName? = null
    private var sessionId = 0L
    private val handler = Handler(Looper.getMainLooper())
    private var startupTimeout: Runnable? = null

    override fun onStartListening(intent: Intent, callback: Callback) {
        releaseRecognizer("replace")
        val selectedProvider = resolveProvider()
        if (selectedProvider == null) {
            Log.e(TAG, "stage=provider_unavailable error=${SpeechRecognizer.ERROR_CLIENT}")
            callback.error(SpeechRecognizer.ERROR_CLIENT)
            return
        }

        val currentSession = ++sessionId
        provider = selectedProvider
        Log.i(TAG, "stage=create provider=${selectedProvider.flattenToShortString()}")

        val created = try {
            SpeechRecognizer.createSpeechRecognizer(this, selectedProvider)
        } catch (_: RuntimeException) {
            reportStartupFailure(callback, selectedProvider, "create")
            return
        }
        recognizer = created

        try {
            created.setRecognitionListener(object : RecognitionListener {
                private fun isCurrent(): Boolean =
                    currentSession == sessionId && recognizer === created

                private fun noteCallback(): Boolean {
                    if (!isCurrent()) return false
                    clearStartupTimeout()
                    return true
                }

                override fun onReadyForSpeech(params: Bundle?) {
                    if (noteCallback()) callback.readyForSpeech(params)
                }

                override fun onBeginningOfSpeech() {
                    if (noteCallback()) callback.beginningOfSpeech()
                }

                override fun onRmsChanged(rmsdB: Float) {
                    if (noteCallback()) callback.rmsChanged(rmsdB)
                }

                override fun onBufferReceived(buffer: ByteArray?) {
                    if (noteCallback()) callback.bufferReceived(buffer)
                }

                override fun onEndOfSpeech() {
                    if (noteCallback()) callback.endOfSpeech()
                }

                override fun onError(error: Int) {
                    if (!noteCallback()) return
                    Log.w(TAG, "stage=callback_error error=$error provider=${selectedProvider.flattenToShortString()}")
                    releaseRecognizer("callback_error")
                    callback.error(error)
                }

                override fun onResults(results: Bundle?) {
                    if (!noteCallback()) return
                    releaseRecognizer("results")
                    callback.results(results)
                }

                override fun onPartialResults(results: Bundle?) {
                    if (noteCallback()) callback.partialResults(results)
                }

                override fun onEvent(eventType: Int, params: Bundle?) = Unit
            })
            Log.i(TAG, "stage=start provider=${selectedProvider.flattenToShortString()}")
            scheduleStartupTimeout(currentSession, created, callback, selectedProvider)
            created.startListening(intent)
        } catch (_: RuntimeException) {
            if (currentSession == sessionId) {
                reportStartupFailure(callback, selectedProvider, "start")
            }
        }
    }

    override fun onStopListening(callback: Callback) {
        val currentProvider = provider
        if (currentProvider != null) {
            Log.i(TAG, "stage=stop provider=${currentProvider.flattenToShortString()}")
        }
        try {
            recognizer?.stopListening()
        } catch (_: RuntimeException) {
            Log.e(
                TAG,
                "stage=stop error=${SpeechRecognizer.ERROR_CLIENT} provider=${currentProvider?.flattenToShortString()}"
            )
            releaseRecognizer("stop_failure")
            callback.error(SpeechRecognizer.ERROR_CLIENT)
        }
    }

    override fun onCancel(callback: Callback) {
        releaseRecognizer("cancel", cancelFirst = true)
    }

    override fun onDestroy() {
        releaseRecognizer("destroy", cancelFirst = true)
        super.onDestroy()
    }

    private fun resolveProvider(): ComponentName? {
        val installed = packageManager.queryIntentServices(Intent(SERVICE_INTERFACE), 0)
            .map { ComponentName(it.serviceInfo.packageName, it.serviceInfo.name) }
            .filter { it.packageName != packageName }
            .distinct()
            .sortedBy { it.flattenToString() }

        val selected = runCatching {
            Settings.Secure.getString(
                contentResolver,
                "voice_recognition_service"
            )?.let(ComponentName::unflattenFromString)
        }.getOrNull()

        if (selected != null && selected.packageName != packageName && selected in installed) {
            Log.i(TAG, "stage=provider_selected provider=${selected.flattenToShortString()}")
            return selected
        }

        val fallback = installed.firstOrNull()
        if (fallback != null) {
            Log.i(TAG, "stage=provider_fallback provider=${fallback.flattenToShortString()}")
        }
        return fallback
    }

    private fun reportStartupFailure(callback: Callback, failedProvider: ComponentName, stage: String) {
        Log.e(
            TAG,
            "stage=${stage}_failure error=${SpeechRecognizer.ERROR_CLIENT} " +
                "provider=${failedProvider.flattenToShortString()}"
        )
        releaseRecognizer("${stage}_failure")
        callback.error(SpeechRecognizer.ERROR_CLIENT)
    }

    private fun scheduleStartupTimeout(
        expectedSession: Long,
        expectedRecognizer: SpeechRecognizer,
        callback: Callback,
        selectedProvider: ComponentName
    ) {
        clearStartupTimeout()
        val timeout = Runnable {
            if (expectedSession != sessionId || recognizer !== expectedRecognizer) return@Runnable
            Log.e(
                TAG,
                "stage=start_timeout error=${SpeechRecognizer.ERROR_CLIENT} " +
                    "provider=${selectedProvider.flattenToShortString()}"
            )
            releaseRecognizer("start_timeout", cancelFirst = true)
            callback.error(SpeechRecognizer.ERROR_CLIENT)
        }
        startupTimeout = timeout
        handler.postDelayed(timeout, STARTUP_TIMEOUT_MS)
    }

    private fun clearStartupTimeout() {
        startupTimeout?.let(handler::removeCallbacks)
        startupTimeout = null
    }

    private fun releaseRecognizer(stage: String, cancelFirst: Boolean = false) {
        clearStartupTimeout()
        val oldRecognizer = recognizer
        val oldProvider = provider
        recognizer = null
        provider = null
        sessionId++
        if (oldRecognizer == null) return

        if (oldProvider != null) {
            Log.i(TAG, "stage=$stage provider=${oldProvider.flattenToShortString()}")
        }
        if (cancelFirst) runCatching { oldRecognizer.cancel() }
        runCatching { oldRecognizer.destroy() }
    }

    private companion object {
        const val TAG = "AssistantRecognition"
        const val STARTUP_TIMEOUT_MS = 10_000L
    }
}
