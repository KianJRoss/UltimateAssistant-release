from __future__ import annotations

import importlib.util
import os
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    herald_url: str = os.environ.get("HERALD_URL", "http://127.0.0.1:8790")
    herald_model: str = os.environ.get("HERALD_MODEL", "codex-primary")
    herald_session: str = os.environ.get("ULTIMATE_ASSISTANT_SESSION", "ultimate-assistant")
    herald_project: str = os.environ.get("ULTIMATE_ASSISTANT_PROJECT", "friend-assistant")
    herald_part: str = os.environ.get("ULTIMATE_ASSISTANT_PART", "conversation")

    @property
    def workspace_root(self) -> Path:
        return Path(__file__).resolve().parents[3]

    @property
    def herald_source_root(self) -> Path:
        """The directory holding the `herald` package: a development checkout named by
        HERALD_SOURCE_ROOT, else wherever setup installed the pinned Herald release."""
        configured_root = os.environ.get("HERALD_SOURCE_ROOT")
        if configured_root:
            return Path(configured_root)
        spec = importlib.util.find_spec("herald")
        if not spec or not spec.origin:
            raise RuntimeError("Herald is not installed in this environment; run apps\assistant\setup.ps1")
        return Path(spec.origin).resolve().parents[1]

    @property
    def web_search_server(self) -> Path:
        return self.herald_source_root / "herald" / "websearch_tools.py"

    @property
    def user_settings_file(self) -> Path:
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / ".ultimate-assistant"
        return base / "UltimateAssistant" / "settings.json"

    @property
    def user_data_dir(self) -> Path:
        return self.user_settings_file.parent / "data"

    @property
    def local_search_db(self) -> Path:
        configured_root = os.environ.get("COLLEGE_ASSISTANT_ROOT")
        if configured_root:
            return Path(configured_root) / "data" / "local_search.db"
        return self.user_data_dir / "knowledge" / "local_search.db"

    @property
    def conversations_db(self) -> Path:
        return self.user_data_dir / "conversations.sqlite3"

    @property
    def tasks_db(self) -> Path:
        return self.user_data_dir / "tasks.sqlite3"

    @property
    def memory_db(self) -> Path:
        return self.user_data_dir / "memory.sqlite3"

    @property
    def assistant_files_root(self) -> Path:
        configured = os.environ.get("ULTIMATE_ASSISTANT_FILES_ROOT")
        if configured:
            return Path(configured).expanduser().resolve()
        try:
            config = json.loads(self.user_settings_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = {}
        saved = config.get("files_root") if isinstance(config, dict) else None
        if saved:
            return Path(str(saved)).expanduser().resolve()
        documents = Path.home() / "Documents"
        return documents / "UltimateAssistant"


settings = Settings()
