"""Durable source-to-canvas bindings; existing APScheduler runs bounded polls."""
from __future__ import annotations

import copy
from contextlib import contextmanager
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import httpx

from .feed_sources import fetch_source, select_path, validate_source

LOG = logging.getLogger(__name__)
EVENT = 'app.assistant.feed.condition'
OPS = {'eq', 'ne', 'gt', 'ge', 'lt', 'le', 'changed'}


def _set(data, path, value):
    parts = path.split('.')
    target = data
    for index, part in enumerate(parts[:-1]):
        if isinstance(target, list):
            target = target[int(part)]
        else:
            target = target.setdefault(part, [] if parts[index + 1].isdigit() else {})
    if isinstance(target, list):
        target[int(parts[-1])] = value
    else:
        target[parts[-1]] = value


class FeedManager:
    def __init__(self, path, canvas, *, fetch=fetch_source, emit=None, scheduler=None):
        self.path, self.canvas = Path(path), canvas
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fetch, self.emit = fetch, emit or self._emit
        self.scheduler = scheduler
        self._running = False
        self._guard = threading.RLock()
        self._locks = {}
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS feeds (id TEXT PRIMARY KEY, spec TEXT NOT NULL, state TEXT NOT NULL)')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _emit(payload):
        from .settings import settings
        key = os.environ.get('HERALD_API_KEY')
        response = httpx.post((os.environ.get('HERALD_URL') or settings.herald_url).rstrip('/') + '/event-bus/emit',
            headers={'Authorization': 'Bearer ' + key} if key else {}, timeout=10,
            json={'event_type': EVENT, 'source': 'ultimate-assistant-feeds', 'importance': .4, 'payload': payload})
        response.raise_for_status()

    def validate(self, value):
        from .canvas import validate
        spec = copy.deepcopy(value)
        if not isinstance(spec, dict):
            raise ValueError('feed must be an object')
        if not isinstance(spec.get('id'), str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,50}', spec['id']):
            raise ValueError('feed id: 1-50 letters, digits, dot, dash or underscore')
        spec['source'] = validate_source(spec.get('source'))
        interval = spec.get('interval_seconds', 600 if spec['source']['type'] == 'weather' else 60)
        minimum = {'weather': 600, 'html': 60}.get(spec['source']['type'], 10)
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not minimum <= interval <= 86400:
            raise ValueError(f'interval_seconds must be {minimum}..86400 for this source')
        spec['interval_seconds'] = interval
        if not isinstance(spec.get('enabled', True), bool):
            raise ValueError('enabled must be boolean')
        spec.setdefault('enabled', True)
        card = spec.get('card')
        if not isinstance(card, dict):
            raise ValueError('card must be a canvas element')
        card.setdefault('id', 'feed-' + spec['id'])
        card.setdefault('ttl_seconds', 0)
        spec['card'] = validate(card)
        if card.get('kind') != 'ui':
            raise ValueError('feed card must have kind ui')
        if card.get('ttl_seconds') != 0:
            raise ValueError('feed cards use ttl_seconds=0; closing or removing pauses the feed')
        mapping = spec.setdefault('mapping', {})
        if not isinstance(mapping, dict) or len(mapping) > 80 or any(
                not isinstance(k, str) or not k or not isinstance(v, str) for k, v in mapping.items()):
            raise ValueError('mapping must map card data paths to source paths')
        conditions = spec.setdefault('conditions', [])
        if not isinstance(conditions, list) or len(conditions) > 20:
            raise ValueError('conditions must be a list of up to 20 conditions')
        seen = set()
        for c in conditions:
            if not isinstance(c, dict) or not isinstance(c.get('id'), str) or not c['id'] or c['id'] in seen:
                raise ValueError('conditions need unique nonempty ids')
            seen.add(c['id'])
            if c.get('op') not in OPS or not isinstance(c.get('path'), str):
                raise ValueError('condition requires path and op: ' + ', '.join(sorted(OPS)))
            if c['op'] != 'changed' and 'value' not in c:
                raise ValueError('condition requires value')
            if not isinstance(c.get('fire_initial', False), bool):
                raise ValueError('fire_initial must be boolean')
        if len(json.dumps(spec, allow_nan=False)) > 100000:
            raise ValueError('feed definition is too large')
        return spec

    def get(self, identity):
        with self._db() as db:
            row = db.execute('SELECT * FROM feeds WHERE id=?', (identity,)).fetchone()
        if not row:
            raise KeyError(identity)
        return {'spec': json.loads(row['spec']), 'state': json.loads(row['state'])}

    def list(self):
        with self._db() as db:
            rows = db.execute('SELECT * FROM feeds ORDER BY id').fetchall()
        return [{'spec': json.loads(row['spec']), 'state': json.loads(row['state'])} for row in rows]

    def save(self, value):
        spec = self.validate(value)
        with self._guard, self._db() as db:
            if not db.execute('SELECT 1 FROM feeds WHERE id=?', (spec['id'],)).fetchone() and db.execute('SELECT COUNT(*) FROM feeds').fetchone()[0] >= 40:
                raise ValueError('at most 40 feeds may be saved')
            for row in db.execute('SELECT id,spec FROM feeds WHERE id<>?', (spec['id'],)):
                if json.loads(row['spec'])['card']['id'] == spec['card']['id']:
                    raise ValueError('another feed already owns that card id')
            generation = uuid.uuid4().hex
            db.execute('INSERT OR REPLACE INTO feeds VALUES (?,?,?)',
                       (spec['id'], json.dumps(spec), json.dumps({'generation': generation, 'status': 'waiting'})))
        if self._running:
            self._schedule(spec)
        return self.get(spec['id'])

    def _write_state(self, identity, generation, state):
        with self._db() as db:
            row = db.execute('SELECT state FROM feeds WHERE id=?', (identity,)).fetchone()
            if not row or json.loads(row['state']).get('generation') != generation:
                return False
            db.execute('UPDATE feeds SET state=? WHERE id=?', (json.dumps(state, allow_nan=False), identity))
        return True

    def pause_card(self, identity):
        # Called after a local close/clear. Invalidate in-flight polls, without waiting on HTTP or scripts.
        with self._guard, self._db() as db:
            for row in db.execute('SELECT * FROM feeds').fetchall():
                spec, state = json.loads(row['spec']), json.loads(row['state'])
                if spec['card']['id'] != identity:
                    continue
                spec['enabled'] = False
                state.update(generation=uuid.uuid4().hex, status='paused', paused_at=time.time())
                db.execute('UPDATE feeds SET spec=?,state=? WHERE id=?', (json.dumps(spec), json.dumps(state), row['id']))
        # A poll may have published between the first local removal and this callback.
        if self.canvas.get(identity) is not None:
            self.canvas.remove(identity)

    def remove(self, identity):
        with self._guard, self._db() as db:
            row = db.execute('SELECT spec FROM feeds WHERE id=?', (identity,)).fetchone()
            if not row:
                raise KeyError(identity)
            db.execute('DELETE FROM feeds WHERE id=?', (identity,))
        if self.scheduler and self.scheduler.get_job('canvas-feed-' + identity):
            self.scheduler.remove_job('canvas-feed-' + identity)
        self.canvas.remove(json.loads(row['spec'])['card']['id'])
        return {'removed': identity}

    def _card(self, spec, raw, state):
        card = copy.deepcopy(spec['card'])
        data = card.setdefault('data', {})
        if spec['mapping']:
            for target, source in spec['mapping'].items():
                _set(data, target, select_path(raw, source))
        else:
            data.update(raw)
        data['_feed'] = {'id': spec['id'], 'observed_at': state.get('last_success'),
                         'status': state['status'], 'error': state.get('error')}
        if card.get('template') == 'stats':
            data['status'] = 'Stale' if state['status'] != 'ready' else 'Updated ' + time.strftime('%H:%M:%S')
        return card

    def refresh(self, identity):
        with self._guard:
            lock = self._locks.setdefault(identity, threading.Lock())
        if not lock.acquire(blocking=False):
            return {'id': identity, 'status': 'busy'}
        try:
            row = self.get(identity)
            spec, state = row['spec'], row['state']
            if not spec['enabled']:
                return {'id': identity, 'status': 'paused'}
            generation = state['generation']
            now = time.time()
            try:
                candidate_state = copy.deepcopy(state)
                raw = self.fetch(spec['source'])
                if not isinstance(raw, dict) or len(json.dumps(raw, allow_nan=False)) > 64000:
                    raise ValueError('source must return an object under 64KB; select only needed fields')
                condition_state = {}
                pending = list(state.get('pending_events', []))
                for c in spec['conditions']:
                    current = select_path(raw, c['path'])
                    previous = state.get('conditions', {}).get(c['id'])
                    op = c['op']
                    if op == 'changed':
                        active = previous is not None and current != previous['value']
                    else:
                        rhs = c['value']
                        active = {'eq': lambda: current == rhs, 'ne': lambda: current != rhs,
                                  'gt': lambda: current > rhs, 'ge': lambda: current >= rhs,
                                  'lt': lambda: current < rhs, 'le': lambda: current <= rhs}[op]()
                    if active and (op == 'changed' or not previous or not previous['active']) and (previous is not None or c.get('fire_initial')):
                        pending.append({'event_id': uuid.uuid4().hex, 'feed_id': identity,
                            'condition_id': c['id'], 'previous': previous['value'] if previous else None,
                            'current': current, 'observed_at': now})
                    condition_state[c['id']] = {'value': current, 'active': bool(active)}
                candidate_state.update(status='ready', last_attempt=now, last_success=now, error=None,
                             data=raw, conditions=condition_state, pending_events=pending[-100:])
                card = self._card(spec, raw, candidate_state)
                from .canvas import validate
                validate(card)
                state = candidate_state
            except Exception as exc:
                # Do not expose script output, query strings or request credentials in status.
                state.update(status='stale' if 'data' in state else 'error', last_attempt=now,
                             error=(str(exc)[:300] if isinstance(exc, ValueError) else
                                    f'{type(exc).__name__}: source refresh failed; check source configuration'))
                LOG.warning('Canvas feed %s failed (%s)', identity, type(exc).__name__)
                card = self._card(spec, state['data'], state) if 'data' in state else copy.deepcopy(spec['card'])
                if 'data' not in state:
                    card.setdefault('data', {}).update(status='Unavailable', caption=state['error'],
                        _feed={'id': identity, 'status': 'error', 'observed_at': None})
            with self._guard:
                latest = self.get(identity)
                if latest['state']['generation'] != generation or not latest['spec']['enabled']:
                    return {'id': identity, 'status': 'superseded'}
                if not self._write_state(identity, generation, state):
                    return {'id': identity, 'status': 'superseded'}
                if card:
                    self.canvas.show(card)
            # Durable outbox: retry delivery only, never re-run a script to retry an event.
            for event in list(state.get('pending_events', [])):
                try:
                    self.emit(event)
                except Exception:
                    state['event_error'] = 'Herald event delivery pending'
                    break
                state['pending_events'].remove(event)
                state.pop('event_error', None)
            self._write_state(identity, generation, state)
            return {'id': identity, **state}
        except KeyError:
            return {'id': identity, 'status': 'removed'}
        finally:
            lock.release()

    def _schedule(self, spec):
        from datetime import datetime, timezone
        job = 'canvas-feed-' + spec['id']
        if self.scheduler.get_job(job):
            self.scheduler.remove_job(job)
        if spec['enabled']:
            self.scheduler.add_job(self.refresh, 'interval', seconds=spec['interval_seconds'],
                args=[spec['id']], id=job, coalesce=True, max_instances=1,
                misfire_grace_time=30, next_run_time=datetime.now(timezone.utc))

    def start(self):
        if self._running:
            return
        if self.scheduler is None:
            from apscheduler.schedulers.background import BackgroundScheduler
            self.scheduler = BackgroundScheduler(daemon=True)
        self._running = True
        for row in self.list():
            self._schedule(row['spec'])
        self.scheduler.start()

    def stop(self):
        self._running = False
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=True)
