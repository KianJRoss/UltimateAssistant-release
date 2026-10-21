package io.github.kianjross.herald.assistant

import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract
import org.json.JSONObject
import java.io.File
import java.util.UUID

/** Conversation tools for files selected through Android's persisted folder grants. */
class PhoneFileAccess(private val context: Context) {
    private val resolver get() = context.contentResolver
    private val privateRoot get() = context.filesDir.canonicalFile

    fun roots(): Map<String, Any?> = mapOf("roots" to buildList {
        add(mapOf("uri" to Uri.fromFile(privateRoot).toString(), "name" to "Assistant files", "directory" to true))
        resolver.persistedUriPermissions.filter { it.isReadPermission && DocumentsContract.isTreeUri(it.uri) }.forEach {
            add(mapOf("uri" to it.uri.toString(), "name" to DocumentsContract.getTreeDocumentId(it.uri), "directory" to true))
        }
    })

    /** Resolve only existing grants; URI text prefixes are never authority. */
    private fun authorized(raw: String): Uri {
        val uri = Uri.parse(raw)
        if (uri.scheme == "file") {
            val file = File(requireNotNull(uri.path)).canonicalFile
            require(file == privateRoot || file.path.startsWith(privateRoot.path + File.separator)) { "File is outside assistant storage" }
            return Uri.fromFile(file)
        }
        require(uri.scheme == "content") { "Use a granted folder or assistant file URI" }
        for (grant in resolver.persistedUriPermissions.filter { it.isReadPermission }) {
            if (uri == grant.uri && !DocumentsContract.isTreeUri(grant.uri)) return uri
            if (!DocumentsContract.isTreeUri(grant.uri) || uri.authority != grant.uri.authority) continue
            val rootId = DocumentsContract.getTreeDocumentId(grant.uri)
            val root = DocumentsContract.buildDocumentUriUsingTree(grant.uri, rootId)
            val target = if (uri == grant.uri) root else {
                val targetId = try { DocumentsContract.getDocumentId(uri) } catch (_: Exception) { continue }
                DocumentsContract.buildDocumentUriUsingTree(grant.uri, targetId)
            }
            if (target == root || isInside(root, target, rootId)) return target
        }
        error("File requires an Android picker grant")
    }

    /** Whether target is inside the granted folder; Android 10+ asks the provider, older versions
     *  compare document ids (providers name children "<folder id>/<path>"). */
    private fun isInside(root: Uri, target: Uri, rootId: String): Boolean =
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.Q) DocumentsContract.isChildDocument(resolver, root, target)
        else try { DocumentsContract.getDocumentId(target).startsWith(rootId.trimEnd('/') + "/") } catch (_: Exception) { false }

    fun list(raw: String, limit: Int = 100): Map<String, Any?> {
        val uri = authorized(raw)
        val bounded = limit.coerceIn(1, 200)
        val items = mutableListOf<Map<String, Any?>>()
        if (uri.scheme == "file") {
            val directory = File(requireNotNull(uri.path))
            require(directory.isDirectory) { "URI is not a folder" }
            directory.listFiles()?.sortedBy { it.name.lowercase() }?.take(bounded)?.forEach {
                val canonical = it.canonicalFile
                if (canonical.path.startsWith(privateRoot.path + File.separator)) items.add(mapOf("uri" to Uri.fromFile(canonical).toString(), "name" to it.name, "directory" to it.isDirectory, "size" to it.length(), "modified" to it.lastModified()))
            }
        } else {
            val children = DocumentsContract.buildChildDocumentsUriUsingTree(uri, DocumentsContract.getDocumentId(uri))
            resolver.query(children, arrayOf(DocumentsContract.Document.COLUMN_DOCUMENT_ID, DocumentsContract.Document.COLUMN_DISPLAY_NAME, DocumentsContract.Document.COLUMN_MIME_TYPE, DocumentsContract.Document.COLUMN_SIZE, DocumentsContract.Document.COLUMN_LAST_MODIFIED), null, null, null)?.use { cursor ->
                while (items.size < bounded && cursor.moveToNext()) {
                    val mime = cursor.getString(2)
                    items.add(mapOf("uri" to DocumentsContract.buildDocumentUriUsingTree(uri, cursor.getString(0)).toString(), "name" to cursor.getString(1), "media_type" to mime, "directory" to (mime == DocumentsContract.Document.MIME_TYPE_DIR), "size" to if (cursor.isNull(3)) null else cursor.getLong(3), "modified" to if (cursor.isNull(4)) null else cursor.getLong(4)))
                }
            }
        }
        return mapOf("uri" to uri.toString(), "items" to items, "limit" to bounded, "may_have_more" to (items.size == bounded))
    }

    private fun input(uri: Uri) = if (uri.scheme == "file") File(requireNotNull(uri.path)).inputStream() else resolver.openInputStream(uri) ?: error("Cannot read document")

    fun read(raw: String, maxBytes: Int = 65536): Map<String, Any?> {
        val uri = authorized(raw)
        val bound = maxBytes.coerceIn(1, 256 * 1024)
        val bytes = input(uri).use { source ->
            val buffer = ByteArray(bound + 1)
            var count = 0
            while (count < buffer.size) {
                val read = source.read(buffer, count, buffer.size - count)
                if (read < 0) break
                count += read
            }
            buffer.copyOf(count)
        }
        require(bytes.take(minOf(bytes.size, bound)).none { it == 0.toByte() }) { "Binary file: upload it for analysis instead" }
        return mapOf("uri" to uri.toString(), "text" to bytes.copyOf(minOf(bytes.size, bound)).toString(Charsets.UTF_8), "truncated" to (bytes.size > bound), "encoding" to "UTF-8")
    }

    /** Retain bytes in the existing share upload spool; caller enqueues its conversation capture. */
    fun capture(raw: String, fileId: String = UUID.randomUUID().toString()): JSONObject {
        require(fileId.matches(Regex("[A-Za-z0-9_-]{1,120}"))) { "Invalid capture identity" }
        val uri = authorized(raw)
        var name = if (uri.scheme == "file") File(requireNotNull(uri.path)).name else "document"
        if (uri.scheme == "content") resolver.query(uri, arrayOf(DocumentsContract.Document.COLUMN_DISPLAY_NAME), null, null, null)?.use { if (it.moveToFirst()) name = it.getString(0) ?: name }
        val folder = File(privateRoot, "shared-captures").apply { mkdirs() }
        val destination = File(folder, fileId)
        val partial = File(folder, "$fileId.partial")
        try {
            input(uri).use { source -> partial.outputStream().use { sink ->
                val buffer = ByteArray(64 * 1024)
                var total = 0L
                while (true) {
                    val count = source.read(buffer)
                    if (count < 0) break
                    total += count
                    require(total <= 64L * 1024 * 1024) { "File exceeds 64 MB" }
                    sink.write(buffer, 0, count)
                }
            } }
            check(partial.renameTo(destination)) { "Cannot retain document bytes" }
        } catch (failure: Exception) { partial.delete(); throw failure }
        return JSONObject().put("file_id", fileId).put("uri", uri.toString()).put("name", name.take(240)).put("media_type", resolver.getType(uri) ?: "application/octet-stream").put("local_path", destination.absolutePath).put("size", destination.length())
    }
}
