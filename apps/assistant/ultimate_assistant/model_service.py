"""Per-runtime local model preferences, shared by the app and its tool processes."""
import json
import os
from pathlib import Path
from .settings import settings


def preference(environment: str, key: str, default: str) -> str:
    if os.environ.get(environment):
        return os.environ[environment]
    root = os.environ.get('ULTIMATE_ASSISTANT_USER_ROOT')
    file = Path(root) / 'settings.json' if root else settings.user_settings_file
    try:
        saved = json.loads(file.read_text('utf-8'))
        return str(saved.get(key) or default) if isinstance(saved, dict) else default
    except (OSError, ValueError):
        return default
