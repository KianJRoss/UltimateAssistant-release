from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .settings import settings
from .flashcard_store import FlashcardStore
from .study_store import StudySessionStore


mcp = FastMCP("ultimate-assistant-study-sessions")


def _store() -> StudySessionStore:
    path = Path(os.environ.get("ULTIMATE_ASSISTANT_STUDY_DB", str(settings.study_sessions_db)))
    return StudySessionStore(path)


def _flashcards() -> FlashcardStore:
    path = Path(os.environ.get("ULTIMATE_ASSISTANT_FLASHCARDS_DB", str(settings.flashcards_db)))
    return FlashcardStore(path)


@mcp.tool()
def list_study_sessions(include_completed: bool = False, limit: int = 30) -> str:
    """List locally saved study sessions and their planned/completed status."""
    sessions = _store().list_sessions(include_completed=include_completed, limit=limit)
    return json.dumps({"sessions": sessions}, ensure_ascii=False)


@mcp.tool()
def schedule_study_session(
    subject: str, title: str, planned_minutes: int,
    planned_start: str = "", notes: str = "",
) -> str:
    """Save a study session after the user asks to schedule or save one."""
    subject = subject.strip()
    title = title.strip()
    if not subject or len(subject) > 160 or not title or len(title) > 300:
        return json.dumps({"error": "Subject must be 1–160 characters and title 1–300 characters."})
    if planned_minutes < 5 or planned_minutes > 600:
        return json.dumps({"error": "Planned minutes must be between 5 and 600."})
    start = planned_start.strip() or None
    if start:
        try:
            start = datetime.fromisoformat(start).isoformat()
        except ValueError:
            return json.dumps({"error": "Planned start must be an ISO date or datetime."})
    session = _store().create_session(subject, title, planned_minutes, planned_start=start, notes=notes)
    return json.dumps({"session": session}, ensure_ascii=False)


@mcp.tool()
def start_study_session(session_id: str) -> str:
    """Mark a planned study session as started."""
    session = _store().start_session(session_id.strip())
    return json.dumps({"session": session} if session else {"error": "No planned session has that ID."})


@mcp.tool()
def complete_study_session(session_id: str, actual_minutes: int = 0, notes: str = "") -> str:
    """Record completion and optional actual duration/learning notes."""
    if actual_minutes < 0 or actual_minutes > 600:
        return json.dumps({"error": "Actual minutes must be between 0 and 600."})
    session = _store().complete_session(
        session_id.strip(), actual_minutes=actual_minutes or None, notes=notes,
    )
    return json.dumps({"session": session} if session else {"error": "No active session has that ID."}, ensure_ascii=False)


@mcp.tool()
def create_flashcard_deck(name: str, subject: str, cards_json: str, source: str = "") -> str:
    """Save user-requested flashcards from material they provided; cards_json is a JSON list of front/back objects."""
    if not name.strip() or len(name) > 160 or not subject.strip() or len(subject) > 160:
        return json.dumps({"error": "Deck name and subject must each be 1–160 characters."})
    try:
        cards = json.loads(cards_json)
    except json.JSONDecodeError:
        return json.dumps({"error": "cards_json must be a JSON array."})
    if not isinstance(cards, list) or not 1 <= len(cards) <= 100:
        return json.dumps({"error": "Provide between 1 and 100 cards."})
    cleaned = []
    for card in cards:
        if not isinstance(card, dict) or not isinstance(card.get("front"), str) or not isinstance(card.get("back"), str):
            return json.dumps({"error": "Every card needs string front and back fields."})
        front, back = card["front"].strip(), card["back"].strip()
        if not front or not back or len(front) > 2000 or len(back) > 4000:
            return json.dumps({"error": "Card fronts must be 1–2000 characters and backs 1–4000 characters."})
        cleaned.append({"front": front, "back": back})
    deck = _flashcards().create_deck(name, subject, cleaned, source)
    return json.dumps({"deck": deck}, ensure_ascii=False)


@mcp.tool()
def list_flashcard_decks() -> str:
    """List saved flashcard decks with total and currently due card counts."""
    return json.dumps({"decks": _flashcards().list_decks()}, ensure_ascii=False)


@mcp.tool()
def get_due_flashcards(deck_id: str = "", limit: int = 20) -> str:
    """Return due flashcards for active recall; present the front first and wait for the user's attempt before revealing the back."""
    cards = _flashcards().due_cards(deck_id=deck_id.strip() or None, limit=limit)
    return json.dumps({"cards": cards}, ensure_ascii=False)


@mcp.tool()
def review_flashcard(card_id: str, rating: str) -> str:
    """Record a learner's self-rating (again, hard, good, easy) and schedule the next review."""
    try:
        card = _flashcards().review_card(card_id.strip(), rating.strip().lower())
    except ValueError as error:
        return json.dumps({"error": str(error)})
    return json.dumps({"card": card} if card else {"error": "Flashcard not found."}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
