"""Redaction helpers for errors and telemetry that may contain credential URLs."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_URL = re.compile(r"https?://[^\s'\"<>]+")
_SENSITIVE = re.compile(r"(key|token|secret|password|signature|credential|auth)", re.I)


def sanitize_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if not parts.query:
            return url
        query = urlencode([
            (name, "[REDACTED]" if _SENSITIVE.search(name) else "[MASKED]")
            for name, _value in parse_qsl(parts.query, keep_blank_values=True)
        ])
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
    except (TypeError, ValueError):
        return "[REDACTED URL]"


def sanitize_error(error: Any) -> str:
    """Render an exception/message with every URL query value removed."""
    text = str(error)
    return _URL.sub(lambda match: sanitize_url(match.group(0)), text)
