"""Durable work/break phases. Timers never complete tasks or establish learning."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
from .paths import life_root


class WorkSessions:
    def __init__(self, home=None, clock=None):
        self.home=Path(home or life_root()/'planning'/'work-sessions')
        self.home.mkdir(parents=True,exist_ok=True)
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,request_id TEXT UNIQUE,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY,session_id TEXT,payload TEXT NOT NULL);''')

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.home/'sessions.sqlite',timeout=10)
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                yield db
        finally:
            db.close()

    @staticmethod
    def instant(value):
        result=(value if isinstance(value, datetime) else
                datetime.fromisoformat(value.replace('Z','+00:00')))
        if result.utcoffset() is None:
            raise ValueError('Session times require timezone offsets')
        return result.astimezone(timezone.utc)

    def now(self):
        result=self.clock()
        if result.utcoffset() is None:
            raise ValueError('Session clock requires timezone offset')
        return result.astimezone(timezone.utc)

    def save(self,db,value,event):
        value['revision']+=1
        value['updated_at']=self.now().isoformat()
        payload=json.dumps(value,allow_nan=False,sort_keys=True)
        db.execute('UPDATE sessions SET payload=? WHERE id=?',(payload,value['id']))
        db.execute('INSERT INTO events(session_id,payload) VALUES(?,?)',
                   (value['id'],json.dumps({'event':event,'state':value},allow_nan=False)))
        return value

    def tick(self,db,value):
        if value and value['phase'] in ('focus','break') and self.now()>=self.instant(value['ends_at']):
            value['phase']='focus_due' if value['phase']=='focus' else 'ready'
            value['remaining_seconds']=0
            self.save(db,value,'timer_due')
        return value

    def current(self):
        with self.db() as db:
            row=db.execute('SELECT payload FROM sessions ORDER BY rowid DESC LIMIT 1').fetchone()
            return self.tick(db,json.loads(row[0])) if row else None

    def start(self,block,*,break_minutes=5,request_id,device=None,task=None,ownership=None,expected_revision=None,expiry=None):
        block=json.loads(json.dumps(block,allow_nan=False,sort_keys=True))
        if not isinstance(block,dict) or block.get('type')!='focus' or not isinstance(block.get('activity_id'),str) or not block['activity_id'].strip():
            raise ValueError('Start requires an identified focus block')
        if not isinstance(request_id,str) or not request_id.strip() or len(request_id)>200:
            raise ValueError('A request identifier is required')
        if isinstance(break_minutes,bool) or not isinstance(break_minutes,int) or not 0<=break_minutes<=120:
            raise ValueError('Break minutes must be an integer from 0 to 120')
        now_ts=self.now().timestamp()
        if expiry is not None and now_ts>=expiry:
            raise ValueError('Work session request has expired')
        request=json.dumps({'block':block,'break_minutes':break_minutes,'device':device,'task':task,'ownership':ownership},sort_keys=True)
        with self.db() as db:
            previous=db.execute('SELECT payload FROM sessions WHERE request_id=?',(request_id,)).fetchone()
            if previous:
                value=json.loads(previous[0])
                if value['request_payload']!=request:
                    raise ValueError('Request identifier belongs to a different session')
                return self.tick(db,value)
            row=db.execute('SELECT payload FROM sessions ORDER BY rowid DESC LIMIT 1').fetchone()
            active=self.tick(db,json.loads(row[0])) if row else None
            if active and active['phase'] not in ('ended','ready','cancelled'):
                raise ValueError('An existing work or break phase must finish first')
            if expected_revision is not None:
                cur_rev=active['revision'] if active else 0
                if expected_revision!=cur_rev:
                    raise ValueError(f'Expected revision conflict: expected {expected_revision}, got {cur_rev}')
            now=self.now()
            start=self.instant(block['starts_at'])
            end=self.instant(block['ends_at'])
            seconds=(end-now).total_seconds()
            if not start<=now<end or not 0<seconds<=180*60:
                raise ValueError('Focus block is not currently available')
            value={'id':uuid.uuid4().hex,'activity_id':block['activity_id'],'phase':'focus',
                   'source_block':block,'starts_at':now.isoformat(),'ends_at':end.isoformat(),
                   'remaining_seconds':seconds,'break_minutes':break_minutes,'revision':0,
                   'request_payload':request,'reported_progress':None,'enforcement_active':False,
                   'device':str(device) if device else None,'task':str(task) if task else block['activity_id'],
                   'ownership':str(ownership) if ownership else 'assistant','expiry':float(expiry) if expiry is not None else None,
                   'observed_result':'started'}
            db.execute('INSERT INTO sessions VALUES(?,?,?)',(value['id'],request_id,json.dumps(value)))
            return self.save(db,value,'started')

    def transition(self,session_id,revision,command,progress=None,*,device=None,task=None,ownership=None,expiry=None):
        if isinstance(revision,bool) or not isinstance(revision,int):
            raise ValueError('Expected revision must be an integer')
        now_ts=self.now().timestamp()
        if expiry is not None and now_ts>=expiry:
            raise ValueError('Work session action has expired')
        with self.db() as db:
            row=db.execute('SELECT payload FROM sessions ORDER BY rowid DESC LIMIT 1').fetchone()
            value=self.tick(db,json.loads(row[0])) if row else None
            if not value or value['id']!=session_id or value['revision']!=revision:
                raise ValueError('Session changed; refresh before issuing this action')
            if device:
                value['device']=str(device)
            if task:
                value['task']=str(task)
            if ownership:
                value['ownership']=str(ownership)
            phase=value['phase']
            if command=='pause' and phase in ('focus','break'):
                value['remaining_seconds']=max(0,(self.instant(value['ends_at'])-self.now()).total_seconds())
                value['paused_phase']=phase
                value['phase']='paused'
                value['observed_result']='paused'
                if progress is not None:
                    encoded=json.dumps(progress,allow_nan=False)
                    if len(encoded)>20000 or not isinstance(progress,dict):
                        raise ValueError('Progress record must be a reasonably sized object')
                    value['reported_progress']=json.loads(encoded)
                    if progress.get('return_plan') is not None:
                        value['return_plan']=progress['return_plan']
            elif command=='resume' and phase=='paused':
                value['phase']=value.pop('paused_phase')
                value['ends_at']=(self.now()+timedelta(seconds=value['remaining_seconds'])).isoformat()
                value['observed_result']='resumed'
            elif command=='finish_focus' and phase in ('focus','focus_due'):
                encoded=json.dumps(progress,allow_nan=False)
                if len(encoded)>20000:
                    raise ValueError('Progress record is too large')
                progress=json.loads(encoded)
                if not isinstance(progress,dict) or not isinstance(progress.get('note'),str) or not progress['note'].strip():
                    raise ValueError('Record what actually happened before finishing work')
                value['reported_progress']=progress
                return_plan = progress.get('return_plan')
                if return_plan is not None:
                    value['return_plan'] = return_plan
                value['phase']='break' if value['break_minutes'] else 'ready'
                value['starts_at']=self.now().isoformat()
                value['ends_at']=(self.now()+timedelta(minutes=value['break_minutes'])).isoformat()
                value['remaining_seconds']=value['break_minutes']*60
                value['observed_result']='focus_finished'
            elif command=='cancel' and phase not in ('ended',):
                value['phase']='cancelled'
                value['remaining_seconds']=0
                value['observed_result']='cancelled'
                if progress is not None and isinstance(progress, dict):
                    value['reported_progress']=progress
            elif command=='end' and phase not in ('ended','break') and not (phase=='paused' and value.get('paused_phase')=='break'):
                value['phase']='ended'
                value['remaining_seconds']=0
                value['observed_result']='ended'
            else:
                raise ValueError('Action is unavailable in the current phase')
            return self.save(db,value,command)

    def cancel(self, session_id, revision, reason="cancelled_by_user", *, progress=None):
        note = progress.get('note') if isinstance(progress, dict) else str(reason)
        evidence = {'note': note} if not isinstance(progress, dict) else progress
        return self.transition(session_id, revision, 'cancel', evidence)

    def checkpoint(self, session_id, revision, stage, data=None):
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ValueError('Expected revision must be an integer')
        with self.db() as db:
            row = db.execute('SELECT payload FROM sessions ORDER BY rowid DESC LIMIT 1').fetchone()
            value = self.tick(db, json.loads(row[0])) if row else None
            if not value or value['id'] != session_id or value['revision'] != revision:
                raise ValueError('Session changed; refresh before issuing this action')
            checkpoint_info = {'stage': str(stage), 'data': data or {}, 'at': self.now().isoformat()}
            value.setdefault('checkpoints', []).append(checkpoint_info)
            value['observed_result'] = f'checkpoint:{stage}'
            return self.save(db, value, f'checkpoint:{stage}')

    def generate_break_return_intent(self, *, now=None, session_id=None, sink=None):
        """Generate a durable follow-up return intent if a break has concluded or is due."""
        cur = self.current()
        if not cur:
            return None
        if session_id and cur.get('id') != session_id:
            return None
        phase = cur.get('phase')
        # Break concluded when phase is 'ready' after a break or break ended past ends_at
        now_dt = self.instant(now) if now else self.now()
        ends_dt = self.instant(cur['ends_at']) if cur.get('ends_at') else None
        is_break_due = (phase == 'ready' and cur.get('break_minutes', 0) > 0 and cur.get('observed_result') in ('focus_finished', 'timer_due')) or \
                       (phase == 'break' and ends_dt and now_dt >= ends_dt)
        if not is_break_due:
            return None
        activity_id = cur.get('activity_id')
        ret_plan = cur.get('return_plan') or {}
        next_step = ret_plan.get('return_to') or cur.get('task') or activity_id
        message = f"Break complete. Time to resume {activity_id}: {next_step}."
        intent = {
            'kind': 'break_return_intent',
            'session_id': cur.get('id'),
            'activity_id': activity_id,
            'phase': phase,
            'return_to': next_step,
            'break_minutes': cur.get('break_minutes'),
            'message': message,
            'title': f"Resume Work: {activity_id}",
            'as_of': now_dt.isoformat(),
            'completion_inferred': False,
        }
        for key in ('learning_evidence', 'preparation_context', 'linked_commitment'):
            if ret_plan.get(key) is not None:
                intent[key] = json.loads(json.dumps(ret_plan[key], allow_nan=False))
        intent['learning_evidence_inferred_completion'] = False
        source_id = ret_plan.get('source_id')
        revision = ret_plan.get('revision')
        deadline = ret_plan.get('deadline')
        if source_id is not None:
            intent['source_id'] = source_id
        if revision is not None:
            intent['source_revision'] = revision
        if deadline is not None:
            intent['deadline'] = deadline
        attached = {key: ret_plan.get(key) for key in
                    ('learning_evidence', 'preparation_context', 'linked_commitment')
                    if ret_plan.get(key) is not None}
        stable = ':'.join(str(value) for value in (
            cur.get('id'), activity_id, next_step, cur.get('ends_at'), source_id, revision,
            deadline, json.dumps(attached, sort_keys=True, allow_nan=False)))
        fingerprint = hashlib.sha256(stable.encode('utf-8')).hexdigest()
        intent['fingerprint'] = fingerprint
        if sink is not None:
            if hasattr(sink, 'save_proactive_record'):
                source_revision = intent.get('source_revision')
                record_revision = (source_revision if isinstance(source_revision, int)
                                   and source_revision > 0 else 1)
                intent['sink_result'] = sink.save_proactive_record(
                    intent['kind'], intent, fingerprint=fingerprint,
                    intent_key=f"break_return_intent:{cur.get('id')}",
                    revision=record_revision)
            elif callable(sink):
                intent['sink_result'] = sink(intent)
            elif hasattr(sink, 'append'):
                sink.append(intent)
                intent['sink_result'] = True
            else:
                raise TypeError('sink must accept an intent or save proactive records')
        return intent

    def generate_interruption_intent(self, *, session_id=None, sink=None):
        """Retain a reassessment responsibility for explicitly paused work."""
        cur = self.current()
        if not cur or cur.get('phase') != 'paused':
            return None
        if session_id and cur.get('id') != session_id:
            return None
        plan = cur.get('return_plan') or {}
        next_step = plan.get('return_to') or cur.get('task') or cur.get('activity_id')
        intent = {
            'kind': 'learning_reassessment_intent',
            'session_id': cur.get('id'),
            'activity_id': cur.get('activity_id'),
            'interrupted_phase': cur.get('paused_phase'),
            'return_to': next_step,
            'message': f"Work on {cur.get('activity_id')} was interrupted; reassess before resuming: {next_step}.",
            'title': f"Reassess {cur.get('activity_id')}",
            'completion_inferred': False,
        }
        for key in ('learning_evidence', 'preparation_context', 'linked_commitment'):
            if plan.get(key) is not None:
                intent[key] = json.loads(json.dumps(plan[key], allow_nan=False))
        intent['learning_evidence_inferred_completion'] = False
        for source, target in (('source_id', 'source_id'), ('revision', 'source_revision'),
                               ('deadline', 'deadline')):
            if plan.get(source) is not None:
                intent[target] = plan[source]
        attached = {key: plan.get(key) for key in
                    ('learning_evidence', 'preparation_context', 'linked_commitment')
                    if plan.get(key) is not None}
        stable = ':'.join(str(value) for value in (
            cur.get('id'), cur.get('revision'), next_step, intent.get('source_id'),
            intent.get('source_revision'), intent.get('deadline'),
            json.dumps(attached, sort_keys=True, allow_nan=False)))
        fingerprint = hashlib.sha256(stable.encode('utf-8')).hexdigest()
        intent['fingerprint'] = fingerprint
        if sink is not None:
            if hasattr(sink, 'save_proactive_record'):
                source_revision = intent.get('source_revision')
                record_revision = (source_revision if isinstance(source_revision, int)
                                   and source_revision > 0 else 1)
                intent['sink_result'] = sink.save_proactive_record(
                    intent['kind'], intent, fingerprint=fingerprint,
                    intent_key=f"learning_reassessment_intent:{cur.get('id')}",
                    revision=record_revision)
            elif callable(sink):
                intent['sink_result'] = sink(intent)
            elif hasattr(sink, 'append'):
                sink.append(intent)
                intent['sink_result'] = True
            else:
                raise TypeError('sink must accept an intent or save proactive records')
        return intent

    def history(self,session_id):
        with self.db() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT payload FROM events WHERE session_id=? ORDER BY sequence',(session_id,))]
