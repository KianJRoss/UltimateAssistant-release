package io.github.kianjross.herald.assistant

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import rikka.shizuku.Shizuku

/** Platform permission dialogs and app settings, never inferred or fake grants. */
class PhonePermissionController(
    private val activity: Activity,
    private val onStatus: (String) -> Unit,
    private val onGrantsChanged: () -> Unit
) {
    companion object { const val MICROPHONE_REQUEST = 7101; const val NOTIFICATIONS_REQUEST = 7102; const val SHIZUKU_REQUEST = 7103 }
    private val prefs = activity.getSharedPreferences("phone_permissions", Activity.MODE_PRIVATE)
    private var voiceContinuation: (() -> Unit)? = null
    private var lastGrants: Map<String, Boolean>? = null
    private val binderReceived = Shizuku.OnBinderReceivedListener {
        activity.runOnUiThread { onResume(); requestPrivileged(automatic = true) }
    }
    private val binderDead = Shizuku.OnBinderDeadListener {
        activity.runOnUiThread { onStatus("Shizuku disconnected; privileged commands unavailable"); onResume() }
    }
    private val privilegedPermission = Shizuku.OnRequestPermissionResultListener { code, result ->
        if(code == SHIZUKU_REQUEST) activity.runOnUiThread {
            onStatus(if(result == PackageManager.PERMISSION_GRANTED) "Shizuku access enabled" else "Shizuku permission denied; regular phone features remain available")
            onResume()
        }
    }
    fun observePrivileged() {
        Shizuku.addRequestPermissionResultListener(privilegedPermission)
        Shizuku.addBinderDeadListener(binderDead)
        Shizuku.addBinderReceivedListenerSticky(binderReceived)
    }
    fun close() {
        Shizuku.removeRequestPermissionResultListener(privilegedPermission)
        Shizuku.removeBinderDeadListener(binderDead)
        Shizuku.removeBinderReceivedListener(binderReceived)
    }
    fun requestPrivileged(automatic: Boolean = false) {
        val status = PrivilegedControl.status()
        if(status["available"] == true) { onStatus("Shizuku access enabled (${status["scope"]})"); onResume(); return }
        if(status["binder_alive"] != true) { if(!automatic) onStatus("Start Shizuku, then enable its assistant permission here"); return }
        if(automatic && prefs.getBoolean("shizuku_requested", false)) return
        runCatching {
            if(Shizuku.isPreV11()) { onStatus("Update Shizuku to enable assistant access"); return }
            if(Shizuku.shouldShowRequestPermissionRationale()) {
                if(!automatic) onStatus("Enable UltimateAssistant in Shizuku's authorized applications")
                return
            }
            prefs.edit().putBoolean("shizuku_requested", true).apply()
            Shizuku.requestPermission(SHIZUKU_REQUEST)
        }.onFailure { onStatus("Shizuku permission request unavailable: ${it.message}") }
    }

    fun ensureNotificationPermission() {
        if (Build.VERSION.SDK_INT < 33 || granted(Manifest.permission.POST_NOTIFICATIONS)) return
        if (prefs.getBoolean("notification_requested", false)) return
        prefs.edit().putBoolean("notification_requested", true).apply()
        activity.requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), NOTIFICATIONS_REQUEST)
    }

    fun requestVoice(onGranted: () -> Unit) {
        if (granted(Manifest.permission.RECORD_AUDIO)) { onGranted(); return }
        voiceContinuation = onGranted
        if (prefs.getBoolean("microphone_requested", false) &&
            !activity.shouldShowRequestPermissionRationale(Manifest.permission.RECORD_AUDIO)) {
            voiceContinuation = null
            explainDenied("Microphone", "Android has blocked microphone prompts. Enable microphone access in this app's permissions to use voice input.")
            return
        }
        prefs.edit().putBoolean("microphone_requested", true).apply()
        activity.requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), MICROPHONE_REQUEST)
    }

    fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, results: IntArray): Boolean {
        if (requestCode != MICROPHONE_REQUEST && requestCode != NOTIFICATIONS_REQUEST) return false
        val allowed = results.isNotEmpty() && results[0] == PackageManager.PERMISSION_GRANTED
        if (requestCode == MICROPHONE_REQUEST) {
            val continuation = voiceContinuation; voiceContinuation = null
            if (allowed) continuation?.invoke()
            else {
                onStatus("Microphone permission denied; typing still works")
                explainDenied("Microphone", "Microphone access is needed only when you choose voice input. You can enable it in app permissions.")
            }
        } else if (!allowed) onStatus("Notifications disabled; enable them in app settings for background connection notices")
        onResume()
        return true
    }

    fun onResume() {
        val observed = DeviceIdentity(activity).observeCapabilityGrants()
        if (lastGrants != observed) { lastGrants = observed; onGrantsChanged() }
    }

    fun openSettings() {
        val observed = DeviceIdentity(activity).observeCapabilityGrants()
        val labels = arrayOf(
            "App permissions (microphone: ${if (observed["microphone"] == true) "enabled" else "disabled"})",
            "Notifications: ${if (observed["notifications"] == true) "enabled" else "disabled"}",
            "Notification access: ${if (observed["notification_access"] == true) "enabled" else "disabled"}",
            "Accessibility control: ${if (observed["accessibility"] == true) "enabled" else "disabled"}",
            "Display over other apps (the canvas): ${if (CanvasOverlay.permitted(activity)) "enabled" else "disabled"}"
        )
        AlertDialog.Builder(activity).setTitle("Assistant permissions").setItems(labels) { _, choice ->
            when (choice) {
                0 -> appSettings()
                1 -> activity.startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                    .putExtra(Settings.EXTRA_APP_PACKAGE, activity.packageName))
                2 -> specialSettings("Notification access", "Allow the assistant to observe notifications and use their available actions.", Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)
                3 -> specialSettings("Accessibility control", "Allow the assistant to inspect and interact with visible apps. Android requires you to enable the assistant service here.", Settings.ACTION_ACCESSIBILITY_SETTINGS)
                4 -> activity.startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                    android.net.Uri.parse("package:" + activity.packageName)))
            }
        }.setNegativeButton("Close", null).show()
    }
    private fun granted(permission: String) = activity.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED
    private fun appSettings() = activity.startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
        Uri.parse("package:${activity.packageName}")))
    private fun explainDenied(title: String, message: String) {
        AlertDialog.Builder(activity).setTitle(title).setMessage(message)
            .setPositiveButton("App settings") { _, _ -> appSettings() }.setNegativeButton("Keep typing", null).show()
    }
    private fun specialSettings(title: String, message: String, action: String) {
        AlertDialog.Builder(activity).setTitle(title).setMessage(message)
            .setPositiveButton("Open Android settings") { _, _ -> activity.startActivity(Intent(action)) }
            .setNegativeButton("Cancel", null).show()
    }
}
