"""Authenticated HTTP routes for the phone app (pairing, conversation, commands, voice, canvas)."""
from __future__ import annotations

import json
import uuid
import math
import numbers
import asyncio
import time
from starlette.concurrency import run_in_threadpool

from fastapi import Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from . import DEFAULT_DEVICE
from .mobile_conversation import ConversationConflict, UploadTooLarge

# How far each conversation's phone event stream has read, and when: canvas_bridge.delivery
STREAMS: dict[str, dict] = {}


def register(app, *, pairing_store, conversation, commands, device_id=DEFAULT_DEVICE,
             asset_records=None, authorize_asset=None, voice=None, canvas_event=None):
    """Attach device-credential routes without reusing the administrator token."""
    def get_voice():
        if voice is not None:
            return voice() if callable(voice) else voice
        v = getattr(conversation, "voice", None)
        return v() if callable(v) else v

    def authenticate(authorization, pairing_revision=None):
        prefix = "Bearer "
        if not authorization or not authorization.startswith(prefix):
            raise HTTPException(401, "device credential is required")
        try:
            return pairing_store.authenticate(
                authorization[len(prefix):], device_id,
                expected_pairing_revision=pairing_revision,
                connection_evidence="native-http:" + uuid.uuid4().hex)
        except PermissionError as exc:
            raise HTTPException(401, str(exc)) from exc

    async def payload(request):
        try:
            value = await request.json()
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, "invalid JSON body") from exc
        if not isinstance(value, dict):
            raise HTTPException(400, "request body must be an object")
        return value

    # Use the same custom speech providers as desktop, authenticated as this device.
    @app.post("/api/mobile/v1/voice/transcribe")
    async def voice_transcribe(request: Request, authorization: str | None = Header(default=None)):
        authenticate(authorization)
        from . import custom_speech
        recording = bytearray()
        async for chunk in request.stream():
            recording.extend(chunk)
            if len(recording) > custom_speech.MAX_AUDIO:
                raise HTTPException(413, "Recording exceeds 25 MB")
        try:
            return await run_in_threadpool(custom_speech.transcribe, bytes(recording), request.headers.get("content-type", "audio/mp4"))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, "Custom speech transcription is unavailable") from exc

    @app.post("/api/mobile/v1/voice/speech")
    async def voice_speech(request: Request, authorization: str | None = Header(default=None)):
        authenticate(authorization)
        from . import custom_speech
        from fastapi.responses import Response
        body = await payload(request)
        try:
            audio, content_type, provider = await run_in_threadpool(custom_speech.synthesize, body.get("text", ""))
            return Response(audio, media_type=content_type, headers={"Cache-Control": "no-store", "X-TTS-Provider": provider})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, "Custom speech synthesis is unavailable") from exc

    @app.get("/api/mobile/v1/fallbacks")
    def emergency_fallbacks(authorization: str | None = Header(default=None)):
        """Desktops the phone can ask while the main node is unreachable (phone/fallbacks.py)."""
        authenticate(authorization)
        from .fallbacks import listing
        return {"fallbacks": [{"node": f["node"], "label": f["label"], "url": f["url"]} for f in listing()]}

    @app.post("/api/mobile/v1/pairing/auto")
    async def automatic_pairing(request: Request):
        raw = await request.body()
        if len(raw) > 4096:
            raise HTTPException(400, "pairing request is too large")
        body = await payload(request) if raw else {}
        if set(body) - {"installation_id"}:
            raise HTTPException(400, "only installation_id is permitted")
        try:
            paired = pairing_store.auto_pair(
                peer=request.client.host if request.client else None,
                forwarded_for=request.headers.get("x-forwarded-for"),
                forwarded_proto=request.headers.get("x-forwarded-proto"),
                login=request.headers.get("tailscale-user-login"),
                installation_id=body.get("installation_id"), device_id=device_id)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"device_id": paired["device_id"],
                "device_credential": paired["device_credential"],
                "pairing_revision": paired["pairing_revision"],
                "state": paired["state"], "capabilities": paired["capabilities"],
                "protocol": "phone.v1"}

    @app.post("/api/mobile/v1/pairing/redeem")
    async def redeem_invitation(request: Request):
        """The expiring operator-issued challenge is this route's credential."""
        body = await payload(request)
        if set(body) != {"challenge"}:
            raise HTTPException(400, "exactly one field, challenge, is required")
        try:
            paired = pairing_store.redeem(body["challenge"], device_id=device_id)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "device_id": paired["device_id"],
            "device_credential": paired["device_credential"],
            "pairing_revision": paired["pairing_revision"],
            "state": paired["state"],
            "capabilities": paired["capabilities"],
            "protocol": "phone.v1",
        }

    @app.get("/api/mobile/v1/conversations/{conversation_id}/history")
    def history(conversation_id: str, after_sequence: int = 0, limit: int = 200,
                authorization: str | None = Header(default=None)):
        authenticate(authorization)
        return {"conversation_id": conversation_id,
                "messages": conversation.history(conversation_id,
                                                 after_sequence=after_sequence, limit=limit)}

    @app.get("/api/mobile/v1/conversations/{conversation_id}/requests")
    def request_activity(conversation_id: str, limit: int = 200,
                         authorization: str | None = Header(default=None)):
        authenticate(authorization)
        return {"conversation_id": conversation_id,
                "requests": conversation.requests(conversation_id, limit=limit)}

    @app.post("/api/mobile/v1/files/{capture_id}")
    async def upload_capture(capture_id: str, request: Request, name: str,
                             media_type: str = "application/octet-stream",
                             authorization: str | None = Header(default=None)):
        device = authenticate(authorization)
        revision = device["pairing_revision"]
        def verify_authority():
            authenticate(authorization, revision)
            pairing_store.authorize("file_share", device_id,
                                    expected_pairing_revision=revision)
        try:
            verify_authority()
            return await conversation.capture_upload(
                request.stream(), capture_id, name=name, media_type=media_type,
                verify_authority=verify_authority)
        except UploadTooLarge as exc:
            raise HTTPException(413, str(exc)) from exc
        except ConversationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

    @app.get("/api/mobile/v1/conversations/{conversation_id}/events")
    async def events(conversation_id: str, request: Request, after_revision: int = 0,
               authorization: str | None = Header(default=None)):
        device = await run_in_threadpool(authenticate, authorization)
        async def stream():
            revision = after_revision
            while not await request.is_disconnected():
                wait_events = getattr(conversation, "wait_events", None)
                if callable(wait_events):
                    rows = await run_in_threadpool(wait_events, conversation_id,
                                                   after_revision=revision, timeout=5)
                else:
                    rows = await run_in_threadpool(conversation.events, conversation_id,
                                                   after_revision=revision)
                # Authority may change during a wait. Check again before any
                # resulting batch leaves the server.
                try:
                    await run_in_threadpool(authenticate, authorization, device["pairing_revision"])
                except HTTPException:
                    return
                for event in rows:
                    revision = event["revision"]
                    yield "id: %s\nevent: %s\ndata: %s\n\n" % (
                        revision, event["kind"],
                        json.dumps(event["payload"], ensure_ascii=False))
                yield ": keepalive\n\n"
                STREAMS[conversation_id] = {"revision": revision, "at": time.time()}
                if not callable(wait_events):
                    await asyncio.sleep(5)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store"})

    def authenticate_presentation_asset(
            authorization: str | None = Header(default=None)):
        return authenticate(authorization)

    @app.get("/api/mobile/v1/presentation-assets/{kind}/{asset_id}")
    def presentation_asset(kind: str, asset_id: str, revision: int,
                           authenticated=Depends(authenticate_presentation_asset),
                           auth_revision: int | None = None,
                           authorization: str | None = Header(default=None)):
        """Resolve fresh render evidence after rechecking device and asset authority."""
        device = authenticated
        if kind not in {"source", "artifact"}:
            raise HTTPException(400, "asset kind must be source or artifact")
        if asset_records is None:
            raise HTTPException(503, "asset authority is unavailable")
        from .presentation_assets import resolve_asset
        records = asset_records() if callable(asset_records) else asset_records
        key = "source_id" if kind == "source" else "artifact_id"
        reference = {"kind": kind, key: asset_id, "revision": revision}
        if auth_revision is not None:
            reference["auth_revision"] = auth_revision
        def permitted(record, ref):
            # Authentication is deliberately rechecked at retrieval time so a
            # pairing revocation cannot race an earlier presentation event.
            authenticate(authorization, device["pairing_revision"])
            return (authorize_asset(record, ref, device) if authorize_asset
                    is not None else record.get("authorized") is True)
        result = resolve_asset(reference, records, authorize=permitted)
        if result["state"] == "unavailable":
            raise HTTPException(403 if result.get("reason", "").startswith("authorization")
                                else 404, result)
        if result["state"] == "stale":
            raise HTTPException(409, result)
        return result

    @app.post("/api/mobile/v1/conversations/{conversation_id}/messages")
    async def message(conversation_id: str, request: Request,
                      authorization: str | None = Header(default=None)):
        body = await payload(request)
        try:
            revision = body["expected_pairing_revision"]
        except KeyError as exc:
            raise HTTPException(400, "missing field: expected_pairing_revision") from exc
        authenticated = authenticate(authorization, revision)
        auth_device_id = authenticated.get("device_id") or device_id
        auth_pairing_revision = authenticated.get("pairing_revision", revision)
        identity_evidence = authenticated.get("evidence_id") or "authenticated_pairing"

        raw_ctx = (body.get("interaction_context")
                   or body.get("native_interaction")
                   or body.get("voice_session")
                   or body.get("session"))
        interaction_ctx = None
        session_id = None
        is_voice_session = False

        if raw_ctx is not None:
            if not isinstance(raw_ctx, dict):
                raise HTTPException(400, "interaction_context must be an object")
            if len(raw_ctx) > 32:
                raise HTTPException(400, "interaction_context contains too many fields")
            if len(json.dumps(raw_ctx, ensure_ascii=False)) > 4096:
                raise HTTPException(400, "interaction_context exceeds maximum size")
            ctx_version = raw_ctx.get("version", raw_ctx.get("revision", 1))
            if isinstance(ctx_version, bool) or not isinstance(ctx_version, int) or ctx_version < 1:
                raise HTTPException(400, "interaction_context version must be a positive integer")
            interaction_ctx = dict(raw_ctx)
            raw_sess = interaction_ctx.get("session_id") or interaction_ctx.get("native_session_id")
            if raw_sess is not None:
                if not isinstance(raw_sess, str) or not raw_sess.strip() or len(raw_sess) > 256:
                    raise HTTPException(400, "session_id must be a bounded non-empty string")
                session_id = raw_sess.strip()
            kind = str(interaction_ctx.get("kind") or interaction_ctx.get("type") or interaction_ctx.get("mode") or "").lower()
            interface_claim = str(interaction_ctx.get("interface") or "").lower()
            if (interface_claim == "voice"
                    or kind in {"voice", "voice_session", "assistant_voice_session", "native_assistant", "assistant"}
                    or interaction_ctx.get("is_voice") is True
                    or interaction_ctx.get("native_assistant") is True
                    or (session_id is not None and "voice" in session_id.lower())):
                is_voice_session = True
        elif isinstance(body.get("session_id"), str) and body.get("session_id").strip():
            raw_sess = body["session_id"].strip()
            if len(raw_sess) > 256:
                raise HTTPException(400, "session_id exceeds maximum length")
            session_id = raw_sess
            if str(body.get("interface", "")).lower() == "voice" or "voice" in session_id.lower():
                is_voice_session = True

        interface = "voice" if is_voice_session else "phone_app"
        origin_device = {
            "device_id": auth_device_id,
            "platform": "android",
            "interface": interface,
            "identity_evidence": identity_evidence,
            "pairing_revision": auth_pairing_revision,
        }
        if session_id:
            origin_device["session_id"] = session_id

        try:
            accepted = await run_in_threadpool(conversation.accept,
                conversation_id=conversation_id, request_id=body["request_id"],
                content=body["content"], attachments=body.get("attachments"),
                correlation_id=body.get("correlation_id"),
                continuation=body.get("continuation"),
                expires_at=body.get("expires_at"),
                origin_device=origin_device,
                session_id=session_id,
                interaction_context=interaction_ctx)
        except KeyError as exc:
            raise HTTPException(400, f"missing field: {exc.args[0]}") from exc
        except ConversationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        voice_service = get_voice()
        if voice_service is not None and is_voice_session and not accepted.get("duplicate"):
            try:
                voice_service.request_accepted(
                    session_id=session_id,
                    request_id=body["request_id"],
                    correlation_id=accepted.get("correlation_id"),
                    conversation_id=conversation_id,
                    device_id=auth_device_id,
                )
            except Exception:
                pass
        return accepted

    @app.post("/api/mobile/v1/conversations/{conversation_id}/voice/session")
    async def voice_session_transition(conversation_id: str, request: Request,
                                       authorization: str | None = Header(default=None)):
        body = await payload(request)
        try:
            revision = body["expected_pairing_revision"]
        except KeyError as exc:
            raise HTTPException(400, "missing field: expected_pairing_revision") from exc
        auth_device = authenticate(authorization, revision)
        voice_service = get_voice()
        if voice_service is None:
            raise HTTPException(503, "voice lifecycle authority is unavailable")
        transition = body.get("transition") or body.get("action")
        if not transition or not isinstance(transition, str):
            raise HTTPException(400, "transition is required")
        session_id = body.get("session_id")
        request_id = body.get("request_id")
        correlation_id = body.get("correlation_id")
        auth_device_id = auth_device.get("device_id") or device_id
        try:
            return await run_in_threadpool(
                voice_service.transition,
                transition,
                session_id=session_id,
                request_id=request_id,
                correlation_id=correlation_id,
                conversation_id=conversation_id,
                device_id=auth_device_id,
                cancelled=body.get("cancelled", False),
                value=body.get("value", True),
                response=body.get("response"),
            )
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/mobile/v1/conversations/{conversation_id}/voice/session")
    def voice_session_snapshot(conversation_id: str, session_id: str | None = None,
                               authorization: str | None = Header(default=None)):
        auth_device = authenticate(authorization)
        voice_service = get_voice()
        if voice_service is None:
            raise HTTPException(503, "voice lifecycle authority is unavailable")
        auth_device_id = auth_device.get("device_id") or device_id
        return voice_service.snapshot(session_id=session_id,
                                      conversation_id=conversation_id,
                                      device_id=auth_device_id)

    @app.get("/api/mobile/v1/commands")
    def pending_commands(pairing_revision: int,
                         authorization: str | None = Header(default=None)):
        authenticate(authorization, pairing_revision)
        try:
            return commands.pending(pairing_revision=pairing_revision)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

    @app.post("/api/mobile/v1/commands/{command_id}/accept")
    async def accept_command(command_id: str, request: Request,
                             authorization: str | None = Header(default=None)):
        body = await payload(request)
        try:
            revision = body["pairing_revision"]
        except KeyError as exc:
            raise HTTPException(400, "missing field: pairing_revision") from exc
        authenticate(authorization, revision)
        try:
            command = commands.accept(
                command_id, owner=body["owner"], pairing_revision=revision,
                lease_seconds=body.get("lease_seconds", 30))
        except KeyError as exc:
            raise HTTPException(400, f"missing field: {exc.args[0]}") from exc
        except (ValueError, PermissionError) as exc:
            raise HTTPException(409, str(exc)) from exc
        if command is None:
            raise HTTPException(409, "command is stale, expired, or already accepted")
        return {"state": "accepted", "command": command}

    @app.post("/api/mobile/v1/commands/{command_id}/heartbeat")
    async def command_heartbeat(command_id: str, request: Request,
                                authorization: str | None = Header(default=None)):
        body = await payload(request)
        try:
            revision = body["pairing_revision"]
            owner = body["owner"]
            lease_token = body["lease_token"]
        except KeyError as exc:
            raise HTTPException(400, f"missing field: {exc.args[0]}") from exc
        authenticate(authorization, revision)
        if (not isinstance(owner, str) or not owner or len(owner) > 256
                or not isinstance(lease_token, str) or not lease_token
                or len(lease_token) > 256):
            raise HTTPException(400, "bounded owner and lease_token are required")
        lease_seconds = body.get("lease_seconds", 30)
        running = body.get("running", True)
        if (isinstance(lease_seconds, bool) or not isinstance(lease_seconds, numbers.Real)
                or not math.isfinite(lease_seconds) or not 1 <= lease_seconds <= 3600
                or type(running) is not bool):
            raise HTTPException(400, "bounded lease_seconds and boolean running are required")
        current = commands.control.get_command(command_id)
        if not current or current.get("target_device") != device_id:
            raise HTTPException(404, "unknown phone command")
        if current.get("arguments", {}).get("pairing_revision") != revision:
            return {"heartbeated": False, "command": current}
        try:
            changed = commands.heartbeat(
                command_id, owner=owner, lease_token=lease_token,
                checkpoint=body.get("checkpoint"),
                lease_seconds=lease_seconds, running=running)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"heartbeated": changed,
                "command": commands.control.get_command(command_id)}

    @app.post("/api/mobile/v1/commands/{command_id}/result")
    async def command_result(command_id: str, request: Request,
                             authorization: str | None = Header(default=None)):
        body = await payload(request)
        try:
            revision = body["pairing_revision"]
        except KeyError as exc:
            raise HTTPException(400, "missing field: pairing_revision") from exc
        authenticate(authorization, revision)
        try:
            if ("result" in body) == ("error" in body):
                raise ValueError("exactly one of result or error is required")
            outcome = ({"result": body["result"]} if "result" in body
                       else {"error": body["error"]})
            return commands.acknowledge(
                command_id, owner=body["owner"], lease_token=body["lease_token"],
                pairing_revision=revision, **outcome)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/mobile/v1/grants")
    async def grant_evidence(request: Request,
                             authorization: str | None = Header(default=None)):
        body = await payload(request)
        try:
            revision = body["pairing_revision"]
        except KeyError as exc:
            raise HTTPException(400, "missing field: pairing_revision") from exc
        authenticate(authorization, revision)
        try:
            return pairing_store.report_grant_evidence(
                device_id, pairing_revision=revision, grants=body["grants"],
                evidence_id=body["evidence_id"])
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

    # -- interactive canvas cards ("card" presentation elements) -------------------
    # The phone app fetches a card with its device credential and draws it with the
    # renderer it ships, in a WebView that holds no credential; taps and state come back here.
    from .. import canvas as _canvas

    def phone_card(card_id: str) -> dict:
        element = _canvas.store.get(card_id)
        if not element or element.get("kind") != "ui" or not _canvas.matches(element, "phone", main_node=False):
            raise HTTPException(404, "no such card on the phone's canvas")
        return element

    @app.get("/api/mobile/v1/canvas/cards/{card_id}")
    def canvas_card(card_id: str, authorization: str | None = Header(default=None)):
        authenticate(authorization)
        return {"card": phone_card(card_id)}

    @app.post("/api/mobile/v1/canvas/cards/{card_id}/state")
    async def canvas_card_state(card_id: str, request: Request,
                                authorization: str | None = Header(default=None)):
        authenticate(authorization)
        body = await payload(request)
        phone_card(card_id)
        try:
            return {"saved": _canvas.store.set_state(card_id, body.get("state"))}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/mobile/v1/canvas/cards/{card_id}/events")
    async def canvas_card_event(card_id: str, request: Request,
                                authorization: str | None = Header(default=None)):
        authenticate(authorization)
        body = await payload(request)
        phone_card(card_id)
        if canvas_event is not None:  # the shared handler: a message also runs a chat turn
            return await canvas_event(card_id, body)
        try:
            return {"event": _canvas.store.add_event(card_id, body)}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
