"""Encrypted named memory sessions for simple, stateful package agents."""
from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from herald.router.secret_vault import SecretVault


DEFAULT_DB_PATH = Path.home() / ".herald" / "agent_sessions.db"
DEFAULT_VAULT_ROOT = Path.home() / ".herald" / "agent_vault"
NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
RECENT_TURNS = 8
LEDGER_ENTRIES = 40
MEMORY_PROMPT_CHARS = 40_000


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class AgentSession:
    id: str
    scope_key: str
    name: str
    memory: str
    model: str
    mode: str
    project: str | None
    part: str | None
    agentic: bool
    state_ref: str
    turn_count: int
    created_at: str
    updated_at: str

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "memory": self.memory,
            "model": self.model,
            "mode": self.mode,
            "project": self.project,
            "part": self.part,
            "agentic": self.agentic,
            "turn_count": self.turn_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class AgentSessionStore:
    """SQLite metadata plus encrypted conversation state in ``SecretVault``."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH, vault: SecretVault | None = None) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # Agent memory has its own key namespace. This avoids coupling ordinary
        # script memory to credential/capture artifacts and allows independent
        # backup, rotation, and recovery policies.
        self.vault = vault or SecretVault(DEFAULT_VAULT_ROOT)
        self._lock = threading.RLock()
        with closing(self._connect()) as conn, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    id TEXT PRIMARY KEY, scope_key TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL, memory TEXT NOT NULL, model TEXT NOT NULL,
                    mode TEXT NOT NULL, instructions TEXT NOT NULL,
                    project TEXT, part TEXT, agentic INTEGER NOT NULL,
                    state_ref TEXT NOT NULL, turn_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _scope_key(project: str | None, part: str | None, name: str, memory: str) -> str:
        return "\x1f".join((project or "", part or "", name, memory))

    def open(
        self, *, name: str, memory: str, model: str, mode: str,
        instructions: str = "", project: str | None = None,
        part: str | None = None, agentic: bool = True,
        recent_turns: int = RECENT_TURNS, ledger_entries: int = LEDGER_ENTRIES,
        retention_days: int | None = None,
    ) -> AgentSession:
        if not NAME_PATTERN.fullmatch(name):
            raise ValueError("agent name must start with a letter/underscore and contain only letters, numbers, '.', '-', or '_'")
        if not NAME_PATTERN.fullmatch(memory):
            raise ValueError("memory name must start with a letter/underscore and contain only letters, numbers, '.', '-', or '_'")
        if bool(project) != bool(part):
            raise ValueError("project and part must be supplied together")
        scope_key = self._scope_key(project, part, name, memory)
        with self._lock:
            existing = self.get_by_scope(scope_key)
            if existing:
                # Keep the originally resolved model pinned to this memory,
                # while allowing edited instructions/settings to take effect
                # when the same named agent is reopened by a later script run.
                state = self._state(existing)
                state["instructions"] = instructions
                state["settings"] = self._settings(recent_turns, ledger_entries, retention_days)
                self.vault.put_json(
                    f"agents/{existing.id}/memory", state,
                    metadata={"kind": "agent-memory", "session_id": existing.id},
                )
                with closing(self._connect()) as conn, conn:
                    conn.execute(
                        "UPDATE agent_sessions SET mode=?, instructions=?, agentic=?, updated_at=? WHERE id=?",
                        (mode, "", int(agentic), _now(), existing.id),
                    )
                return self.require(existing.id)
            session_id, now = uuid.uuid4().hex, _now()
            state_ref = self.vault.put_json(
                f"agents/{session_id}/memory",
                {"instructions": instructions, "ledger": [], "turns": [],
                 "responses": {},
                 "settings": self._settings(recent_turns, ledger_entries, retention_days)},
                metadata={"kind": "agent-memory", "session_id": session_id},
            )
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "INSERT INTO agent_sessions "
                    "(id,scope_key,name,memory,model,mode,instructions,project,part,agentic,state_ref,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (session_id, scope_key, name, memory, model, mode, "",
                     project, part, int(agentic), state_ref, now, now),
                )
            return self.require(session_id)

    def get(self, session_id: str) -> AgentSession | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM agent_sessions WHERE id=?", (session_id,)).fetchone()
        return self._row(row) if row else None

    def get_by_scope(self, scope_key: str) -> AgentSession | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM agent_sessions WHERE scope_key=?", (scope_key,)).fetchone()
        return self._row(row) if row else None

    def search(
        self, query: str, *, project: str | None = None, part: str | None = None,
        since: str | None = None, until: str | None = None, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search session turn content (prompts/responses) by substring.

        Turn content lives encrypted in the vault, not as plaintext SQLite
        columns -- there's no SQL-level full-text index to query directly.
        Cheap metadata filters (project/part/date range) run first via SQL to
        narrow the candidate set, then each candidate session's state is
        decrypted and its turns scanned in-process. Fine at personal scale;
        would need a different approach (an encrypted-at-rest search index)
        if session volume ever grew large enough for full decryption on every
        search to become slow.
        """
        needle = query.lower()
        clauses, params = [], []
        if project:
            clauses.append("project = ?")
            params.append(project)
        if part:
            clauses.append("part = ?")
            params.append(part)
        if since:
            clauses.append("updated_at >= ?")
            params.append(since)
        if until:
            clauses.append("updated_at <= ?")
            params.append(until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM agent_sessions {where} ORDER BY updated_at DESC", params,
            ).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            session = self._row(row)
            try:
                state = self._state(session)
            except Exception:
                continue  # damaged/inaccessible session -- skip, don't fail the whole search
            turns = state.get("turns") or []
            for index, turn in enumerate(turns):
                turn_input = str(turn.get("input", ""))
                turn_output = str(turn.get("output", ""))
                if needle in turn_input.lower() or needle in turn_output.lower():
                    results.append({
                        "session_id": session.id, "session_name": session.name,
                        "project": session.project, "part": session.part,
                        "turn_index": index, "input": turn_input, "output": turn_output,
                        "updated_at": session.updated_at,
                    })
                    if len(results) >= limit:
                        return results
        return results

    def list(self, limit: int = 100) -> list[AgentSession]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM agent_sessions ORDER BY updated_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def memory_prompt(self, session_id: str) -> str:
        session = self.require(session_id)
        state = self._state(session)
        self._check_expiry(session, state)
        settings = state.get("settings") or {}
        recent_limit = int(settings.get("recent_turns", RECENT_TURNS))
        ledger_limit = int(settings.get("ledger_entries", LEDGER_ENTRIES))
        sections: list[str] = []
        ledger = state.get("ledger") or []
        if ledger:
            sections.append("Earlier decision ledger:\n" + "\n".join(str(item) for item in ledger[-ledger_limit:]))
        turns = state.get("turns") or []
        if turns:
            rendered = []
            for turn in turns[-recent_limit:]:
                rendered.append(
                    f"Earlier input:\n{turn.get('input', '')}\n"
                    f"Earlier response:\n{turn.get('output', '')}"
                )
            sections.append("Recent exchanges:\n" + "\n\n".join(rendered))
        return "\n\n".join(sections)[-MEMORY_PROMPT_CHARS:]

    def instructions(self, session_id: str) -> str:
        """Return permanent instructions from encrypted state, never metadata."""
        return str(self._state(self.require(session_id)).get("instructions") or "")

    def append(self, session_id: str, input_text: str, output_text: str,
               *, request_id: str | None = None) -> AgentSession:
        with self._lock:
            session = self.require(session_id)
            session_id = session.id
            state = self._state(session)
            turns = list(state.get("turns") or [])
            ledger = list(state.get("ledger") or [])
            settings = state.get("settings") or {}
            recent_limit = int(settings.get("recent_turns", RECENT_TURNS))
            ledger_limit = int(settings.get("ledger_entries", LEDGER_ENTRIES))
            turns.append({"input": input_text[-12_000:], "output": output_text[-12_000:]})
            while len(turns) > recent_limit:
                old = turns.pop(0)
                old_input = " ".join(str(old.get("input", "")).split())[:400]
                old_output = " ".join(str(old.get("output", "")).split())[:600]
                ledger.append(f"Input: {old_input} | Result: {old_output}")
            responses = dict(state.get("responses") or {})
            if request_id:
                responses[request_id] = output_text
                responses = dict(list(responses.items())[-100:])
            state = {"instructions": state.get("instructions", ""),
                     "ledger": ledger[-ledger_limit:], "turns": turns,
                     "responses": responses, "settings": settings}
            reference = self.vault.put_json(
                f"agents/{session_id}/memory", state,
                metadata={"kind": "agent-memory", "session_id": session_id},
            )
            now = _now()
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "UPDATE agent_sessions SET state_ref=?, turn_count=turn_count+1, updated_at=? WHERE id=?",
                    (reference, now, session_id),
                )
            return self.require(session_id)

    def reset(self, session_id: str, *, force: bool = False,
              instructions: str = "") -> AgentSession:
        with self._lock:
            session = self.require(session_id)
            session_id = session.id
            if force:
                state = {"instructions": instructions, "settings": {}}
            else:
                state = self._state(session)
            reference = self.vault.put_json(
                f"agents/{session_id}/memory",
                {"instructions": state.get("instructions", ""), "ledger": [], "turns": [],
                 "responses": {}, "settings": state.get("settings") or {}},
                metadata={"kind": "agent-memory", "session_id": session_id},
            )
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "UPDATE agent_sessions SET state_ref=?, turn_count=0, updated_at=? WHERE id=?",
                    (reference, _now(), session_id),
                )
            return self.require(session_id)

    def cached_response(self, session_id: str, request_id: str | None) -> str | None:
        if not request_id:
            return None
        value = (self._state(self.require(session_id)).get("responses") or {}).get(request_id)
        return str(value) if value is not None else None

    def inspect(self, session_id: str) -> dict[str, Any]:
        session = self.require(session_id)
        try:
            state = self._state(session)
            return {
                **session.public(),
                "status": "expired" if self._expired(session, state) else "ready",
                "recent_turns": len(state.get("turns") or []),
                "ledger_entries": len(state.get("ledger") or []),
                "settings": state.get("settings") or {},
            }
        except RuntimeError as exc:
            return {**session.public(), "status": "inaccessible", "error": str(exc)}

    def export(self, session_id: str) -> dict[str, Any]:
        session = self.require(session_id)
        return {"version": 1, "session": session.public(), "state": self._state(session)}

    def import_state(self, session_id: str, value: dict[str, Any]) -> AgentSession:
        state = value.get("state") if "state" in value else value
        if not isinstance(state, dict) or not isinstance(state.get("turns", []), list):
            raise ValueError("invalid agent memory export")
        session = self.require(session_id)
        session_id = session.id
        self.vault.put_json(
            f"agents/{session_id}/memory", state,
            metadata={"kind": "agent-memory", "session_id": session_id},
        )
        count = len(state.get("turns") or []) + len(state.get("ledger") or [])
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE agent_sessions SET turn_count=?, updated_at=? WHERE id=?",
                (count, _now(), session_id),
            )
        return self.require(session_id)

    def delete(self, session_id: str) -> bool:
        try:
            session = self.require(session_id)
        except KeyError:
            return False
        session_id = session.id
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM agent_sessions WHERE id=?", (session_id,))
        self.vault.remove(session.state_ref.removeprefix("vault:"))
        return True

    def require(self, session_id: str) -> AgentSession:
        session = self.get(session_id)
        # Prefix fallback below is for a human-typed truncated ID, not a
        # supported shorthand -- every real caller passes a full ID straight
        # through from an API request. IDs are uuid4().hex (32 chars); a
        # 6-char threshold made an accidental collision on a wrong/corrupted
        # ID variable plausible and silent (wrong session, no error). 24
        # chars (75% of the ID) keeps the human-typo case working while
        # making an accidental match statistically impossible.
        if session is None and len(session_id) >= 24:
            with closing(self._connect()) as conn:
                rows = conn.execute(
                    "SELECT * FROM agent_sessions WHERE id LIKE ? LIMIT 2",
                    (f"{session_id}%",),
                ).fetchall()
            if len(rows) == 1:
                session = self._row(rows[0])
        if session is None:
            raise KeyError(session_id)
        return session

    def _state(self, session: AgentSession) -> dict[str, Any]:
        state = self.vault.get_json(session.state_ref.removeprefix("vault:"))
        if not isinstance(state, dict):
            raise RuntimeError("agent memory artifact is invalid")
        # Migrate the short-lived development schema that placed instructions
        # in SQLite before this feature was released.
        if "instructions" not in state:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT instructions FROM agent_sessions WHERE id=?", (session.id,),
                ).fetchone()
            state["instructions"] = str(row["instructions"] or "") if row else ""
            self.vault.put_json(
                f"agents/{session.id}/memory", state,
                metadata={"kind": "agent-memory", "session_id": session.id},
            )
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "UPDATE agent_sessions SET instructions='' WHERE id=?", (session.id,),
                )
        return state

    @staticmethod
    def _settings(recent_turns: int, ledger_entries: int,
                  retention_days: int | None) -> dict[str, Any]:
        if not 1 <= recent_turns <= 100 or not 1 <= ledger_entries <= 1000:
            raise ValueError("recent_turns must be 1..100 and ledger_entries must be 1..1000")
        if retention_days is not None and not 1 <= retention_days <= 3650:
            raise ValueError("retention_days must be 1..3650")
        return {"recent_turns": recent_turns, "ledger_entries": ledger_entries,
                "retention_days": retention_days}

    @staticmethod
    def _expired(session: AgentSession, state: dict[str, Any]) -> bool:
        days = (state.get("settings") or {}).get("retention_days")
        if not days:
            return False
        expires = datetime.fromisoformat(session.updated_at) + timedelta(days=int(days))
        return datetime.now(UTC) > expires

    def _check_expiry(self, session: AgentSession, state: dict[str, Any]) -> None:
        if self._expired(session, state):
            raise RuntimeError("agent memory retention period has expired; reset or delete it")

    @staticmethod
    def _row(row: sqlite3.Row) -> AgentSession:
        return AgentSession(
            id=row["id"], scope_key=row["scope_key"], name=row["name"],
            memory=row["memory"], model=row["model"], mode=row["mode"],
            project=row["project"], part=row["part"],
            agentic=bool(row["agentic"]), state_ref=row["state_ref"],
            turn_count=row["turn_count"], created_at=row["created_at"], updated_at=row["updated_at"],
        )
