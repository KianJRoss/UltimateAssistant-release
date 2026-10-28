"""Local deterministic plan refresh; never activates modes or executes tasks."""
from datetime import datetime, timezone
import hashlib
import json
import threading

from .today_service import snapshot
from .record import outings


def _appointments(now, days=7):
    from datetime import timedelta
    from .record import busy
    return [{'starts_at': b['start'].isoformat(), 'returned_at': b['end'].isoformat()}
            for b in busy(now, now + timedelta(days=days)) if b['kind'] == 'appointment']
from .paths import life_root


class Planner:
    def __init__(self, store=None, inputs=None, clock=None, sessions=None, progress=None,
                 units=None, learning=None):
        from .planning_state import PlanningStore
        self.store = store or PlanningStore()
        self.inputs = inputs or self._inputs
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        from .work_sessions import WorkSessions
        self.sessions=sessions or WorkSessions(self.store.home/'work-sessions',clock=self.clock)
        from .progress_state import ProgressStore
        self.progress = progress if progress is not None else ProgressStore(self.store.home.parent/'progress')
        from .task_units import TaskUnitStore
        self.units = units if units is not None else TaskUnitStore(self.store.home.parent/'task-units')
        # Study progress (shown with the plan, never changes it) comes from the school module when
        # it's installed (life/study.py); a test may pass its own reader with get_planning_evidence().
        self.learning = learning
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.error = None

    @staticmethod
    def _inputs(now):
        value = snapshot(now=now.isoformat(), limit=100)
        # Read all commitments, not just the UI's bounded visible selection.
        import os
        from pathlib import Path
        from .today_service import _read
        home=life_root()
        from .record import tasks_db
        tasks, state = _read(tasks_db(),
                             'assistant_tasks', "WHERE status NOT IN ('done','completed','cancelled','canceled')")
        if state['state']=='unavailable' or state['truncated']:
            raise ValueError('Commitment source unavailable or exceeds the planning record limit')
        commitments = [r for r in tasks if r.get('kind','commitment')=='commitment']
        # Organizer-created contexts are canonical linked commitments, not
        # completion evidence. Keep them visible even before a provider-backed
        # item has been copied into the ordinary task view.
        from .preparation.store import Store as PreparationStore
        known = {str(item.get('id')) for item in commitments}
        for context in PreparationStore(home/'preparation').list_context():
            if context.get('kind') not in ('task', 'project', 'chore'):
                continue
            commitment_id = str(context.get('linked_commitment_id') or context['id'])
            if commitment_id in known:
                continue
            commitments.append({
                'id': commitment_id, 'title': context.get('title'),
                'due_at': context.get('due'), 'next_action': context.get('next_action'),
                'preparation_context_id': context['id'],
                'source_identity': context.get('source_identity'),
                'source_revision': context.get('source_revision'),
                'learning_activity_id': context.get('learning_activity_id'),
                'material_refs': context.get('material_refs') or [],
                'completion_inferred': False,
            })
            known.add(commitment_id)
        return {'commitments':commitments,
                # Time already taken: outings (an unknown return keeps the rest of the day taken) and
                # appointments, so planned work never lands on top of them.
                'outings':outings(include_returned=True) + _appointments(now),
                'learning':value['learning']['activities'], 'source_state':state['state']}

    @staticmethod
    def _validated_progress(progress):
        from .progress_state import _note, _unit_ids, _remaining_minutes
        if not isinstance(progress, dict):
            raise ValueError('Progress must be an object')
        if set(progress) - {'note', 'completed_unit_ids', 'remaining_minutes'}:
            raise ValueError('Unknown progress fields')
        note = _note(progress.get('note'))
        if not note.strip():
            raise ValueError('Record what actually happened before finishing work')
        _unit_ids(progress.get('completed_unit_ids'))
        _remaining_minutes(progress.get('remaining_minutes'))
        encoded = json.dumps(progress, allow_nan=False)
        if len(encoded) > 20000:
            raise ValueError('Progress record is too large')
        return json.loads(encoded)

    def _persist_session_progress(self, session):
        evidence = session.get('reported_progress')
        if evidence is None:
            return None
        evidence = self._validated_progress(evidence)
        finished=next((event['state']['updated_at'] for event in self.sessions.history(session['id'])
                       if event['event']=='finish_focus'),session['updated_at'])
        return self.progress.record(session['activity_id'], note=evidence['note'],
                                    completed_unit_ids=evidence.get('completed_unit_ids'),
                                    remaining_minutes=evidence.get('remaining_minutes'),
                                    request_id='session:'+session['id'], observed_at=finished)

    def _recover_progress(self):
        # Recover every retained session, including ones superseded after a crash.
        # Read the durable session payloads without ticking timers or changing phases.
        with self.sessions.db() as db:
            sessions = [json.loads(row[0]) for row in
                        db.execute('SELECT payload FROM sessions ORDER BY rowid')]
        recorded={r['request_id'] for r in self.progress.list_records()}
        for session in sessions:
            if 'session:'+session['id'] not in recorded:
                self._persist_session_progress(session)

    def _session_evidence(self):
        with self.sessions.db() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT payload FROM sessions ORDER BY rowid')]

    @staticmethod
    def _block_key(plan_record, block):
        raw = json.dumps({"plan_sequence": plan_record["sequence"], "block": block},
                         sort_keys=True, separators=(',', ':'), allow_nan=False)
        return "missed:" + hashlib.sha256(raw.encode()).hexdigest()

    def _reassess_retained_plan(self, retained, now, progress_records):
        """Record misses/reconciliations from the retained plan before it is replaced."""
        sessions = self._session_evidence()
        observations = []
        for block in (retained['plan'].get('blocks', []) if retained else []):
            if block.get('type') != 'focus':
                continue
            end = datetime.fromisoformat(block['ends_at'].replace('Z', '+00:00')).astimezone(timezone.utc)
            if end >= now.astimezone(timezone.utc):
                continue
            activity_id = block.get('activity_id')
            start = datetime.fromisoformat(block['starts_at'].replace('Z', '+00:00')).astimezone(timezone.utc)
            session_matches = [s for s in sessions if s.get('activity_id') == activity_id and
                               start <= datetime.fromisoformat(s['starts_at'].replace('Z', '+00:00')).astimezone(timezone.utc) <= end]
            progress_matches = []
            for record in progress_records:
                if record.get('activity_id') != activity_id:
                    continue
                stamp = record.get('observed_at') or record.get('created_at')
                if stamp and datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone(timezone.utc) >= start:
                    progress_matches.append(record)
            key = self._block_key(retained, block)
            prior = self.store.list_observations(observation_key=key)
            evidenced = bool(session_matches or progress_matches)
            if evidenced and not prior:
                # Work observed before the first assessment means the block was not missed.
                continue
            status = 'reconciled' if evidenced else 'missed'
            payload = {'status': status, 'activity_id': activity_id,
                       'scheduled_start': block['starts_at'], 'scheduled_end': block['ends_at'],
                       'minutes': block.get('minutes', 0), 'plan_sequence': retained['sequence'],
                       'evidence': {'session_ids': [s['id'] for s in session_matches],
                                    'progress_sequences': [r['sequence'] for r in progress_matches]}}
            observation = self.store.record_observation(key, 'missed_work', payload)
            observations.append(observation)
        # Evidence can arrive after later refreshes have replaced the plan that
        # contained the missed block. Preserve that historical miss and append
        # a reconciliation revision to the same observation identity.
        histories = self.store.list_observations(kind='missed_work')
        latest = {}
        for record in histories:
            latest[record['observation_key']] = record
        for key, record in latest.items():
            payload = record['payload']
            if payload.get('status') != 'missed':
                continue
            start = datetime.fromisoformat(payload['scheduled_start'].replace('Z', '+00:00')).astimezone(timezone.utc)
            matches = [r for r in progress_records if r.get('activity_id') == payload.get('activity_id') and
                       (r.get('observed_at') or r.get('created_at')) and
                       datetime.fromisoformat((r.get('observed_at') or r['created_at']).replace('Z', '+00:00')).astimezone(timezone.utc) >= start]
            if matches:
                reconciled = dict(payload, status='reconciled',
                                  evidence=dict(payload.get('evidence') or {},
                                                progress_sequences=[r['sequence'] for r in matches]))
                observations.append(self.store.record_observation(key, 'missed_work', reconciled))
        return observations

    def finish_work(self, session_id, revision, progress):
        evidence = self._validated_progress(progress)
        with self._lock:
            value = self.sessions.transition(session_id, revision, 'finish_focus', evidence)
            try:
                self._persist_session_progress(value)
            except Exception as exc:
                # The session holds a durable recovery copy. Do not claim saved evidence.
                self.error = str(exc)
                return dict(value, progress_persisted=False, planning_error=str(exc))
        try:
            self.refresh()
        except Exception as exc:
            self.error = str(exc)
            return dict(value, progress_persisted=True, planning_error=str(exc))
        return dict(value, progress_persisted=True)

    def refresh(self):
        from .commitment_engine import build_daily_plan
        with self._lock:
            self._recover_progress()
            preferences=self.store.preferences()
            if preferences is None:
                self.error=None
                return {'state':'needs_availability','message':'Work windows have not been recorded.', 'plan':None}
            now=self.clock().replace(second=0,microsecond=0)
            inputs=dict(self.inputs(now))
            if self.learning is not None:
                learning_evidence = self.learning.get_planning_evidence()
            else:
                from .study import snapshot as study_snapshot
                study = study_snapshot(now.isoformat())
                learning_evidence = {'evidence_distinct_from_completion': True, 'completion_inferred': False,
                                     'activities': study['understanding'], 'flashcards': study['flashcards'],
                                     'source': study['state']}
            from .task_units import apply_definitions
            from .progress_state import apply_progress
            records = self.progress.list_records()
            retained = self.store.latest_plan()
            observations = self._reassess_retained_plan(retained, now, records)
            inputs['commitments'] = apply_definitions(inputs['commitments'], self.units.snapshot())
            # Stored definitions describe immutable work units; completion is
            # supplied separately by the progress ledger.
            for commitment in inputs['commitments']:
                if 'unit_definition_revision' not in commitment:
                    continue
                for unit in commitment.get('units', []):
                    unit.setdefault('completed', False)
            inputs['commitments'] = apply_progress(inputs['commitments'], records)
            active=self.sessions.current()
            inputs['active_session']=active
            outings=list(inputs['outings'])
            if active and active['phase'] in ('focus','break'):
                outings.append({'starts_at':active['starts_at'],'returned_at':active['ends_at']})
            # One durable snapshot per changed input or minute; polls are free of inference.
            payload={'preferences':preferences,'commitments':inputs['commitments'],
                     'outings':outings,'learning':inputs.get('learning'),
                     'learning_evidence':learning_evidence,
                     'active_session':active,'minute':now.isoformat()[:16]}
            fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest()
            plan=build_daily_plan(inputs['commitments'],now=now.isoformat(),
                                 work_windows=preferences['work_windows'],outings=outings,
                                 learning=inputs.get('learning'),timezone_name=preferences['timezone_name'],
                                 focus_minutes=preferences['focus_minutes'],break_minutes=preferences['break_minutes'])
            plan['progress_evidence'] = records
            # Learning observations inform later review/preparation choices but
            # are never translated into commitment completion.
            plan['learning_evidence'] = learning_evidence
            plan['completion_inferred_from_learning'] = False
            plan['active_session']=active
            plan['remaining_effort_unverified']=bool(active and active['phase'] not in ('ended','ready','cancelled'))
            plan['planning_observations'] = observations
            record=self.store.record_plan(plan,input_fingerprint=fingerprint,request_id='refresh:'+fingerprint)
            self.error=None
            return {'state':'ready','plan':plan,'record':record,'source_state':inputs.get('source_state','supplied'),
                    'enforcement_active':False}

    def save_preferences(self, work_windows, *, focus_minutes=25, break_minutes=5, timezone_name='America/Indianapolis'):
        from .commitment_engine import build_daily_plan
        # Validate the relative horizon before storing settings that cannot run.
        build_daily_plan([],now=self.clock(),work_windows=work_windows,outings=[],
                         focus_minutes=focus_minutes,break_minutes=break_minutes,timezone_name=timezone_name)
        with self._lock:
            self.store.save_preferences(work_windows,focus_minutes=focus_minutes,
                                        break_minutes=break_minutes,timezone_name=timezone_name)
        return self.refresh()

    def status(self):
        return {'active_session':self.sessions.current(),'preferences':self.store.preferences(),'latest':self.store.latest_plan(),
                'error':self.error,'running':bool(self._thread and self._thread.is_alive()),
                'enforcement_active':False}

    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        def run():
            while not self._stop.is_set():
                try:
                    self.refresh()
                except Exception as exc:
                    self.error=str(exc)
                self._stop.wait(60)
        self._thread=threading.Thread(target=run,name='personal-plan-refresh',daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
