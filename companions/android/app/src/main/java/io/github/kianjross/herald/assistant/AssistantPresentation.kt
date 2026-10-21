package io.github.kianjross.herald.assistant

import android.net.Uri
import org.json.JSONArray
import org.json.JSONObject
import java.time.OffsetDateTime

/** Immutable projection of assistant.presentation v1. It never owns domain/job state. */
data class AssistantPresentation(
    val id: String, val revision: Long, val kind: String, val title: String,
    val markdown: String, val assets: List<Asset>, val expiresAtMillis: Long?,
    val revoked: Boolean, val conversationId: String?,
    val elements: List<Element> = emptyList(), val emptyCanvas: Boolean = false,
    val correlationId: String? = null
) {
    data class Bounds(
        val x: Double? = null, val y: Double? = null, val width: Double? = null,
        val height: Double? = null, val left: Double? = null, val top: Double? = null,
        val right: Double? = null, val bottom: Double? = null, val anchor: String? = null
    )
    data class Action(val id: String, val kind: String, val label: String)
    data class Asset(
        val kind: String, val identity: String, val revision: Long, val uri: Uri?,
        val mediaType: String?, val alt: String?, val authRevision: Long? = null,
        val sha256: String? = null
    )
    data class Element(
        val id: String, val operation: String, val revision: Long, val kind: String?,
        val zOrder: Int, val bounds: Bounds?, val interactionPolicy: String,
        val title: String?, val content: String?, val prompt: String?,
        val choices: List<Pair<String, String>>, val front: String?, val back: String?,
        val asset: Asset?, val actions: List<Action>, val expiresAtMillis: Long?,
        val anchor: CanvasGeometry.Anchor? = null,
        val drawing: CanvasGeometry.Drawing? = null,
        val alt: String? = null,
        /** For kind "card": the interactive canvas card this element shows (fetched by id). */
        val cardId: String? = null,
        val cardRevision: Long = 0,
        val draggable: Boolean = false
    )

    fun isAvailable(nowMillis: Long = System.currentTimeMillis()) =
        !revoked && (expiresAtMillis == null || nowMillis < expiresAtMillis)

    companion object {
        const val SCHEMA = "assistant.presentation"
        private const val LEGACY_SCHEMA = "assistant.presentation.v1"
        private val KINDS = setOf("card", "reminder", "learning", "artifact", "media")
        private val ELEMENT_KINDS = setOf("text", "markdown", "math", "image", "artifact", "question", "flashcard", "feedback", "annotation", "reminder", "drawing", "card")
        private val OPERATIONS = setOf("create", "update", "remove")
        private val POLICIES = setOf("touch_through", "interactive", "dismissible", "none")

        fun parse(envelope: JSONObject): AssistantPresentation = when (envelope.getString("schema")) {
            SCHEMA -> parseTyped(envelope)
            LEGACY_SCHEMA -> parseLegacy(envelope)
            else -> throw IllegalArgumentException("unsupported presentation schema")
        }

        private fun parseTyped(envelope: JSONObject): AssistantPresentation {
            require(envelope.getInt("version") == 1) { "unsupported presentation version" }
            val id = envelope.getString("id").trim()
            val revision = envelope.getLong("revision")
            require(id.isNotEmpty() && id.length <= 256 && revision > 0) { "invalid presentation identity" }
            val provenance = envelope.getJSONObject("provenance")
            val conversationId = provenance.getString("conversation_id").trim()
            val correlationId = provenance.getString("correlation_id").trim()
            require(conversationId.isNotEmpty() && correlationId.isNotEmpty()) { "missing presentation provenance" }
            val empty = envelope.optBoolean("empty_canvas", false)
            val elementsJson = envelope.optJSONArray("elements") ?: JSONArray()
            val blocks = envelope.optJSONArray("blocks") ?: JSONArray()
            require(elementsJson.length() <= 128 && blocks.length() <= 128) { "too many presentation elements" }
            require(empty || elementsJson.length() > 0 || blocks.length() > 0) { "empty presentation" }
            val elements = buildList {
                for (index in 0 until elementsJson.length()) add(parseElement(elementsJson.getJSONObject(index), index))
                if (elementsJson.length() == 0) for (index in 0 until blocks.length()) {
                    val block = JSONObject(blocks.getJSONObject(index).toString())
                    if (!block.has("id")) block.put("id", "block-${index + 1}")
                    block.put("operation", "create").put("revision", revision).put("z_order", index)
                    add(parseElement(block, index))
                }
            }
            if (empty) require(elements.all { it.operation == "remove" }) { "empty canvas contains visible elements" }
            val markdown = elements.filter { it.operation != "remove" }.joinToString("\n\n") { displayText(it) }
            return AssistantPresentation(
                id, revision, "spatial", envelope.optString("title").trim(), markdown,
                elements.mapNotNull { it.asset }, instant(envelope, "expires_at"), false,
                conversationId, elements, empty, correlationId
            )
        }

        private fun parseElement(value: JSONObject, index: Int): Element {
            val id = value.getString("id").trim()
            val operation = value.optString("operation", "create")
            val revision = value.optLong("revision", 1)
            require(id.isNotEmpty() && id.length <= 128 && operation in OPERATIONS && revision > 0) { "invalid element identity" }
            if (operation == "remove") return Element(id, operation, revision, null, index, null, "none", null, null, null, emptyList(), null, null, null, emptyList(), null)
            val kind = value.getString("kind")
            val policy = value.optString("interaction_policy", "touch_through")
            require(kind in ELEMENT_KINDS && policy in POLICIES) { "unsupported canvas element" }
            val choices = buildList {
                val array = value.optJSONArray("choices") ?: JSONArray()
                require(array.length() <= 20) { "too many choices" }
                for (i in 0 until array.length()) {
                    val choice = array.getJSONObject(i)
                    add(choice.getString("id") to choice.getString("text"))
                }
            }
            val actions = buildList {
                val array = value.optJSONArray("actions") ?: JSONArray()
                require(array.length() <= 16) { "too many actions" }
                for (i in 0 until array.length()) {
                    val action = array.getJSONObject(i)
                    add(Action(action.getString("id"), action.getString("kind"), action.getString("label")))
                }
            }
            val drawing = if (value.has("drawing")) {
                CanvasGeometry.parseDrawing(value.getJSONObject("drawing"))
            } else if (kind == "drawing" && value.has("primitives")) {
                CanvasGeometry.parseDrawing(value)
            } else null
            val anchor = value.optJSONObject("anchor")?.let(CanvasGeometry::parseAnchor) ?: drawing?.anchor
            val alt = value.optString("alt").takeIf(String::isNotBlank)
            require(!value.has("draggable") || value.get("draggable") is Boolean) { "draggable must be boolean" }
            val card = if (kind == "card") value.getJSONObject("card") else null
            if (card != null) require(card.getString("id").isNotBlank() && card.getLong("revision") > 0) { "invalid card reference" }
            return Element(id, operation, revision, kind, value.optInt("z_order", index),
                value.optJSONObject("bounds")?.let(::parseBounds), policy,
                value.optString("title").takeIf(String::isNotBlank), value.optString("content").takeIf(String::isNotBlank),
                value.optString("prompt").takeIf(String::isNotBlank), choices,
                value.optString("front").takeIf(String::isNotBlank), value.optString("back").takeIf(String::isNotBlank),
                if (kind in setOf("image", "artifact")) parseAsset(value) else null, actions, instant(value, "expires_at"),
                anchor = anchor, drawing = drawing, alt = alt,
                cardId = card?.getString("id"), cardRevision = card?.getLong("revision") ?: 0, draggable = value.optBoolean("draggable", false))
        }

        private fun parseBounds(value: JSONObject) = Bounds(
            number(value, "x"), number(value, "y"), number(value, "width"), number(value, "height"),
            number(value, "left"), number(value, "top"), number(value, "right"), number(value, "bottom"),
            value.optString("anchor").takeIf(String::isNotBlank)
        ).also { require((it.width ?: 0.0) >= 0.0 && (it.height ?: 0.0) >= 0.0) { "negative element bounds" } }

        private fun number(value: JSONObject, key: String): Double? = if (value.has(key) && !value.isNull(key)) value.getDouble(key) else null

        private fun parseAsset(value: JSONObject): Asset {
            val reference = value.getJSONObject("asset")
            val kind = reference.getString("kind")
            require(kind == "source" || kind == "artifact") { "invalid asset kind" }
            val identity = reference.getString(if (kind == "source") "source_id" else "artifact_id").trim()
            val revision = reference.getLong("revision")
            val uri = (value.optString("uri").takeIf(String::isNotBlank)
                ?: reference.optString("uri").takeIf(String::isNotBlank))?.let(Uri::parse)
            require(identity.isNotEmpty() && revision > 0 && (uri == null || uri.scheme == "content")) { "invalid asset reference" }
            val digest = (reference.optString("sha256").takeIf(String::isNotBlank)
                ?: value.optString("sha256").takeIf(String::isNotBlank)
                ?: reference.optString("digest").takeIf(String::isNotBlank))?.lowercase()
            require(digest == null || digest.matches(Regex("[0-9a-f]{64}"))) { "invalid asset digest" }
            val mediaType = value.optString("media_type").takeIf(String::isNotBlank) ?: reference.optString("media_type").takeIf(String::isNotBlank)
            val alt = value.optString("alt").takeIf(String::isNotBlank) ?: reference.optString("alt").takeIf(String::isNotBlank)
            val authRevision = reference.optLong("auth_revision", 0).takeIf { it > 0 } ?: value.optLong("auth_revision", 0).takeIf { it > 0 }
            return Asset(kind, identity, revision, uri, mediaType, alt, authRevision, digest)
        }

        private fun instant(value: JSONObject, key: String): Long? = value.optString(key)
            .takeIf { value.has(key) && !value.isNull(key) && it.isNotBlank() }
            ?.let { runCatching { OffsetDateTime.parse(it).toInstant().toEpochMilli() }.getOrElse { cause -> throw IllegalArgumentException("invalid $key", cause) } }

        private fun displayText(element: Element): String = when (element.kind) {
            "question" -> listOfNotNull(element.prompt, element.choices.joinToString("\n") { "- ${it.second}" }.takeIf(String::isNotBlank)).joinToString("\n")
            "flashcard" -> listOfNotNull(element.front, element.back).joinToString("\n\n")
            "image", "artifact" -> element.asset?.alt ?: element.alt ?: "Attached ${element.kind}"
            "drawing" -> element.alt ?: listOfNotNull(element.title, "Drawing ${element.id}").first()
            "card" -> element.title ?: "Interactive card"
            else -> listOfNotNull(element.title, element.content).joinToString("\n")
        }

        private fun parseLegacy(envelope: JSONObject): AssistantPresentation {
            val id = envelope.getString("presentation_id").trim()
            val revision = envelope.getLong("revision")
            val kind = envelope.getString("kind")
            require(id.isNotEmpty() && revision > 0 && kind in KINDS) { "invalid legacy presentation" }
            val revoked = envelope.optString("state", "active") == "revoked"
            val content = envelope.optJSONObject("content") ?: JSONObject()
            val markdown = content.optString("markdown").trim()
            val element = if (!revoked && markdown.isNotEmpty()) Element("legacy-content", "create", revision, "markdown", 0, null, "touch_through", content.optString("title").takeIf(String::isNotBlank), markdown, null, emptyList(), null, null, null, emptyList(), null) else null
            val expires = envelope.optDouble("expires_at", Double.NaN).takeUnless(Double::isNaN)?.let { (it * 1000).toLong() }
            return AssistantPresentation(id, revision, kind, content.optString("title"), markdown, emptyList(), expires, revoked,
                envelope.optString("conversation_id").takeIf(String::isNotBlank), listOfNotNull(element), revoked)
        }
    }
}
