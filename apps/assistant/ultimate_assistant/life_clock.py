"""The assistant's clock: reminds the user of what's coming and gets louder as it gets close.

Runs on the main node, once a minute, over life/record.upcoming(). For each item it picks a
level (soon / close / now-or-late, by kind) and shows a card on every device the first time an
item reaches a level: a quiet corner card that expires, then one that stays until answered, then
one in the middle of the screen. The card's buttons (Done, Snooze, Got it) come back as canvas
events and are acted on: done closes the item, snooze holds it, got it quiets that level.
State per item is kept in life_root()/reminders.json.

Accountability: when something is close (a task due within PUSHBACK["task"] minutes, an
appointment within PUSHBACK["appointment"]) and a device has had a leisure app in focus for
LEISURE_MINUTES, it says so on the canvas: "Essay due in 3 h; you've been in <app> for 40 min."
Leisure is judged by the decision model once per app; without one it never pushes back. It never
blocks anything: "I need this" stops it for that item.
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
}
SNOOZE = {"Snooze 15 min": 15, "Snooze 1 hour": 60, "Tomorrow": 18 * 60}
PUSHBACK = {"task": 3 * 60, "appointment": 30}
LEISURE_MINUTES = 20
LEISURE = ("This program is mainly for entertainment rather than work, study, chores or communication: "
           "a game, a game launcher, a game console emulator (Citron, Yuzu, Ryujinx, Cemu, Dolphin, RetroArch), "
           "videos or streaming, or social media. A music player is not entertainment here: people work "
           "with music on.")
_LEISURE: dict[str, bool] = {}


def is_leisure(process: str) -> bool:
    """The decision model's call on a foreground app, once per app; False when it can't say."""
    if not process:
        return False
    if process not in _LEISURE:
        from .decisions import is_true
        probability = is_true(f"Foreground program: {process}", LEISURE)
        if probability is None:
            return False  # don't cache: try again when the decision model is back
        _LEISURE[process] = probability >= 0.7
    return _LEISURE[process]


def level(kind: str, minutes_until: float) -> int:
    thresholds = LEVELS.get(kind)
    if not thresholds:
        return 0
    return sum(1 for limit in thresholds if minutes_until <= limit)


def card(item: dict[str, Any], lvl: int, minutes_until: float) -> dict[str, Any]:
    from .conversation_flow import user_timezone
    at = datetime.fromisoformat(item["at"].replace("Z", "+00:00")).astimezone(user_timezone())
    clock = at.strftime("%I:%M %p").lstrip("0")
    day = "" if at.date() == datetime.now(user_timezone()).date() else at.strftime("%a ")
    if item["kind"] == "outing":
        question = f"Are you back? You expected to be back by {day}{clock}."
        options = ["I'm back", "Snooze 1 hour"]
    else:
        when = (f"in {round(minutes_until)} min" if 0 < minutes_until < 90 else
                "now" if -5 <= minutes_until <= 0 else
                f"{round(-minutes_until / 60, 1)} h late" if minutes_until < 0 else f"at {day}{clock}")
        question = f"{item['title']} {when}"
        options = ["Done", "Snooze 15 min" if lvl >= 2 else "Snooze 1 hour", "Got it"]
    return {"kind": "ui", "template": "choice", "id": f"life-{item['kind']}-{item['id']}"[:64],
            "title": question[:200], "devices": ["everywhere"],
            "position": "center" if lvl >= 3 else "top-right",
            "ttl_seconds": 1800 if lvl == 1 else 0,
            "data": {"header": {1: "Coming up", 2: "Soon", 3: "Now"}[lvl], "question": question,
                     "options": options, "allow_other": False}}


class LifeClock:
    def __init__(self, show: Callable[[dict], Any], events: Callable[[str, int], list[dict]],
                 remove: Callable[[str], Any], state_file: Path | None = None,
                 upcoming: Callable[..., list[dict]] | None = None, finish: Callable[..., Any] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 activity: Callable[[], dict[str, dict]] | None = None,
                 leisure: Callable[[str], bool] | None = None):
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
                if not (event.get("type") == "answer" or event.get("name") == "answer"):
                    continue
                choice = ((event.get("data") or {}).get("choices") or [""])[0]
                item = items.get(key.removeprefix("push:"))
                if key.startswith("push:"):
                    entry["quiet_until"] = (now + timedelta(minutes={"10 more minutes": 10}.get(choice, 30))).isoformat()
                    entry["stopped"] = choice == "I need this"
                elif choice in {"Done", "I'm back"} and item:
                    self.finish(item["kind"], item["id"])
                    entry["done"] = True
                elif choice in SNOOZE:
                    entry["snoozed_until"] = (now + timedelta(minutes=SNOOZE[choice])).isoformat()
                else:
                    entry["quiet_level"] = entry.get("level", 0)
                self.remove(card_id)
                entry["card"] = None

    def tick(self) -> list[str]:
        """One pass; returns the cards shown."""
        now = self.clock()
        state = self._load()
        items = {f"{i['kind']}:{i['id']}": i for i in self.upcoming(days=2, now=now)}
        self._answers(state, items, now)
        shown = []
        for key, item in items.items():
            entry = state.setdefault(key, {})
            if entry.get("done"):
                continue
            if entry.get("snoozed_until") and datetime.fromisoformat(entry["snoozed_until"]) > now:
                continue
            minutes = (datetime.fromisoformat(item["at"].replace("Z", "+00:00")) - now).total_seconds() / 60
            lvl = level(item["kind"], minutes)
            if lvl == 0 or lvl <= entry.get("level", 0) and not entry.get("snoozed_until"):
                continue
            if lvl <= entry.get("quiet_level", 0):
                continue
            element = card(item, lvl, minutes)
            try:
                self.show(element)
            except Exception as exc:  # noqa: BLE001 - try again next minute
                LOG.warning("Could not show reminder %s: %s", key, exc)
                continue
            entry.update(level=lvl, card=element["id"], shown_at=now.isoformat(), snoozed_until=None)
            shown.append(element["id"])
        shown += self.accountability(state, items, now)
        for key in [k for k in state if k.removeprefix("push:") not in items]:
            del state[key]  # closed or no longer upcoming
        self._save(state)
        return shown

    def accountability(self, state: dict[str, dict], items: dict[str, dict], now: datetime) -> list[str]:
        """Push back, once per quiet period, on leisure while the most pressing item is close."""
        pressing = []
        for key, item in items.items():
            limit = PUSHBACK.get(item["kind"])
            minutes = (datetime.fromisoformat(item["at"].replace("Z", "+00:00")) - now).total_seconds() / 60
            if limit is not None and minutes <= limit and not state.get(key, {}).get("done"):
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
            due = (f"in {round(minutes)} min" if 0 < minutes < 90 else
                   f"in {round(minutes / 60, 1)} h" if minutes >= 90 else "now")
            question = (f"{item['title']} is due {due}; you've been in {doing['process']} for "
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
