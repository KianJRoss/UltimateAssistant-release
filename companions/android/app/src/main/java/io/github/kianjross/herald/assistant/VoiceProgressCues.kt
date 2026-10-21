package io.github.kianjross.herald.assistant

internal class VoiceProgressCues {
    private var searchCueGiven = false
    fun startTurn(text: String): String {
        reset()
        return if (Regex("""\b(search|lookup|look\s+up)\b""", RegexOption.IGNORE_CASE).containsMatchIn(text))
            "I'll look that up" else "On it"
    }
    fun toolStarted(toolName: String): String? {
        val name = toolName.lowercase().split('.', ':', '/').last()
        if (searchCueGiven || name !in setOf("search", "search_query", "web_search", "websearch")) return null
        searchCueGiven = true
        return "Searching"
    }
    /** The assistant said its own first line; generic cues would only repeat it. */
    fun acknowledged() { searchCueGiven = true }
    fun reset() { searchCueGiven = false }
}
