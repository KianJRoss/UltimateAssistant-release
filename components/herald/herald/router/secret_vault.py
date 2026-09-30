"""Encrypted-at-rest secret artifacts for Herald.

The vault key is machine/user scoped. On Windows it is protected with DPAPI;
else Herald first tries the OS keyring and finally a mode-0600 local key file.
Vault payloads are never returned by ordinary inventory APIs.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


DEFAULT_VAULT_ROOT = Path.home() / ".herald" / "vault"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _restrict(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


class SecretVault:
    def __init__(self, root: str | Path | None = None) -> None:
        configured = os.environ.get("HERALD_DATA_DIR")
        self.root = Path(root) if root is not None else (Path(configured).expanduser() / "vault" if configured else DEFAULT_VAULT_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(self._load_or_create_key())

    def _load_or_create_key(self) -> bytes:
        configured = os.environ.get("HERALD_VAULT_KEY")
        if configured:
            try:
                Fernet(configured.encode())
            except (ValueError, TypeError) as exc:
                raise RuntimeError("HERALD_VAULT_KEY is not a valid Fernet key") from exc
            return configured.encode()

        if os.name == "nt":
            try:
                import win32crypt
                # v1 incorrectly indexed CryptProtectData's bytes result and
                # wrote an empty file with current pywin32. Keep that file in
                # place for forensic/recovery purposes and use a corrected,
                # explicitly versioned key from now on.
                protected_path = self.root / ".master.dpapi.v2"
                if protected_path.exists():
                    return bytes(win32crypt.CryptUnprotectData(protected_path.read_bytes(), None, None, None, 0)[1])
                key = Fernet.generate_key()
                protected_result = win32crypt.CryptProtectData(
                    key, "Herald vault", None, None, None, 0,
                )
                protected = (
                    protected_result[1]
                    if isinstance(protected_result, tuple)
                    else protected_result
                )
                self._atomic_write(protected_path, bytes(protected))
                _restrict(protected_path)
                return key
            except (ImportError, OSError):
                pass

        try:
            import keyring
            stored = keyring.get_password("herald-vault", "master")
            if stored:
                return stored.encode()
            key = Fernet.generate_key()
            keyring.set_password("herald-vault", "master", key.decode())
            return key
        except Exception:  # no usable keyring on many headless Linux hosts
            key_path = self.root / ".master.key"
            if key_path.exists():
                return key_path.read_bytes().strip()
            key = Fernet.generate_key()
            self._atomic_write(key_path, key)
            _restrict(key_path)
            return key

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass

    def _path(self, name: str) -> Path:
        digest = hashlib.sha256(name.encode()).hexdigest()
        return self.root / f"{digest}.vault"

    def put(self, name: str, payload: bytes, *, metadata: dict[str, Any] | None = None) -> str:
        envelope = {
            "name": name, "created_at": _now(),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "metadata": metadata or {},
            "payload": base64.b64encode(payload).decode(),
        }
        encrypted = self._fernet.encrypt(json.dumps(envelope, separators=(",", ":")).encode())
        path = self._path(name)
        self._atomic_write(path, encrypted)
        _restrict(path)
        return f"vault:{name}"

    def put_json(self, name: str, value: Any, *, metadata: dict[str, Any] | None = None) -> str:
        return self.put(name, json.dumps(value, separators=(",", ":")).encode(), metadata=metadata)

    def _read_envelope(self, name: str) -> dict[str, Any]:
        path = self._path(name)
        if not path.exists():
            raise KeyError(f"vault artifact '{name}' not found")
        try:
            envelope = json.loads(self._fernet.decrypt(path.read_bytes()))
        except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"vault artifact '{name}' failed integrity verification") from exc
        if envelope.get("name") != name:
            raise RuntimeError("vault artifact identity mismatch")
        return envelope

    def get(self, name: str) -> bytes:
        envelope = self._read_envelope(name)
        payload = base64.b64decode(envelope["payload"])
        if hashlib.sha256(payload).hexdigest() != envelope.get("sha256"):
            raise RuntimeError("vault payload checksum mismatch")
        return payload

    def get_json(self, name: str) -> Any:
        return json.loads(self.get(name))

    def describe(self, name: str) -> dict[str, Any]:
        envelope = self._read_envelope(name)
        return {
            "name": name, "created_at": envelope.get("created_at"),
            "sha256": envelope.get("sha256"), "metadata": envelope.get("metadata", {}),
        }

    def remove(self, name: str) -> bool:
        path = self._path(name)
        if not path.exists():
            return False
        path.unlink()
        return True
