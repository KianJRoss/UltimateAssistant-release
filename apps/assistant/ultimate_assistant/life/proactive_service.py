"""Durable advisory organization over existing personal-domain authorities.

This module schedules no background thread and sends no reminder itself. Callers may
run ``prepare`` and surface its durable records through the conversation product.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

from .commitment_orchestration import CommitmentOrchestrator
from .preparation.store import Store
from .today_service import snapshot as today_snapshot


class ProactiveService:
    """Thin, restart-safe coordinator; existing stores remain the authorities."""

    def __init__(self, *, commitments=None, preparation=None, today=None, clock=None,
                 scheduler=None, notifications=None, activity=None, sink=None,
                 retry_base_seconds=60, retry_max_seconds=3600):
        self.commitments = commitments or CommitmentOrchestrator()
        self.preparation = preparation or Store()
        self.today = today or today_snapshot
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # Optional delivery providers remain subordinate to this service.  They
        # are injected so construction never starts timers or contacts endpoints.
        self.scheduler = scheduler
        self.notifications = notifications
        self.activity = activity
        if sink is not None and not callable(sink):
            raise ValueError('sink must be callable')
        self.sink = sink
        self.retry_base_seconds = int(retry_base_seconds)
        self.retry_max_seconds = int(retry_max_seconds)
        if self.retry_base_seconds < 1 or self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError('retry bounds must be positive and ordered')
        self._started = False

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
        match = re.search(r"\[Dependencies: prep=([0-9.]+)m, travel=([0-9.]+)m\]", notes or '')
        return (float(match.group(1)), float(match.group(2))) if match else (0.0, 0.0)

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

    def schedule(self, record_id, *, identity, trigger='date', trigger_args=None,
                 coalesce=True, misfire_grace_time=300):
        """Declare timed delivery of an existing record without starting a timer."""
        from .proactive_delivery import DeliveryIdentity
        if self.scheduler is None:
            return {'ok': False, 'state': 'needs_setup', 'provider': 'apscheduler',
                    'record_id': record_id, 'error': 'No scheduling provider is configured'}
        records = self.preparation.list_proactive_records(state=None, limit=500)
        if not any(item['id'] == record_id for item in records):
            raise KeyError(record_id)
        ident = DeliveryIdentity.from_value(identity)
        continuation = dict(ident.continuation)
        existing = continuation.get('record_id')
        if existing is not None and existing != record_id:
            raise ValueError('identity continuation belongs to a different record')
        continuation['record_id'] = record_id
        identity_payload = {**ident.payload(), 'continuation': continuation}
        return self.scheduler.schedule(
            self.deliver, identity=identity_payload, trigger=trigger,
            trigger_args=trigger_args or {}, coalesce=coalesce,
            misfire_grace_time=misfire_grace_time)

    def deliver(self, *, identity, record_id=None, method='POST', now=None):
        """Deliver one ready record; transport acceptance does not acknowledge it."""
        from .proactive_delivery import DeliveryIdentity
        ident = DeliveryIdentity.from_value(identity)
        record_id = record_id or ident.continuation.get('record_id')
        if not record_id:
            raise ValueError('record_id is required in the call or continuation')
        record = next((item for item in self.preparation.list_proactive_records(
            state='ready', limit=500) if item['id'] == record_id), None)
        if record is None:
            return {'ok': False, 'state': 'unavailable', 'record_id': record_id,
                    'identity': ident.payload(), 'error': 'Ready proactive record was not found'}
        instant = self._instant(now or self.clock()).astimezone(timezone.utc)
        if record.get('transport_accepted') is not None:
            return {'ok': True, 'state': 'accepted', 'record_id': record_id,
                    'record_state': record['state'], 'identity': ident.payload(),
                    'attempts': record.get('attempts', 0), 'retry_at': None,
                    'duplicate': True, 'delivered': False,
                    'acknowledged': False, 'completed': False}
        if record.get('next_attempt') and record['next_attempt'] > instant.timestamp():
            return {'ok': False, 'state': 'backoff', 'record_id': record_id,
                    'identity': ident.payload(), 'retry_at': record['next_attempt'],
                    'delivered': False,
                    'acknowledged': False, 'completed': False}
        payload = record['payload']
        envelope = {
            'record_id': record_id, 'intent_key': record.get('intent_key'),
            'revision': record.get('revision', 1), 'identity': ident.payload(),
            'correlation_id': ident.correlation_id,
        }
        try:
            if self.sink is not None:
                result = self.sink(record, envelope)
                if not isinstance(result, dict) or result.get('state') not in {
                        'accepted', 'unavailable', 'failed'}:
                    raise ValueError('sink must return accepted, unavailable, or failed')
                result = dict(result)
                result.setdefault('ok', result['state'] == 'accepted')
                result.setdefault('provider', 'conversation')
            elif self.notifications is not None:
                result = self.notifications.publish(
                    payload.get('message') or payload.get('title') or record['kind'],
                    title=payload.get('title'), priority=payload.get('priority', 3),
                    actions=payload.get('actions'), identity=ident, method=method)
            else:
                result = {'ok': False, 'state': 'unavailable',
                          'provider': 'conversation',
                          'error': 'No conversation delivery sink is configured'}
        except Exception as exc:
            result = {'ok': False, 'state': 'failed', 'provider': 'conversation',
                      'error': str(exc)}
        outcome = result.get('state')
        if outcome not in {'accepted', 'unavailable', 'failed'}:
            outcome = 'accepted' if result.get('ok') else 'failed'
        result.setdefault('delivered', outcome == 'accepted')
        retry_at = None
        if outcome != 'accepted':
            delay = min(self.retry_max_seconds,
                        self.retry_base_seconds * (2 ** min(record.get('attempts', 0), 10)))
            retry_at = instant.timestamp() + delay
        updated = self.preparation.record_proactive_attempt(
            record_id, outcome=outcome, error=result.get('error'),
            now=instant.timestamp(), retry_at=retry_at)
        return {**result, 'state': outcome, 'record_id': record_id,
                'record_state': updated['state'] if updated else record['state'],
                'attempts': updated['attempts'] if updated else record.get('attempts', 0),
                'retry_at': retry_at, 'identity': ident.payload(),
                'acknowledged': False, 'completed': False}

    def run_once(self, *, now=None, available_minutes=None, context=None, limit=10,
                 prepare=True):
        """Perform one deterministic preparation/delivery pass for a lifecycle owner."""
        instant = self._instant(now or self.clock()).astimezone(timezone.utc)
        prepared = (self.prepare(now=instant, available_minutes=available_minutes,
                                 context=context, limit=limit) if prepare else None)
        deliveries = []
        for record in self.preparation.list_proactive_records(state='ready', limit=limit):
            if record.get('transport_accepted') is not None:
                continue
            if record.get('next_attempt') and record['next_attempt'] > instant.timestamp():
                continue
            identity = {'activity_id': record['payload'].get('activity_id'),
                        'correlation_id': record.get('intent_key'),
                        'continuation': {'record_id': record['id'],
                                         'revision': record.get('revision', 1)}}
            deliveries.append(self.deliver(identity=identity, record_id=record['id'],
                                           now=instant))
        return {'ok': all(item.get('state') != 'failed' for item in deliveries),
                'state': 'completed', 'as_of': instant.isoformat(),
                'prepared': prepared, 'deliveries': deliveries,
                'actions_executed': False}

    def start(self):
        """Enable externally-owned lifecycle ticks; no timer is created here."""
        self._started = True
        return {'ok': True, 'state': 'running', 'scheduler_created': False}

    def stop(self):
        self._started = False
        return {'ok': True, 'state': 'stopped'}

    @property
    def running(self):
        return self._started

    def observe_activity(self, *, bucket_id=None, start=None, end=None, limit=100,
                         queries=None):
        """Read optional context; observations never establish work completion."""
        if self.activity is None:
            return {'ok': False, 'state': 'needs_setup', 'provider': 'activitywatch',
                    'error': 'No activity provider is configured'}
        if queries is not None:
            return self.activity.query(queries, start=start, end=end)
        if bucket_id is None:
            return self.activity.buckets()
        return self.activity.events(bucket_id, start=start, end=end, limit=limit)

    def cancel_delivery(self, job_id):
        """Request timer cancellation; the result is not task completion."""
        if self.scheduler is None:
            return {'ok': False, 'state': 'needs_setup', 'provider': 'apscheduler',
                    'job_id': job_id, 'cancellation_requested': False,
                    'error': 'No scheduling provider is configured'}
        return self.scheduler.cancel(job_id)

    def record_reply(self, record_id, *, action, identity):
        """Apply a notification reply while keeping acknowledgement distinct."""
        from .proactive_delivery import DeliveryIdentity
        ident = DeliveryIdentity.from_value(identity)
        if action not in {'acknowledge', 'complete', 'dismiss', 'open', 'cancel'}:
            raise ValueError('Unsupported notification action')
        acknowledged = self.acknowledge(record_id) if action == 'acknowledge' else False
        completed = self.complete(record_id) if action == 'complete' else False
        cancelled = self.cancel_delivery(ident.job_id) if action == 'cancel' and ident.job_id else None
        return {'ok': True, 'state': 'accepted', 'record_id': record_id,
                'action': action, 'identity': ident.payload(),
                'acknowledged': acknowledged, 'completed': completed,
                'cancellation': cancelled}
