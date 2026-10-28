"""The assistant's clock: reminds the user of what's coming and gets louder as it gets close.

Runs on the main node, once a minute, over life/record.upcoming(). For each item it picks a
level (soon / close / now-or-late, by kind, counted to when the user must leave or start preparing
when the item has prep or travel time) and shows a card on every device the first time an item
reaches a level. How loud that card is comes from LOUDNESS, adjusted by what the user has already
said and whether they are at a screen (loudness()): a quiet corner card that expires, one that stays
until answered, one in the middle of the screen, or that and said aloud. The card's buttons (Done,
Snooze, Got it) come back as canvas events and are acted on: done closes the item, snooze holds it,
got it quiets that level and makes the levels after it one step quieter. When DIGEST_AT or more corner
cards would appear at once (a busy day, seen for the first time) they share one summary card instead.
State per item is kept in life_root()/reminders.json.

Nudges: other parts of the assistant (and builds on top) add one-off nudges through add_source, e.g.
"back to work after your break" or "you missed the study block you planned": each is shown once at
its time. A nudge's optional "done" callback runs when it's answered.

Voice: an item is said once on the paired phone (phone/announce.py) at the loudest step: when it
matters and reaches its last level, when a nudge asks to be spoken, or when a card in the middle of
the screen would go unseen because no device is in use. Ordinary items stay on screen otherwise.

Important items: the first time an item could come up the decision model is asked, once, whether
missing it would really matter (IMPORTANT). If so it follows IMPORTANT_LEVELS (an appointment the
evening before, a chore that matters within the hour) and the louder LOUDNESS row, and its last
reminder is never quieted below the middle of the screen. Without an answer the item is treated as
ordinary and asked about again when it next would get louder.

Accountability: when something that matters is close (a task due within PUSHBACK["task"] minutes, an
appointment within PUSHBACK["appointment"]) and a device has had a leisure app in focus for
LEISURE_MINUTES, it says so on the canvas: "Essay due in 3 h; you've been in <app> for 40 min."
Leisure is judged by the decision model once per app; without one it never pushes back. It never
blocks anything: "I need this" stops it for that item.

tools/capabilities/proactive_bench.py checks all of this against scenarios of what is too much and
what is too little.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

LOG = logging.getLogger(__name__)
TICK_SECONDS = 60

# Minutes before (positive) or after (negative) the item's time at which each level starts.
LEVELS: dict[str, tuple[float, float, float]] = {
    "appointment": (120, 30, 5),
    "task": (24 * 60, 3 * 60, 0),
    "chore": (0, -3 * 60, -12 * 60),
    "outing": (0, -60, -3 * 60),  # time = expected return
    "nudge": (0,),  # shown once, at its time
}
# Items that matter are heard about earlier: an appointment the evening before, a chore within the hour.
IMPORTANT_LEVELS = {**LEVELS, "appointment": (18 * 60, 120, 30, 5), "chore": (0, -30, -120)}
# How loud each level is, (ordinary, important): 1 corner card that expires, 2 corner card that stays,
# 3 middle of the screen, 4 that and said aloud. Routine tasks and chores never take the middle of the
# screen: only being somewhere (an appointment, coming back) or something that matters does.
LOUDNESS: dict[str, tuple[tuple[int, ...], tuple[int, ...]]] = {
    "appointment": ((1, 2, 3), (2, 2, 3, 4)),
    "task": ((1, 2, 2), (2, 2, 4)),
    "chore": ((1, 2, 2), (2, 3, 4)),
    "outing": ((1, 2, 3), (2, 3, 4)),
    "nudge": ((1,), (1,)),
}
SNOOZE = {"Snooze 15 min": 15, "Snooze 1 hour": 60, "Tomorrow": 18 * 60}
PUSHBACK = {"task": 3 * 60, "appointment": 30}
LEISURE_MINUTES = 20
DIGEST_AT = 3  # this many quiet reminders at once go on one summary card instead
LEISURE = ("This program is mainly for entertainment rather than work, study, chores or communication: "
           "a game, a game launcher, a game console emulator (Citron, Yuzu, Ryujinx, Cemu, Dolphin, RetroArch), "
           "videos or streaming, or social media. A music player is not entertainment here: people work "
           "with music on.")
_LEISURE: dict[tuple[str, ...], bool] = {}
IMPORTANT = ("Missing or forgetting this would have real consequences. That includes schoolwork or work "
             "with a deadline (an essay, report, homework, problem set, presentation, exam or test, even when "
             "the title doesn't say 'due'); health (a medical appointment, taking or picking up medication); "
             "money and paperwork (a bill, rent, insurance, taxes, a passport or other official document); a "
             "flight or other travel; a work shift or interview; or something someone else is counting on. "
             "Not a routine chore, an errand that can wait, or a casual plan.")
# Functions returning nudges: [{"id", "title", "at" (ISO 8601), "speak"?: bool, "done"?: callable}].
SOURCES: list[Callable[[datetime], list[dict[str, Any]]]] = []


def add_source(source: Callable[[datetime], list[dict[str, Any]]]) -> None:
    """Have the clock show nudges from this source (called each minute with the current time)."""
    if source not in SOURCES:
        SOURCES.append(source)


def is_leisure(process: str) -> bool:
    """Classify foreground leisure through either route, with an executable-name rule fallback."""
    if not process:
        return False
    from . import decision_interface as di
    cache_key = (*di.decision_settings().values(), process)
    if cache_key not in _LEISURE:
        state = f"Foreground program: {process}"
        executable = process.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        outcome = di.yes_no("leisure", LEISURE, state,
                            llm=lambda: di.llm_yes_no(state, LEISURE),
                            rule=lambda: executable in {"steam", "playnite", "playnite.desktopapp", "lutris", "heroic", "gamescope"})
        if not outcome.ok:
            return False  # don't cache: try again when the decision model is back
        _LEISURE[cache_key] = bool(outcome.value)
    return _LEISURE[cache_key]


def is_important(item: dict[str, Any]) -> bool | None:
    """Would missing this item matter? Both routes share evidence and a structured rule fallback."""
    from . import decision_interface as di
    state = {key: item.get(key) for key in ("kind", "title", "priority", "important", "due_at", "at")}
    outcome = di.yes_no("importance", IMPORTANT, state,
                        llm=lambda: di.llm_yes_no(state, IMPORTANT),
                        rule=lambda: item.get("important") is True or item.get("priority") in {"high", "urgent"}
                        or item.get("kind") in {"exam", "appointment"})
    return bool(outcome.value) if outcome.ok else None


def level(kind: str, minutes_until: float, important: bool = False) -> int:
    thresholds = (IMPORTANT_LEVELS if important else LEVELS).get(kind)
    if not thresholds:
        return 0
    return sum(1 for limit in thresholds if minutes_until <= limit)


def loudness(kind: str, lvl: int, important: bool, dismissed: int = 0, present: bool = True) -> int:
    """How loud this level is (1-4, see LOUDNESS): one step quieter for each time the user already said
    "Got it" or closed it, never below the middle of the screen for the last reminder of something that
    matters, and said aloud when it would be in the middle of a screen nobody is looking at."""
    steps = LOUDNESS.get(kind, LOUDNESS["task"])[important]
    loud = steps[min(lvl, len(steps)) - 1] - dismissed
    if important and lvl >= len(steps):
        loud = max(loud, 3)
    loud = max(loud, 1)
    return 4 if loud == 3 and not present else loud


def card(item: dict[str, Any], lvl: int, minutes_until: float, loud: int = 1, last: int = 3) -> dict[str, Any]:
    from .conversation_flow import user_timezone
    at = datetime.fromisoformat(item["at"].replace("Z", "+00:00")).astimezone(user_timezone())
    clock = at.strftime("%I:%M %p").lstrip("0")
    day = "" if at.date() == datetime.now(user_timezone()).date() else at.strftime("%a ")
    remaining = last - lvl
    if item["kind"] == "outing":
        question = f"Are you back? You expected to be back by {day}{clock}."
        options = ["I'm back", "Snooze 1 hour"]
    elif item["kind"] == "nudge":
        question = item["title"]
        options = ["Got it", "Snooze 15 min"]
    else:
        when = (f"in {round(minutes_until)} min" if 0 < minutes_until < 90 else
                "now" if -5 <= minutes_until <= 0 else
                f"{round(-minutes_until / 60, 1)} h late" if minutes_until < 0 else f"at {day}{clock}")
        question = f"{item['title']} {when}"
        if item.get("lead_minutes") and minutes_until > item["lead_minutes"] - 5:
            to_leave = minutes_until - item["lead_minutes"]
            leave = (at - timedelta(minutes=item["lead_minutes"])).strftime("%I:%M %p").lstrip("0")
            question = f"{item['title']} at {day}{clock}; " + (
                f"leave in {round(to_leave)} min" if 0 < to_leave < 90 else
                "time to leave" if to_leave <= 0 else f"leave by {leave}")
        options = ["Done", "Snooze 15 min" if remaining <= 1 else "Snooze 1 hour", "Got it"]
    return {"kind": "ui", "template": "choice", "id": f"life-{item['kind']}-{item['id']}"[:64],
            "title": question[:200], "devices": ["everywhere"],
            "position": "center" if loud >= 3 else "top-right",
            "ttl_seconds": 1800 if loud == 1 else 0,
            "data": {"header": "Heads up" if item["kind"] == "nudge" else
                     {0: "Now", 1: "Soon"}.get(remaining, "Coming up"),
                     "question": question,
                     "options": options, "allow_other": False}}


class LifeClock:
    def __init__(self, show: Callable[[dict], Any], events: Callable[[str, int], list[dict]],
                 remove: Callable[[str], Any], state_file: Path | None = None,
                 upcoming: Callable[..., list[dict]] | None = None, finish: Callable[..., Any] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 activity: Callable[[], dict[str, dict]] | None = None,
                 leisure: Callable[[str], bool] | None = None,
                 speak: Callable[[str, str], Any] | None = None,
                 important: Callable[[dict[str, Any]], bool | None] | None = None):
        if upcoming is None or finish is None:
            from .life import record
            upcoming, finish = upcoming or record.upcoming, finish or record.finish
        if state_file is None:
            from .life.paths import life_root
            state_file = life_root() / "reminders.json"
        self.show, self.events, self.remove = show, events, remove
        self.upcoming, self.finish = upcoming, finish
        self.state_file, self.clock = state_file, clock or (lambda: datetime.now(timezone.utc))
        if activity is None:
            from .activity import activity as follower
            activity = follower.current
        self.activity, self.leisure = activity, leisure or is_leisure
        if speak is None:
            from .phone.announce import announce
            speak = announce
        self.speak = speak
        self.important = important or is_important
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _load(self) -> dict[str, dict]:
        try:
            return json.loads(self.state_file.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict[str, dict]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        pending = self.state_file.with_suffix(".pending")
        pending.write_text(json.dumps(state, indent=1), "utf-8")
        pending.replace(self.state_file)

    def _answers(self, state: dict[str, dict], items: dict[str, dict], now: datetime) -> None:
        for key, entry in state.items():
            card_id = entry.get("card")
            if not card_id:
                continue
            for event in self.events(card_id, entry.get("seq", 0)):
                entry["seq"] = max(entry.get("seq", 0), int(event.get("seq", 0)))
                if event.get("type") == "close":
                    entry["quiet_level"] = entry.get("level", 0)
                    entry["dismissed"] = entry.get("dismissed", 0) + 1
                if not (event.get("type") == "answer" or event.get("name") == "answer"):
                    continue
                choices = (event.get("data") or {}).get("choices") or [""]
                choice = choices[0]
                item = items.get(key.removeprefix("push:"))
                if key.startswith("push:"):
                    entry["quiet_until"] = (now + timedelta(minutes={"10 more minutes": 10}.get(choice, 30))).isoformat()
                    entry["stopped"] = choice == "I need this"
                elif entry.get("digest") and item:  # one of several on a summary: ticked off, or seen
                    if entry["digest"] in choices:
                        self.finish(item["kind"], item["id"])
                        entry["done"] = True
                    else:
                        entry["quiet_level"] = entry.get("level", 0)
                elif choice in {"Done", "I'm back"} and item:
                    self.finish(item["kind"], item["id"])
                    entry["done"] = True
                elif choice == "Got it" and item and item["kind"] == "nudge":
                    if callable(item.get("done")):
                        item["done"]()
                    entry["done"] = True
                elif choice in SNOOZE:
                    entry["snoozed_until"] = (now + timedelta(minutes=SNOOZE[choice])).isoformat()
                else:
                    entry["quiet_level"] = entry.get("level", 0)
                    entry["dismissed"] = entry.get("dismissed", 0) + 1
                self.remove(card_id)
                entry["card"] = None

    def tick(self) -> list[str]:
        """One pass; returns the cards shown."""
        now = self.clock()
        state = self._load()
        items = {f"{i['kind']}:{i['id']}": i for i in self.upcoming(days=2, now=now)}
        for source in list(SOURCES):
            try:
                for nudge in source(now):
                    items[f"nudge:{nudge['id']}"] = {**nudge, "kind": "nudge"}
            except Exception:  # noqa: BLE001 - one source failing doesn't stop the others
                LOG.exception("Nudge source failed")
        self._answers(state, items, now)
        try:
            present = any(not doing["idle"] for doing in self.activity().values())
        except Exception:  # noqa: BLE001 - unknown: assume nobody is looking, as before activity existed
            present = False
        shown, ready = [], []
        for key, item in items.items():
            entry = state.setdefault(key, {})
            if entry.get("done"):
                continue
            if entry.get("snoozed_until") and datetime.fromisoformat(entry["snoozed_until"]) > now:
                continue
            minutes = (datetime.fromisoformat(item["at"].replace("Z", "+00:00")) - now).total_seconds() / 60
            lead = minutes - float(item.get("lead_minutes") or 0)  # until it's time to leave or get ready
            if "important" not in entry and item["kind"] != "nudge" and \
                    level(item["kind"], lead, True) > entry.get("level", 0):
                try:
                    verdict = self.important(item)
                except Exception:  # noqa: BLE001 - ordinary until the decision model answers
                    verdict = None
                if verdict is not None:
                    entry["important"] = verdict
                    from .agents.dispatch import decision_log
                    decision_log("reminder", {"kind": item["kind"], "important": verdict})
            important = bool(entry.get("important"))
            lvl = level(item["kind"], lead, important)
            if lvl == 0 or lvl <= entry.get("level", 0) and not entry.get("snoozed_until"):
                continue
            if lvl <= entry.get("quiet_level", 0):
                continue
            last = len((IMPORTANT_LEVELS if important else LEVELS).get(item["kind"], ()))
            loud = loudness(item["kind"], lvl, important, entry.get("dismissed", 0), present)
            ready.append((key, item, entry, lvl, last, loud, card(item, lvl, minutes, loud, last)))
        quiet = [r for r in ready if r[5] <= 2 and r[1]["kind"] != "nudge"]
        if len(quiet) >= DIGEST_AT:
            shown += self._digest(quiet, now)
            ready = [r for r in ready if r not in quiet]
        for key, item, entry, lvl, last, loud, element in ready:
            try:
                self.show(element)
            except Exception as exc:  # noqa: BLE001 - try again next minute
                LOG.warning("Could not show reminder %s: %s", key, exc)
                continue
            entry.update(level=lvl, card=element["id"], shown_at=now.isoformat(), snoozed_until=None, digest=None)
            shown.append(element["id"])
            spoken = last if entry.get("spoken") is True else entry.get("spoken") or 0
            if spoken < lvl and (loud >= 4 or item.get("speak") and not spoken):
                try:
                    self.speak(element["data"]["question"], f"{key}:{lvl}")
                    entry["spoken"] = lvl
                except Exception as exc:  # noqa: BLE001 - the card is already up
                    LOG.warning("Could not say reminder %s on the phone: %s", key, exc)
        shown += self.accountability(state, items, now)
        for key in [k for k in state if k.removeprefix("push:") not in items]:
            del state[key]  # closed or no longer upcoming
        self._save(state)
        return shown

    def _digest(self, quiet: list[tuple], now: datetime) -> list[str]:
        """Several quiet reminders at once (a busy day the first time it's seen) go on one card: each
        can be ticked off as done, and the rest count as seen."""
        options, covers = [], {}
        for key, item, entry, lvl, last, loud, element in quiet:
            label = item["title"][:80]
            while label in covers.values():
                label += " "
            covers[key] = label
            options.append({"label": label, "description": element["data"]["question"].removeprefix(item["title"]).strip()})
        options.append({"label": "Got it", "description": "Seen them all"})
        element = {"kind": "ui", "template": "choice", "id": f"life-digest-{now.strftime('%Y%m%d%H%M')}",
                   "title": f"{len(quiet)} things coming up", "devices": ["everywhere"], "position": "top-right",
                   "ttl_seconds": 1800 if all(r[5] == 1 for r in quiet) else 0,
                   "data": {"header": "Coming up", "question": f"{len(quiet)} things coming up: tick what's done",
                            "options": options, "multi": True, "allow_other": False, "covers": covers}}
        try:
            self.show(element)
        except Exception as exc:  # noqa: BLE001 - try again next minute
            LOG.warning("Could not show the reminder summary: %s", exc)
            return []
        for key, item, entry, lvl, last, loud, _ in quiet:
            entry.update(level=lvl, card=element["id"], shown_at=now.isoformat(), snoozed_until=None,
                         digest=covers[key])
        return [element["id"]]

    def accountability(self, state: dict[str, dict], items: dict[str, dict], now: datetime) -> list[str]:
        """Push back, once per quiet period, on leisure while the most pressing item is close."""
        pressing = []
        for key, item in items.items():
            limit = PUSHBACK.get(item["kind"])
            minutes = (datetime.fromisoformat(item["at"].replace("Z", "+00:00")) - now).total_seconds() / 60
            entry = state.get(key, {})
            lead = float(item.get("lead_minutes") or 0)  # the deadline is when to leave, not when it starts
            if limit is not None and minutes - lead <= limit and entry.get("important") and not entry.get("done"):
                pressing.append((minutes, key, item))
        if not pressing:
            return []
        minutes, key, item = min(pressing, key=lambda entry: entry[0])
        entry = state.setdefault("push:" + key, {})
        if entry.get("stopped") or entry.get("card") or (
                entry.get("quiet_until") and datetime.fromisoformat(entry["quiet_until"]) > now):
            return []
        for device, doing in self.activity().items():
            if doing["idle"] or doing["minutes"] < LEISURE_MINUTES or not self.leisure(doing["process"]):
                continue
            lead = float(item.get("lead_minutes") or 0)
            until = minutes - lead
            due = (f"in {round(until)} min" if 0 < until < 90 else
                   f"in {round(until / 60, 1)} h" if until >= 90 else "now")
            question = (f"{item['title']}{': leave' if lead else ' is due'} {due}; you've been in {doing['process']} for "
                        f"{round(doing['minutes'])} min.")
            element = {"kind": "ui", "template": "choice", "id": f"push-{item['kind']}-{item['id']}"[:64],
                       "title": question[:200], "devices": ["everywhere"], "position": "center",
                       "ttl_seconds": 0,
                       "data": {"header": "Heads up", "question": question, "allow_other": False,
                                "options": ["Switching now", "10 more minutes", "I need this"]}}
            try:
                self.show(element)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Could not show pushback: %s", exc)
                return []
            entry.update(card=element["id"], device=device, shown_at=now.isoformat())
            return [element["id"]]
        return []

    def start(self) -> None:
        def run():
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 - the clock keeps going
                    LOG.exception("Life clock tick failed")
                self._stop.wait(TICK_SECONDS)
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=run, name="life-clock", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()


_CLOCK: LifeClock | None = None


def start_on_main_node() -> LifeClock | None:
    """Start the clock in this process if it is the main node (once)."""
    global _CLOCK
    from .node_link import is_main
    if _CLOCK is not None or not is_main():
        return _CLOCK
    from . import canvas
    _CLOCK = LifeClock(show=canvas.store.show,
                       events=lambda card_id, after: canvas.store.events(after, element_id=card_id),
                       remove=lambda card_id: canvas.store.remove(card_id))
    from .activity import activity
    activity.follow()
    _CLOCK.start()
    return _CLOCK
