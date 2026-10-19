"""The assistant over the Agent Client Protocol (ACP).

Apps that embed agents over ACP (Obsidian's Agent Client plugin, Zed,
JetBrains...) launch this as a stdio subprocess on whatever device the user
is on. It talks to the assistant's main node through Herald
(``/nodes/main/http``), so the app gets the one assistant -- same memory,
conversations and tools -- not a separate one.

    ACP session      = a conversation (the main node's /api/conversations)
    session/new      -> a fresh conversation id
    session/list     -> GET /api/conversations
    session/load     -> replays that conversation, then continues it
    session/resume   -> continues it without replay
    session/prompt   -> POST /api/chat; progress -> thought updates

When a message changes the objective the main node moves it to another
conversation; this session follows it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from typing import Any
from urllib.parse import quote

import httpx

LOG = logging.getLogger("ultimate_assistant.acp")


class AssistantError(RuntimeError):
    pass


class MainNode:
    """The few assistant API calls ACP needs, through Herald's relay."""

    def __init__(self, url: str, api_key: str | None = None) -> None:
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def _get(self, path: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=60.0) as http:
            response = await http.get(self.url + path, headers=self.headers)
        if response.status_code >= 400:
            raise AssistantError(_detail(response.status_code, response.text))
        return response.json()

    async def conversations(self) -> list[dict[str, Any]]:
        return (await self._get("/api/conversations")).get("conversations", [])

    async def messages(self, conversation_id: str) -> list[dict[str, Any]]:
        return (await self._get(f"/api/conversations/{quote(conversation_id, safe='')}")).get("messages", [])

    async def chat(self, conversation_id: str, message: str, interface: str):
        """Yield (event, data) from the assistant's chat stream."""
        body = {"message": message, "conversation_id": conversation_id, "interface": interface}
        timeout = httpx.Timeout(connect=15, read=None, write=30, pool=15)
        async with httpx.AsyncClient(timeout=timeout) as http:
            async with http.stream("POST", self.url + "/api/chat", json=body, headers=self.headers) as response:
                if response.status_code >= 400:
                    raise AssistantError(_detail(response.status_code, (await response.aread()).decode(errors="replace")))
                kind = None
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        kind = line[6:].strip()
                    elif line.startswith("data:") and kind:
                        try:
                            yield kind, json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            pass
                        kind = None


def _detail(status: int, text: str) -> str:
    try:
        detail = json.loads(text).get("detail")
    except (ValueError, AttributeError):
        detail = text[:300]
    if status == 503:
        return f"The assistant's main node is not connected to Herald right now ({detail})."
    return f"Assistant {status}: {detail}"


def prompt_text(blocks: list[Any]) -> str:
    """Flatten ACP prompt content: text as-is, attached files with their path."""
    parts: list[str] = []
    for block in blocks or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            parts.append(block.text)
        elif kind == "resource":
            text = getattr(block.resource, "text", None)
            if text is not None:
                parts.append(f"[Attached: {block.resource.uri}]\n{text}")
        elif kind == "resource_link":
            parts.append(f"[Linked: {block.uri}]")
    return "\n\n".join(part for part in parts if part).strip()


def interface_text(client: Any, cwd: str | None, guidance: str) -> str:
    """Tell the assistant where the user is talking from, for this turn."""
    name = (getattr(client, "title", None) or getattr(client, "name", None) or "an editor") if client else "an editor"
    lines = [f"The user is talking to you from inside {name}, through its built-in agent chat."]
    if cwd:
        lines.append(f"Its workspace folder is: {cwd}")
    if guidance.strip():
        lines.append(guidance.strip())
    return "\n".join(lines)


def build_agent(main: MainNode, guidance: str = ""):
    """The ACP Agent, built lazily so `acp` is only imported when used."""
    from acp import (
        PROTOCOL_VERSION, Agent, InitializeResponse, LoadSessionResponse, NewSessionResponse,
        PromptResponse, update_agent_message_text, update_agent_thought_text, update_user_message_text,
    )
    from acp.schema import (
        AgentCapabilities, Implementation, ListSessionsResponse, PromptCapabilities, ResumeSessionResponse,
        SessionCapabilities, SessionInfo, SessionListCapabilities, SessionResumeCapabilities,
    )

    class AssistantACPAgent(Agent):
        def __init__(self) -> None:
            self.conn = None
            self.client = None
            self.cwd: dict[str, str] = {}
            # ACP session id -> the conversation it is in now (objective switches move it).
            self.current: dict[str, str] = {}
            self.cancelled: set[str] = set()

        def on_connect(self, conn) -> None:
            self.conn = conn

        async def initialize(self, protocol_version: int, client_capabilities=None,
                             client_info=None, **kwargs: Any) -> InitializeResponse:
            self.client = client_info
            return InitializeResponse(
                protocol_version=PROTOCOL_VERSION,
                agent_capabilities=AgentCapabilities(
                    load_session=True,
                    prompt_capabilities=PromptCapabilities(image=False, audio=False, embedded_context=True),
                    session_capabilities=SessionCapabilities(
                        list=SessionListCapabilities(), resume=SessionResumeCapabilities()),
                ),
                agent_info=Implementation(name="ultimate-assistant", title="Ultimate Assistant", version="1"),
            )

        async def new_session(self, cwd: str, additional_directories=None, mcp_servers=None,
                              **kwargs: Any) -> NewSessionResponse:
            session_id = str(uuid.uuid4())
            self.cwd[session_id] = cwd
            return NewSessionResponse(session_id=session_id)

        async def list_sessions(self, cwd: str | None = None, cursor: str | None = None,
                                **kwargs: Any) -> ListSessionsResponse:
            rows = list(reversed(await main.conversations()))  # newest first
            return ListSessionsResponse(sessions=[
                SessionInfo(session_id=row["id"], cwd=cwd or os.getcwd(),
                            title=row.get("title") or "Conversation", updated_at=row.get("last_at"))
                for row in rows
            ])

        async def load_session(self, cwd: str, session_id: str, mcp_servers=None,
                               additional_directories=None, **kwargs: Any) -> LoadSessionResponse:
            self.cwd[session_id] = cwd
            for message in await main.messages(session_id):
                text = str(message.get("content", ""))
                update = update_user_message_text(text) if message.get("role") == "user" else update_agent_message_text(text)
                await self.conn.session_update(session_id, update)
            return LoadSessionResponse()

        async def resume_session(self, session_id: str, cwd: str, additional_directories=None,
                                 mcp_servers=None, **kwargs: Any) -> ResumeSessionResponse:
            self.cwd[session_id] = cwd
            return ResumeSessionResponse()

        async def cancel(self, session_id: str, **kwargs: Any) -> None:
            # The main node finishes the turn and saves it; the app just stops waiting.
            self.cancelled.add(session_id)

        async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
            text = prompt_text(prompt)
            if not text:
                return PromptResponse(stop_reason="end_turn")
            self.cancelled.discard(session_id)
            conversation = self.current.get(session_id, session_id)
            interface = interface_text(self.client, self.cwd.get(session_id), guidance)
            say = lambda update: self.conn.session_update(session_id, update)  # noqa: E731
            try:
                async for kind, data in main.chat(conversation, text, interface):
                    if session_id in self.cancelled:
                        return PromptResponse(stop_reason="cancelled")
                    if kind == "conversation":
                        self.current[session_id] = data["conversation_id"]
                        note = ("New conversation" if data.get("kind") == "new"
                                else f"Back to: {data.get('title') or 'an earlier conversation'}")
                        await say(update_agent_thought_text(f"{note} — {data.get('reason') or 'objective changed'}\n"))
                    elif kind == "ack" and data.get("text"):
                        # The immediate first line; the full answer follows in the same message.
                        await say(update_agent_message_text(str(data["text"]) + "\n\n"))
                    elif kind == "progress" and data.get("label"):
                        await say(update_agent_thought_text(str(data["label"]) + "\n"))
                    elif kind == "complete":
                        await say(update_agent_message_text(str(data.get("reply") or "")))
                        return PromptResponse(stop_reason="end_turn")
                    elif kind == "error":
                        await say(update_agent_message_text(f"The assistant could not finish this: {data}"))
                        return PromptResponse(stop_reason="end_turn")
            except (AssistantError, httpx.HTTPError) as exc:
                await say(update_agent_message_text(str(exc) or "Could not reach the assistant."))
                return PromptResponse(stop_reason="end_turn")
            await say(update_agent_message_text("The assistant stopped without a reply."))
            return PromptResponse(stop_reason="end_turn")

    return AssistantACPAgent()


def _main_url(explicit: str | None) -> str:
    if explicit:
        return explicit
    from .settings import settings
    return settings.herald_url.rstrip("/") + "/nodes/main/http"


async def _run(args: argparse.Namespace) -> None:
    from acp import run_agent
    from .node_link import load_router_key
    load_router_key()
    guidance = ""
    if args.interface_file:
        with open(args.interface_file, encoding="utf-8") as handle:
            guidance = handle.read()
    await run_agent(build_agent(MainNode(_main_url(args.main), os.environ.get("HERALD_API_KEY")), guidance))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve the assistant over ACP (stdio)")
    parser.add_argument("--main", help="the main node's API (default: <HERALD_URL>/nodes/main/http)")
    parser.add_argument("--interface-file", help="guidance for behaving inside this app (e.g. the Obsidian module's)")
    args = parser.parse_args(argv)
    # stdout carries the protocol; logs go to stderr only.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
