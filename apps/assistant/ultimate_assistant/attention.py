"""Read-only attention projection over existing durable tasks and delegated jobs."""
from datetime import datetime, timezone

TASK_STATES = {'open':'planned','in_progress':'in_progress','awaiting_verification':'needs_review','done':'completed'}
JOB_STATES = {'pending':'queued','queued':'queued','running':'working','completed':'completed','failed':'needs_review','interrupted':'needs_review'}


def snapshot(tasks, jobs, *, tasks_available=True, jobs_available=True):
    items=[]
    for task in tasks:
        items.append({'id':'task:'+task['id'],'source':'tasks','source_id':task['id'],
                      'title':task['title'],'state':TASK_STATES.get(task['status'],'unknown'),
                      'status':task['status'],'doer':task.get('doer'),'details':task.get('details',''),
                      'evidence':task.get('evidence',''),'due_at':task.get('due_at'),
                      'updated_at':task.get('updated_at')})
    for job in jobs:
        items.append({'id':'job:'+job['id'],'source':'jobs','source_id':job['id'],
                      'title':str(job.get('kind') or 'Delegated work')+' · '+job['id'][:8],
                      'state':JOB_STATES.get(job.get('status'),'unknown'),'status':job.get('status'),
                      'details':job.get('content') or '', 'error':job.get('error') or '',
                      'parent_id':job.get('parent_id'),'updated_at':job.get('updated_at')})
    return {'items':items,'sources':{'tasks':{'available':tasks_available},'jobs':{'available':jobs_available}},
            'observed_at':datetime.now(timezone.utc).isoformat()}
