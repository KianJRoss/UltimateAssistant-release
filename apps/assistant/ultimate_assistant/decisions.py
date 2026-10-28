"""Quick typed decisions (the low-level call; decision points go through decision_interface.run) through the Router's decision model (Herald /v1/decide, Jev by default).

For repeated, checkable choices: "is this a commitment", "how urgent", "which of these". Each
answer has probabilities and (choice/score) a confidence; act when confident, otherwise fall back
to the LLM path. Returns None when the Router has no decision model, so callers always fall back.
"""
from __future__ import annotations

import logging
from typing import Any

LOG = logging.getLogger(__name__)


def decide(state: Any, questions: dict[str, Any], *, timeout: float = 15.0,
           provider: str | None = None, model: str | None = None) -> dict[str, Any] | None:
    """The answers, or None, through the Herald SDK. Bypassed on the default `llm` route."""
    from . import decision_interface as di
    config = di.decision_settings()
    if config["route"] != di.ROUTE_DECISION_MODEL:
        return None
    try:
        envelope = di.herald_decide(state, questions, provider=provider or config["provider"] or None,
                                    model=model or config["model"] or None, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 404: an older Router; 503: no decision model
        LOG.info("Decision request failed: %s", type(exc).__name__)
        return None
    answers = envelope.get("answers")
    return answers if isinstance(answers, dict) else None


def is_true(state: Any, statement: str, *, criteria: dict[str, str] | None = None) -> float | None:
    """Probability that `statement` holds for `state` (a Noul), or None without a decision model."""
    question: dict[str, Any] = {"type": "noul", "instructions": statement}
    if criteria:
        question["criteria"] = criteria
    answers = decide(state, {"q": question})
    from .decision_interface import _number
    try:
        return _number(answers["q"]["noul"]) if answers else None  # NaN / out of range: unknown
    except (KeyError, TypeError):
        return None
