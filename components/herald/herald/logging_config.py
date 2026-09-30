"""Single place that actually turns Herald's logging on.

Every module across the codebase does `logger = logging.getLogger(...)` and
calls `logger.info(...)`/`logger.debug(...)` -- but Python's logging module
does nothing with those calls until something configures a handler and
level. Nothing did: there was no `logging.basicConfig()`/`dictConfig()`
anywhere in the codebase, so every INFO/DEBUG call from Herald's own loggers
was silently discarded (the root logger's default level is WARNING with no
handler attached). Only WARNING and above ever reached anyone, and that was
only because Python's own "handler of last resort" happens to print those.

Call `configure_logging()` once, as early as possible in a process's
lifetime (before anything else has a chance to log), from both the Router
entry point and the CLI entry point.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

_CONFIGURED = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


# Third-party loggers that are useful at DEBUG for Herald's own code but are
# too chatty to run at DEBUG themselves; capped one notch above whatever
# level Herald's own loggers use, unless the operator asks for everything.
_NOISY_THIRD_PARTY = ("httpx", "httpcore", "urllib3", "asyncio")


def configure_logging(*, force: bool = False) -> None:
    """Attach a handler and level to the root logger exactly once.

    Env vars (read at call time, so tests can monkeypatch them):
      HERALD_LOG_LEVEL  -- DEBUG/INFO/WARNING/ERROR (default INFO)
      HERALD_LOG_FORMAT -- "plain" (default) or "json"
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    level_name = os.environ.get("HERALD_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = os.environ.get("HERALD_LOG_FORMAT", "plain").lower()

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))

    root = logging.getLogger()
    root.setLevel(level)
    # Replace, don't stack -- configure_logging(force=True) is for tests
    # that need a clean handler list, not for accumulating duplicates.
    root.handlers = [handler]

    if level > logging.DEBUG:
        for name in _NOISY_THIRD_PARTY:
            logging.getLogger(name).setLevel(logging.WARNING)

    _CONFIGURED = True
