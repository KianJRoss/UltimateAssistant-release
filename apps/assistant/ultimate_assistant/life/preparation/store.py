"""Local preparation records. Never edits sources or performs project/assignment work."""
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from ..paths import life_root

EXCLUDED = {'.git', '.obsidian', '__pycache__', 'node_modules', 'bin', 'obj', 'artifacts', 'generated', 'backups', 'venv', 'credentials', 'secrets', 'accounts'}
SENSITIVE = ('credential', 'password', 'secret', 'token', 'private_key', 'settings', 'account', 'vault')


class Store:
    def __init__(self, home=None):
        self.home = Path(home or life_root() / 'preparation').resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, device TEXT NOT NULL, enabled INTEGER NOT NULL, state TEXT, error TEXT, scanned REAL);
            CREATE TABLE IF NOT EXISTS materials(source TEXT, path TEXT, hash TEXT, excerpt TEXT, modified REAL, active INTEGER DEFAULT 1, PRIMARY KEY(source,path));
            CREATE TABLE IF NOT EXISTS contexts(id TEXT PRIMARY KEY, kind TEXT, title TEXT, due TEXT, next_action TEXT, notes TEXT, last_state TEXT, revision INTEGER, updated REAL);
            CREATE TABLE IF NOT EXISTS context_revisions(id TEXT, revision INTEGER, record TEXT, updated REAL, PRIMARY KEY(id,revision));
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, kind TEXT, payload TEXT, dedupe TEXT, state TEXT, lease_until REAL, owner TEXT, attempts INTEGER DEFAULT 0, result TEXT, error TEXT, created REAL);
            CREATE UNIQUE INDEX IF NOT EXISTS active_job_dedupe ON jobs(dedupe) WHERE state IN ('queued','running');
            CREATE TABLE IF NOT EXISTS proactive_records(
                id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
                payload TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                acknowledged REAL
            );
            CREATE TABLE IF NOT EXISTS standing_authority(
                id TEXT PRIMARY KEY, record TEXT NOT NULL, revision INTEGER NOT NULL,
                updated REAL NOT NULL
            );
            ''')
            if 'rerun' not in {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}:
                db.execute('ALTER TABLE jobs ADD COLUMN rerun INTEGER NOT NULL DEFAULT 0')
            context_columns = {row['name'] for row in db.execute('PRAGMA table_info(contexts)')}
            for name, declaration in {
                'linked_commitment_id': 'TEXT', 'source_identity': 'TEXT',
                'source_revision': 'INTEGER', 'learning_activity_id': 'TEXT',
                'material_refs': 'TEXT NOT NULL DEFAULT \'[]\'',
            }.items():
                if name not in context_columns:
                    db.execute(f'ALTER TABLE contexts ADD COLUMN {name} {declaration}')
            proactive_columns = {row['name'] for row in db.execute(
                'PRAGMA table_info(proactive_records)')}
            for name, declaration in {
                'intent_key': 'TEXT', 'revision': 'INTEGER NOT NULL DEFAULT 1',
                'superseded': 'REAL', 'attempts': 'INTEGER NOT NULL DEFAULT 0',
                'last_attempt': 'REAL', 'next_attempt': 'REAL',
                'transport_accepted': 'REAL', 'last_error': 'TEXT',
                'completed': 'REAL',
            }.items():
                if name not in proactive_columns:
                    db.execute(f'ALTER TABLE proactive_records ADD COLUMN {name} {declaration}')
            # Transport acceptance is delivery evidence, not resolution of the
            # responsibility.  Normalize rows written by the short-lived
            # ``accepted`` state implementation while retaining its timestamp.
            db.execute("UPDATE proactive_records SET state='ready' WHERE state='accepted'")
            db.execute('CREATE INDEX IF NOT EXISTS proactive_intent ON proactive_records(intent_key,revision)')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.home / 'preparation.sqlite', timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def register_source(self, path, device='local', enabled=True):
        root = Path(path).expanduser().resolve()
        if root.parent == root or root == Path.home().resolve() or self.home == root or self.home.is_relative_to(root):
            raise ValueError('Register a specific source directory outside preparation storage, not a drive or home root.')
        if root.exists() and not root.is_dir():
            raise ValueError('Source must be a directory.')
        sid = hashlib.sha256(str(root).casefold().encode()).hexdigest()[:20]
        with self.db() as db:
            db.execute('INSERT INTO sources(id,path,device,enabled) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET device=excluded.device,enabled=excluded.enabled', (sid,str(root),device,int(enabled)))
        return sid

    def list_sources(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM sources ORDER BY path')]

    def list_materials(self, source=None):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM materials WHERE active=1 AND (? IS NULL OR source=?) ORDER BY source,path',(source,source))]

    def scan_source(self, source, max_files=500, max_bytes=262144):
        if not 1 <= max_files <= 5000 or not 1 <= max_bytes <= 1048576:
            raise ValueError('Scan bounds must be positive and limited.')
        with self.db() as db:
            row = db.execute('SELECT * FROM sources WHERE id=?',(source,)).fetchone()
        if row is None:
            raise KeyError(source)
        if not row['enabled']:
            return {'changed':0,'state':'disabled'}
        root = Path(row['path'])
        try:
            if not root.is_dir():
                raise FileNotFoundError('Source unavailable on this device')
            found, changed, partial = [], 0, False
            # Do not follow links/junctions; resolved bounds also checked per file.
            for directory, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d.casefold() not in EXCLUDED and not Path(directory,d).is_symlink() and not getattr(Path(directory,d),'is_junction',lambda:False)() and Path(directory,d).resolve().is_relative_to(root))
                for name in sorted(files):
                    file = Path(directory,name)
                    if name.startswith('.') or file.suffix.lower() not in {'.md','.txt'} or any(word in name.casefold() for word in SENSITIVE):
                        continue
                    if file.is_symlink() or not file.resolve().is_relative_to(root) or file.stat().st_size > max_bytes:
                        continue
                    if len(found) >= max_files:
                        partial = True
                        break
                    raw = file.read_bytes()
                    if len(raw) > max_bytes:
                        continue
                    text = raw.decode('utf-8',errors='replace')
                    relative = file.relative_to(root).as_posix()
                    found.append(relative)
                    digest = hashlib.sha256(raw).hexdigest()
                    with self.db() as db:
                        old = db.execute('SELECT hash,active FROM materials WHERE source=? AND path=?',(source,relative)).fetchone()
                        if old is None or old['hash'] != digest or not old['active']:
                            changed += 1
                            db.execute('INSERT INTO materials VALUES(?,?,?,?,?,1) ON CONFLICT(source,path) DO UPDATE SET hash=excluded.hash,excerpt=excluded.excerpt,modified=excluded.modified,active=1',(source,relative,digest,text[:12000],file.stat().st_mtime))
                if partial:
                    break
            with self.db() as db:
                if not partial:
                    active = db.execute('SELECT path FROM materials WHERE source=? AND active=1',(source,)).fetchall()
                    for material in active:
                        if material['path'] not in found:
                            changed += 1
                            db.execute('UPDATE materials SET active=0 WHERE source=? AND path=?',(source,material['path']))
                db.execute('UPDATE sources SET state=?,error=NULL,scanned=? WHERE id=?',('partial' if partial else 'ready',time.time(),source))
            if changed:
                self.enqueue('source_digest',{'source':source},dedupe='digest:'+source)
            return {'changed':changed,'files':len(found),'state':'partial' if partial else 'ready'}
        except (OSError,ValueError) as exc:
            with self.db() as db:
                db.execute('UPDATE sources SET state=?,error=?,scanned=? WHERE id=?',('unavailable',str(exc),time.time(),source))
            return {'changed':0,'state':'unavailable','error':str(exc)}

    def save_context(self, kind, title, *, id=None, due=None, next_action='', notes='', last_state='',
                     linked_commitment_id=None, source_identity=None, source_revision=None,
                     learning_activity_id=None, material_refs=None):
        if kind not in {'task','project','appointment','chore'}:
            raise ValueError('Unsupported context kind')
        if due:
            from datetime import datetime
            datetime.fromisoformat(due.replace('Z','+00:00'))
        cid = id or uuid.uuid4().hex
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM contexts WHERE id=?',(cid,)).fetchone()
            rev = old['revision']+1 if old else 1
            linked_commitment_id = (old['linked_commitment_id'] if old and linked_commitment_id is None
                                    else linked_commitment_id)
            source_identity = old['source_identity'] if old and source_identity is None else source_identity
            source_revision = old['source_revision'] if old and source_revision is None else source_revision
            learning_activity_id = (old['learning_activity_id'] if old and learning_activity_id is None
                                    else learning_activity_id)
            refs = (json.loads(old['material_refs'] or '[]') if old and material_refs is None
                    else ([] if material_refs is None else material_refs))
            if not isinstance(refs, list) or any(not isinstance(item, str) for item in refs):
                raise ValueError('material_refs must be a list of strings')
            if source_revision is not None and (isinstance(source_revision, bool) or
                                                not isinstance(source_revision, int) or source_revision < 1):
                raise ValueError('source_revision must be a positive integer')
            record = dict(id=cid,kind=kind,title=title,due=due,next_action=next_action,
                          notes=notes,last_state=last_state,revision=rev,updated=time.time(),
                          linked_commitment_id=linked_commitment_id,
                          source_identity=source_identity, source_revision=source_revision,
                          learning_activity_id=learning_activity_id,
                          material_refs=list(dict.fromkeys(refs)))
            db.execute('''INSERT OR REPLACE INTO contexts
                (id,kind,title,due,next_action,notes,last_state,revision,updated,
                 linked_commitment_id,source_identity,source_revision,learning_activity_id,material_refs)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (cid,kind,title,due,next_action,notes,last_state,rev,record['updated'],
                 linked_commitment_id,source_identity,source_revision,learning_activity_id,
                 json.dumps(record['material_refs'])))
            db.execute('INSERT INTO context_revisions VALUES(?,?,?,?)',(cid,rev,json.dumps(record),record['updated']))
        return record

    def list_context(self, kind=None):
        with self.db() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM contexts WHERE ? IS NULL OR kind=? ORDER BY updated DESC',(kind,kind))]
        for row in rows:
            row['material_refs'] = json.loads(row.get('material_refs') or '[]')
        return rows

    def context_history(self, id):
        with self.db() as db:
            return [json.loads(row['record']) for row in db.execute('SELECT record FROM context_revisions WHERE id=? ORDER BY revision',(id,))]

    def enqueue(self, kind, payload, dedupe=None):
        if kind not in {'source_digest','scan_source'}:
            raise ValueError('Only organization/preparation jobs are allowed')
        jid = uuid.uuid4().hex
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute("SELECT id,state FROM jobs WHERE dedupe=? AND state IN ('queued','running')",(dedupe,)).fetchone() if dedupe else None
            if existing:
                # A change arriving during execution must get another pass, not
                # disappear behind the currently running deduplication key.
                db.execute('UPDATE jobs SET payload=?,rerun=? WHERE id=?',(json.dumps(payload),int(existing['state']=='running'),existing['id']))
                return existing['id']
            db.execute('INSERT INTO jobs(id,kind,payload,dedupe,state,created) VALUES(?,?,?,?,?,?)',(jid,kind,json.dumps(payload),dedupe,'queued',time.time()))
        return jid

    def claim(self, owner, lease_seconds=120, now=None):
        if not owner or not 1 <= lease_seconds <= 3600:
            raise ValueError('A worker identity and bounded positive lease are required.')
        now = time.time() if now is None else now
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE state='queued' OR (state='running' AND lease_until<=?) ORDER BY created LIMIT 1",(now,)).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET state='running',owner=?,lease_until=?,attempts=attempts+1 WHERE id=?",(owner,now+lease_seconds,row['id']))
            result = dict(db.execute('SELECT * FROM jobs WHERE id=?',(row['id'],)).fetchone())
            result['payload'] = json.loads(result['payload'])
            return result

    def complete(self, id, owner, result=None, error=None):
        with self.db() as db:
            return bool(db.execute("UPDATE jobs SET state=CASE WHEN rerun=1 THEN 'queued' ELSE ? END,result=?,error=?,lease_until=NULL,rerun=0 WHERE id=? AND owner=? AND state='running' AND lease_until>?",('failed' if error else 'done',json.dumps(result),error,id,owner,time.time())).rowcount)

    def cancel(self, id):
        with self.db() as db:
            return bool(db.execute("UPDATE jobs SET state='cancelled',lease_until=NULL WHERE id=? AND state IN ('queued','running')",(id,)).rowcount)

    def list_jobs(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 100')]

    def save_proactive_record(self, kind, payload, *, fingerprint=None, intent_key=None,
                              revision=1, now=None):
        """Persist a conversation-ready advisory record idempotently."""
        if not isinstance(kind, str) or not kind.strip() or not isinstance(payload, dict):
            raise ValueError('A nonblank kind and object payload are required')
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True,
                             separators=(',', ':'))
        fingerprint = fingerprint or hashlib.sha256((kind + '\0' + encoded).encode()).hexdigest()
        record_id = hashlib.sha256(('proactive\0' + fingerprint).encode()).hexdigest()[:32]
        if not isinstance(revision, int) or revision < 1:
            raise ValueError('revision must be a positive integer')
        created = time.time() if now is None else float(now)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if intent_key:
                # A revised observation makes older advice obsolete.  It remains
                # retained for audit, but can no longer be delivered or followed up.
                db.execute("UPDATE proactive_records SET state='superseded',superseded=? "
                           "WHERE intent_key=? AND fingerprint<>? AND state IN ('ready','accepted')",
                           (created, intent_key, fingerprint))
            db.execute('''INSERT OR IGNORE INTO proactive_records
                (id,fingerprint,kind,payload,state,created,acknowledged,intent_key,revision)
                VALUES(?,?,?,?,?,?,NULL,?,?)''',
                       (record_id, fingerprint, kind, encoded, 'ready', created,
                        intent_key, revision))
            row = db.execute('SELECT * FROM proactive_records WHERE fingerprint=?',
                             (fingerprint,)).fetchone()
        result = dict(row)
        result['payload'] = json.loads(result['payload'])
        return result

    def list_proactive_records(self, *, state='ready', limit=100):
        if state not in (None, 'ready', 'accepted', 'acknowledged', 'completed',
                         'superseded', 'revoked') or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError('Invalid proactive record query')
        with self.db() as db:
            rows = db.execute(
                'SELECT * FROM proactive_records WHERE (? IS NULL OR state=?) ORDER BY created,id LIMIT ?',
                (state, state, limit)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['payload'] = json.loads(item['payload'])
            result.append(item)
        return result

    def acknowledge_proactive_record(self, record_id, *, now=None):
        instant = time.time() if now is None else float(now)
        with self.db() as db:
            changed = db.execute(
                "UPDATE proactive_records SET state='acknowledged',acknowledged=? "
                "WHERE id=? AND state IN ('ready','accepted')",
                (instant, record_id)).rowcount
        return bool(changed)

    def record_proactive_attempt(self, record_id, *, outcome, error=None, now=None,
                                 retry_at=None):
        """Record transport outcome without conflating it with user response."""
        if outcome not in {'accepted', 'unavailable', 'failed'}:
            raise ValueError('Unsupported delivery outcome')
        instant = time.time() if now is None else float(now)
        with self.db() as db:
            changed = db.execute('''UPDATE proactive_records SET
                attempts=attempts+1,last_attempt=?,next_attempt=?,last_error=?,
                transport_accepted=CASE WHEN ?='accepted' THEN ? ELSE transport_accepted END
                WHERE id=? AND state IN ('ready','accepted')''',
                (instant, retry_at, error, outcome, instant, record_id)).rowcount
            row = db.execute('SELECT * FROM proactive_records WHERE id=?', (record_id,)).fetchone()
        if not changed or row is None:
            return None
        result = dict(row)
        result['payload'] = json.loads(result['payload'])
        return result

    def complete_proactive_record(self, record_id, *, now=None):
        instant = time.time() if now is None else float(now)
        with self.db() as db:
            return bool(db.execute(
                "UPDATE proactive_records SET state='completed',completed=? "
                "WHERE id=? AND state IN ('ready','accepted','acknowledged')",
                (instant, record_id)).rowcount)

    def save_standing_authority(self, record, *, authority_id='private-standing', now=None):
        if not isinstance(record, dict):
            raise ValueError('authority record must be an object')
        instant = time.time() if now is None else float(now)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT revision FROM standing_authority WHERE id=?',
                             (authority_id,)).fetchone()
            revision = old['revision'] + 1 if old else 1
            value = dict(record, delegation_id=authority_id, revision=revision)
            db.execute('INSERT OR REPLACE INTO standing_authority VALUES(?,?,?,?)',
                       (authority_id, json.dumps(value, sort_keys=True), revision, instant))
        return value

    def get_standing_authority(self, authority_id='private-standing'):
        with self.db() as db:
            row = db.execute('SELECT record FROM standing_authority WHERE id=?',
                             (authority_id,)).fetchone()
        return json.loads(row['record']) if row else None

    def revoke_standing_authority(self, authority_id='private-standing', *, now=None):
        current = self.get_standing_authority(authority_id)
        if current is None:
            return False
        current['revoked'] = True
        current['revoked_at'] = time.time() if now is None else float(now)
        self.save_standing_authority(current, authority_id=authority_id, now=now)
        with self.db() as db:
            db.execute("UPDATE proactive_records SET state='revoked' "
                       "WHERE state IN ('ready','accepted')")
        return True

    def _digest(self, source):
        sources = {item['id']:item for item in self.list_sources()}
        if source not in sources:
            raise KeyError(source)
        metadata = sources[source]
        items = self.list_materials(source)
        lines = ['# Source preparation digest','',f"Device: {metadata['device']}",f"Source: {metadata['path']}",'','Source excerpts and review prompts; not verified learning material.','']
        for item in items:
            lines.extend([f"## {item['path']}",f"Provenance: {item['source']} / {item['path']} | SHA256 {item['hash']}",'','```text',item['excerpt'].replace('```','\u0060 \u0060 \u0060'),'```','','Review prompts: Explain the central idea in your own words. Which claims need clarification? Can you give an independent example?',''])
        content = '\n'.join(lines)
        folder = self.home / 'artifacts' / time.strftime('%Y-%m-%d')
        folder.mkdir(parents=True,exist_ok=True)
        path = folder / (source+'.md')
        # Single deterministic artifact per source/day; atomic replacement on changes.
        if not path.exists() or path.read_text(encoding='utf-8') != content:
            temporary = path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
            temporary.write_text(content,encoding='utf-8')
            temporary.replace(path)
        return {'artifact':str(path),'materials':len(items)}

    def run_once(self, scan=True, max_jobs=3):
        results = []
        if scan:
            for source in self.list_sources():
                if source['enabled']:
                    results.append(self.scan_source(source['id']))
        owner = uuid.uuid4().hex
        for _ in range(max_jobs):
            job = self.claim(owner)
            if job is None:
                break
            try:
                result = self._digest(job['payload']['source']) if job['kind']=='source_digest' else self.scan_source(job['payload']['source'])
                self.complete(job['id'],owner,result)
                results.append(result)
            except Exception as exc:
                self.complete(job['id'],owner,error=str(exc))
                results.append({'error':str(exc)})
        return results
