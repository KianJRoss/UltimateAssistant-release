package io.github.kianjross.herald.assistant

import org.json.JSONObject

/** Reminder projection mapped into transient spatial elements, not a separate screen. */
data class AssistantReminderPresentation(val presentation: AssistantPresentation, val reminderId: String, val occurrenceRevision: Long) {
    init { require(presentation.elements.any { it.kind == "reminder" } && reminderId.isNotBlank() && occurrenceRevision > 0) }
    companion object {
        fun parse(envelope: JSONObject): AssistantReminderPresentation {
            val projection = envelope.optJSONObject("reminder_projection") ?: envelope
            return AssistantReminderPresentation(AssistantPresentation.parse(envelope),
                projection.getString("reminder_id"), projection.getLong("occurrence_revision"))
        }
    }
}
