from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any


_BOX_DAYS = (0, 1, 3, 7, 14, 30, 60)


class FlashcardStore:
    def __init__(self, database: Path) -> None:
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS flashcard_decks (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS flashcards (
                    id TEXT PRIMARY KEY,
                    deck_id TEXT NOT NULL REFERENCES flashcard_decks(id) ON DELETE CASCADE,
                    front TEXT NOT NULL,
                    back TEXT NOT NULL,
                    box INTEGER NOT NULL DEFAULT 0,
                    due_at TEXT NOT NULL,
                    review_count INTEGER NOT NULL DEFAULT 0,
                    last_reviewed TEXT
                )"""
            )
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("CREATE INDEX IF NOT EXISTS flashcards_due ON flashcards(deck_id, due_at)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create_deck(self, name: str, subject: str, cards: list[dict[str, str]], source: str = "") -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        deck_id = uuid.uuid4().hex
        clean_cards = [
            (uuid.uuid4().hex, card["front"].strip(), card["back"].strip())
            for card in cards
        ]
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO flashcard_decks (id,name,subject,source,created_at) VALUES (?,?,?,?,?)",
                (deck_id, name.strip(), subject.strip(), source.strip()[:1000], now.isoformat()),
            )
            connection.executemany(
                "INSERT INTO flashcards (id,deck_id,front,back,box,due_at) VALUES (?,?,?,?,0,?)",
                [(card_id, deck_id, front, back, now.isoformat()) for card_id, front, back in clean_cards],
            )
        return {"id": deck_id, "name": name.strip(), "subject": subject.strip(), "card_count": len(clean_cards)}

    def list_decks(self) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT d.*, COUNT(c.id) AS card_count, "
                "COALESCE(SUM(CASE WHEN c.due_at <= ? THEN 1 ELSE 0 END), 0) AS due_count "
                "FROM flashcard_decks d LEFT JOIN flashcards c ON c.deck_id=d.id "
                "GROUP BY d.id ORDER BY d.created_at DESC",
                (datetime.now(timezone.utc).isoformat(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def due_cards(self, *, deck_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        condition = "WHERE c.due_at <= ?" + (" AND c.deck_id=?" if deck_id else "")
        params: tuple[Any, ...] = (now, deck_id, max(1, min(limit, 100))) if deck_id else (now, max(1, min(limit, 100)))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT c.*, d.name AS deck_name, d.subject FROM flashcards c "
                "JOIN flashcard_decks d ON d.id=c.deck_id " + condition + " ORDER BY c.due_at, c.review_count LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def review_card(self, card_id: str, rating: str) -> dict[str, Any] | None:
        if rating not in {"again", "hard", "good", "easy"}:
            raise ValueError("Rating must be again, hard, good, or easy.")
        now = datetime.now(timezone.utc)
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT box FROM flashcards WHERE id=?", (card_id,)).fetchone()
            if not row:
                return None
            old_box = int(row["box"])
            if rating == "again":
                new_box, delay = 0, 0
            elif rating == "hard":
                new_box = old_box
                delay = max(1, _BOX_DAYS[new_box])
            else:
                new_box = min(len(_BOX_DAYS) - 1, old_box + (2 if rating == "easy" else 1))
                delay = _BOX_DAYS[new_box]
            due_at = (now + timedelta(days=delay)).isoformat()
            connection.execute(
                "UPDATE flashcards SET box=?, due_at=?, review_count=review_count+1, last_reviewed=? WHERE id=?",
                (new_box, due_at, now.isoformat(), card_id),
            )
            updated = connection.execute("SELECT * FROM flashcards WHERE id=?", (card_id,)).fetchone()
        return dict(updated) if updated else None
