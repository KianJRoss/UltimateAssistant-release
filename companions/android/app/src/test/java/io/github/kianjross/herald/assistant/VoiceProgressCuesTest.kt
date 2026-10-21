package io.github.kianjross.herald.assistant

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** Bounded cues describe user intent and observed work, without making tool claims early. */
class VoiceProgressCuesTest {
    @Test
    fun explicitSearchIntentGetsImmediateAcknowledgement() {
        val cues = VoiceProgressCues()
        for (request in listOf("Search for the nearest library", "SEARCH the weather",
            "Look up the train timetable", "lookup the population of Indianapolis")) {
            assertEquals("I'll look that up", cues.startTurn(request))
        }
    }

    @Test
    fun ordinaryRequestsAndSearchWordFragmentsGetGenericAcknowledgement() {
        val cues = VoiceProgressCues()
        for (request in listOf("What device am I speaking from?", "Set a reminder for tomorrow",
            "Summarize my research notes", "Who are the searchers in this story?")) {
            assertEquals("On it", cues.startTurn(request))
        }
    }

    @Test
    fun GenericWorkDoesNotClaimSearchingOrConsumeTheActualSearchCue() {
        val cues = VoiceProgressCues()
        assertEquals("On it", cues.startTurn("Explain this problem"))
        for (tool in listOf("python", "computer.click", "files.read", "research_notes",
            "search_results_cache", "web.open")) {
            assertNull(cues.toolStarted(tool))
        }
        assertEquals("Searching", cues.toolStarted("web.search_query"))
    }

    @Test
    fun initialSearchAcknowledgementAllowsExactlyOneObservedSearchCue() {
        val cues = VoiceProgressCues()
        assertEquals("I'll look that up", cues.startTurn("Search for the forecast"))
        assertEquals("Searching", cues.toolStarted("web.search_query"))
        assertNull(cues.toolStarted("web_search"))
        assertNull(cues.toolStarted("search"))
        assertNull(cues.toolStarted("files.read"))
    }

    @Test
    fun eachNewTurnRestoresTheSearchBudget() {
        val cues = VoiceProgressCues()
        cues.startTurn("Find the answer")
        assertEquals("Searching", cues.toolStarted("search"))
        assertNull(cues.toolStarted("search"))
        assertEquals("On it", cues.startTurn("What is the answer?"))
        assertEquals("Searching", cues.toolStarted("web_search"))
        assertNull(cues.toolStarted("search_query"))
        assertEquals("I'll look that up", cues.startTurn("Look up another answer"))
        assertEquals("Searching", cues.toolStarted("websearch"))
    }

    @Test
    fun searchMetadataIsRecognizedAcrossToolNamespaces() {
        val cues = VoiceProgressCues()
        for (tool in listOf("search", "search_query", "web_search", "websearch",
            "web.search_query", "web:search_query", "web/search_query")) {
            cues.startTurn("Answer my question")
            assertEquals("Searching", cues.toolStarted(tool))
        }
    }
}
