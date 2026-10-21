package io.github.kianjross.herald.assistant

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.service.voice.VoiceInteractionService

/** Promotes the existing OS-owned voice session only during user-activated voice. */
object VoiceSessionForeground {
    private const val CHANNEL = "ua_voice_session"
    private const val NOTIFICATION = 7104

    /** Bounded states ensure notification content describes the voice lifecycle only. */
    enum class State(val title: String, val detail: String) {
        STARTING("Assistant voice is starting", "Preparing microphone"),
        LISTENING("Assistant is listening", "Microphone is active"),
        PROCESSING("Assistant is processing", "Microphone input has ended"),
        SPEAKING("Assistant is speaking", "Microphone is paused"),
        IDLE("Assistant is ready", "Voice session is idle"),
        MUTED("Assistant voice is muted", "Microphone is off"),
        UNAVAILABLE("Assistant voice is unavailable", "Microphone could not be started"),
        ERROR("Assistant voice stopped", "Voice activation failed")
    }

    // Android 34 while-in-use exemption: system-started/current
    // VoiceInteractionService app; RECORD_AUDIO is still explicitly required.
    // https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start
    fun start(service: Service, state: State = State.STARTING) {
        check(service.checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            "Microphone permission is required for an active voice session"
        }
        check(VoiceInteractionService.isActiveService(service,
            ComponentName(service, AssistantVoiceInteractionService::class.java))) {
            "Select UltimateAssistant as Android's default assistant before background voice"
        }
        ensureChannel(service)
        val notification = buildNotification(service, state)
        if(Build.VERSION.SDK_INT >= 29) service.startForeground(NOTIFICATION, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        else service.startForeground(NOTIFICATION, notification)
    }

    fun updateStatus(service: Service, state: State) {
        val manager = service.getSystemService(NotificationManager::class.java) ?: return
        ensureChannel(service)
        manager.notify(NOTIFICATION, buildNotification(service, state))
    }

    /** Adapter for the existing controller callback; arbitrary text never enters the notification. */
    fun updateStatus(service: Service, status: String) {
        updateStatus(service, stateForControllerStatus(status))
    }

    fun showUnavailable(service: Service) = updateStatus(service, State.UNAVAILABLE)

    fun showError(service: Service) = updateStatus(service, State.ERROR)

    private fun ensureChannel(service: Service) {
        service.getSystemService(NotificationManager::class.java)?.createNotificationChannel(
            NotificationChannel(CHANNEL, "Active assistant conversation",
                NotificationManager.IMPORTANCE_LOW).apply {
                description = "Visible only while your user-activated assistant voice session remains active"
            }
        )
    }

    private fun buildNotification(service: Service, state: State): Notification {
        val intent = Intent(service, AssistantVoiceInteractionService::class.java).apply {
            action = "io.github.kianjross.herald.assistant.RESUME_CONVERSATION"
        }
        val open = PendingIntent.getService(service, NOTIFICATION, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val builder = Notification.Builder(service, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle(state.title)
            .setContentText(state.detail)
            .setContentIntent(open).setOngoing(true).setCategory(Notification.CATEGORY_SERVICE)
        if(Build.VERSION.SDK_INT >= 31) builder.setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE)
        return builder.build()
    }

    private fun stateForControllerStatus(status: String): State = when (status) {
        "Starting microphone..." -> State.STARTING
        "Already listening", "Listening...", "Listening in Android speech input" -> State.LISTENING
        "Transcribing...", "Voice input ready" -> State.PROCESSING
        "Preparing speech output..." -> State.SPEAKING
        // The controller also cancels recognition while a turn is processed or spoken.
        // Actual mute disables voice first, so its queued callback is intentionally ignored.
        "Voice input cancelled" -> State.PROCESSING
        else -> State.ERROR
    }

    fun stop(service: Service) {
        service.stopForeground(Service.STOP_FOREGROUND_REMOVE)
        // Android owns the bound VoiceInteractionSessionService lifecycle.
    }
}
