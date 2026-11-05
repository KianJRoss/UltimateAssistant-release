"""Read existing installation evidence without login probes or changing setup records."""
from __future__ import annotations

import json
import time
from pathlib import Path


def snapshot(conversations, settings_file: Path, phone: dict, *, preferred=None, explicit=False, clock=time.time):
    try:
        progress=json.loads((settings_file.parent/'setup-progress.json').read_text(encoding='utf-8-sig'))
        if not isinstance(progress,dict):progress={}
    except (OSError,ValueError):progress={}
    latest=conversations.latest_conversation()
    preferred_exists=bool(preferred and conversations.message_page(preferred,limit=1)['messages'])
    # Older UIs always reset the browser to the initial setup thread. Preserve a
    # deliberate user selection, but repair that legacy startup redirect.
    legacy_anchor=preferred==progress.get('conversation_id') and progress.get('phase')=='setup_started'
    resume=preferred if preferred_exists and (explicit or not legacy_anchor) else latest
    paired=bool(phone.get('paired') and not phone.get('revoked'))
    verified=phone.get('last_verified')
    age=max(0,clock()-verified) if isinstance(verified,(int,float)) else None
    return {'has_chat_history':bool(latest),'existing_installation':bool(latest or paired),
            'resume_conversation_id':resume,'setup_phase':progress.get('phase','unknown'),
            'phone':{'paired':paired,'revoked':bool(phone.get('revoked')),
                     'last_connection_evidence_age_seconds':age,
                     'permission_evidence_state':phone.get('permission_evidence_state','unobserved')}}


def context(state):
    return ('Observed installation state for this turn: '+json.dumps(state)+
            '\nAn upgrade, new conversation, or newly installed frontend does not make this a new user. '
            'Resume existing work and saved setup observations; do not restart onboarding or overwrite preferences. '
            'A stored phone pairing is separate from live connectivity and Android permissions. If paired, '
            'preserve it and do not propose pairing again merely because a permission is missing or the phone '
            'is temporarily disconnected. Use phone_status and, when needed, device_status for fresh evidence. '
            'Replace a pairing only when the user explicitly requests replacement. These records do not prove '
            'that every setup step is complete or that an account is currently authenticated.')
