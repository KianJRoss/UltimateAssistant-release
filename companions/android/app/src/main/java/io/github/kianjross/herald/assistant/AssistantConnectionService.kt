package io.github.kianjross.herald.assistant

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import org.json.JSONObject

/** Cross-device conversation continuity; not an always-listening microphone service.
 * Android remoteMessaging type: developer.android.com/develop/background-work/services/fgs/service-types
 * WorkManager retains its constrained periodic fallback if Android stops this service.
 */
class AssistantConnectionService : Service() {
    private lateinit var identity: DeviceIdentity
    private lateinit var bridge: BridgeClient
    private val handler = Handler(Looper.getMainLooper())
    private var streaming = false
    private var stopping = false
    private var lastRevision = 0L
    private var streamedConversation = ""
    private var overlay: CanvasOverlay? = null
    private val tick = object : Runnable {
        override fun run() {
            if (stopping) return
            if (identity.snapshot() == null || !connectionEnabled(this@AssistantConnectionService)) {
                stopSelf(); return
            }
            SyncWorker.scheduleImmediateSync(this@AssistantConnectionService)
            val currentConversation = getSharedPreferences("conversation_ui", MODE_PRIVATE).getString("conversation_id", "personal") ?: "personal"
            if (streaming && currentConversation != streamedConversation) {
                bridge.stopStreaming(); streaming = false; lastRevision = 0L
            }
            if (!streaming) startConversationStream()
            // The canvas floats over other apps once "Display over other apps" is allowed.
            if (overlay == null && CanvasOverlay.permitted(this@AssistantConnectionService))
                overlay = CanvasOverlay(this@AssistantConnectionService).also { it.start() }
            handler.postDelayed(this, 15_000L)
        }
    }

    override fun onCreate() {
        super.onCreate()
        identity = DeviceIdentity(this)
        bridge = BridgeClient(this, identity, getString(R.string.bridge_base_url))
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL, "Assistant conversation connection", NotificationManager.IMPORTANCE_LOW))
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == STOP) {
            setConnectionEnabled(this, false)
            stopSelf()
            return START_NOT_STICKY
        }
        if (identity.snapshot() == null || !connectionEnabled(this)) {
            stopSelf(); return START_NOT_STICKY
        }
        val notification = connectionNotification("Keeping your assistant conversation available across devices")
        if (Build.VERSION.SDK_INT >= 34)
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_REMOTE_MESSAGING)
        else startForeground(NOTIFICATION_ID, notification)
        SyncWorker.schedulePeriodicSync(this)
        handler.removeCallbacks(tick)
        handler.post(tick)
        return START_NOT_STICKY
    }

    private fun startConversationStream() {
        val prefs = getSharedPreferences("conversation_ui", MODE_PRIVATE)
        val conversation = prefs.getString("conversation_id", "personal") ?: "personal"
        streamedConversation = conversation
        lastRevision = maxOf(lastRevision, prefs.getLong("last_event_revision", 0))
        streaming = true
        bridge.streamEvents(conversation, lastRevision,
            onEvent = { revision, event -> handler.post {
                if (stopping || revision <= lastRevision) return@post
                lastRevision = revision
                val message = event.optJSONObject("message") ?: event
                if (message.optString("role") == "assistant") {
                    val content = message.optString("content")
                    if (content.isNotBlank()) getSystemService(NotificationManager::class.java)
                        .notify(NOTIFICATION_ID, connectionNotification(content))
                }
            } },
            onClosed = { unavailable -> handler.post {
                if (stopping) return@post
                streaming = false
                if (unavailable?.code == "credential_rejected") {
                    identity.forget()
                    stopSelf()
                } else if (unavailable != null) {
                    getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID,
                        connectionNotification("Connection unavailable; retained messages retry when connected"))
                }
            } })
    }

    private fun connectionNotification(text: String): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val stop = PendingIntent.getService(this, 1, Intent(this, AssistantConnectionService::class.java).setAction(STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle("UltimateAssistant conversation")
            .setContentText(text.take(240))
            .setStyle(Notification.BigTextStyle().bigText(text.take(2000)))
            .setContentIntent(open).setOngoing(true).setOnlyAlertOnce(true)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .addAction(Notification.Action.Builder(null, "Pause connection", stop).build())
            .build()
    }

    override fun onDestroy() {
        stopping = true
        overlay?.stop()
        overlay = null
        handler.removeCallbacksAndMessages(null)
        bridge.close()
        stopForeground(STOP_FOREGROUND_REMOVE)
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        private const val CHANNEL = "assistant_conversation_connection"
        private const val NOTIFICATION_ID = 4201
        private const val STOP = "io.github.kianjross.herald.assistant.PAUSE_CONNECTION"
        fun connectionEnabled(context: Context): Boolean = context.getSharedPreferences("assistant_connection", Context.MODE_PRIVATE)
            .getBoolean("enabled", true)
        fun setConnectionEnabled(context: Context, enabled: Boolean) {
            context.getSharedPreferences("assistant_connection", Context.MODE_PRIVATE).edit().putBoolean("enabled", enabled).apply()
        }
        /** Must be called by a visible Activity, never a boot receiver or arbitrary background job. */
        fun startVisible(context: Context) {
            if (connectionEnabled(context)) context.startForegroundService(Intent(context, AssistantConnectionService::class.java))
        }
        fun pause(context: Context) {
            setConnectionEnabled(context, false)
            context.stopService(Intent(context, AssistantConnectionService::class.java))
        }
    }
}
