"""Resolve presentation asset references against authoritative records."""
from __future__ import annotations

import re

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SAFE_MEDIA = re.compile(r"^(?:(?:image|audio|video|text)/[-+.a-z0-9]+|application/pdf)$", re.I)


def resolve_asset(reference, records, *, authorize=None, max_bytes=12 * 1024 * 1024):
    """Return render evidence, never a local path or arbitrary URI."""
    if not isinstance(reference, dict) or reference.get("kind") not in {"source", "artifact"}:
        raise ValueError("invalid asset reference")
    key = "source_id" if reference["kind"] == "source" else "artifact_id"
    identity = reference.get(key)
    revision = reference.get("revision")
    if not isinstance(identity, str) or not identity or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("asset identity and positive revision are required")
    record = records.get(identity) if isinstance(records, dict) else None
    base = {"kind": reference["kind"], key: identity, "revision": revision}
    if not isinstance(record, dict):
        return dict(base, state="unavailable", reason="missing_record", authorized=False)
    authorized = bool(authorize(record, reference)) if authorize is not None else record.get("authorized") is True
    if not authorized:
        reason = "authorization_revoked" if record.get("authorized") is False else "authorization_unknown"
        return dict(base, state="unavailable", reason=reason, authorized=False)
    if record.get("state") in {"revoked", "unavailable", "deleted"}:
        return dict(base, state="unavailable", reason="authorization_revoked" if record.get("authorized") is False or record.get("state") == "revoked" else "source_unavailable", authorized=False)
    current = record.get("revision")
    if current != revision:
        return dict(base, state="stale", current_revision=current, reason="revision_mismatch", authorized=True)
    if "auth_revision" in record:
        auth_rev = record["auth_revision"]
        ref_auth_rev = reference.get("auth_revision")
        if ref_auth_rev is not None and ref_auth_rev != auth_rev:
            return dict(base, state="stale", reason="auth_revision_mismatch", authorized=False)
    if record.get("expires_at"):
        import datetime as _dt
        try:
            exp_str = record["expires_at"].replace("Z", "+00:00")
            exp_dt = _dt.datetime.fromisoformat(exp_str)
            if exp_dt <= _dt.datetime.now(_dt.timezone.utc):
                return dict(base, state="unavailable", reason="asset_expired", authorized=False)
        except Exception:
            pass
    size_bytes = record.get("byte_size") or record.get("size_bytes") or record.get("bytes") or record.get("byte_bounds")
    if size_bytes is not None:
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0 or size_bytes > max_bytes:
            return dict(base, state="unavailable", reason="byte_bounds_exceeded", authorized=True)
    digest = record.get("sha256") or record.get("digest") or record.get("hash")
    media_type = record.get("media_type")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest.lower()):
        return dict(base, state="unavailable", reason="digest_unverified", authorized=True)
    if not isinstance(media_type, str) or not _SAFE_MEDIA.fullmatch(media_type):
        return dict(base, state="unavailable", reason="media_type_unsupported", authorized=True)
    res = dict(base, state="ready", media_type=media_type, sha256=digest.lower(),
               authorized=True, fetch_token=record.get("fetch_token"))
    if size_bytes is not None:
        res["byte_bounds"] = size_bytes
    if record.get("auth_revision") is not None:
        res["auth_revision"] = record["auth_revision"]
    return res
