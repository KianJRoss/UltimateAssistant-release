"""Notice commitments in what the user says and record them without being asked.

"I have a dentist appointment Thursday at 3", "my essay is due Friday", "I'm heading out, back
around 6", "I need to do laundry tonight": the user shouldn't have to say "remember that".
Each user message is checked in the background, never slowing a reply:

1. a cheap gate: the Router's decision model (Jev) says whether it mentions a commitment still
   ahead; without one, how close it is to how people mention plans (embedding model, else word
   overlap). Most messages stop here;
2. a small model pulls out the items as JSON (kind, title, times with the user's offset);
3. each item is stored through life/record.py (idempotent per message), and the assistant
   says what it noted on the canvas, where the user can correct it.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from typing import Any, Callable

from . import skills

LOG = logging.getLogger(__name__)

GATE_SCORE = 0.58
LEXICAL_GATE_SCORE = 0.25
EXAMPLES = (
    "I have a dentist appointment Thursday at 3",
    "my essay is due friday",
    "I have a chem exam next week",
    "I need to do laundry tonight",
    "rent is due on the first",
    "I'm heading out, back around 6",
    "I work 4 to 10 tomorrow",
    "remind me to call mom on sunday",
    "I should start that project this weekend",
    "I take out the trash every tuesday",
    "meeting with my advisor at 2pm",
    "pay the phone bill every month",
)
_VECTORS: list[list[float]] | None = None
_LOCK = threading.Lock()


def gate(message: str) -> float:
    global _VECTORS
    query = skills.embed(message)
    if query:
        with _LOCK:
            if _VECTORS is None:
                _VECTORS = [v for v in (skills.embed(e) for e in EXAMPLES) if v]
        if _VECTORS:
            return max(skills.cosine(query, v) for v in _VECTORS)
    return max(skills.lexical(message, e) for e in EXAMPLES)


def extract_prompt(message: str, now: datetime) -> str:
    return (
        f"Now: {now.isoformat(timespec='minutes')} ({now.strftime('%A')}).\n"
        f"The user said: <<<{message[:1500]}>>>\n\n"
        "List the commitments in it: things they must do, attend, pay, submit, or where they're going.\n"
        "kind: task (one-off, optional due_at), appointment (starts_at, optional ends_at), outing "
        "(they are leaving or away; starts_at, expected_return), chore (repeats; interval_days, due_at = next "
        "time), hobby (optional next step).\n"
        "Rules: chore only when they say it repeats ('every tuesday', 'each month'); a one-time job is a "
        "task. Every date is its next occurrence after Now ('monday' is the coming Monday; 'the 1st' is next "
        "month's if this month's has passed). Times: ISO 8601 with the same offset as Now; a vague time "
        "('tonight', 'friday') becomes a sensible time that day. Questions, requests for suggestions or "
        "recommendations, opinions, wishes and past events are not commitments.\n"
        'Answer only JSON: {"items": [{"kind": "...", "title": "short", "starts_at": "", "ends_at": "", '
        '"due_at": "", "expected_return": "", "interval_days": 0, "location": "", "notes": ""}]} '
        'or {"items": []}.'
    )


def parse_items(answer: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(answer)
    except ValueError:
        match = re.search(r"\{.*\}", answer or "", re.S)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return []
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    return [{k: v for k, v in item.items() if v not in ("", None, 0)} for item in items
            if isinstance(item, dict) and item.get("kind") and item.get("title")]


def next_occurrence(item: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Small models still put "monday" or "the 1st" in the past; a date means its next occurrence."""
    from datetime import timedelta
    out = dict(item)
    for field in ("due_at", "starts_at", "ends_at", "expected_return"):
        value = out.get(field)
        if not value:
            continue
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            out.pop(field)
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=now.tzinfo)
        if field == "starts_at" and out.get("kind") == "outing":
            pass  # leaving now or earlier today is fine
        elif moment < now - timedelta(hours=1):
            if now - moment <= timedelta(days=7):
                moment += timedelta(days=7)  # a weekday that already passed this week
            while moment < now:
                month = moment.month % 12 + 1
                year = moment.year + (moment.month == 12)
                try:
                    moment = moment.replace(year=year, month=month)
                except ValueError:  # the 31st in a shorter month
                    moment = moment.replace(year=year, month=month, day=28)
        out[field] = moment.isoformat()
    return out


def describe(saved: dict[str, Any]) -> str:
    when = ""
    if saved.get("when"):
        from .conversation_flow import user_timezone
        moment = datetime.fromisoformat(saved["when"].replace("Z", "+00:00")).astimezone(user_timezone())
        when = " - " + moment.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")
    return f"{saved['title']}{when}"


COMMITMENT = ("The user mentions something still ahead of them that they have to do, attend, pay or submit, "
              "a chore they do regularly, or a place they are going now; not something already done, a "
              "question or a wish.")


def worth_extracting(message: str, judge: Callable[[str, str], float | None] | None = None) -> bool:
    """Is a commitment mentioned? One typed yes/no (decision_interface): on the `decision_model` route the
    model answers when confident (it ignores questions and past events); the similarity gate is the rule
    form, used on the default `llm` route and whenever the typed answer is uncertain or unavailable."""
    from . import decision_interface as di

    def similarity() -> bool:
        cutoff = GATE_SCORE if skills.embed("probe") else LEXICAL_GATE_SCORE
        return gate(message) >= cutoff

    outcome = di.yes_no("commitment_gate", COMMITMENT, message, rule=similarity,
                        backend=di.judge_backend(judge))
    return bool(outcome.value) if outcome.ok else False


def observe(message: str, ask: Callable[[str], str], tell: Callable[[str], None],
            now: datetime | None = None, store: Callable[..., dict] | None = None,
            judge: Callable[[str, str], float | None] | None = None) -> list[dict[str, Any]]:
    """Record the commitments in this message; returns what was saved."""
    if not worth_extracting(message, judge):
        return []
    from .conversation_flow import user_timezone
    now = now or datetime.now(user_timezone())
    try:
        items = parse_items(ask(extract_prompt(message, now)))
    except Exception:  # noqa: BLE001 - no model: the agents and life tools still can
        LOG.info("Could not extract commitments from a message")
        return []
    if store is None:
        from .life.record import record as store
    saved = []
    for item in items:
        try:
            saved.append(store(next_occurrence(item, now), source="conversation", evidence=message))
        except Exception as exc:  # noqa: BLE001 - one bad item must not drop the others
            LOG.info("Could not record %s: %s", item.get("title"), exc)
    if saved:
        tell("Noted: " + "; ".join(describe(s) for s in saved) + ". Tell me if any of that is wrong.")
    return saved


def observe_in_background(message: str, ask: Callable[[str], str], tell: Callable[[str], None]) -> None:
    def run():
        try:
            observe(message, ask, tell)
        except Exception:  # noqa: BLE001 - never break a conversation over this
            LOG.exception("Commitment capture failed")
    threading.Thread(target=run, name="life-capture", daemon=True).start()
