package io.github.kianjross.herald.assistant

import org.json.JSONArray
import org.json.JSONObject
import java.util.regex.Pattern

/**
 * Transport-neutral geometry for task-anchored canvas drawings.
 * Primitives (stroke, shape, arrow, label) use normalized coordinates (0.0..1.0)
 * relative to an anchor (task, element, asset, screen).
 */
object CanvasGeometry {
    const val MAX_PRIMITIVES = 256
    const val MAX_STROKE_POINTS = 512
    val PRIMITIVE_TYPES = setOf("stroke", "shape", "arrow", "label")
    val SHAPES = setOf("rect", "ellipse", "line")
    val OPERATIONS = setOf("create", "update", "remove")
    val ANCHOR_KINDS = setOf("task", "element", "asset", "screen")
    val COORDINATE_SPACES = setOf("normalized")

    private val COLOR_PATTERN = Pattern.compile("^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
    private val MARKUP_PATTERN = Pattern.compile("<\\s*/?\\s*[a-zA-Z][^>]*>|(?:javascript|vbscript)\\s*:", Pattern.CASE_INSENSITIVE)

    data class Point(val x: Float, val y: Float)

    data class Anchor(
        val kind: String,
        val ref: String,
        val revision: Long? = null
    )

    data class Style(
        val color: String? = null,
        val width: Float? = null,
        val opacity: Float? = null,
        val filled: Boolean? = null
    )

    sealed class Primitive {
        abstract val id: String
        abstract val operation: String
        abstract val revision: Long

        data class Stroke(
            override val id: String,
            override val operation: String = "create",
            override val revision: Long = 1,
            val points: List<Point>,
            val style: Style? = null
        ) : Primitive()

        data class Shape(
            override val id: String,
            override val operation: String = "create",
            override val revision: Long = 1,
            val shape: String,
            val from: Point? = null,
            val to: Point? = null,
            val x: Float? = null,
            val y: Float? = null,
            val width: Float? = null,
            val height: Float? = null,
            val style: Style? = null
        ) : Primitive()

        data class Arrow(
            override val id: String,
            override val operation: String = "create",
            override val revision: Long = 1,
            val from: Point,
            val to: Point,
            val style: Style? = null
        ) : Primitive()

        data class Label(
            override val id: String,
            override val operation: String = "create",
            override val revision: Long = 1,
            val text: String,
            val at: Point,
            val style: Style? = null
        ) : Primitive()

        data class Remove(
            override val id: String,
            override val operation: String = "remove",
            override val revision: Long = 1
        ) : Primitive()
    }

    data class Drawing(
        val coordinateSpace: String = "normalized",
        val anchor: Anchor,
        val primitives: List<Primitive>
    )

    fun parseAnchor(json: JSONObject): Anchor {
        val kind = json.getString("kind").trim()
        require(kind in ANCHOR_KINDS) { "anchor.kind must be one of $ANCHOR_KINDS" }
        val ref = json.getString("ref").trim()
        require(ref.isNotEmpty() && ref.toByteArray(Charsets.UTF_8).size <= 128 && !ref.contains('\u0000')) { "invalid anchor.ref" }
        val revision = if (json.has("revision") && !json.isNull("revision")) json.getLong("revision").takeIf { it > 0 } else null
        return Anchor(kind, ref, revision)
    }

    fun parseStyle(json: JSONObject?): Style? {
        if (json == null) return null
        val color = json.optString("color").takeIf(String::isNotBlank)?.let {
            require(COLOR_PATTERN.matcher(it).matches()) { "style.color must be #RRGGBB or #RRGGBBAA" }
            it
        }
        val width = if (json.has("width") && !json.isNull("width")) {
            val w = json.getDouble("width").toFloat()
            require(w > 0f && w <= 64f && w.isFinite()) { "style.width must be within (0, 64]" }
            w
        } else null
        val opacity = if (json.has("opacity") && !json.isNull("opacity")) {
            val op = json.getDouble("opacity").toFloat()
            require(op in 0f..1f && op.isFinite()) { "style.opacity must be within [0, 1]" }
            op
        } else null
        val filled = if (json.has("filled") && !json.isNull("filled")) json.getBoolean("filled") else null
        return Style(color, width, opacity, filled)
    }

    fun parsePoint(value: Any): Point {
        return when (value) {
            is JSONObject -> {
                val x = value.getDouble("x").toFloat()
                val y = value.getDouble("y").toFloat()
                require(x in 0f..1f && y in 0f..1f && x.isFinite() && y.isFinite()) { "point coordinates must be within [0, 1]" }
                Point(x, y)
            }
            is JSONArray -> {
                require(value.length() == 2) { "point must be [x, y]" }
                val x = value.getDouble(0).toFloat()
                val y = value.getDouble(1).toFloat()
                require(x in 0f..1f && y in 0f..1f && x.isFinite() && y.isFinite()) { "point coordinates must be within [0, 1]" }
                Point(x, y)
            }
            else -> throw IllegalArgumentException("point must be [x, y] or {x, y}")
        }
    }

    fun parsePrimitive(json: JSONObject): Primitive {
        val id = json.getString("id").trim()
        require(id.isNotEmpty() && id.toByteArray(Charsets.UTF_8).size <= 128 && !id.contains('\u0000')) { "invalid primitive.id" }
        val operation = json.optString("operation", "create")
        require(operation in OPERATIONS) { "primitive.operation must be one of $OPERATIONS" }
        val revision = json.optLong("revision", 1)
        require(revision > 0) { "primitive.revision must be positive" }
        if (operation == "remove") {
            return Primitive.Remove(id, operation, revision)
        }
        val type = json.getString("type")
        require(type in PRIMITIVE_TYPES) { "primitive.type must be one of $PRIMITIVE_TYPES" }
        val style = json.optJSONObject("style")?.let(::parseStyle)
        return when (type) {
            "stroke" -> {
                val pointsArray = json.getJSONArray("points")
                require(pointsArray.length() in 2..MAX_STROKE_POINTS) { "stroke points must contain 2 to $MAX_STROKE_POINTS points" }
                val points = (0 until pointsArray.length()).map { parsePoint(pointsArray.get(it)) }
                Primitive.Stroke(id, operation, revision, points, style)
            }
            "shape" -> {
                val shape = json.getString("shape")
                require(shape in SHAPES) { "shape must be one of $SHAPES" }
                if (shape == "line") {
                    val from = parsePoint(json.get("from"))
                    val to = parsePoint(json.get("to"))
                    Primitive.Shape(id, operation, revision, shape, from = from, to = to, style = style)
                } else {
                    val x = json.getDouble("x").toFloat()
                    val y = json.getDouble("y").toFloat()
                    val w = json.getDouble("width").toFloat()
                    val h = json.getDouble("height").toFloat()
                    require(x in 0f..1f && y in 0f..1f && w in 0f..1f && h in 0f..1f) { "shape coordinates must be normalized" }
                    require(x + w <= 1.0001f && y + h <= 1.0001f) { "shape extends outside the normalized canvas" }
                    Primitive.Shape(id, operation, revision, shape, x = x, y = y, width = w, height = h, style = style)
                }
            }
            "arrow" -> {
                val from = parsePoint(json.get("from"))
                val to = parsePoint(json.get("to"))
                Primitive.Arrow(id, operation, revision, from, to, style)
            }
            "label" -> {
                val text = json.getString("text").trim()
                require(text.isNotEmpty() && text.toByteArray(Charsets.UTF_8).size <= 1024 && !text.contains('\u0000')) { "invalid label text" }
                require(!MARKUP_PATTERN.matcher(text).find()) { "label text must not contain HTML or scripts" }
                val at = parsePoint(json.get("at"))
                Primitive.Label(id, operation, revision, text, at, style)
            }
            else -> throw IllegalArgumentException("unknown primitive type: $type")
        }
    }

    fun parseDrawing(json: JSONObject): Drawing {
        val space = json.optString("coordinate_space", "normalized")
        require(space in COORDINATE_SPACES) { "unsupported coordinate_space" }
        val anchor = parseAnchor(json.getJSONObject("anchor"))
        val primitivesArray = json.getJSONArray("primitives")
        require(primitivesArray.length() in 1..MAX_PRIMITIVES) { "primitives must contain 1 to $MAX_PRIMITIVES items" }
        val primitives = (0 until primitivesArray.length()).map { parsePrimitive(primitivesArray.getJSONObject(it)) }
        val seen = mutableSetOf<String>()
        for (p in primitives) {
            require(seen.add(p.id)) { "duplicate primitive id: ${p.id}" }
        }
        return Drawing(space, anchor, primitives)
    }

    /**
     * Merge a validated drawing update into current primitives by revision.
     * Stale updates (revision older than retained) are ignored and removals delete the primitive.
     */
    fun applyDrawing(current: List<Primitive>, update: Drawing): List<Primitive> {
        val state = linkedMapOf<String, Primitive>()
        current.forEach { state[it.id] = it }
        for (p in update.primitives) {
            val old = state[p.id]
            if (old != null && p.revision <= old.revision) {
                continue
            }
            if (p.operation == "remove") {
                // Retain an invisible revision tombstone so a delayed create or
                // update cannot resurrect a primitive that was already removed.
                state[p.id] = p
            } else {
                state[p.id] = p
            }
        }
        return state.values.toList()
    }
}
