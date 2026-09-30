"""Self-improving capability drafting -- roadmap #5/#9.

When a tool-execution error indicates Herald is simply missing a capability
(not misconfigured, not a transient failure), draft a fix instead of just
failing: detect the gap, ask Herald's own model router to write a tool
implementation, sandbox-test it in an isolated subprocess, static-scan it
for dangerous patterns, and queue the result as a reviewable proposal.
Nothing here auto-deploys -- a human reviews and manually wires an approved
draft into coding_tools.py.

Pattern mirrors the shape (not the code -- that's Pydantic/different
framework) of the user's own ai-kernel project's
kernel/analysis/capability_analyzer.py: pattern-match an error string to a
CapabilityGap, then act on it.

Deliberately NOT done in this pass (follow-up, not oversight):
  - wiring gap-detection into every tool_executor.py call site (a bigger
    integration decision -- this pipeline is callable but not yet automatic)
  - auto-deploying an approved draft into coding_tools.py
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from herald.router.storage_paths import database_path
from typing import Any

from herald.router import event_bus

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = database_path(
    "capabilities.db",
    env_var="HERALD_CAPABILITIES_DB",
    legacy_path=Path(__file__).resolve().parent / "capabilities.db",
)

# Herald's real "missing tool" error shapes, found in the actual codebase
# (not assumed from ai-kernel's regexes, which target a different error
# format): server.py's callable-tool lookup and tool_registry.py's bound-tool
# lookup both produce "'<name>' not found" phrasing.
_MISSING_TOOL_PATTERNS = [
    re.compile(r"callable tool '([^']+)' not found"),
    re.compile(r"tool instance '([^']+)' not found"),
    re.compile(r"^unsupported transport '([^']+)'$"),
]

GAP_MISSING_TOOL = "missing_tool"
GAP_UNSUPPORTED_TRANSPORT = "unsupported_transport"
GAP_RESEARCH_IMPROVEMENT = "research_improvement"

RISK_CRITICAL = "CRITICAL"
RISK_HIGH = "HIGH"
RISK_MEDIUM = "MEDIUM"
RISK_LOW = "LOW"

_CRITICAL_PATTERNS = [r"\beval\(", r"\bexec\(", r"\bos\.system\(", r"\b__import__\("]
_HIGH_PATTERNS = [r"\bsocket\b", r"\burllib\b", r"\brequests\b", r"\bhttpx\b", r"\bsubprocess\b"]
_MEDIUM_PATTERNS = [r"\bshutil\.rmtree\(", r"\bos\.remove\(", r"\bos\.unlink\(", r"open\([^)]*[\"']w"]


@dataclass
class CapabilityGap:
    gap_type: str
    details: dict[str, Any]
    severity: str  # "critical" | "high" | "medium" | "low"
    recommended_action: str


def detect_gap(error: str, context: dict[str, Any] | None = None) -> CapabilityGap | None:
    """Pattern-match a Herald error string to a capability gap. Best-effort,
    not exhaustive -- returns None for errors that don't match a known
    "we're missing something, not just broken" shape."""
    context = context or {}
    for pattern in _MISSING_TOOL_PATTERNS[:2]:
        match = pattern.search(error)
        if match:
            tool_name = match.group(1)
            return CapabilityGap(
                gap_type=GAP_MISSING_TOOL,
                details={"tool_name": tool_name, "error": error, "context": context},
                severity="high",
                recommended_action="draft_tool",
            )
    match = _MISSING_TOOL_PATTERNS[2].search(error)
    if match:
        return CapabilityGap(
            gap_type=GAP_UNSUPPORTED_TRANSPORT,
            details={"transport": match.group(1), "error": error, "context": context},
            severity="medium",
            recommended_action="draft_transport_adapter",
        )
    return None


@dataclass
class SandboxResult:
    ok: bool
    stdout: str
    stderr: str
    exit_code: int | None


def sandbox_test(source: str, *, timeout: float = 15.0) -> SandboxResult:
    """Run drafted tool source in an isolated subprocess: import it fresh
    and confirm it doesn't raise on import. Process isolation only -- not a
    real sandbox/container, deliberately out of scope for this pass.

    draft_tool()/draft_improvement() both prompt the model to write code
    assuming `mcp = FastMCP(...)` already exists in scope (matching
    coding_tools.py's house style, where the drafted function is meant to
    be pasted in) -- so the probe injects a stub `mcp` object with a
    no-op `.tool()` decorator before exec'ing the draft. Without this,
    every single draft fails sandbox_ok=False on the decorator alone
    (`NameError: name 'mcp' is not defined'`), regardless of whether the
    actual logic is correct -- confirmed empirically, this was silently
    undermining every prior proposal's sandbox result.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        module_path = Path(tmpdir) / "draft_tool.py"
        module_path.write_text(source, encoding="utf-8")
        probe = (
            f"import importlib.util, sys, types\n"
            f"stub_mcp = types.SimpleNamespace(tool=lambda *a, **k: (lambda fn: fn))\n"
            f"spec = importlib.util.spec_from_file_location('draft_tool', r'{module_path}')\n"
            f"mod = importlib.util.module_from_spec(spec)\n"
            f"mod.mcp = stub_mcp\n"
            f"sys.modules['draft_tool'] = mod\n"
            f"spec.loader.exec_module(mod)\n"
            f"print('IMPORT_OK')\n"
        )
        try:
            proc = subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True, text=True, timeout=timeout, cwd=tmpdir,
            )
        except subprocess.TimeoutExpired as exc:
            return SandboxResult(ok=False, stdout=exc.stdout or "", stderr=f"sandbox timed out after {timeout}s", exit_code=None)
        ok = proc.returncode == 0 and "IMPORT_OK" in proc.stdout
        return SandboxResult(ok=ok, stdout=proc.stdout[-4000:], stderr=proc.stderr[-4000:], exit_code=proc.returncode)


@dataclass
class RiskScan:
    level: str
    findings: list[str] = field(default_factory=list)


def risk_scan(source: str) -> RiskScan:
    """Grep-style pattern scan over drafted source for dangerous
    constructs. Not real static analysis -- best-effort triage so a human
    reviewer knows what to look at first."""
    findings: list[str] = []
    level = RISK_LOW

    for pattern in _CRITICAL_PATTERNS:
        if re.search(pattern, source):
            findings.append(f"critical pattern: {pattern}")
            level = RISK_CRITICAL
    if level != RISK_CRITICAL:
        for pattern in _HIGH_PATTERNS:
            if re.search(pattern, source):
                findings.append(f"network/process pattern: {pattern}")
                level = RISK_HIGH
    if level not in (RISK_CRITICAL, RISK_HIGH):
        for pattern in _MEDIUM_PATTERNS:
            if re.search(pattern, source):
                findings.append(f"filesystem-write pattern: {pattern}")
                level = RISK_MEDIUM

    return RiskScan(level=level, findings=findings)


def draft_tool(gap: CapabilityGap) -> str:
    """Ask Herald's own model router to draft a tool implementation for a
    missing-tool gap, following coding_tools.py's @mcp.tool() house style."""
    from herald import _get_client

    tool_name = gap.details.get("tool_name", "unknown_tool")
    prompt = (
        f"Herald (a self-hosted AI agent router) hit a missing-tool error: "
        f"a session tried to call a tool named '{tool_name}' that doesn't exist.\n\n"
        f"Error context: {gap.details.get('error', '')}\n\n"
        "Write a single Python function implementing a plausible tool named "
        f"'{tool_name}', following this house style (from herald/coding_tools.py):\n\n"
        "```python\n"
        "@mcp.tool()\n"
        f"def {tool_name}(...) -> str:\n"
        '    """One-line summary.\n\n'
        "    Args:\n"
        "        ...\n\n"
        "    Returns:\n"
        "        ...\n"
        '    """\n'
        "    ...\n"
        "```\n\n"
        "Assume `from mcp.server.fastmcp import FastMCP` and `mcp = FastMCP(...)` "
        "already exist in scope. Return ONLY the Python code, no prose, no markdown fences. "
        "Keep it self-contained -- only import from the Python standard library."
    )
    client = _get_client()
    response = client.chat(prompt, model=None)
    # Strip markdown fences if the model added them despite instructions.
    code = response.strip()
    code = re.sub(r"^```(?:python)?\n", "", code)
    code = re.sub(r"\n```$", "", code)
    return code


def draft_improvement(description: str, *, tool_name: str, source_context: str = "") -> str:
    """Ask Herald's own model router to draft a tool implementation for a
    PROACTIVELY researched improvement (not a reactive missing-tool error).
    Same house style and same downstream sandbox/risk-scan/store pipeline
    as draft_tool(), just a broader prompt since there's no error string to
    anchor on -- the model is given a free-text description of what was
    found and why it's worth adding."""
    from herald import _get_client

    prompt = (
        f"Herald (a self-hosted AI agent router) runs a periodic research pass "
        f"looking for real, adoptable improvements from other open-source "
        f"projects and ecosystems. This one was found:\n\n{description}\n\n"
        f"{f'Source context: {source_context}' + chr(10) + chr(10) if source_context else ''}"
        "Write a single Python function implementing this as a new Herald tool, "
        f"named '{tool_name}', following this house style (from herald/coding_tools.py):\n\n"
        "```python\n"
        "@mcp.tool()\n"
        f"def {tool_name}(...) -> str:\n"
        '    """One-line summary.\n\n'
        "    Args:\n"
        "        ...\n\n"
        "    Returns:\n"
        "        ...\n"
        '    """\n'
        "    ...\n"
        "```\n\n"
        "Assume `from mcp.server.fastmcp import FastMCP` and `mcp = FastMCP(...)` "
        "already exist in scope. Return ONLY the Python code, no prose, no markdown fences. "
        "Keep it self-contained -- only import from the Python standard library, or from "
        "packages already used elsewhere in this codebase (do not assume an arbitrary "
        "third-party package is installed)."
    )
    client = _get_client()
    response = client.chat(prompt, model=None)
    code = response.strip()
    code = re.sub(r"^```(?:python)?\n", "", code)
    code = re.sub(r"\n```$", "", code)
    return code


@dataclass
class CapabilityProposal:
    id: int
    gap_type: str
    gap_details: dict[str, Any]
    draft_source: str
    sandbox_ok: bool
    sandbox_output: str
    risk_level: str
    risk_findings: list[str]
    status: str
    created_at: str
    decided_at: str | None


class CapabilityStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = str(db_path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gap_type TEXT NOT NULL,
                    gap_details_json TEXT NOT NULL,
                    draft_source TEXT NOT NULL,
                    sandbox_ok INTEGER NOT NULL,
                    sandbox_output TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    risk_findings_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                )
                """
            )

    def add(self, gap: CapabilityGap, draft_source: str, sandbox: SandboxResult, risk: RiskScan) -> int:
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                """
                INSERT INTO proposals
                    (gap_type, gap_details_json, draft_source, sandbox_ok, sandbox_output,
                     risk_level, risk_findings_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    gap.gap_type, json.dumps(gap.details), draft_source,
                    int(sandbox.ok), f"exit={sandbox.exit_code}\nstdout:\n{sandbox.stdout}\nstderr:\n{sandbox.stderr}",
                    risk.level, json.dumps(risk.findings),
                    datetime.now(UTC).isoformat(),
                ),
            )
            return int(cur.lastrowid)

    def _row_to_proposal(self, row: sqlite3.Row) -> CapabilityProposal:
        return CapabilityProposal(
            id=row["id"], gap_type=row["gap_type"], gap_details=json.loads(row["gap_details_json"]),
            draft_source=row["draft_source"], sandbox_ok=bool(row["sandbox_ok"]),
            sandbox_output=row["sandbox_output"], risk_level=row["risk_level"],
            risk_findings=json.loads(row["risk_findings_json"]), status=row["status"],
            created_at=row["created_at"], decided_at=row["decided_at"],
        )

    def list_pending(self) -> list[CapabilityProposal]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM proposals WHERE status = 'pending' ORDER BY created_at ASC").fetchall()
        return [self._row_to_proposal(r) for r in rows]

    def get(self, proposal_id: int) -> CapabilityProposal | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        return self._row_to_proposal(row) if row else None

    def decide(self, proposal_id: int, status: str) -> CapabilityProposal | None:
        proposal = self.get(proposal_id)
        if proposal is None or proposal.status != "pending":
            return None
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE proposals SET status = ?, decided_at = ? WHERE id = ?",
                (status, datetime.now(UTC).isoformat(), proposal_id),
            )
        return proposal


_store = CapabilityStore()


def get_store() -> CapabilityStore:
    return _store


def run_pipeline(error: str, context: dict[str, Any] | None = None) -> CapabilityProposal | None:
    """Full pipeline: detect -> draft -> sandbox -> risk-scan -> store -> emit.
    Returns None if the error didn't match a known gap pattern. Callable
    directly; NOT yet wired into tool_executor.py's live request path."""
    gap = detect_gap(error, context)
    if gap is None:
        return None

    event_bus.emit_nowait(
        "capability.gap_detected", importance=0.5,
        payload={"gap_type": gap.gap_type, "details": gap.details}, source="capability_drafting",
    )

    if gap.recommended_action != "draft_tool":
        logger.info("capability_drafting: gap %s has no drafting handler yet, skipping", gap.gap_type)
        return None

    try:
        source = draft_tool(gap)
    except Exception:
        logger.exception("capability_drafting: draft generation failed for gap %s", gap.details)
        return None

    sandbox = sandbox_test(source)
    risk = risk_scan(source)
    proposal_id = _store.add(gap, source, sandbox, risk)

    event_bus.emit_nowait(
        "capability.proposal_ready", importance=0.6,
        payload={"proposal_id": proposal_id, "risk_level": risk.level, "sandbox_ok": sandbox.ok},
        source="capability_drafting",
    )
    return _store.get(proposal_id)


def run_research_pipeline(
    description: str, *, tool_name: str, source_context: str = "",
) -> CapabilityProposal | None:
    """Same draft -> sandbox -> risk-scan -> store -> emit pipeline as
    run_pipeline(), but for a proactively researched improvement instead of
    a reactive tool-not-found error. No detect_gap() step -- the caller
    (a scheduled research pass) already knows what it found and why."""
    gap = CapabilityGap(
        gap_type=GAP_RESEARCH_IMPROVEMENT,
        details={"description": description, "tool_name": tool_name, "source_context": source_context},
        severity="low",
        recommended_action="draft_improvement",
    )
    event_bus.emit_nowait(
        "capability.gap_detected", importance=0.4,
        payload={"gap_type": gap.gap_type, "details": gap.details}, source="capability_drafting",
    )
    try:
        source = draft_improvement(description, tool_name=tool_name, source_context=source_context)
    except Exception:
        logger.exception("capability_drafting: research draft generation failed for %s", tool_name)
        return None

    sandbox = sandbox_test(source)
    risk = risk_scan(source)
    proposal_id = _store.add(gap, source, sandbox, risk)

    event_bus.emit_nowait(
        "capability.proposal_ready", importance=0.5,
        payload={"proposal_id": proposal_id, "risk_level": risk.level, "sandbox_ok": sandbox.ok},
        source="capability_drafting",
    )
    return _store.get(proposal_id)
