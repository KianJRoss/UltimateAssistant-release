"""Read existing personal records without creating stores or starting workers."""
import os
import json
from contextlib import closing
from pathlib import Path
import sqlite3

from .today import build_today
from .paths import life_root

MAX_RECORDS = 10000


def _read(database, table, condition='', order='rowid'):
    path = Path(database).resolve()
    if not path.is_file():
        return [], {'state': 'not_connected', 'records': 0, 'truncated': False}
    try:
        # mode=ro also prevents accidentally creating an empty replacement DB.
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            rows = db.execute(f'SELECT * FROM {table} {condition} ORDER BY {order} LIMIT ?',
                              (MAX_RECORDS + 1,)).fetchall()
        return [dict(row) for row in rows[:MAX_RECORDS]], {
            'state': 'ready', 'records': min(len(rows), MAX_RECORDS),
            'truncated': len(rows) > MAX_RECORDS}
    except sqlite3.Error:
        # Display failure rather than claiming the source contains no work.
        return [], {'state': 'unavailable', 'records': 0, 'truncated': False}


def snapshot(*, tasks_db=None, study_db=None, preparation_db=None, learning_db=None,
             progress_db=None, unit_home=None, routine_home=None, now=None, limit=10):
    """Return a read-only Today snapshot; limit controls visible cards per section."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 100:
        raise ValueError('limit must be an integer from 0 to 100')
    home = life_root()
    from .record import tasks_db as _tasks_db
    tasks, task_state = _read(tasks_db or _tasks_db(),
                              'assistant_tasks', "WHERE status NOT IN ('done','completed','cancelled','canceled')")
    from .task_units import apply_definitions, read_definitions
    definitions, unit_state = read_definitions(unit_home)
    tasks = apply_definitions(tasks, definitions)
    for task in tasks:
        if 'unit_definition_revision' not in task:
            continue
        for unit in task['units']:
            unit.setdefault('completed', False)
        task['estimated_minutes'] = sum(unit['estimated_minutes']
                                        for unit in task['units'])
    progress_rows, progress_state = _read(progress_db or home/'progress/progress.sqlite', 'progress_records',order='sequence')
    progress=[]
    try:
        from .progress_state import apply_progress, _bounded_text, _note, _unit_ids, _remaining_minutes
        for row in progress_rows:
            record=json.loads(row['payload'])
            if not isinstance(record,dict):
                raise ValueError('Invalid progress record')
            _bounded_text(record.get('activity_id'),'activity_id',512)
            _bounded_text(record.get('request_id'),'request_id',512)
            _note(record.get('note'))
            _unit_ids(record.get('completed_unit_ids'))
            _remaining_minutes(record.get('remaining_minutes'))
            record['sequence']=row['sequence']
            progress.append(record)
        tasks=apply_progress(tasks,progress)
    except (ValueError,TypeError,KeyError,OverflowError):
        progress=[]
        progress_state=dict(progress_state,state='unavailable')
    contexts, context_state = _read(preparation_db or home / 'preparation/preparation.sqlite', 'contexts')
    # Operational records belong to Preparation/development monitoring, not
    # the user's daily activity list. Do not infer this from arbitrary titles.
    visible_contexts = [row for row in contexts
                        if row.get('id') != 'assistant-admin-development'
                        and not str(row.get('id', '')).startswith('tester-')]
    context_state['operational_records_excluded'] = len(contexts) - len(visible_contexts)
    sessions, study_state = _read(study_db or os.environ.get('ULTIMATE_ASSISTANT_STUDY_DB', home / 'data/study_sessions.sqlite3'),
                                  'study_sessions', "WHERE status NOT IN ('completed','cancelled','canceled')",
                                  'planned_start, created_at, id')
    sources, source_state = _read(preparation_db or home / 'preparation/preparation.sqlite', 'sources', order='path')
    value = build_today(tasks, visible_contexts, now=now, limit=limit)
    from .deadline_policy import rank_commitments
    ranked=rank_commitments([t for t in tasks if t.get('kind','commitment')=='commitment'],now=value['as_of'])
    for card in ranked:
        action=card['record'].get('next_action')
        ready=isinstance(action,str) and bool(action.strip())
        card['next_step']={'action':'work' if ready else 'review',
                          'text':action if ready else 'Review this commitment and choose its next action.'}
    value['commitments']['items']=ranked[:limit]
    groups={}
    for card in ranked[:limit]:
        domain=card['record'].get('domain')
        domain=domain if isinstance(domain,str) and domain.strip() else 'unspecified'
        groups.setdefault(domain,[]).append(card)
    value['domain_groups']=[{'domain':key,'items':groups[key]} for key in sorted(groups)]
    # Study progress comes from the school module's own records when it's installed (life/study.py).
    from .study import snapshot as study_snapshot
    study = study_snapshot(value.get('as_of'), path=learning_db)
    learning_state = {'state': 'not_connected' if study['state'] == 'not_installed' else study['state'],
                      'records': len(study['understanding']), 'truncated': False}
    value['learning']['activities'] = study['understanding']
    value['learning']['flashcards'] = study['flashcards']
    from .routine_store import RoutineStore
    routines = RoutineStore(routine_home).snapshot(now=value['as_of'], limit=limit)
    routine_state = {
        'state': routines['state'],
        'records': routines['total'],
        'truncated': routines['omitted'] > 0,
    }
    value['inputs'] = {'tasks': task_state, 'contexts': context_state, 'study_sessions': study_state,
                       'preparation_sources': source_state, 'learning_observations':learning_state,
                       'progress_evidence':progress_state, 'task_units':unit_state,
                       'routines': routine_state}
    value['study_sessions'] = {'items': sessions[:limit], 'total': len(sessions),
                               'omitted': max(0, len(sessions) - limit)}
    value['preparation_sources'] = sources
    value['progress_evidence'] = progress
    value['routines'] = routines
    value['advisory'] = True
    return value
