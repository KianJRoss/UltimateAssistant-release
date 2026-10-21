package io.github.kianjross.herald.assistant

import android.accessibilityservice.AccessibilityServiceInfo
import androidx.core.app.NotificationManagerCompat
import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.view.accessibility.AccessibilityManager
import org.json.JSONObject
import java.security.KeyStore
import java.util.UUID
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import android.util.Base64

/** Device-scoped phone.v1 identity. No administrator credential is accepted or stored here. */
class DeviceIdentity(private val context: Context) {
    data class Snapshot(
        val deviceId: String,
        val credential: String,
        val pairingRevision: Long,
        val grants: Map<String, Boolean>,
        val evidenceId: String
    )

    private val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    @Synchronized
    fun installPairing(credential: String, pairingRevision: Long, deviceId: String = DEVICE_ID) {
        require(credential.isNotBlank()) { "device credential is required" }
        require(pairingRevision > 0) { "pairing revision must be positive" }
        val endpoint = prefs.getString("backend_url", null)
        forget()
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, secretKey())
        val encrypted = cipher.doFinal(credential.toByteArray(Charsets.UTF_8))
        prefs.edit()
            .putString(CREDENTIAL, Base64.encodeToString(encrypted, Base64.NO_WRAP))
            .putString(CREDENTIAL_IV, Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
            .putLong(PAIRING_REVISION, pairingRevision)
            .putBoolean("auto_pairing_allowed", true)
            .putString("device_id", deviceId)
            .putString("backend_url", endpoint)
            .apply()
    }

    fun snapshot(): Snapshot? {
        val revision = prefs.getLong(PAIRING_REVISION, 0)
        val credential = decryptCredential() ?: return null
        if (revision <= 0) return null
        val grants = observeCapabilityGrants()
        val evidence = UUID.randomUUID().toString()
        prefs.edit().putString(EVIDENCE_ID, evidence).putString(GRANTS, JSONObject(grants).toString()).apply()
        return Snapshot(prefs.getString("device_id", DEVICE_ID) ?: DEVICE_ID, credential, revision, grants, evidence)
    }

    @Synchronized
    fun installationId(): String {
        prefs.getString("installation_id", null)?.let { return it }
        val id = UUID.randomUUID().toString()
        prefs.edit().putString("installation_id", id).commit()
        return id
    }

    fun autoPairingAllowed(): Boolean = prefs.getBoolean("auto_pairing_allowed", true)
    fun allowAutoPairing() { prefs.edit().putBoolean("auto_pairing_allowed", true).commit() }

    fun backendUrl(fallback: String): String = prefs.getString("backend_url", null) ?: fallback

    fun setBackendUrl(value: String) {
        val uri = java.net.URI(value.trim())
        require(uri.scheme.equals("https", ignoreCase = true) && !uri.host.isNullOrBlank() &&
            uri.rawUserInfo == null && uri.rawQuery == null && uri.rawFragment == null) {
            "Use an HTTPS backend URL without credentials, query or fragment"
        }
        prefs.edit().putString("backend_url", value.trim().trimEnd('/')).commit()
    }

    fun pairingRevision(): Long = prefs.getLong(PAIRING_REVISION, 0)

    /** Removes the credential and all evidence immediately; stale outbox entries are fenced by revision. */
    @Synchronized
    fun forget() {
        val endpoint = prefs.getString("backend_url", null)
        val installation = installationId()
        prefs.edit().clear().putString("backend_url", endpoint)
            .putString("installation_id", installation).putBoolean("auto_pairing_allowed", false).commit()
        runCatching {
            KeyStore.getInstance("AndroidKeyStore").apply { load(null) }.deleteEntry(KEY_ALIAS)
        }
    }

    fun observeCapabilityGrants(): Map<String, Boolean> {
        val notificationAccess = NotificationManagerCompat.getEnabledListenerPackages(context)
            .contains(context.packageName)
        val accessibilityManager = context.getSystemService(AccessibilityManager::class.java)
        val accessibility = accessibilityManager
            ?.getEnabledAccessibilityServiceList(AccessibilityServiceInfo.FEEDBACK_ALL_MASK)
            ?.any { it.resolveInfo.serviceInfo.packageName == context.packageName } == true
        // App interaction is accessibility-backed; overlay permission is unrelated.
        val appControl = accessibility
        return linkedMapOf(
            "notification_access" to notificationAccess,
            "microphone" to (context.checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) == android.content.pm.PackageManager.PERMISSION_GRANTED),
            "notifications" to NotificationManagerCompat.from(context).areNotificationsEnabled(),
            "accessibility" to accessibility,
            "android_app_control" to appControl,
            "privileged_control" to (PrivilegedControl.status()["available"] == true),
            // Canvas cards draw over other apps; report it so the assistant needn't guess.
            "overlay" to android.provider.Settings.canDrawOverlays(context)
        )
    }

    fun grantEvidenceEnvelope(): JSONObject? = snapshot()?.let {
        JSONObject()
            .put("pairing_revision", it.pairingRevision)
            .put("evidence_id", it.evidenceId)
            .put("grants", JSONObject(it.grants))
    }

    /** Evidence from this running installation, not from a release/build advertisement. */
    @Suppress("DEPRECATION")
    fun executionEvidence(): Map<String, Any?> {
        val observedAt = System.currentTimeMillis()
        val grants = observeCapabilityGrants()
        val installed = runCatching {
            val info = context.packageManager.getPackageInfo(context.packageName, 0)
            mapOf("package_name" to context.packageName,
                "version_name" to info.versionName,
                "version_code" to if (android.os.Build.VERSION.SDK_INT >= 28) info.longVersionCode else info.versionCode.toLong(),
                "last_update_time_ms" to info.lastUpdateTime,
                "source_revision" to null)
        }.getOrElse { mapOf("state" to "unavailable", "error" to it.javaClass.simpleName) }
        return mapOf("device_id" to (prefs.getString("device_id", DEVICE_ID) ?: DEVICE_ID),
            "pairing_revision" to pairingRevision(), "evidence_id" to UUID.randomUUID().toString(),
            "observed_at_ms" to observedAt,
            "age_ms" to (System.currentTimeMillis() - observedAt).coerceAtLeast(0),
            "grants" to grants, "accessibility_service_connected" to PhoneAccessibilityService.isAvailable,
            "installed_build" to installed, "privileged_control" to PrivilegedControl.status(),
            "default_assistant" to DefaultAssistant.status(context),
            "announcement_capability" to mapOf(
                "protocol" to "phone.v1", "command" to "phone.announcement",
                "delivery" to "at_most_once", "starts_recognition" to false,
                "max_speakable_characters" to 2000))
    }

    private fun decryptCredential(): String? {
        return try {
        val encrypted = prefs.getString(CREDENTIAL, null) ?: return null
        val iv = prefs.getString(CREDENTIAL_IV, null) ?: return null
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(
            Cipher.DECRYPT_MODE,
            secretKey(),
            GCMParameterSpec(128, Base64.decode(iv, Base64.NO_WRAP))
        )
        String(cipher.doFinal(Base64.decode(encrypted, Base64.NO_WRAP)), Charsets.UTF_8)
    } catch (_: Exception) {
        null
    }
    }

    private fun secretKey(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(
                KeyGenParameterSpec.Builder(
                    KEY_ALIAS,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
                ).setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .build()
            )
            generateKey()
        }
    }

    companion object {
        /** The id before pairing; the assistant assigns the real one when the phone pairs. */
        const val DEVICE_ID = "phone"

        /** This phone's id as the assistant knows it (from pairing). */
        fun current(context: Context): String =
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString("device_id", DEVICE_ID) ?: DEVICE_ID
        private const val PREFS = "phone_device_identity"
        private const val CREDENTIAL = "credential_ciphertext"
        private const val CREDENTIAL_IV = "credential_iv"
        private const val PAIRING_REVISION = "pairing_revision"
        private const val EVIDENCE_ID = "grant_evidence_id"
        private const val GRANTS = "observed_grants"
        private const val KEY_ALIAS = "ultimate_assistant_phone_device_credential"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
    }
}
