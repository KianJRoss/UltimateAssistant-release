"""Optional in-process worker. No model calls, subprocesses, or startup registration."""
import logging
import hashlib
import json
import threading
import time
from pathlib import Path

from .store import Store


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding='utf-8')
    temporary.replace(path)


def _content(record):
    value = record.get('content')
    return value if isinstance(value, dict) else {'text': '' if value is None else str(value)}


def _title(record):
    content = _content(record)
    return str(content.get('title') or content.get('name') or
               content.get('summary') or record.get('source_id') or
               record['source_identity'])


def apply_organized_ingestion(store, organized, *, batch=None, material_root=None):
    """Apply selected-source observations through the retained preparation APIs.

    The small checkpoint records source identity/revision/freshness; canonical
    commitments remain ``Store`` contexts and material inputs remain registered
    ``Store`` sources.  Source originals and existing user-written context notes
    are never edited or replaced.
    """
    if not isinstance(store, Store):
        raise TypeError('store must be a preparation Store')
    batch = batch or {}
    state_path = store.home / 'selected-source-ingestion.json'
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        state = {'version': 1, 'records': {}, 'sources': {}}
    retained = state.setdefault('records', {})
    source = str(batch.get('source') or organized.get('provider') or 'selected')
    snapshot = str(batch.get('snapshot') or
                   ('complete' if batch.get('complete') or
                    (batch.get('checkpoint') or {}).get('complete') else 'partial'))
    authoritative = bool(batch.get('deletion_authoritative', snapshot == 'complete'))
    source_state = str(batch.get('state') or 'verified')
    freshness = dict(batch.get('freshness') or {})
    contexts = {item['id']: item for item in store.list_context()}
    organized_commitments = {
        str(item.get('source_identity')): item
        for item in (organized.get('commitments') or ()) if item.get('source_identity')
    }
    organized_materials = {
        str(item.get('source_identity')): item
        for item in (organized.get('materials') or ()) if item.get('source_identity')
    }
    related = {}
    for link in organized.get('links') or ():
        identities = [str(value) for value in link.get('source_identities') or ()]
        material_ids = [organized_materials[value]['id'] for value in identities
                        if value in organized_materials]
        for identity in identities:
            related.setdefault(identity, []).extend(material_ids)
    changes, materials = [], []
    material_mutated = False

    for record in organized.get('records') or ():
        identity = str(record['source_identity'])
        old = retained.get(identity) or {}
        digest = str(record.get('revision_digest') or
                     (record.get('provenance') or {}).get('raw_sha256') or '')
        revision = int(record.get('revision') or old.get('revision') or 1)
        if record.get('tombstone'):
            if not authoritative:
                continue
            material_path = old.get('material_path')
            if material_path and material_root is not None:
                candidate = Path(material_path)
                root_bound = Path(material_root).resolve()
                if candidate.is_file() and candidate.resolve().is_relative_to(root_bound):
                    candidate.unlink()
                    material_mutated = True
            retained[identity] = {**old, 'active': False, 'revision': revision,
                                  'digest': digest, 'source': source}
            changes.append({'source_identity': identity, 'revision': revision,
                            'change': 'deleted'})
            continue
        category = record.get('classification')
        content = _content(record)
        entry = {**old, 'active': True, 'revision': revision, 'digest': digest,
                 'source': source, 'classification': category,
                 'provenance': record.get('provenance'),
                 'source_url': record.get('source_url'),
                 'due_at': content.get('due_at') or content.get('due') or content.get('deadline')}
        retained[identity] = entry
        changed = old.get('digest') != digest or not old.get('active', True)
        if category in ('assignment', 'task', 'appointment'):
            context_id = 'source-' + hashlib.sha256(identity.encode()).hexdigest()[:24]
            due = content.get('due_at') or content.get('due') or content.get('deadline')
            kind = 'appointment' if category == 'appointment' else 'task'
            current = contexts.get(context_id)
            organized_commitment = organized_commitments.get(identity) or {}
            provenance_note = 'Source: ' + identity
            desired = {
                'kind': kind, 'title': _title(record), 'due': due,
                'next_action': str(content.get('next_action') or 'Review source update'),
                # Notes belong to the user.  Only initialize an empty record
                # with provenance; later source changes preserve their value.
                'notes': current['notes'] if current else provenance_note,
                'last_state': 'source revision ' + str(revision),
                'linked_commitment_id': str(content.get('commitment_id') or
                                            organized_commitment.get('id') or context_id),
                'source_identity': identity,
                'source_revision': revision,
                'learning_activity_id': content.get('learning_activity_id'),
                'material_refs': list(dict.fromkeys(
                    list(content.get('material_refs') or []) + related.get(identity, []))),
            }
            same = current and all(current.get(key) == value for key, value in desired.items())
            context = current if same else store.save_context(id=context_id, **desired)
            entry['context_id'] = context_id
            entry['context_revision'] = context['revision']
            entry['linked_commitment_id'] = context['linked_commitment_id']
        if category in ('course_material', 'note', 'reference'):
            materials.append(record)
        if changed:
            changes.append({'source_identity': identity, 'revision': revision,
                            'change': 'added' if not old else 'changed',
                            'context_id': entry.get('context_id')})

    # Provider failures and partial snapshots update freshness only.  They do
    # not infer absence, delete records, or erase previously prepared material.
    state.setdefault('sources', {})[source] = {
        'state': source_state,
        'snapshot': snapshot,
        'freshness': freshness,
        'stale': source_state not in ('verified', 'ready') or snapshot == 'partial',
        'deletion_authoritative': authoritative,
    }
    material_source = None
    if (materials or material_mutated) and material_root is not None:
        root = Path(material_root).resolve() / hashlib.sha256(source.encode()).hexdigest()[:16]
        root.mkdir(parents=True, exist_ok=True)
        for record in materials:
            identity = str(record['source_identity'])
            target = root / (hashlib.sha256(identity.encode()).hexdigest() + '.md')
            content = record.get('content')
            body = content if isinstance(content, str) else json.dumps(content, indent=2,
                                                                        sort_keys=True)
            rendered = ('Source identity: ' + identity + '\nSource revision: ' +
                        str(record.get('revision', 1)) + '\n\n' + body)
            if not target.exists() or target.read_text(encoding='utf-8') != rendered:
                target.write_text(rendered, encoding='utf-8')
            retained[str(record['source_identity'])]['material_path'] = str(target)
        material_source = store.register_source(root, device='selected source: ' + source)
        store.scan_source(material_source)
    _atomic_json(state_path, state)
    # Ingestion is event-driven: publish its own revision observations into the
    # same durable proactive authority.  Cheap runtime ticks never poll a
    # provider, and unchanged revisions produce no additional responsibility.
    for change in changes:
        identity = change['source_identity']
        revision = change['revision']
        payload = {
            'kind': 'source_revision_followup', **change,
            'source': source, 'source_state': source_state,
            'message': ('A linked source was deleted; reassess retained commitments.'
                        if change['change'] == 'deleted' else
                        'A linked source changed; review its commitment and prepared materials.'),
            'completion_inferred': False,
        }
        fingerprint = hashlib.sha256(json.dumps(
            payload, sort_keys=True, default=str).encode()).hexdigest()
        store.save_proactive_record(
            payload['kind'], payload, fingerprint=fingerprint,
            intent_key='source_revision_followup:' + identity,
            revision=revision if isinstance(revision, int) and revision > 0 else 1)
    return {'identity_changes': changes, 'material_records': len(materials),
            'material_source': material_source, 'snapshot': snapshot,
            'stale': state['sources'][source]['stale']}


class Worker:
    def __init__(self, store=None, interval=60, scan_interval=300):
        self.store = store or Store()
        self.interval = max(30,interval)
        self.scan_interval = max(300,scan_interval)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,name='personal-preparation',daemon=True)
        self._thread.start()
        return self

    def _run(self):
        last_scan = float('-inf')
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                scan = now-last_scan >= self.scan_interval
                self.store.run_once(scan=scan)
                if scan:
                    last_scan = now
            except Exception:
                logging.getLogger(__name__).exception('Preparation pass failed; will retry at next interval')
            self._stop.wait(self.interval)

    def stop(self, timeout=5):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
