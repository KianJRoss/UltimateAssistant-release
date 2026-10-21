package io.github.kianjross.herald.assistant

import android.content.Context
import android.content.ContextWrapper
import android.content.SharedPreferences
import android.os.SystemClock
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.util.Collections
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** Real paired-phone transport regression (skipped until paired). No assistant session, recognition, or TTS is started. */
@RunWith(AndroidJUnit4::class)
class BridgeClientTransportTest {
    @Test
    fun rejectedHeadAndLegacyNullContinuationDoNotBlockCanonicalReplies() {
        val app = InstrumentationRegistry.getInstrumentation().targetContext.applicationContext
        val unique = UUID.randomUUID().toString()
        val prefsName = "phone_bridge_outbox_validation_$unique"
        val isolated = object : ContextWrapper(app) {
            override fun getApplicationContext(): Context = this
            override fun getSharedPreferences(name: String, mode: Int): SharedPreferences =
                super.getSharedPreferences(if (name == "phone_bridge_outbox") prefsName else name, mode)
        }
        val prefs = isolated.getSharedPreferences("phone_bridge_outbox", Context.MODE_PRIVATE)
        val identity = DeviceIdentity(app) // Keep the actual installed pairing and endpoint.
        val snapshot = identity.snapshot()
        // A real paired phone and its assistant only: skipped on a fresh device or emulator.
        org.junit.Assume.assumeNotNull(snapshot)
        val revision = snapshot!!.pairingRevision
        val conversation = "transport-validation-$unique"
        val rejectedId = "rejected-$unique"
        val legacyId = "legacy-$unique"
        val question = "What device am I speaking from?"
        fun item(requestId: String, continuation: JSONObject): JSONObject {
            val envelope = JSONObject().put("request_id", requestId)
                .put("conversation_id", conversation).put("content", question)
                .put("expected_pairing_revision", revision).put("attachments", JSONArray())
                .put("correlation_id", "correlation-$requestId")
                .put("continuation", continuation)
            return JSONObject().put("request_id", requestId).put("conversation_id", conversation)
                .put("pairing_revision", revision).put("envelope", envelope)
        }
        val rejectedEnvelope = item(rejectedId, JSONObject()
            .put("context_id", "nonexistent-validation-$unique").put("expected_revision", 1))
        val legacyEnvelope = item(legacyId, JSONObject()
            .put("context_id", "null").put("expected_revision", 0))
        assertTrue(prefs.edit().putString("ordered_requests", JSONArray()
            .put(rejectedEnvelope).put(legacyEnvelope).toString()).commit())
        val bridge = BridgeClient(isolated, identity, identity.backendUrl(""))
        try {
            val results = Collections.synchronizedList(mutableListOf<BridgeClient.Result>())
            val streamReplies = Collections.synchronizedList(mutableListOf<JSONObject>())
            val streamed = CountDownLatch(2)
            bridge.streamEvents(conversation, 0, onEvent = { _, event ->
                if (event.optString("role") == "assistant") {
                    streamReplies.add(event)
                    streamed.countDown()
                }
            }, onClosed = {})
            val delivered = CountDownLatch(3) // One rejection, then two accepted envelopes.
            val currentId = bridge.submitMessage(conversation, question, contextId = "null",
                expectedContinuationRevision = 0, correlationId = "new-correlation-$unique") {
                results.add(it)
                delivered.countDown()
            }
            assertTrue("Queued requests did not finish delivery", delivered.await(120, TimeUnit.SECONDS))
            assertNull("Queue worker barrier failed", bridge.replayAwait().unavailable)
            val outcomes = synchronized(results) { results.toList() }
            val failure = outcomes.mapNotNull { it.unavailable }.single()
            assertEquals("revision_conflict", failure.code)
            assertFalse(failure.retryable)
            assertEquals("Terminal failure must preserve its owning request", rejectedId, failure.requestId)
            val acceptedIds = outcomes.mapNotNull { it.body?.optString("request_id") }.toSet()
            assertEquals(setOf(legacyId, currentId), acceptedIds)
            val rejected = JSONArray(prefs.getString("rejected_requests", "[]"))
            assertEquals(1, rejected.length())
            val archived = rejected.getJSONObject(0)
            assertEquals(rejectedId, archived.getString("request_id"))
            assertEquals(rejectedEnvelope.getJSONObject("envelope").toString(),
                archived.getJSONObject("envelope").toString())
            assertEquals("revision_conflict", archived.getJSONObject("failure").getString("code"))
            assertEquals(0, JSONArray(prefs.getString("ordered_requests", "[]")).length())

            // Acceptance is not completion: read actual backend assistant messages and verify
            // canonical ownership, with deliberately different correlation IDs above.
            val expected = setOf(legacyId, currentId)
            val deadline = SystemClock.elapsedRealtime() + 120_000
            var completed = emptySet<String>()
            while (SystemClock.elapsedRealtime() < deadline && completed != expected) {
                val ready = CountDownLatch(1)
                var history: BridgeClient.Result? = null
                bridge.history(conversation, 0) { history = it; ready.countDown() }
                assertTrue("History transport timed out", ready.await(65, TimeUnit.SECONDS))
                assertNull("History transport unavailable", history!!.unavailable)
                val messages = history!!.body!!.getJSONArray("messages")
                completed = (0 until messages.length()).map { messages.getJSONObject(it) }
                    .filter { it.optString("role") == "assistant" &&
                        it.optString("request_id") in expected && it.optString("content").isNotBlank() }
                    .map { it.getString("request_id") }.toSet()
                if (completed != expected) SystemClock.sleep(1000)
            }
            assertEquals("Backend replies must retain both canonical request IDs", expected, completed)
            val stateReady = CountDownLatch(1)
            var states: BridgeClient.Result? = null
            bridge.requestStates(conversation) { states = it; stateReady.countDown() }
            assertTrue("Request state transport timed out", stateReady.await(65, TimeUnit.SECONDS))
            assertNull(states!!.unavailable)
            val requests = states!!.body!!.getJSONArray("requests")
            val successful = (0 until requests.length()).map { requests.getJSONObject(it) }
                .filter { it.optString("request_state") == "completed" }
                .map { it.getString("request_id") }.toSet()
            assertEquals("Replies must represent completed requests", expected, successful)
            assertTrue("Reply stream did not deliver both assistant messages", streamed.await(10, TimeUnit.SECONDS))
            val events = synchronized(streamReplies) { streamReplies.toList() }
            assertEquals("Live voice stream must carry canonical ownership", expected,
                events.map { it.optString("request_id") }.toSet())
        } finally {
            bridge.close()
            prefs.edit().clear().commit()
            app.deleteSharedPreferences(prefsName)
        }
    }
}
