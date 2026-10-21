"""The phone app's server side: pairing, the phone's view of the conversation, commands the
assistant sends to the phone, the canvas on the phone, and voice.

The phone only displays and controls; the assistant itself runs on the computer (the main node).
Everything here is the same for every user: one paired phone per assistant, its records under
<user data>/phone (or the folder a build on top sets through life.paths.default_root, so an
existing layout keeps working).
"""
from __future__ import annotations

import os
from pathlib import Path

# The paired phone's id in records and commands. One phone per assistant for now; a build on top
# with existing records can keep its own id (set before this package is imported).
DEFAULT_DEVICE = os.environ.get("ULTIMATE_ASSISTANT_PHONE_DEVICE") or "phone"


def phone_root() -> Path:
    configured = os.environ.get("ULTIMATE_ASSISTANT_PHONE_HOME")
    if configured:
        return Path(configured)
    from ..life import paths
    if paths.default_root is not None:
        return paths.default_root()
    from ..settings import settings
    return settings.user_data_dir / "phone"


def conversation_id() -> str:
    """The phone's durable conversation view (the assistant follows the user's current
    conversation inside it; see unified_chat)."""
    return os.environ.get("ULTIMATE_ASSISTANT_PHONE_CONVERSATION") or os.environ.get("PERSONAL_CONVERSATION_ID") or "main"
