package io.github.kianjross.herald.assistant

import android.content.Context
import android.graphics.BitmapFactory
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.ImageView
import android.widget.TextView
import java.io.ByteArrayOutputStream
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.Future

/** Loads only authenticated, app-delivered content URIs. There is deliberately no URL loader. */
class AssistantAssetRenderer(context: Context) {
    private val appContext = context.applicationContext
    private val executor = Executors.newFixedThreadPool(2)
    private val main = Handler(Looper.getMainLooper())
    private val loads = ConcurrentHashMap<String, Future<*>>()
    private val generations = ConcurrentHashMap<String, Long>()

    fun render(elementId: String, asset: AssistantPresentation.Asset, image: ImageView, status: TextView? = null) {
        cancel(elementId)
        val generation = (generations[elementId] ?: 0L) + 1L
        generations[elementId] = generation
        status?.text = asset.alt ?: asset.identity
        image.setImageDrawable(null)
        val uri = asset.uri
        if (uri == null) {
            val prev = status?.text ?: (asset.alt ?: asset.identity)
            status?.text = "$prev — authenticated bytes unavailable"
            return
        }
        loads[elementId] = executor.submit {
            val result = runCatching {
                require(asset.identity.isNotBlank() && asset.revision > 0) { "invalid asset authority identity" }
                require(asset.kind in setOf("source", "artifact")) { "unsupported asset kind" }
                require(asset.authRevision == null || asset.authRevision > 0) { "invalid asset auth revision" }
                require(uri.scheme == "content") { "unsupported asset transport" }
                require(!uri.authority.isNullOrBlank()) { "invalid asset uri authority" }
                require(asset.mediaType?.startsWith("image/") == true) { "unsupported asset media type" }
                val expectedDigest = requireNotNull(asset.sha256) { "asset digest unavailable" }
                require(expectedDigest.matches(Regex("^[0-9a-f]{64}$"))) { "invalid asset digest format" }
                val bytes = appContext.contentResolver.openInputStream(uri).use { input ->
                    requireNotNull(input) { "asset authorization unavailable" }
                    val output = ByteArrayOutputStream()
                    val buffer = ByteArray(64 * 1024)
                    var total = 0
                    while (true) {
                        if (Thread.currentThread().isInterrupted) throw InterruptedException()
                        val count = input.read(buffer)
                        if (count < 0) break
                        total += count
                        require(total <= MAX_IMAGE_BYTES) { "asset exceeds rendering limit" }
                        output.write(buffer, 0, count)
                    }
                    output.toByteArray()
                }
                val actualDigest = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
                require(actualDigest.equals(expectedDigest, ignoreCase = true)) { "asset digest mismatch" }
                val options = BitmapFactory.Options().apply { inJustDecodeBounds = true }
                BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options)
                require(options.outWidth in 1..MAX_IMAGE_EDGE && options.outHeight in 1..MAX_IMAGE_EDGE) { "invalid image dimensions" }
                require(options.outWidth.toLong() * options.outHeight <= MAX_IMAGE_PIXELS) { "image exceeds pixel limit" }
                BitmapFactory.decodeByteArray(bytes, 0, bytes.size) ?: error("unsupported image bytes")
            }
            main.post {
                if (generations[elementId] == generation) {
                    result.onSuccess { image.setImageBitmap(it); status?.text = asset.alt ?: "" }
                        .onFailure {
                            if (it !is InterruptedException) {
                                status?.visibility = View.VISIBLE
                                status?.text = "${asset.alt ?: asset.identity} — unavailable or authorization expired"
                            }
                        }
                    loads.remove(elementId)
                }
            }
        }
    }

    fun cancel(elementId: String) {
        generations.compute(elementId) { _, old -> (old ?: 0L) + 1L }
        loads.remove(elementId)?.cancel(true)
    }
    fun close() { loads.values.forEach { it.cancel(true) }; loads.clear(); generations.clear(); executor.shutdownNow() }

    companion object {
        private const val MAX_IMAGE_BYTES = 12 * 1024 * 1024
        private const val MAX_IMAGE_EDGE = 8192
        private const val MAX_IMAGE_PIXELS = 32L * 1024 * 1024
    }
}
