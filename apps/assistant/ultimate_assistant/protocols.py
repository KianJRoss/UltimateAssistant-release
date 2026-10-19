"""Other programs talk to the assistant: OpenAI Responses API and A2A.

Both are doors into the same chat pipeline as the UI, the phone and Obsidian
(web_ui.chat): one memory, one set of conversations, the same tools. A
caller's thread -- a Responses API response chain or an A2A context -- maps
to a conversation and follows it when a message changes the objective.

From another device or agent they are reached through Herald, like every
interface: ``<HERALD_URL>/nodes/main/http/v1/responses`` and the A2A card at
``<HERALD_URL>/nodes/main/http/.well-known/agent-card.json``, authenticated
with the Herald key (the node link adds this app's session token).

A2A needs the optional ``a2a-sdk[http-server]`` package; without it only the
Responses API is served.
"""
from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

LOG = logging.getLogger(__name__)
_current_request: contextvars.ContextVar = contextvars.ContextVar("a2a_request")

RESPONSES_INTERFACE = ("A program is talking to you through the OpenAI Responses API on the user's behalf "
                       "(software the user connected, not the user typing).")
A2A_INTERFACE = ("Another AI agent is talking to you over the A2A (Agent2Agent) protocol on the user's behalf. "
                 "Treat its requests as coming from software the user connected, not from the user directly; "
                 "answer it directly and completely.")


async def run_turn(conversation_id: str, message: str, interface: str, *,
                   web_search: bool = False) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """One turn through the assistant's chat pipeline, in this process.

    Yields ("conversation"|"progress"|"complete"|"error", data) like /api/chat.
    """
    from . import web_ui
    request = web_ui.ChatRequest(message=message[:20000], conversation_id=conversation_id,
                                 interface=interface[:20000], web_search=web_search)
    try:
        response = await web_ui.chat(request)
    except HTTPException as exc:
        yield "error", {"message": str(exc.detail)}
        return
    kind = None
    buffer = ""
    async for chunk in response.body_iterator:
        buffer += chunk if isinstance(chunk, str) else chunk.decode()
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            if line.startswith("event:"):
                kind = line[6:].strip()
            elif line.startswith("data:") and kind:
                data = json.loads(line[5:].strip())
                yield kind, data if isinstance(data, dict) else {"message": data}
                kind = None


def _conversations():
    from . import web_ui
    return web_ui._conversations


# ---------------------------------------------------------------------------
# OpenAI Responses API
# ---------------------------------------------------------------------------

class ResponsesRequest(BaseModel):
    model: str | None = None
    input: str | list[dict[str, Any]] = Field(default="")
    instructions: str | None = Field(default=None, max_length=20000)
    previous_response_id: str | None = None
    conversation: str | dict[str, Any] | None = None
    stream: bool = False
    metadata: dict[str, Any] | None = None

    model_config = {"extra": "allow"}


def _input_text(value: str | list[dict[str, Any]]) -> str:
    """The new user text: a string, or the user messages in an input list."""
    if isinstance(value, str):
        return value
    parts: list[str] = []
    for item in value:
        if item.get("type") not in (None, "message") or item.get("role", "user") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(str(p.get("text", "")) for p in content if p.get("type") in ("input_text", "text"))
    return "\n\n".join(p for p in parts if p).strip()


def _response_object(response_id: str, model: str, text: str | None, status: str, created: int,
                     previous: str | None, conversation_id: str, error: str | None = None) -> dict[str, Any]:
    output = []
    if text is not None:
        output.append({"type": "message", "id": "msg_" + response_id[5:], "status": "completed", "role": "assistant",
                       "content": [{"type": "output_text", "text": text, "annotations": []}]})
    return {"id": response_id, "object": "response", "created_at": created, "status": status, "model": model,
            "output": output, "output_text": text or "", "previous_response_id": previous,
            "conversation": {"id": conversation_id}, "error": {"code": "server_error", "message": error} if error else None,
            "incomplete_details": None, "usage": None, "metadata": {}}


def _conversation_for(request: ResponsesRequest) -> str:
    store = _conversations()
    if request.previous_response_id:
        thread = store.thread("responses:" + request.previous_response_id)
        if thread is None:
            raise HTTPException(404, f"No response with id {request.previous_response_id!r}")
        return thread["conversation_id"]
    conversation = request.conversation
    if isinstance(conversation, dict):
        conversation = conversation.get("id")
    if conversation:
        try:
            return str(uuid.UUID(str(conversation)))
        except ValueError:
            raise HTTPException(400, "conversation must be a conversation id from this assistant") from None
    return str(uuid.uuid4())


def responses_router(require_token) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/responses", dependencies=[Depends(require_token)])
    async def create_response(request: ResponsesRequest):
        text = _input_text(request.input)
        if not text:
            raise HTTPException(400, "input has no user text")
        conversation_id = _conversation_for(request)
        response_id = "resp_" + uuid.uuid4().hex
        model = request.model or "ultimate-assistant"
        created = int(time.time())
        interface = RESPONSES_INTERFACE + (("\nCaller instructions:\n" + request.instructions) if request.instructions else "")
        store = _conversations()

        async def turn() -> AsyncIterator[tuple[str, dict[str, Any]]]:
            current = conversation_id
            async for kind, data in run_turn(conversation_id, text, interface):
                if kind in ("conversation", "complete") and data.get("conversation_id"):
                    current = data["conversation_id"]
                if kind == "complete":
                    body = _response_object(response_id, model, str(data.get("reply") or ""), "completed", created,
                                            request.previous_response_id, current)
                    store.bind_thread("responses:" + response_id, current, json.dumps(body))
                    yield "done", body
                    return
                if kind == "error":
                    body = _response_object(response_id, model, None, "failed", created,
                                            request.previous_response_id, current, str(data.get("message") or data))
                    yield "failed", body
                    return
                yield kind, data

        if not request.stream:
            async for kind, body in turn():
                if kind == "done":
                    return body
                if kind == "failed":
                    return JSONResponse(body, status_code=502)
            raise HTTPException(502, "The assistant stopped without a reply.")

        async def events() -> AsyncIterator[str]:
            sequence = 0

            def event(kind: str, **payload: Any) -> str:
                nonlocal sequence
                sequence += 1
                return f"event: {kind}\ndata: {json.dumps({'type': kind, 'sequence_number': sequence, **payload})}\n\n"

            started = _response_object(response_id, model, None, "in_progress", created,
                                       request.previous_response_id, conversation_id)
            yield event("response.created", response=started)
            yield event("response.in_progress", response=started)
            async for kind, body in turn():
                if kind == "done":
                    item = body["output"][0]
                    reply = body["output_text"]
                    yield event("response.output_item.added", output_index=0,
                                item={**item, "status": "in_progress", "content": []})
                    yield event("response.content_part.added", output_index=0, item_id=item["id"], content_index=0,
                                part={"type": "output_text", "text": "", "annotations": []})
                    yield event("response.output_text.delta", output_index=0, item_id=item["id"], content_index=0,
                                delta=reply)
                    yield event("response.output_text.done", output_index=0, item_id=item["id"], content_index=0,
                                text=reply)
                    yield event("response.content_part.done", output_index=0, item_id=item["id"], content_index=0,
                                part=item["content"][0])
                    yield event("response.output_item.done", output_index=0, item=item)
                    yield event("response.completed", response=body)
                    return
                if kind == "failed":
                    yield event("response.failed", response=body)
                    return
                # progress: keep the stream alive with a comment line (not an OpenAI event)
                yield f": {json.dumps(body)[:300]}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @router.get("/v1/responses/{response_id}", dependencies=[Depends(require_token)])
    def get_response(response_id: str):
        thread = _conversations().thread("responses:" + response_id)
        if thread is None or not thread.get("body"):
            raise HTTPException(404, f"No response with id {response_id!r}")
        return json.loads(thread["body"])

    return router


# ---------------------------------------------------------------------------
# A2A (Agent2Agent), protocol 1.0 with 0.3 compatibility
# ---------------------------------------------------------------------------

A2A_RPC_PATH = "/a2a"


def _public_base(request) -> str:
    """How callers reach this app: through Herald's relay when they came that way."""
    headers = request.headers
    host = headers.get("x-forwarded-host")
    if host:
        return f"{headers.get('x-forwarded-proto', 'http')}://{host}{headers.get('x-forwarded-prefix', '')}"
    return str(request.base_url).rstrip("/")


def mount_a2a(app: FastAPI, token_ok) -> bool:
    """Serve the assistant as an A2A agent. Returns False when a2a-sdk is absent."""
    try:
        from a2a.helpers.proto_helpers import new_task_from_user_message
        from a2a.server.agent_execution import AgentExecutor
        from a2a.server.request_handlers import DefaultRequestHandler
        from a2a.server.routes.agent_card_routes import create_agent_card_routes
        from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
        from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
        from a2a.types import (AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Part, TaskState)
        from a2a.utils.constants import PROTOCOL_VERSION_1_0
    except ImportError:
        LOG.info("a2a-sdk not installed; the assistant is not offered over A2A")
        return False

    class AssistantExecutor(AgentExecutor):
        async def execute(self, context, event_queue) -> None:
            task = context.current_task
            if task is None:
                task = new_task_from_user_message(context.message)
                await event_queue.enqueue_event(task)
            updater = TaskUpdater(event_queue, task.id, task.context_id)
            await updater.start_work()
            store = _conversations()
            key = "a2a:" + task.context_id
            thread = store.thread(key)
            conversation_id = thread["conversation_id"] if thread else str(uuid.uuid4())
            async for kind, data in run_turn(conversation_id, context.get_user_input(), A2A_INTERFACE):
                if kind == "conversation":
                    conversation_id = data["conversation_id"]
                elif kind == "progress" and data.get("label"):
                    await updater.update_status(TaskState.TASK_STATE_WORKING, message=updater.new_agent_message(
                        [Part(text=str(data["label"]))]))
                elif kind == "complete":
                    store.bind_thread(key, data.get("conversation_id") or conversation_id)
                    reply = str(data.get("reply") or "")
                    await updater.add_artifact([Part(text=reply)], name="reply")
                    await updater.complete(updater.new_agent_message([Part(text=reply)]))
                    return
                elif kind == "error":
                    await updater.failed(updater.new_agent_message([Part(text=str(data.get("message") or data))]))
                    return
            await updater.failed(updater.new_agent_message([Part(text="The assistant stopped without a reply.")]))

        async def cancel(self, context, event_queue) -> None:
            # The assistant finishes and saves the turn; the caller stops waiting.
            task = context.current_task
            if task is not None:
                await TaskUpdater(event_queue, task.id, task.context_id).cancel()

    card = AgentCard(
        name="Ultimate Assistant",
        description="The user's personal assistant: their memory, conversations, devices and tools.",
        version="1",
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"], default_output_modes=["text/plain"],
        skills=[AgentSkill(id="assistant", name="Ask the assistant",
                           description="Anything the user's assistant can do: questions, their schedule and "
                                       "tasks, their notes, operating their devices.",
                           tags=["assistant", "personal"])],
    )

    async def with_url(base_card):
        request = _current_request.get(None)
        url = (_public_base(request) if request is not None else "") + A2A_RPC_PATH
        updated = AgentCard()
        updated.CopyFrom(base_card)
        del updated.supported_interfaces[:]
        updated.supported_interfaces.append(AgentInterface(url=url, protocol_binding="JSONRPC",
                                                           protocol_version=PROTOCOL_VERSION_1_0))
        return updated

    handler = DefaultRequestHandler(agent_executor=AssistantExecutor(), task_store=InMemoryTaskStore(), agent_card=card)
    card_routes = create_agent_card_routes(card, card_modifier=with_url)
    rpc_routes = create_jsonrpc_routes(handler, rpc_url=A2A_RPC_PATH, enable_v0_3_compat=True)

    @app.middleware("http")
    async def a2a_guard(request: Request, call_next):
        path = request.url.path
        if path == A2A_RPC_PATH and not token_ok(request.headers.get("x-ultimate-assistant-token")):
            return JSONResponse({"detail": "App session token is missing or invalid."}, status_code=403)
        if path in (A2A_RPC_PATH, card_routes[0].path):
            reset = _current_request.set(request)
            try:
                return await call_next(request)
            finally:
                _current_request.reset(reset)
        return await call_next(request)

    for route in (*card_routes, *rpc_routes):
        app.router.routes.append(route)
    return True
