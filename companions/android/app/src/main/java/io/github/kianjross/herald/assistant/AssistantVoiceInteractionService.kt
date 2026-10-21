package io.github.kianjross.herald.assistant

import android.service.voice.VoiceInteractionService
import android.content.Intent
import android.util.Log

/** Android owns this service lifecycle; it starts no microphone or hotword worker. */
class AssistantVoiceInteractionService : VoiceInteractionService() {
    override fun onReady() {
        super.onReady()
        // Android restores this default-assistant binding after updates/restarts.
        // Reuse the existing connection and queue; availability never opens a mic.
        if (DeviceIdentity(this).snapshot() != null && AssistantConnectionService.connectionEnabled(this)) {
            SyncWorker.schedulePeriodicSync(this)
            SyncWorker.scheduleImmediateSync(this)
            runCatching { AssistantConnectionService.startVisible(this) }.onFailure {
                Log.w("AssistantConnection", "Default-assistant connection start unavailable: ${it.javaClass.simpleName}")
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if(intent?.action == "io.github.kianjross.herald.assistant.RESUME_CONVERSATION") showSession(null, 0)
        return START_NOT_STICKY
    }
}
