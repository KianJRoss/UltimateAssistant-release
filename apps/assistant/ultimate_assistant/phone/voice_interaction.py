"""Speech-first session state over the existing conversation authority."""
from __future__ import annotations

import re
import threading

from . import DEFAULT_DEVICE, conversation_id as default_conversation


_MARKUP = re.compile(r"(?:```.*?```|`([^`]*)`|\*\*|__|\[(.*?)\]\([^)]*\))", re.S)


def speech_text(value, *, maximum=700):
    """Produce a bounded TTS rendering; the retained transcript is unchanged."""
    text = str(value or "").strip()
    text = _MARKUP.sub(lambda match: next((g for g in match.groups() if g), ""), text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= maximum:
        return text
    clipped = text[:maximum].rsplit(" ", 1)[0].rstrip(" ,;:")
    return clipped + "."


class _SessionState:
    def __init__(self, conversation_id: str, device_id: str, session_id: str, clock):
        self.conversation_id = conversation_id
        self.device_id = device_id
        self.session_id = session_id
        self.clock = clock
        self.state = "ended"
        self.muted = False
        self.turn = 0
        self.request_id = None
        self.correlation_id = None
        self.created_at = clock()
        self.updated_at = clock()
        self.engaged_at = 0.0


class VoiceInteraction:
    """Own recognition/TTS transitions; canvas and transport remain projections."""

    def __init__(self, conversation, *, conversation_id=None, device_id=DEFAULT_DEVICE,
                 clock=None, max_sessions=200):
        import time
        self.conversation = conversation
        self.conversation_id = conversation_id or default_conversation()
        self.device_id = device_id
        self.clock = clock or time.time
        self._max_sessions = max_sessions
        self._lock = threading.RLock()
        self._sessions: dict[tuple[str, str, str], _SessionState] = {}
        self._default_session_id = "default"

    def _get_session(self, session_id: str | None = None, conversation_id: str | None = None,
                     device_id: str | None = None, *, create: bool = True) -> _SessionState:
        conv_id = conversation_id or self.conversation_id
        dev_id = device_id or self.device_id
        sess_id = session_id or self._default_session_id
        key = (conv_id, dev_id, sess_id)
        if key in self._sessions:
            session = self._sessions[key]
            session.updated_at = self.clock()
            return session
        if not create:
            return _SessionState(conv_id, dev_id, sess_id, self.clock)
        if len(self._sessions) >= self._max_sessions:
            inactive = [k for k, s in self._sessions.items() if s.state in {"ended", "expired"}]
            if inactive:
                del self._sessions[inactive[0]]
            else:
                oldest = min(self._sessions.keys(), key=lambda k: self._sessions[k].updated_at)
                del self._sessions[oldest]
        session = _SessionState(conv_id, dev_id, sess_id, self.clock)
        self._sessions[key] = session
        return session

    @property
    def _state(self):
        return self._get_session().state

    @_state.setter
    def _state(self, value):
        self._get_session().state = value

    @property
    def _muted(self):
        return self._get_session().muted

    @_muted.setter
    def _muted(self, value):
        self._get_session().muted = value

    @property
    def _turn(self):
        return self._get_session().turn

    @_turn.setter
    def _turn(self, value):
        self._get_session().turn = value

    def _event(self, session: _SessionState, kind: str, **payload):
        # Metadata-only diagnostic event: NEVER store transcripts or rendered response text!
        for text_key in ("text", "transcript", "content", "prompt", "rendered", "response"):
            payload.pop(text_key, None)
        event_payload = {
            "conversation_id": session.conversation_id,
            "device_id": session.device_id,
            "session_id": session.session_id,
            "request_id": session.request_id,
            "correlation_id": session.correlation_id,
            "state": session.state,
            "muted": session.muted,
            "turn": session.turn,
            "observed_at": self.clock(),
        }
        event_payload.update(payload)
        return self.conversation.emit_event(session.conversation_id,
                                            "voice." + kind, event_payload)

    def start(self, *, session_id: str | None = None, conversation_id: str | None = None,
              device_id: str | None = None):
        with self._lock:
            session = self._get_session(session_id, conversation_id, device_id)
            if session.state in {"ended", "expired"}:
                session.state = "listening" if not session.muted else "muted"
                session.engaged_at = self.clock()
                self._event(session, "session", transition="started")
            return self.snapshot(session_id=session.session_id,
                                 conversation_id=session.conversation_id,
                                 device_id=session.device_id)

    def recognized(self, transcript, *, request_id=None, correlation_id=None,
                   presentation=None, session_id=None, conversation_id=None,
                   device_id=None):
        """Accept a spoken turn through the normal durable request outbox."""
        with self._lock:
            session = self._get_session(session_id, conversation_id, device_id)
            if session.state != "listening" or session.muted:
                raise RuntimeError("voice session is not listening")
            session.turn += 1
            session.engaged_at = self.clock()
            session.state = "thinking"
            req_id = request_id or f"voice-{session.turn}-{int(self.clock() * 1000)}"
            session.request_id = req_id
            session.correlation_id = correlation_id
            origin_device = {
                "interface": "voice",
                "device_id": session.device_id,
                "platform": "android" if session.device_id == DEFAULT_DEVICE else "windows",
                "identity_evidence": "authenticated_pairing",
            }
            if session.session_id:
                origin_device["session_id"] = session.session_id
            accepted = self.conversation.accept(
                conversation_id=session.conversation_id,
                request_id=req_id,
                content=str(transcript),
                correlation_id=correlation_id,
                origin_device=origin_device,
                session_id=session.session_id,
            )
            projected = (self.conversation.publish_presentation(
                presentation, conversation_id=session.conversation_id)
                if presentation is not None else None)
            self._event(session, "turn", transition="recognized",
                        request_id=accepted["request_id"],
                        canvas_visible=bool(presentation and not presentation.get("empty_canvas")))
            return {"request": accepted, "presentation": projected,
                    "state": session.state}

    def request_accepted(self, *, request_id, correlation_id=None, session_id=None,
                         conversation_id=None, device_id=None):
        """Attach lifecycle state to a request already retained by the HTTP API."""
        with self._lock:
            session = self._get_session(session_id, conversation_id, device_id)
            if session.state in {"speaking", "expired"}:
                raise RuntimeError("voice session cannot accept a turn in its current state")
            session.turn += 1
            session.state = "thinking"
            session.request_id = request_id
            session.correlation_id = correlation_id
            self._event(session, "turn", transition="recognized")
            return self.snapshot(session_id=session.session_id,
                                 conversation_id=session.conversation_id,
                                 device_id=session.device_id)

    def response_ready(self, *, request_id=None, correlation_id=None,
                       session_id=None, conversation_id=None, device_id=None):
        with self._lock:
            session = self._get_session(session_id, conversation_id, device_id)
            if request_id:
                session.request_id = request_id
            if correlation_id:
                session.correlation_id = correlation_id
            if session.state == "thinking":
                session.state = "response_ready"
            self._event(session, "turn", transition="response_ready",
                        request_id=session.request_id)
            return self.snapshot(session_id=session.session_id,
                                 conversation_id=session.conversation_id,
                                 device_id=session.device_id)

    def speak(self, response, *, request_id=None, correlation_id=None,
              session_id=None, conversation_id=None, device_id=None):
        with self._lock:
            session = self._get_session(session_id, conversation_id, device_id)
            if session.state not in {"thinking", "listening", "speaking", "response_ready"}:
                raise RuntimeError("voice session cannot speak in its current state")
            session.state = "speaking"
            if request_id:
                session.request_id = request_id
            if correlation_id:
                session.correlation_id = correlation_id
            rendered = speech_text(response)
            self._event(session, "speech", transition="started", request_id=session.request_id)
            return {"state": session.state, "text": rendered, "session_id": session.session_id,
                    "request_id": session.request_id}

    def speaking_acknowledged(self, *, request_id=None, correlation_id=None,
                              session_id=None, conversation_id=None, device_id=None):
        with self._lock:
            session = self._get_session(session_id, conversation_id, device_id)
            if request_id:
                session.request_id = request_id
            if correlation_id:
                session.correlation_id = correlation_id
            self._event(session, "speech", transition="acknowledged", request_id=session.request_id)
            return self.snapshot(session_id=session.session_id,
                                 conversation_id=session.conversation_id,
                                 device_id=session.device_id)

    def speech_finished(self, *, cancelled=False, session_id=None, conversation_id=None,
                        device_id=None):
        with self._lock:
            session = self._get_session(session_id, conversation_id, device_id)
            if session.state != "speaking":
                return self.snapshot(session_id=session.session_id,
                                     conversation_id=session.conversation_id,
                                     device_id=session.device_id)
            session.state = "muted" if session.muted else "listening"
            self._event(session, "speech", transition="cancelled" if cancelled else "completed",
                        recognition_rearmed=session.state == "listening")
            return self.snapshot(session_id=session.session_id,
                                 conversation_id=session.conversation_id,
                                 device_id=session.device_id)

    def interrupt(self, *, session_id=None, conversation_id=None, device_id=None):
        with self._lock:
            session = self._get_session(session_id, conversation_id, device_id)
            if session.state == "speaking":
                return self.speech_finished(cancelled=True, session_id=session.session_id,
                                            conversation_id=session.conversation_id,
                                            device_id=session.device_id)
            return self.snapshot(session_id=session.session_id,
                                 conversation_id=session.conversation_id,
                                 device_id=session.device_id)

    def mute(self, value=True, *, session_id=None, conversation_id=None, device_id=None):
        with self._lock:
            session = self._get_session(session_id, conversation_id, device_id)
            session.muted = bool(value)
            # Mute gates recognition; it must not discard an in-flight thinking
            # or speaking phase. In particular, unmuting while TTS is still
            # active must not rearm the recognizer and capture our own speech.
            if session.state not in {"ended", "thinking", "speaking", "expired"}:
                session.state = "muted" if session.muted else "listening"
            self._event(session, "session", transition="muted" if session.muted else "unmuted")
            return self.snapshot(session_id=session.session_id,
                                 conversation_id=session.conversation_id,
                                 device_id=session.device_id)

    def end(self, *, session_id=None, conversation_id=None, device_id=None):
        with self._lock:
            session = self._get_session(session_id, conversation_id, device_id)
            session.state = "ended"
            self._event(session, "session", transition="ended")
            return self.snapshot(session_id=session.session_id,
                                 conversation_id=session.conversation_id,
                                 device_id=session.device_id)

    def expire(self, *, request_id=None, session_id=None, conversation_id=None, device_id=None):
        with self._lock:
            session = self._get_session(session_id, conversation_id, device_id)
            session.state = "expired"
            if request_id:
                session.request_id = request_id
            self._event(session, "session", transition="expired", request_id=session.request_id)
            return self.snapshot(session_id=session.session_id,
                                 conversation_id=session.conversation_id,
                                 device_id=session.device_id)

    def transition(self, name: str, *, session_id=None, request_id=None, correlation_id=None,
                   conversation_id=None, device_id=None, **kwargs):
        transition_name = str(name).lower()
        if transition_name in {"start", "invoke", "invocation", "started"}:
            return self.start(session_id=session_id, conversation_id=conversation_id, device_id=device_id)
        elif transition_name in {"recognized", "thinking"}:
            transcript = kwargs.get("transcript", "")
            return self.recognized(transcript, request_id=request_id, correlation_id=correlation_id,
                                   presentation=kwargs.get("presentation"), session_id=session_id,
                                   conversation_id=conversation_id, device_id=device_id)
        elif transition_name in {"response_ready", "response-ready"}:
            return self.response_ready(request_id=request_id, correlation_id=correlation_id,
                                       session_id=session_id, conversation_id=conversation_id, device_id=device_id)
        elif transition_name in {"speak", "speaking"}:
            response = kwargs.get("response", "")
            return self.speak(response, request_id=request_id, correlation_id=correlation_id,
                              session_id=session_id, conversation_id=conversation_id, device_id=device_id)
        elif transition_name in {"speaking_acknowledged", "acknowledged"}:
            return self.speaking_acknowledged(request_id=request_id, correlation_id=correlation_id,
                                              session_id=session_id, conversation_id=conversation_id, device_id=device_id)
        elif transition_name in {"speech_finished", "completed"}:
            return self.speech_finished(cancelled=kwargs.get("cancelled", False),
                                        session_id=session_id, conversation_id=conversation_id, device_id=device_id)
        elif transition_name in {"interrupt", "interrupted"}:
            return self.interrupt(session_id=session_id, conversation_id=conversation_id, device_id=device_id)
        elif transition_name == "mute":
            return self.mute(kwargs.get("value", True), session_id=session_id,
                             conversation_id=conversation_id, device_id=device_id)
        elif transition_name in {"end", "ended"}:
            return self.end(session_id=session_id, conversation_id=conversation_id, device_id=device_id)
        elif transition_name in {"expire", "expired"}:
            return self.expire(request_id=request_id, session_id=session_id,
                               conversation_id=conversation_id, device_id=device_id)
        else:
            raise ValueError(f"Unknown voice lifecycle transition: {name}")

    def active_session(self, max_age=120):
        """Most recently invoked/used voice endpoint; background polling is not engagement."""
        with self._lock:
            candidates = [s for s in self._sessions.values()
                          if not s.muted and s.state not in {'ended', 'expired'}
                          and self.clock() - s.engaged_at <= max_age]
            if not candidates:
                return None
            session = max(candidates, key=lambda s: s.engaged_at)
            return {'device_id': session.device_id, 'session_id': session.session_id,
                    'conversation_id': session.conversation_id, 'engaged_at': session.engaged_at,
                    'state': session.state}

    def snapshot(self, session_id=None, conversation_id=None, device_id=None):
        session = self._get_session(session_id, conversation_id, device_id, create=False)
        return {"state": session.state, "muted": session.muted,
                "turn": session.turn, "conversation_id": session.conversation_id,
                "device_id": session.device_id, "session_id": session.session_id,
                "request_id": session.request_id,
                "recognition_active": session.state == "listening" and not session.muted}
