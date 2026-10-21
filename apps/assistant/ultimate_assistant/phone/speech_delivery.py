"""Select whether an advisory deserves speech; retaining it never grants interruption."""


def speech_target(record, envelope, active, *, now, last_spoken=0):
    payload = record.get('payload') if isinstance(record.get('payload'), dict) else record
    engagement = envelope.get('engagement') or {}
    if record.get('permitted') is False or payload.get('speak') is False:
        return None, 'speech not permitted for this record'
    explicit = payload.get('speak') is True or engagement.get('mode') == 'spoken_announcement'
    target = envelope.get('device_id') or payload.get('device_id')
    if not active:
        return (target, 'explicit requested speech') if explicit and target else (None, 'no engaged voice endpoint')
    if target and target != active['device_id']:
        return (target, 'explicit requested speech') if explicit else (None, 'record targets another device')
    if active.get('state') in {'thinking', 'speaking', 'response_ready'} and not explicit:
        return None, 'do not interrupt the current turn'
    if not explicit:
        priority = payload.get('priority', 0)
        attention = payload.get('requires_attention') is True or (isinstance(priority, (int, float)) and not isinstance(priority, bool) and priority >= 4)
        if not attention:
            return None, 'retained quietly; no immediate attention requested'
        if now - last_spoken < 120:
            return None, 'recent proactive speech; retained quietly'
    return active['device_id'], 'requested speech' if explicit else 'timely attention on engaged device'
