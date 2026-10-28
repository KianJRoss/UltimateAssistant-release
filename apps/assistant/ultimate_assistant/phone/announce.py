"""Say something out loud on the paired phone (the app speaks it and waits for the user to engage).

Used by the clock for the most urgent reminders and for nudges that ask to be spoken. Queued as a
`phone.announcement` command; the same key is never queued twice, so a retry doesn't repeat it.
"""
from __future__ import annotations

import time
from typing import Any

from . import DEFAULT_DEVICE, conversation_id


def announce(text: str, key: str, *, expires_in: float = 300) -> dict[str, Any]:
    from . import services
    from .voice_interaction import speech_text
    status = services.service("pairing").status(DEFAULT_DEVICE)
    if not status.get("paired") or status.get("revoked") or status.get("pairing_revision") is None:
        return {"state": "not_paired"}
    spoken = speech_text(text)
    conversation = conversation_id()
    try:
        queued = services.service("phone_commands").enqueue(
            "phone.announcement",
            {"text": spoken, "conversation_id": conversation,
             "engagement": {"mode": "spoken_announcement", "requires_engagement": True,
                            "conversation_id": conversation}},
            idempotency_key="phone.announcement:" + key, expires_at=time.time() + expires_in,
            pairing_revision=status["pairing_revision"], correlation_id=key)
    except PermissionError as exc:  # paired but not connected right now
        return {"state": "not_connected", "detail": str(exc)}
    except ValueError as exc:
        if "Idempotency key already" in str(exc):
            return {"state": "already_queued"}  # said (or saying) already: never twice
        raise
    return {"state": "queued", "command_id": queued.get("id")}
