"""What the user's studying looks like, read from the school module's own records when it's installed.

The school module (optional-modules/school) keeps study sessions, flashcards and understanding checks
in <modules>/school/data/school.sqlite. The today view and the planner show this; they never write it,
and they work without it.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


def school_db() -> Path | None:
    configured = os.environ.get("ULTIMATE_ASSISTANT_SCHOOL_DB")
    if configured:
        path = Path(configured)
    else:
        from ..settings import settings
        path = settings.user_data_dir / "modules" / "school" / "data" / "school.sqlite"
    return path if path.is_file() else None


def snapshot(now_iso: str | None = None, *, path: Path | None = None) -> dict[str, Any]:
    """{"state", "understanding": {"subject / topic": latest check}, "flashcards": [{deck, cards, due}]}.
    path: a school.sqlite to read instead of the installed module's (tests, other layouts)."""
    path = (Path(path) if Path(path).is_file() else None) if path is not None else school_db()
    if path is None:
        return {"state": "not_installed", "understanding": {}, "flashcards": []}
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            latest = db.execute(
                "SELECT subject, topic, outcome, evidence, at FROM checks c WHERE at = "
                "(SELECT MAX(at) FROM checks d WHERE d.subject = c.subject AND d.topic = c.topic)").fetchall()
            moment = now_iso or ""
            decks = db.execute(
                "SELECT d.name, d.subject, COUNT(c.id) AS cards, SUM(c.due <= ?) AS due FROM decks d "
                "LEFT JOIN cards c ON c.deck_id = d.id GROUP BY d.id ORDER BY d.created_at", (moment,)).fetchall()
    except sqlite3.Error as exc:
        return {"state": "unavailable", "error": str(exc)[:200], "understanding": {}, "flashcards": []}
    return {"state": "ready",
            "understanding": {f"{r['subject']} / {r['topic']}": {"outcome": r["outcome"], "at": r["at"],
                                                                 "evidence": r["evidence"]} for r in latest},
            "flashcards": [{"deck": r["name"], "subject": r["subject"], "cards": r["cards"], "due": r["due"] or 0}
                           for r in decks]}
