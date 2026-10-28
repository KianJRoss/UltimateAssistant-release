"""Voice conversation decisions owned by the assistant backend and its route setting."""
from __future__ import annotations

import re

_FAREWELL_RE = re.compile(
    r"^\s*(?:okay\s+|ok\s+|alright\s+|hey\s+|well\s+)?"
    r"(?:thank(?:s|\s+you)(?:\s+so\s+much|\s+very\s+much)?[\s,.!]*)?"
    r"(?:that(?:'s|\s+is)\s+all|that(?:'s|\s+is)\s+it|nothing\s+else|"
    r"stop\s+listening|end\s+(?:the\s+)?conversation|goodbye|bye(?:\s+for\s+now)?|"
    r"see\s+you(?:\s+later)?|never\s*mind|i(?:'m|\s+am)\s+done|all\s+good(?:\s+now)?)"
    r"[\s.!?,]*$",
    re.IGNORECASE,
)


def is_farewell(text: str) -> bool:
    return bool(_FAREWELL_RE.match(str(text or "").strip()))


FAREWELL = ("The user is ending the voice conversation now: saying goodbye, thanks that's all, or asking the "
            "assistant to stop listening; not asking for anything else.")
FAREWELL_MAX_WORDS = 12  # a longer utterance is a request, not a goodbye


def wants_to_stop(text: str) -> bool:
    """Is the user saying goodbye? A typed yes/no (decision_interface): on the `decision_model` route the
    model answers when confident; the regex above is the rule form on the default `llm` route and when
    the typed answer is uncertain or unavailable. Long utterances are never goodbyes (no call is made)."""
    text = str(text or "").strip()
    if not text or len(text.split()) > FAREWELL_MAX_WORDS:
        return False
    from . import decision_interface as di
    outcome = di.yes_no("farewell", FAREWELL, text, rule=lambda: is_farewell(text))
    return bool(outcome.value) if outcome.ok else False


