"""Deterministic, evidence-based classification of finished agent runs."""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, NamedTuple


class LivenessResult(NamedTuple):
    """A stable database label and a short explanation of its evidence."""

    liveness: str
    reason: str


_READ_WORDS = {
    "cat", "discover", "find", "get", "glob", "grep", "inspect", "list", "ls",
    "open", "pwd", "query", "read", "search", "show", "stat", "view",
}
_SETUP_WORDS = {
    "bootstrap", "checkout", "clone", "create_workspace", "init_workspace", "mkdir",
    "prepare_workspace", "setup", "workspace",
}
_MUTATION_WORDS = {
    "apply", "commit", "create", "delete", "deploy", "edit", "execute", "insert",
    "merge", "move", "patch", "publish", "remove", "rename", "replace", "restart",
    "send", "start", "stop", "update", "upload", "write",
}
_TEST_WORDS = {"check", "lint", "pytest", "spec", "test", "verify"}

_PLAN_RE = re.compile(
    r"(?im)(?:^|[.!?]\s+)(?:i(?:'ll| will| am going to)|we(?:'ll| will)|"
    r"next\s+i(?:'ll| will)|i(?:'d| would) start by)\b|"
    r"^\s*(?:plan|next steps?)\s*:",
)
_COMPLETE_RE = re.compile(
    r"(?i)\b(?:completed|finished|implemented|fixed|resolved|done|all tests pass(?:ed)?|"
    r"tests? pass(?:ed|ing)?|successfully (?:created|updated|wrote|changed|committed))\b",
)
_BLOCKER_RE = re.compile(
    r"(?i)\b(?:blocked|cannot continue|can't continue|unable to continue|waiting (?:for|on) "
    r"(?:the )?user|need (?:your|user) (?:input|approval|confirmation)|missing (?:auth|"
    r"authentication|authorization|credentials?|token|api key|dependency|package)|"
    r"(?:auth|authentication|authorization|credentials?|token|api key) (?:is|are) "
    r"(?:missing|required|unavailable)|permission denied)\b",
)


def _words(value: Any) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _decode(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in "[{":
            try:
                return json.loads(stripped)
            except (TypeError, ValueError):
                pass
    return value


def _walk(value: Any) -> Iterable[Mapping[str, Any]]:
    """Yield mappings from traces, progress events, and JSON-encoded event data."""
    value = _decode(value)
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        for child in value:
            yield from _walk(child)


def _successful(item: Mapping[str, Any]) -> bool:
    if item.get("ok") is False or item.get("success") is False or item.get("error"):
        return False
    data = _decode(item.get("data"))
    if isinstance(data, Mapping):
        if data.get("ok") is False or data.get("success") is False or data.get("error"):
            return False
    result = _decode(item.get("result"))
    if isinstance(result, Mapping):
        if result.get("ok") is False or result.get("success") is False or result.get("error"):
            return False
    return True


def _tool_name(item: Mapping[str, Any]) -> str:
    kind = str(item.get("kind") or item.get("event") or "").lower()
    name = item.get("name") or item.get("tool") or item.get("tool_name")
    if not name and kind in {"tool_started", "tool_finished", "tool_call", "tool"}:
        data = _decode(item.get("data"))
        if isinstance(data, Mapping):
            name = data.get("tool") or data.get("name") or data.get("tool_name")
    return str(name or "").strip().lower()


def _shell_evidence(item: Mapping[str, Any]) -> tuple[str, str] | None:
    args = _decode(item.get("arguments") or item.get("args") or item.get("input"))
    if not isinstance(args, Mapping):
        return None
    command = str(args.get("command") or args.get("cmd") or "").strip()
    if not command:
        return None
    if re.search(r"(?:^|\s)(?:pytest|python\s+-m\s+pytest|npm\s+test|cargo\s+test|go\s+test)(?:\s|$)", command):
        return "test", command[:80]
    if re.search(r"(?:^|\s)git\s+commit(?:\s|$)", command):
        return "commit", "git commit"
    if re.search(
        r"(?:^|[;&|]\s*)(?:cp|install|mv|rm|sed\s+-i|touch|truncate)(?:\s|$)|"
        r"(?:^|[;&|]\s*)git\s+(?:add|apply|merge|mv|rm)(?:\s|$)|"
        r"(?:^|[^<>])>{1,2}(?!=)",
        command,
    ):
        return "action", command[:80]
    if re.search(r"(?:^|\s)(?:cat|ls|pwd|find|rg|grep|git\s+(?:status|diff|log|show))(?:\s|$)", command):
        return "read", command[:80]
    return None


def _evidence(events: Any) -> list[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for item in _walk(events or []):
        name = _tool_name(item)
        if not name or not _successful(item):
            continue
        shell = _shell_evidence(item)
        if shell:
            found.add(shell)
            continue
        words = _words(name.replace("-", "_").replace(".", "_"))
        if words & _SETUP_WORDS:
            found.add(("setup", name))
        elif words & _READ_WORDS:
            found.add(("read", name))
        elif words & _TEST_WORDS:
            found.add(("test", name))
        elif words & _MUTATION_WORDS:
            kind = "commit" if "commit" in words else "action"
            found.add((kind, name))
    return sorted(found)


def classify_run_liveness(
    status: Any,
    final_text: Any = "",
    error: Any = None,
    tool_call_events: Any = None,
    *,
    request_text: Any = "",
    planning_requested: bool = False,
) -> LivenessResult:
    """Classify a terminal run from observed evidence, never model claims alone.

    ``request_text`` lets callers exempt an explicitly requested planning
    deliverable. ``planning_requested`` is available when that fact has already
    been established by the caller.
    """
    normalized_status = str(status or "").strip().lower()
    text = str(final_text or "").strip()
    error_text = str(error or "").strip()

    if normalized_status in {"failed", "error", "errored", "interrupted", "cancelled", "canceled"} or error_text:
        detail = error_text.splitlines()[0][:120] if error_text else normalized_status
        return LivenessResult("failed", f"The run errored: {detail}.")

    evidence = _evidence(tool_call_events)
    concrete = [(kind, name) for kind, name in evidence if kind in {"action", "commit", "test"}]
    if concrete:
        kind, name = concrete[0]
        noun = "test execution" if kind == "test" else "commit" if kind == "commit" else "successful action"
        if text and _COMPLETE_RE.search(text):
            return LivenessResult("completed", f"Completion is supported by {noun} evidence from {name}.")
        return LivenessResult("advanced", f"The run has {noun} evidence from {name}.")

    if text and _BLOCKER_RE.search(text):
        reason = next((line.strip() for line in text.splitlines() if _BLOCKER_RE.search(line)), text)
        reason = reason.split(".", 1)[0].rstrip(".!?:; ")
        return LivenessResult("blocked", f"The run reported a concrete blocker: {reason[:120]}.")

    explicit_plan = planning_requested or bool(re.search(
        r"(?i)\b(?:create|draft|develop|give|make|outline|prepare|provide|write)\s+"
        r"(?:me\s+|us\s+)?(?:a |an )?(?:implementation )?plan\b",
        str(request_text or ""),
    ))
    if explicit_plan and text:
        return LivenessResult("completed", "The requested planning deliverable was returned in the final response.")

    if text and _PLAN_RE.search(text):
        if evidence:
            return LivenessResult("plan_only", "Only read, discovery, or workspace-setup calls preceded a future-work plan.")
        return LivenessResult("plan_only", "The response described future work without concrete action evidence.")

    if not text:
        return LivenessResult("empty_response", "The run succeeded with no useful final text or action evidence.")

    if text and _COMPLETE_RE.search(text):
        return LivenessResult("completed", "The final response reports completion, but no concrete action trace was recorded.")

    return LivenessResult("completed", "The run returned a substantive final response with no unfinished-work signal.")
