package io.github.kianjross.herald.assistant

import android.app.Activity
import android.app.role.RoleManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Build
import android.provider.Settings
import android.service.voice.VoiceInteractionService

/** OS assistant selection is separate from phone capability grants. */
object DefaultAssistant {
    const val REQUEST_ROLE = 7123
    fun status(context: Context): Map<String, Any?> {
        val role = if (Build.VERSION.SDK_INT >= 29) context.getSystemService(RoleManager::class.java) else null
        val assistant = runCatching { Settings.Secure.getString(context.contentResolver, "assistant") }.getOrNull()
        val interaction = runCatching { Settings.Secure.getString(context.contentResolver, "voice_interaction_service") }.getOrNull()
        val held = if (Build.VERSION.SDK_INT >= 29) runCatching { role?.isRoleHeld(RoleManager.ROLE_ASSISTANT) == true }.getOrDefault(false) else false
        val selected = ComponentName.unflattenFromString(assistant ?: "")?.packageName == context.packageName
        val active = runCatching { VoiceInteractionService.isActiveService(context,
            ComponentName(context, AssistantVoiceInteractionService::class.java)) }.getOrDefault(false)
        return mapOf("role_held" to held, "selected" to (held || selected || active),
            "assistant_component" to assistant, "voice_interaction_component" to interaction,
            "voice_service_active" to active, "observed_at_ms" to System.currentTimeMillis(),
            "always_on_hotword" to false)
    }
    fun choose(activity: Activity) {
        if (Build.VERSION.SDK_INT >= 29) {
            val manager = activity.getSystemService(RoleManager::class.java)
            if (runCatching { manager?.isRoleAvailable(RoleManager.ROLE_ASSISTANT) == true }.getOrDefault(false)) {
                val launched = runCatching {
                    activity.startActivityForResult(manager!!.createRequestRoleIntent(RoleManager.ROLE_ASSISTANT), REQUEST_ROLE)
                }.isSuccess
                if (launched) return
            }
        }
        openSettings(activity)
    }
    fun openSettings(activity: Activity) {
        val targets = listOf(Settings.ACTION_VOICE_INPUT_SETTINGS, Settings.ACTION_MANAGE_DEFAULT_APPS_SETTINGS, Settings.ACTION_SETTINGS)
        for (action in targets) if(runCatching { activity.startActivity(Intent(action)) }.isSuccess) return
    }
}
