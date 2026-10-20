"""Versioned, local-only update tours and structured feature-test choices."""
from __future__ import annotations
import json
from pathlib import Path
import threading
import time
import uuid
from .settings import settings

VERSION_FILE = Path(__file__).resolve().parents[1] / 'VERSION'
HIGHLIGHTS_FILE = Path(__file__).with_name('release-highlights.json')
REPORT_SCOPE = 3
_LOCK = threading.RLock()
RESULTS = {'started', 'worked', 'blocked', 'skipped'}


def current_version() -> str:
    return VERSION_FILE.read_text(encoding='utf-8-sig').strip()


def highlights() -> list[dict]:
    catalog = json.loads(HIGHLIGHTS_FILE.read_text(encoding='utf-8'))
    return catalog.get(current_version(), [])


def _load() -> dict:
    try:
        value = json.loads((settings.user_settings_file.parent/'update-experience.json').read_text('utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(value: dict) -> None:
    path = settings.user_settings_file.parent/'update-experience.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def shared_checks() -> dict[str, str]:
    # Export fixed IDs/status choices only; never the user's conversation or notes.
    allowed = {entry['id'] for entry in highlights()}
    checks = _load().get('versions', {}).get(current_version(), {}).get('checks', {})
    return {key: value for key, value in checks.items() if key in allowed and value in RESULTS}


def status() -> dict:
    from .diagnostics import preferences
    saved = _load()
    version = current_version()
    entry = saved.get('versions', {}).get(version, {})
    sharing = saved.get('sharing', {})
    consent = preferences()
    enabled = consent.get('enabled') is True and consent.get('schema_version') == REPORT_SCOPE
    declined = sharing.get('scope') == REPORT_SCOPE and sharing.get('choice') == 'declined'
    return {'version': version, 'features': highlights(), 'checks': shared_checks(),
            'offer_tour': entry.get('status') not in {'done', 'skipped'} and entry.get('snoozed_until', 0) <= time.time(),
            'offer_sharing': not enabled and not declined and sharing.get('snoozed_until', 0) <= time.time(),
            'sharing_enabled': enabled, 'report_scope': REPORT_SCOPE}


def record(action: str, version: str, feature_id: str = '', result: str = '') -> dict:
    if version != current_version():
        raise ValueError('The running version changed. Reopen the update tour.')
    with _LOCK:
        saved = _load()
        entry = saved.setdefault('versions', {}).setdefault(version, {})
        if action in {'done', 'skip'}:
            entry.update(status='done' if action == 'done' else 'skipped', snoozed_until=0)
        elif action == 'later':
            entry['snoozed_until'] = time.time() + 86400
        elif action == 'test':
            if feature_id not in {feature['id'] for feature in highlights()} or result not in RESULTS:
                raise ValueError('Choose a listed feature and test result.')
            entry.setdefault('checks', {})[feature_id] = result
        elif action in {'sharing_decline', 'sharing_later'}:
            saved['sharing'] = {'scope': REPORT_SCOPE,
                                'choice': 'declined' if action == 'sharing_decline' else 'later',
                                'snoozed_until': 0 if action == 'sharing_decline' else time.time()+7*86400}
        else:
            raise ValueError('Unknown update-tour action.')
        _save(saved)
        return status()
