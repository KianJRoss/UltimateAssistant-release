"""Persistent storage locations for installed and existing Herald Routers."""
from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    """Return Herald's user-owned data directory, creating it when needed."""
    configured = os.environ.get("HERALD_DATA_DIR")
    path = Path(configured).expanduser() if configured else Path.home() / ".herald"
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path(
    filename: str,
    *,
    env_var: str,
    legacy_path: Path | None = None,
) -> Path:
    """Resolve a database without disrupting an existing source installation.

    Explicit configuration always wins. Fresh installations store state under
    the user data directory. A pre-existing package-local database remains in
    use only as a backwards-compatibility fallback when no new path was
    explicitly selected and no user-data copy exists.
    """
    configured = os.environ.get(env_var)
    if configured:
        path = Path(configured).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    preferred = data_dir() / filename
    if os.environ.get("HERALD_DATA_DIR"):
        return preferred
    if preferred.exists():
        return preferred
    if legacy_path is not None and legacy_path.exists():
        return legacy_path
    return preferred
