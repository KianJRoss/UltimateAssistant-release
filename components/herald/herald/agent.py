"""Friendly stateful-agent objects for ordinary Python scripts."""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from herald.client import RouterClient
from herald.errors import DecisionValidationError, HeraldError, InvalidResponseError, SessionBusyError


@dataclass(frozen=True)
class Decision:
    """A validated choice returned by :meth:`StatefulAgent.decide`."""

    choice: str
    reason: str
    confidence: float
    raw: str
    requires_human_review: bool = False


class StatefulAgent:
    """One named model identity with router-managed encrypted memory."""

    def __init__(self, client: RouterClient, session: dict[str, Any]) -> None:
        self._client = client
        self._session = session

    @property
    def id(self) -> str:
        return str(self._session["id"])

    @property
    def name(self) -> str:
        return str(self._session["name"])

    @property
    def model(self) -> str:
        return str(self._session["model"])

    @property
    def turn_count(self) -> int:
        return int(self._session.get("turn_count", 0))

    def ask(self, prompt: str) -> str:
        """Send one input and preserve the exchange in this agent's memory."""
        result = self._client.send_agent_message(
            self.id, prompt, request_id=uuid.uuid4().hex,
        )
        if result.get("error"):
            message = str(result["error"])
            if "409" in message or "busy" in message.lower():
                raise SessionBusyError(message)
            raise HeraldError(message)
        if not isinstance(result.get("content"), str):
            raise InvalidResponseError("Herald returned an invalid agent response")
        self._session = result.get("session") or self._session
        return result["content"]

    chat = ask

    async def aask(self, prompt: str) -> str:
        return await asyncio.to_thread(self.ask, prompt)

    achat = aask

    def stream(self, prompt: str, *, chunk_size: int = 120):
        """Yield a response in readable chunks using the stable sync API."""
        text = self.ask(prompt)
        for start in range(0, len(text), max(1, chunk_size)):
            yield text[start:start + max(1, chunk_size)]

    async def astream(self, prompt: str, *, chunk_size: int = 120):
        text = await self.aask(prompt)
        for start in range(0, len(text), max(1, chunk_size)):
            yield text[start:start + max(1, chunk_size)]

    def review(self, item: Any, *, question: str = "Review this item and return your decision with a concise explanation.") -> str:
        """Review one item while retaining standards and precedents from earlier calls."""
        return self.ask(f"{question}\n\nItem to review:\n{_render(item)}")

    def review_each(self, items: Iterable[Any], *, question: str = "Review this item and return your decision with a concise explanation.") -> list[str]:
        """Review dependent items sequentially through this same memory."""
        return [self.review(item, question=question) for item in items]

    async def areview(self, item: Any, *, question: str = "Review this item and return your decision with a concise explanation.") -> str:
        return await asyncio.to_thread(self.review, item, question=question)

    async def areview_each(self, items: Iterable[Any], *, question: str = "Review this item and return your decision with a concise explanation.") -> list[str]:
        """Async entry point for a sequential, shared-memory review loop."""
        return await asyncio.to_thread(self.review_each, items, question=question)

    def structured(self, prompt: str, result_type: Any) -> Any:
        """Return a response validated by a Pydantic model class."""
        if not hasattr(result_type, "model_json_schema") or not hasattr(result_type, "model_validate"):
            raise TypeError("result_type must be a Pydantic model class")
        request = (
            f"{prompt}\n\nReturn only one JSON object matching this JSON Schema:\n"
            f"{json.dumps(result_type.model_json_schema())}"
        )
        raw = self.ask(request)
        try:
            payload = _json_object(raw)
            return result_type.model_validate(payload)
        except Exception:
            raw = self.ask("Repair your previous response. Return only valid JSON matching the requested schema.")
            try:
                return result_type.model_validate(_json_object(raw))
            except Exception as exc:
                raise InvalidResponseError("Herald could not produce the requested structured result") from exc

    async def astructured(self, prompt: str, result_type: Any) -> Any:
        return await asyncio.to_thread(self.structured, prompt, result_type)

    def decide(
        self,
        *,
        question: str,
        options: Iterable[str],
        context: Any,
        confidence_threshold: float = 0.0,
        result_type: Any = None,
    ) -> Any:
        """Choose exactly one allowed option and return a structured decision."""
        allowed = [str(option) for option in options]
        if not allowed or any(not option.strip() for option in allowed):
            raise ValueError("options must contain at least one non-empty choice")
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be between 0.0 and 1.0")
        rendered_options = "\n".join(f"- {option}" for option in allowed)
        schema_note = ""
        if result_type is not None and hasattr(result_type, "model_json_schema"):
            schema_note = "\nThe complete required JSON Schema is:\n" + json.dumps(result_type.model_json_schema())
        prompt = (
            f"""Make this decision:
{question}

Allowed options:
{rendered_options}

Current item or context:
{_render(context)}

Return only a JSON object with these fields:
{{"choice":"one exact allowed option","reason":"concise explanation","confidence":0.0}}
Confidence must be a number from 0.0 to 1.0.{schema_note}"""
        )
        raw = self.ask(prompt)
        try:
            payload = _json_object(raw)
        except RuntimeError:
            raw = self.ask(
                "Repair your previous answer. Return only one valid JSON object with "
                "choice, reason, and numeric confidence fields."
            )
            payload = _json_object(raw)
        choice = str(payload.get("choice", ""))
        canonical = next((option for option in allowed if option.casefold() == choice.casefold()), None)
        if canonical is None:
            raise DecisionValidationError(
                f"Herald returned choice {choice!r}; expected one of {allowed!r}"
            )
        choice = canonical
        payload["choice"] = choice
        if result_type is not None:
            if hasattr(result_type, "model_validate"):
                return result_type.model_validate(payload)
            raise TypeError("result_type must be a Pydantic model class")
        try:
            confidence = float(payload.get("confidence", 0.0))
        except (TypeError, ValueError) as exc:
            raise DecisionValidationError("Herald returned a non-numeric decision confidence") from exc
        if not 0.0 <= confidence <= 1.0:
            raise DecisionValidationError("Herald decision confidence must be between 0.0 and 1.0")
        return Decision(
            choice=choice,
            reason=str(payload.get("reason", "")),
            confidence=confidence,
            raw=raw,
            requires_human_review=confidence < confidence_threshold,
        )

    async def adecide(self, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self.decide, **kwargs)

    def reset(self, *, force: bool = False, instructions: str = "") -> None:
        """Erase this session's conversation memory while keeping its identity."""
        result = self._client.reset_agent_session(
            self.id, force=force, instructions=instructions,
        )
        if result.get("error"):
            raise HeraldError(str(result["error"]))
        self._session = result.get("session") or self._session

    def inspect(self) -> dict[str, Any]:
        return self._client.inspect_agent_session(self.id)

    def export(self) -> dict[str, Any]:
        return self._client.export_agent_session(self.id)

    def delete(self) -> None:
        result = self._client.delete_agent_session(self.id)
        if result.get("error"):
            raise HeraldError(str(result["error"]))


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


def _json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    start = text.find("{")
    if start < 0:
        raise InvalidResponseError("Herald did not return the requested decision JSON")
    try:
        value, _ = decoder.raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise InvalidResponseError("Herald returned malformed decision JSON") from exc
    if not isinstance(value, dict):
        raise InvalidResponseError("Herald decision response must be a JSON object")
    return value
