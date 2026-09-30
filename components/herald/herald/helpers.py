"""Small, validated AI operations built on Herald's routing layer.

The functions in this module deliberately share one implementation with the
project-scoped :class:`herald.project.Part` conveniences.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from pydantic import TypeAdapter, ValidationError

from herald.client import InvocationResult
from herald.errors import InvalidResponseError, RouterUnavailableError

T = TypeVar("T")
Invoker = Callable[..., InvocationResult]


def _default_invoke(prompt: str, **options: Any) -> InvocationResult:
    # Import lazily: herald.__init__ re-exports this module's public functions.
    from herald import _get_client
    return _get_client().invoke(prompt, **options)


def _call(prompt: str, invoke: Invoker | None, **options: Any) -> str:
    result = (invoke or _default_invoke)(prompt, **options)
    # A tuple is accepted as a small compatibility convenience for custom clients,
    # but plain strings are deliberately not: they cannot represent failure state.
    if isinstance(result, tuple) and len(result) == 2:
        result = InvocationResult(bool(result[0]), str(result[1]))
    if not isinstance(result, InvocationResult):
        raise TypeError("AI helper invoker must return InvocationResult")
    if not result.ok:
        if result.error_kind == "timeout":
            message = "AI helper request timed out"
        elif result.error:
            message = f"AI helper router invocation failed: {result.error}"
        else:
            message = "AI helper router invocation failed"
        raise RouterUnavailableError(message)
    if not isinstance(result.content, str):
        raise InvalidResponseError("AI helper returned a non-text response")
    return result.content


def summarize(
    text: str, *, max_words: int | None = None, model: str | None = None,
    timeout: float | None = None, mode: str = "efficiency",
    client: Any = None,
    _invoke: Invoker | None = None,
) -> str:
    """Summarize *text*, optionally targeting a maximum word count."""
    if max_words is not None and max_words < 1:
        raise ValueError("max_words must be a positive integer")
    limit = f" in at most {max_words} words" if max_words is not None else ""
    prompt = f"Summarize the following content{limit}. Return only the summary.\n\n{text}"
    return _call(prompt, _invoke or (client.invoke if client else None),
                 model=model, timeout=timeout, mode=mode).strip()


async def asummarize(text: str, **kwargs: Any) -> str:
    """Async form of :func:`summarize` without blocking the event loop."""
    return await asyncio.to_thread(summarize, text, **kwargs)


def classify(
    text: str, labels: Sequence[str], *, model: str | None = None,
    timeout: float | None = None, mode: str = "efficiency",
    client: Any = None,
    _invoke: Invoker | None = None,
) -> str:
    """Choose exactly one of *labels*, rejecting empty or invented results."""
    if isinstance(labels, (str, bytes)):
        raise ValueError("labels must be a sequence of non-empty strings")
    choices = tuple(labels)
    if not choices or any(not isinstance(label, str) or not label.strip() for label in choices):
        raise ValueError("labels must contain one or more non-empty strings")
    if len(set(choices)) != len(choices):
        raise ValueError("labels must be unique")
    prompt = (
        "Classify the input as exactly one allowed label. Return only the label, "
        "with identical spelling and punctuation.\nAllowed labels: "
        f"{json.dumps(choices)}\n\nInput:\n{text}"
    )
    result = _call(prompt, _invoke or (client.invoke if client else None),
                   model=model, timeout=timeout, mode=mode)
    if not result or result not in choices:
        raise InvalidResponseError("AI classification was not one of the allowed labels")
    return result


async def aclassify(text: str, labels: Sequence[str], **kwargs: Any) -> str:
    """Async form of :func:`classify`."""
    return await asyncio.to_thread(classify, text, labels, **kwargs)


_JSON_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.IGNORECASE | re.DOTALL)


def _json_payload(response: str) -> Any:
    match = _JSON_FENCE.fullmatch(response.strip())
    candidate = match.group(1) if match else response.strip()
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        raise InvalidResponseError("AI extraction returned malformed JSON") from None


def extract(
    text: str, schema: type[T], *, model: str | None = None,
    timeout: float | None = None, mode: str = "efficiency",
    client: Any = None,
    _invoke: Invoker | None = None,
) -> T:
    """Extract JSON from *text* and validate it against a Pydantic-compatible type."""
    try:
        adapter = TypeAdapter(schema)
        schema_json = json.dumps(adapter.json_schema(), separators=(",", ":"))
    except Exception as exc:
        raise TypeError("schema must be a Pydantic-compatible type") from exc
    prompt = (
        "Extract the requested data from the input. Return only valid JSON matching "
        f"this JSON Schema: {schema_json}\n\nInput:\n{text}"
    )
    payload = _json_payload(_call(
        prompt, _invoke or (client.invoke if client else None),
        model=model, timeout=timeout, mode=mode,
    ))
    try:
        return adapter.validate_python(payload)
    except ValidationError:
        # Do not leak model output, source text, or detailed values in the API error.
        raise InvalidResponseError("AI extraction did not match the requested schema") from None


async def aextract(text: str, schema: type[T], **kwargs: Any) -> T:
    """Async form of :func:`extract`."""
    return await asyncio.to_thread(extract, text, schema, **kwargs)


_TRUE_WORDS = {"true", "yes"}
_FALSE_WORDS = {"false", "no"}


def confirm(
    question: str, context: Any = None, *, model: str | None = None,
    timeout: float | None = None, mode: str = "efficiency",
    client: Any = None,
    _invoke: Invoker | None = None,
) -> bool:
    """A validated yes/no gate for a judgment call too fuzzy for a plain `if`.

    Raises InvalidResponseError instead of guessing when the model's answer
    isn't unambiguously true/false -- never silently defaults to False.
    """
    context_block = f"\n\nContext:\n{context}" if context is not None else ""
    prompt = (
        f"Answer this yes/no question. Return only the single word true or "
        f"false, nothing else.\n\nQuestion:\n{question}{context_block}"
    )
    result = _call(prompt, _invoke or (client.invoke if client else None),
                   model=model, timeout=timeout, mode=mode).strip().casefold()
    if result in _TRUE_WORDS:
        return True
    if result in _FALSE_WORDS:
        return False
    raise InvalidResponseError("AI confirmation was not an unambiguous true/false answer")


async def aconfirm(question: str, context: Any = None, **kwargs: Any) -> bool:
    """Async form of :func:`confirm`."""
    return await asyncio.to_thread(confirm, question, context, **kwargs)


def score(
    text: str, criteria: str, *, low: float = 0.0, high: float = 1.0,
    model: str | None = None, timeout: float | None = None, mode: str = "efficiency",
    client: Any = None,
    _invoke: Invoker | None = None,
) -> float:
    """Rate *text* against *criteria* on a numeric scale, validated in-range.

    For triage/priority/quality judgments that need a number, not a label --
    e.g. "how urgent is this ticket" or "how well does this draft meet the
    style guide," where a fixed formula doesn't exist but a rubric does.
    """
    if not (low < high):
        raise ValueError("low must be less than high")
    prompt = (
        f"Rate the input against this criteria on a scale from {low} to {high}. "
        f"Return only the number, nothing else.\n\nCriteria:\n{criteria}\n\nInput:\n{text}"
    )
    result = _call(prompt, _invoke or (client.invoke if client else None),
                   model=model, timeout=timeout, mode=mode).strip()
    try:
        value = float(result)
    except ValueError:
        raise InvalidResponseError("AI score was not a parseable number") from None
    if not (low <= value <= high):
        raise InvalidResponseError(f"AI score {value} was outside the requested range [{low}, {high}]")
    return value


async def ascore(text: str, criteria: str, **kwargs: Any) -> float:
    """Async form of :func:`score`."""
    return await asyncio.to_thread(score, text, criteria, **kwargs)


def compare(
    a: str, b: str, question: str, *, allow_tie: bool = True,
    model: str | None = None, timeout: float | None = None, mode: str = "efficiency",
    client: Any = None,
    _invoke: Invoker | None = None,
) -> str:
    """Pick the better of two options for *question*. Returns "a", "b", or
    "tie" (unless allow_tie=False). Building block for best-of-N selection
    and pairwise ranking."""
    tie_note = ' or "tie" if they are equivalent' if allow_tie else ""
    prompt = (
        f"Compare option A and option B and answer this question: {question}\n"
        f"Return only the single word a or b{tie_note}, nothing else.\n\n"
        f"Option A:\n{a}\n\nOption B:\n{b}"
    )
    result = _call(prompt, _invoke or (client.invoke if client else None),
                   model=model, timeout=timeout, mode=mode).strip().casefold()
    allowed = {"a", "b", "tie"} if allow_tie else {"a", "b"}
    if result not in allowed:
        raise InvalidResponseError(f"AI comparison was not one of {sorted(allowed)!r}")
    return result


async def acompare(a: str, b: str, question: str, **kwargs: Any) -> str:
    """Async form of :func:`compare`."""
    return await asyncio.to_thread(compare, a, b, question, **kwargs)


def route(
    text: str, handlers: dict[str, Callable[[str], Any]], *,
    model: str | None = None, timeout: float | None = None, mode: str = "efficiency",
    client: Any = None,
    _invoke: Invoker | None = None,
) -> Any:
    """Classify *text* into one of `handlers`' keys, then call that handler
    with *text* and return its result. Turns the common
    "classify(), then if/elif on the label" pattern into one call."""
    if not handlers:
        raise ValueError("handlers must contain at least one entry")
    label = classify(
        text, list(handlers.keys()), model=model, timeout=timeout, mode=mode,
        client=client, _invoke=_invoke,
    )
    return handlers[label](text)


async def aroute(text: str, handlers: dict[str, Callable[[str], Any]], **kwargs: Any) -> Any:
    """Async form of :func:`route`. Handler results are awaited if the
    matched handler itself returns a coroutine."""
    result = await asyncio.to_thread(route, text, handlers, **kwargs)
    if asyncio.iscoroutine(result):
        return await result
    return result


def moderate(
    text: str, categories: Sequence[str], *, model: str | None = None,
    timeout: float | None = None, mode: str = "efficiency",
    client: Any = None,
    _invoke: Invoker | None = None,
) -> dict[str, bool]:
    """Flag *text* against several independent *categories* (unlike
    classify()'s exactly-one constraint, any number of categories may match
    at once). Returns a dict with exactly the requested keys."""
    if isinstance(categories, (str, bytes)):
        raise ValueError("categories must be a sequence of non-empty strings")
    names = tuple(categories)
    if not names or any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("categories must contain one or more non-empty strings")
    prompt = (
        "Evaluate the input against each category independently. Return only a "
        "JSON object mapping every category name to true or false, with no "
        f"other keys.\nCategories: {json.dumps(names)}\n\nInput:\n{text}"
    )
    payload = _json_payload(_call(
        prompt, _invoke or (client.invoke if client else None),
        model=model, timeout=timeout, mode=mode,
    ))
    if not isinstance(payload, dict) or set(payload.keys()) != set(names):
        raise InvalidResponseError("AI moderation did not return exactly the requested categories")
    if any(not isinstance(value, bool) for value in payload.values()):
        raise InvalidResponseError("AI moderation returned a non-boolean category value")
    return {name: payload[name] for name in names}


async def amoderate(text: str, categories: Sequence[str], **kwargs: Any) -> dict[str, bool]:
    """Async form of :func:`moderate`."""
    return await asyncio.to_thread(moderate, text, categories, **kwargs)


def redact(
    text: str, patterns: Sequence[str], *, replacement: str = "[REDACTED]",
    model: str | None = None, timeout: float | None = None, mode: str = "efficiency",
    client: Any = None,
    _invoke: Invoker | None = None,
) -> str:
    """Replace content matching semantic *patterns* (not just regex-matchable
    ones -- "anyone's home address," "informal mentions of a diagnosis")
    with *replacement*, leaving everything else unchanged.

    This is model output, not a guarantee: for regulatory-grade redaction,
    pair it with deterministic regex/rule passes rather than relying on it
    alone.
    """
    if isinstance(patterns, (str, bytes)):
        raise ValueError("patterns must be a sequence of non-empty strings")
    names = tuple(patterns)
    if not names or any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("patterns must contain one or more non-empty strings")
    prompt = (
        "Redact every occurrence of the following from the input, replacing "
        f"each with the exact literal text {replacement!r}. Leave all other "
        "content, formatting, and whitespace exactly as it is. Return only "
        f"the redacted text.\nRedact: {json.dumps(names)}\n\nInput:\n{text}"
    )
    return _call(prompt, _invoke or (client.invoke if client else None),
                 model=model, timeout=timeout, mode=mode)


async def aredact(text: str, patterns: Sequence[str], **kwargs: Any) -> str:
    """Async form of :func:`redact`."""
    return await asyncio.to_thread(redact, text, patterns, **kwargs)


def translate(
    text: str, target_language: str, *, model: str | None = None,
    timeout: float | None = None, mode: str = "efficiency",
    client: Any = None,
    _invoke: Invoker | None = None,
) -> str:
    """Translate *text* into *target_language*."""
    if not target_language.strip():
        raise ValueError("target_language must be non-empty")
    prompt = (
        f"Translate the following text into {target_language}. Return only "
        f"the translation, with no explanation.\n\n{text}"
    )
    return _call(prompt, _invoke or (client.invoke if client else None),
                 model=model, timeout=timeout, mode=mode).strip()


async def atranslate(text: str, target_language: str, **kwargs: Any) -> str:
    """Async form of :func:`translate`."""
    return await asyncio.to_thread(translate, text, target_language, **kwargs)


__all__ = [
    "summarize", "asummarize", "classify", "aclassify", "extract", "aextract",
    "confirm", "aconfirm", "score", "ascore", "compare", "acompare",
    "route", "aroute", "moderate", "amoderate", "redact", "aredact",
    "translate", "atranslate",
]
