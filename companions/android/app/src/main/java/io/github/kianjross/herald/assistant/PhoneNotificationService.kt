package io.github.kianjross.herald.assistant

import android.app.Notification
import android.app.PendingIntent
import android.os.Build
import android.os.Looper
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import java.util.concurrent.ConcurrentHashMap

/**
 * PhoneNotificationService observes system notifications and allows explicit execution
 * of allowlisted notification actions (e.g. quick reply, mark as read, dismiss)
 * using the official Android NotificationListenerService patterns (similar to KDE Connect).
 *
 * It maintains observed notifications and provides evidence for grant status.
 */
class PhoneNotificationService : NotificationListenerService() {

    data class NotificationItem(
        val key: String,
        val packageName: String,
        val title: String,
        val text: String,
        val postTime: Long,
        val isOngoing: Boolean,
        val actions: List<NotificationActionItem>
    ) {
        fun toMap(): Map<String, Any?> = mapOf(
            "key" to key,
            "package_name" to packageName,
            "title" to title,
            "text" to text,
            "post_time" to postTime,
            "is_ongoing" to isOngoing,
            "actions" to actions.map { it.toMap() }
        )
    }

    data class NotificationActionItem(
        val title: String,
        val actionIndex: Int
    ) {
        fun toMap(): Map<String, Any?> = mapOf(
            "title" to title,
            "action_index" to actionIndex
        )
    }

    companion object {
        @Volatile
        var instance: PhoneNotificationService? = null
            private set

        val isAvailable: Boolean
            get() = instance != null

        // Cache of active notifications by key
        private val activeNotificationCache = ConcurrentHashMap<String, StatusBarNotification>()
    }

    override fun onListenerConnected() {
        super.onListenerConnected()
        instance = this
        syncActiveNotifications()
    }

    override fun onListenerDisconnected() {
        if (instance == this) {
            instance = null
        }
        activeNotificationCache.clear()
        super.onListenerDisconnected()
    }

    override fun onDestroy() {
        if (instance == this) {
            instance = null
        }
        activeNotificationCache.clear()
        super.onDestroy()
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        if (sbn != null) {
            activeNotificationCache[sbn.key] = sbn
        }
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification?) {
        if (sbn != null) {
            activeNotificationCache.remove(sbn.key)
        }
    }

    private fun syncActiveNotifications() {
        try {
            val sbns = activeNotifications ?: return
            activeNotificationCache.clear()
            for (sbn in sbns) {
                activeNotificationCache[sbn.key] = sbn
            }
        } catch (_: Throwable) {
        }
    }

    /**
     * Return currently observed notifications as structured items.
     */
    fun getObservedNotifications(): List<NotificationItem> {
        return activeNotificationCache.values.mapNotNull { extractNotificationItem(it) }
    }

    /**
     * Execute a specific notification action by notification key and action title.
     */
    fun executeNotificationAction(key: String, actionTitle: String): Boolean {
        if (Looper.myLooper() != Looper.getMainLooper() || instance !== this) return false
        val sbn = activeNotificationCache[key] ?: return false
        val notification = sbn.notification ?: return false
        val actions = notification.actions ?: return false

        for (action in actions) {
            val title = action.title?.toString() ?: continue
            if (title.equals(actionTitle, ignoreCase = true)) {
                val pendingIntent = action.actionIntent ?: return false
                try {
                    pendingIntent.send()
                    return true
                } catch (e: PendingIntent.CanceledException) {
                    return false
                }
            }
        }
        return false
    }

    /**
     * Dismiss a notification by key.
     */
    fun dismissNotification(key: String): Boolean {
        if (Looper.myLooper() != Looper.getMainLooper() || instance !== this) return false
        return try {
            cancelNotification(key)
            activeNotificationCache.remove(key)
            true
        } catch (_: Throwable) {
            false
        }
    }

    private fun extractNotificationItem(sbn: StatusBarNotification): NotificationItem? {
        val notification = sbn.notification ?: return null
        val extras = notification.extras
        val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString() ?: ""
        val text = extras.getCharSequence(Notification.EXTRA_TEXT)?.toString() ?: ""

        val actionList = mutableListOf<NotificationActionItem>()
        notification.actions?.forEachIndexed { index, action ->
            val actTitle = action.title?.toString() ?: ""
            if (actTitle.isNotBlank()) {
                actionList.add(NotificationActionItem(title = actTitle, actionIndex = index))
            }
        }

        return NotificationItem(
            key = sbn.key,
            packageName = sbn.packageName,
            title = title,
            text = text,
            postTime = sbn.postTime,
            isOngoing = sbn.isOngoing,
            actions = actionList
        )
    }
}
