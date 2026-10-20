"""Time awareness and objective-based conversations for the assistant.

A conversation is about one objective, not a block of time. Before each turn
the assistant decides whether the message continues the current
conversation, starts a new one (a clear change of objective, or a long quiet
gap), or returns to an earlier one. Earlier conversations leave the prompt
and come back only through recall tools when a request needs them. Every
turn also tells the model what time it is for the user and how long it has
been since the last message.
"""
from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, tzinfo
from typing import Any, Callable

DEFAULT_GAP_MINUTES = 120
FOLLOW_UP_SECONDS = 180
FOLLOW_UP_WORDS = 6
EARLIER_IN_PROMPT = 8
_TOPIC_SHIFT_RE = re.compile(
    r"\b(?:new topic|different question|different topic|unrelated|switching gears|"
    r"anyway|by the way|btw|going back to|earlier conversation|previous conversation|"
    r"what were we talking about)\b",
    re.I,
)


def now_utc() -> datetime:
    return datetime.now(UTC)


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def user_timezone() -> tzinfo:
    """ULTIMATE_ASSISTANT_TIMEZONE (IANA name), else this device's local zone."""
    name = os.environ.get("ULTIMATE_ASSISTANT_TIMEZONE", "").strip()
    if name.upper() in {"UTC", "Z"}:
        return UTC
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - unknown zone or no tzdata
            pass
    return datetime.now().astimezone().tzinfo or UTC


def gap_minutes() -> int:
    try:
        return max(1, int(os.environ.get("ULTIMATE_ASSISTANT_CONVERSATION_GAP_MINUTES", DEFAULT_GAP_MINUTES)))
    except ValueError:
        return DEFAULT_GAP_MINUTES


def ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "just now"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            count = int(seconds / size + 0.5) if size > 60 else seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "just now"


def clock(moment: datetime, tz: tzinfo, *, with_date: bool = False) -> str:
    local = moment.astimezone(tz)
    hour = local.strftime("%I:%M %p").lstrip("0")
    return f"{local.strftime('%A, %B')} {local.day}, {local.year}, {hour}" if with_date else hour


def time_context(current: dict[str, Any] | None, previous: dict[str, Any] | None,
                 now: datetime, tz: tzinfo) -> str:
    """The time block for this turn. `current`/`previous` are conversation
    summaries from ConversationStore.conversations()."""
    lines = [f"Now: {clock(now, tz, with_date=True)} ({now.astimezone(tz).tzname()})."]
    last = parse((current or {}).get("last_at"))
    started = parse((current or {}).get("started_at"))
    if last:
        lines.append(f"This conversation started {ago((now - started).total_seconds()) if started else 'earlier'}; "
                     f"the last message was {ago((now - last).total_seconds())}.")
    else:
        ended = parse((previous or {}).get("last_at"))
        lines.append("This is the start of a new conversation." + (
            f" The previous one ended {ago((now - ended).total_seconds())} ({clock(ended, tz, with_date=True)})."
            if ended else ""))
    return "\n".join(lines)


def earlier_context(conversations: list[dict[str, Any]], tz: tzinfo) -> str:
    rows = []
    for item in conversations[-EARLIER_IN_PROMPT:]:
        start, end = parse(item.get("started_at")), parse(item.get("last_at"))
        when = f"{clock(start, tz, with_date=True)} – {clock(end, tz)}" if start and end else "time unknown"
        rows.append(f"- [{item['id']}] {when}: {item.get('title', '')}")
    return "\n".join(rows)


def likely_follow_up(message: str, last_at: datetime | None, now: datetime) -> bool:
    """A short reply right after an answer ("yes", "do it", "thanks") continues the
    objective with no model call, unless it says it is changing topic. Longer turns go to
    the model, so "back to the essay" can return to an earlier conversation."""
    if not last_at or (now - last_at).total_seconds() > FOLLOW_UP_SECONDS:
        return False
    if _TOPIC_SHIFT_RE.search(message or ""):
        return False
    return len(message.split()) <= FOLLOW_UP_WORDS


def shift_prompt(message: str, recent: list[dict[str, Any]], earlier: list[dict[str, Any]],
                 last_at: datetime | None, now: datetime, tz: tzinfo) -> str:
    def squash(text: Any, limit: int) -> str:
        return " ".join(str(text or "").split())[:limit]

    turns = "\n".join(f"{m['role'].title()}: {squash(m.get('content'), 400)}" for m in recent)
    others = "\n".join(f"[{c['id']}] {c.get('title', '')}" for c in earlier) or "(none)"
    return (
        "You track which objective a user is pursuing with their personal assistant. "
        "Decide whether the NEW MESSAGE continues the current conversation's objective.\n"
        "- \"same\": follow-ups, clarifications, corrections, sub-steps, or anything plausibly serving the same goal. "
        "When unsure, choose same.\n"
        "- \"new\": the message clearly pursues a different goal (a new topic, \"anyway\", \"different question\", "
        "a task unrelated to what was being worked on).\n"
        "- \"resume\": it clearly returns to one of the EARLIER CONVERSATIONS; give its id.\n\n"
        f"CURRENT CONVERSATION (last message {ago((now - last_at).total_seconds()) if last_at else 'unknown'}):\n{turns}\n\n"
        f"EARLIER CONVERSATIONS:\n{others}\n\nNEW MESSAGE ({clock(now, tz, with_date=True)}):\n{message[:2000]}\n\n"
        'Reply with only one line of JSON: {"decision": "same|new|resume", "conversation_id": "<id or empty>", '
        '"reason": "<ten words or fewer>"}'
    )


def parse_shift(text: str, earlier_ids: set[str]) -> tuple[str | None, str]:
    """("new" | an earlier conversation id | None for same, reason)."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        value = json.loads(match.group(0)) if match else {}
    except ValueError:
        return None, ""
    decision = str(value.get("decision", "")).strip().lower()
    reason = str(value.get("reason", ""))[:200]
    if decision == "new":
        return "new", reason
    if decision == "resume" and str(value.get("conversation_id", "")) in earlier_ids:
        return str(value["conversation_id"]), reason
    return None, ""


def decide(message: str, current: dict[str, Any] | None, recent: list[dict[str, Any]],
           earlier: list[dict[str, Any]], ask_model: Callable[[str], str], *,
           now: datetime | None = None) -> tuple[str | None, str]:
    """Where this message belongs: None (current conversation), "new", or an
    earlier conversation id -- with a short reason. Never raises: any failure
    keeps the current conversation."""
    now = now or now_utc()
    last = parse((current or {}).get("last_at"))
    if not current or not last:
        return None, ""
    if (now - last).total_seconds() > gap_minutes() * 60:
        return "new", "quiet gap"
    if likely_follow_up(message, last, now):
        return None, ""
    try:
        text = ask_model(shift_prompt(message, recent[-8:], earlier[-EARLIER_IN_PROMPT:], last, now, user_timezone()))
    except Exception:  # noqa: BLE001 - the check is best effort
        return None, ""
    return parse_shift(text, {c["id"] for c in earlier})
