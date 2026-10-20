"""Quick typed decisions through the Router's decision model (Herald /v1/decide, Jev by default).

For repeated, checkable choices: "is this a commitment", "how urgent", "which of these". Each
answer has probabilities and (choice/score) a confidence; act when confident, otherwise fall back
to the LLM path. Returns None when the Router has no decision model, so callers always fall back.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from .settings import settings

LOG = logging.getLogger(__name__)


def decide(state: Any, questions: dict[str, Any], *, timeout: float = 15.0) -> dict[str, Any] | None:
    key = os.environ.get("HERALD_API_KEY")
    try:
        response = httpx.post(settings.herald_url.rstrip("/") + "/v1/decide", timeout=timeout,
                              json={"state": state, "questions": questions},
                              headers={"Authorization": f"Bearer {key}"} if key else {})
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        if response.status_code not in {404, 503}:  # 404: an older Router; 503: no decision model
            LOG.info("Decision request refused (%s): %s", response.status_code, response.text[:200])
        return None
    return response.json().get("answers")


def is_true(state: Any, statement: str, *, criteria: dict[str, str] | None = None) -> float | None:
    """Probability that `statement` holds for `state` (a Noul), or None without a decision model."""
    question: dict[str, Any] = {"type": "noul", "instructions": statement}
    if criteria:
        question["criteria"] = criteria
    answers = decide(state, {"q": question})
    try:
        return float(answers["q"]["noul"]) if answers else None
    except (KeyError, TypeError, ValueError):
        return None
