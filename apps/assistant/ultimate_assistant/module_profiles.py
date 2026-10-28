"""Named, isolated accounts for an installed CLI module; no credentials in metadata."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import module_runtime


def _name(profile: str) -> str:
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', profile):
        raise ValueError('Profile must use lowercase letters, numbers and hyphens')
    return profile


def profiles(data: Path) -> list[dict]:
    folder = data / 'profiles'
    if not folder.exists():
        return []
    return [json.loads(file.read_text('utf-8')) for file in sorted(folder.glob('*/profile.json'))]


def create(data: Path, profile: str, label: str) -> dict:
    _name(profile)
    if profile == 'default':
        raise ValueError('default is reserved for the original module account')
    if not label.strip() or len(label) > 120:
        raise ValueError('Provide an account label of 1–120 characters')
    path = data / 'profiles' / profile
    path.mkdir(parents=True, exist_ok=True)
    file = path / 'profile.json'
    if file.exists():
        raise ValueError('Profile already exists; its account has been preserved')
    value = {'profile': profile, 'label': label.strip(), 'status': 'needs_setup'}
    file.write_text(json.dumps(value, indent=2) + '\n', 'utf-8')
    return value


def account_data(data: Path, profile: str = '') -> Path:
    if not profile:
        if profiles(data):
            raise ValueError('Choose an account profile explicitly; list module profiles first (default selects the original account)')
        return data
    _name(profile)
    if profile == 'default':
        return data
    path = data / 'profiles' / profile
    if not (path / 'profile.json').is_file():
        raise ValueError('Create this account profile before using it')
    return path


def verify(release: Path, manifest: dict, data: Path, profile: str) -> dict:
    path = account_data(data, profile)
    report = module_runtime.readiness(release, manifest, path)
    if profile and profile != 'default':
        file = path / 'profile.json'
        meta = json.loads(file.read_text('utf-8'))
        meta['status'] = report['status']
        pending = path / 'profile.pending.json'
        pending.write_text(json.dumps(meta, indent=2) + '\n', 'utf-8')
        pending.replace(file)
    return {**report, 'profile': profile or 'default'}
