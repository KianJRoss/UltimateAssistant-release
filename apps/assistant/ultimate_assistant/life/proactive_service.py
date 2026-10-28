"""Durable advisory records (nudges) over the life records.

It prepares and tracks records; it never delivers them. Delivery is the clock's (life_clock.py): a
build on top hands the ready records to it as nudges (life_clock.add_source).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

from .commitment_orchestration import CommitmentOrchestrator
from .preparation.store import Store
from .today_service import snapshot as today_snapshot


class ProactiveService:
    """Thin, restart-safe coordinator; existing stores remain the authorities."""

    def __init__(self, *, commitments=None, preparation=None, today=None, clock=None):
        self.commitments = commitments or CommitmentOrchestrator()
        self.preparation = preparation or Store()
        self.today = today or today_snapshot
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _instant(value):
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        else:
            raise ValueError('now must be an aware datetime or ISO timestamp')
        if parsed.utcoffset() is None:
            raise ValueError('now must include a timezone offset')
        return parsed

    @staticmethod
    def _dependency_minutes(notes):
        from .record import dependency_minutes
        return dependency_minutes(notes)

    def _appointment_records(self, life, now):
        records = []
        for item in life.get('items', []):
            if item.get('kind') != 'appointment' or item.get('status') != 'active' or not item.get('scheduled_start'):
                continue
            start = self._instant(item['scheduled_start'])
            prep, travel = self._dependency_minutes(item.get('notes'))
            begin = start - timedelta(minutes=prep + travel)
            if begin <= now + timedelta(hours=24):
                records.append({
                    'kind': 'appointment_preparation', 'activity_id': item.get('id'),
                    'revision': item.get('revision', 1),
                    'title': item.get('title'), 'starts_at': start.isoformat(),
                    'prepare_by': begin.isoformat(), 'prep_minutes': prep,
                    'travel_minutes': travel, 'location': item.get('location', ''),
                    'message': f"Prepare for {item.get('title')} by {begin.isoformat()}.",
                })
        return records

    def _outing_records(self, life, now):
        records = []
        for item in life.get('items', []):
            if item.get('kind') != 'away_window' or item.get('status') != 'active':
                continue
            end = item.get('scheduled_end')
            if not end or self._instant(end) <= now:
                records.append({
                    'kind': 'uncertain_outing', 'activity_id': item.get('id'),
                    'expected_return': end, 'message': (
                        'Return time is uncertain; keep dependent plans tentative.' if not end else
                        'Expected return has passed; confirm return before preparing dependent plans.'),
                    'requires_confirmation': True,
                })
        return records

    def prepare(self, *, now=None, available_minutes=None, context=None, limit=10):
        """Prepare durable conversation records without creating obligations or doing projects."""
        instant = self._instant(now or self.clock()).astimezone(timezone.utc)
        life = self.commitments.snapshot(now=instant, limit=100)
        records = self._appointment_records(life, instant) + self._outing_records(life, instant)
        for step in self.commitments.get_prepared_next_steps():
            records.append({'kind': 'restart_safe_next_step', **step,
                            'message': f"Next prepared step: {step.get('next_action', step.get('label', 'review it'))}."})
        if available_minutes is not None:
            suggestions = self.commitments.suggest_open_time_activities(
                now=instant, available_minutes=available_minutes, context=context, limit=limit)
            for suggestion in suggestions:
                # Suggestions remain optional and never become scheduled commitments.
                records.append({'kind': 'open_time_suggestion', **suggestion, 'optional': True,
                                'creates_obligation': False,
                                'message': f"If you want, this fits the open time: {suggestion.get('title')}."})

        saved = []
        for record in records[:limit]:
            record['as_of'] = instant.isoformat()
            if context is not None:
                record['context'] = context
            stable = {key: value for key, value in record.items() if key != 'as_of'}
            fingerprint = hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()
            subject = (record.get('activity_id') or record.get('commitment_id') or
                       record.get('review_id') or record.get('id') or fingerprint)
            intent_key = f"{record['kind']}:{subject}"
            revision = record.get('revision', 1)
            if not isinstance(revision, int) or revision < 1:
                revision = 1
            saved.append(self.preparation.save_proactive_record(record['kind'], record,
                fingerprint=fingerprint, intent_key=intent_key, revision=revision,
                now=instant.timestamp()))
        return {'version': 1, 'as_of': instant.isoformat(), 'records': saved,
                'notification_channel': 'conversation', 'actions_executed': False,
                'project_advancement': False}

    def pending(self, limit=100):
        return self.preparation.list_proactive_records(state='ready', limit=limit)

    def acknowledge(self, record_id):
        return self.preparation.acknowledge_proactive_record(
            record_id, now=self._instant(self.clock()).timestamp())

    def complete(self, record_id):
        return self.preparation.complete_proactive_record(
            record_id, now=self._instant(self.clock()).timestamp())
