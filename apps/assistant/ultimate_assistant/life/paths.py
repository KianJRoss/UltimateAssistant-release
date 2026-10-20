"""Where the user's life records live (commitments, routines, plans, outings, work sessions).

ULTIMATE_ASSISTANT_LIFE_HOME when set, else default_root() when a build on top sets it, otherwise
the assistant's data folder: <user data>/life. Each store keeps its own subfolder.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable


# A build on top of the base may keep its own layout (the personal runtime keeps its existing
# folder): it sets this to a function returning the root, read on every call.
default_root: Callable[[], Path] | None = None


def life_root() -> Path:
    configured = os.environ.get("ULTIMATE_ASSISTANT_LIFE_HOME")
    if configured:
        return Path(configured)
    if default_root is not None:
        return default_root()
    from ..settings import settings
    return settings.user_data_dir / "life"
