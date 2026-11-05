"""Read Canvas through an existing Kapture connection into the assistant's task store.

No Canvas token or copied browser cookies. Only GET requests inside the selected origin;
the existing life clock owns reminder delivery. Missing rows never imply completion.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from contextlib import closing
import sqlite3
from urllib.parse import urlsplit

from ultimate_assistant.task_store import TaskStore


def content_value(result):
    if result.get('ok') is False or result.get('isError'):
        raise RuntimeError('Kapture request failed; previous assignments retained')
    if isinstance(result.get('result'), dict):
        return content_value(result['result'])
    content = result.get('content', [])
    for block in content:
        if block.get('type') == 'text':
            data = json.loads(block['text'])
            if isinstance(data, dict) and (data.get('error') or data.get('success') is False):
                raise RuntimeError('Kapture browser operation failed; previous assignments retained')
            return data
    raise ValueError('Kapture returned no structured result')


def browser_script(origin):
    # Origin is a JSON string, never interpolated as executable JavaScript.
    return '''(async()=>{
const expected=ORIGIN;
if(location.origin!==expected)throw new Error('Wrong Canvas origin');
const profileResponse=await fetch('/api/v1/users/self/profile',{method:'GET',credentials:'same-origin'});
if(!profileResponse.ok)throw new Error('Canvas account check failed');
const profile=await profileResponse.json();
async function all(path){let url=new URL(path,expected).href,rows=[],pages=0;
 while(url){if(++pages>20)throw new Error('Incomplete paginated snapshot');
  const target=new URL(url);if(target.origin!==expected||!target.pathname.startsWith('/api/v1/'))throw new Error('Unexpected pagination target');
  const r=await fetch(target.href,{method:'GET',credentials:'same-origin'});
  if(!r.ok)throw new Error('Canvas GET failed '+r.status);
  const d=await r.json();if(!Array.isArray(d))throw new Error('Expected Canvas array');rows.push(...d);
  const next=(r.headers.get('Link')||'').match(/<([^>]+)>; rel="next"/);url=next?next[1]:null;
 }return rows;}
const courses=await all('/api/v1/courses?enrollment_state=active&include[]=term&per_page=100');
const out=[];
for(const c of courses){if(!c.name||!c.enrollments?.some(e=>e.type==='student'&&e.enrollment_state==='active'))continue;
 const assignments=await all('/api/v1/courses/'+c.id+'/assignments?include[]=submission&per_page=100');
 out.push({course_id:c.id,name:c.name,code:c.course_code,term:c.term?.name||'',complete:true,assignments:assignments.map(a=>({id:a.id,name:a.name,due_at:a.due_at,html_url:a.html_url,points_possible:a.points_possible,published:a.published,submission_types:a.submission_types,submission:a.submission?{workflow_state:a.submission.workflow_state,submitted_at:a.submission.submitted_at,excused:a.submission.excused,missing:a.submission.missing}:null}))});}
return {origin:expected,user_id:profile.id,complete:true,observed_at:new Date().toISOString(),courses:out};})()'''.replace('ORIGIN',json.dumps(origin))


def fetch_snapshot(config, request):
    instance = config['kapture_instance']
    scope = {'project':config['project'], 'part':config['part']}
    def call(name, arguments):
        return content_value(request('POST','/tools/run',{
            **scope,'name':instance+'.'+name,'arguments':arguments}))
    origin=config['origin'].rstrip('/')
    parsed=urlsplit(origin)
    if parsed.scheme!='https' or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError('Configure an HTTPS Canvas origin without credentials or paths')
    tabs=call('list_tabs',{})
    tabs=tabs if isinstance(tabs,list) else tabs.get('tabs',[])
    matches=[t for t in tabs if urlsplit(t.get('url','')).scheme+'://'+urlsplit(t.get('url','')).netloc==origin]
    if not matches:raise RuntimeError('Canvas tab is not connected to Kapture; previous assignments retained')
    tab=matches[0]
    if tab.get('evalAllowed') is False:raise RuntimeError('Canvas tab needs Kapture JavaScript execution enabled')
    response=call('evaluate',{'tabId':str(tab['tabId']),'code':browser_script(origin),'timeout':60000})
    snapshot=response.get('value')
    if not isinstance(snapshot,dict):raise ValueError('Canvas browser snapshot is missing')
    if str(snapshot.get('user_id'))!=str(config['user_id']):raise ValueError('Canvas is signed into a different account; previous assignments retained')
    return snapshot


def import_snapshot(snapshot, database, origin):
    if snapshot.get('complete') is not True or snapshot.get('origin')!=origin:
        raise ValueError('Incomplete or wrong-origin Canvas snapshot')
    observed=datetime.fromisoformat(snapshot['observed_at'].replace('Z','+00:00'))
    if observed.utcoffset() is None:raise ValueError('Snapshot needs a timezone')
    records=[];seen=set()
    for course in snapshot['courses']:
        if course.get('complete') is not True:raise ValueError('Incomplete course snapshot')
        cid=int(course['course_id'])
        for a in course['assignments']:
            aid=int(a['id']);identity=f'canvas:{origin}:{cid}:{aid}'
            if identity in seen:raise ValueError('Duplicate Canvas assignment identity')
            seen.add(identity)
            if a.get('published') is False:continue
            due=a.get('due_at')
            if due:
                moment=datetime.fromisoformat(due.replace('Z','+00:00'))
                if moment.utcoffset() is None:raise ValueError('Due date needs a timezone')
                due=moment.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
            title=f"{course['name']}: {a['name']}"
            url=a.get('html_url','')
            if urlsplit(url).scheme+'://'+urlsplit(url).netloc!=origin:raise ValueError('Assignment link has unexpected origin')
            submission=a.get('submission') or {}
            completed=(submission.get('excused') is True or bool(submission.get('submitted_at'))
                       or submission.get('workflow_state') in ('submitted','pending_review')
                       or (submission.get('workflow_state')=='graded' and submission.get('missing') is not True))
            external='external_tool' in (a.get('submission_types') or [])
            notes=f"Canvas assignment: {url}\nPoints: {a.get('points_possible')}\nObserved: {snapshot['observed_at']}\n"
            if external:notes+='External tool: Canvas unsubmitted status may lag vendor completion; verify in the linked tool.\n'
            records.append((identity,title,due,notes,url,completed))
    # Validate every row before opening the write transaction. Stable identity survives renames/deadline changes.
    TaskStore(Path(database))
    result={'created':0,'updated':0,'completed_from_canvas':0,'assignments_observed':len(records),'courses':len(snapshot['courses'])}
    with closing(sqlite3.connect(database,timeout=15)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        for source,title,due,notes,url,completed in records:
            task_id=hashlib.sha256(source.encode()).hexdigest()[:32]
            old=conn.execute('SELECT status FROM assistant_tasks WHERE id=?',(task_id,)).fetchone()
            # Respect local Done; an external tool reporting unsubmitted must not reopen it.
            status='done' if completed else old[0] if old else 'open'
            now=datetime.now(timezone.utc).isoformat()
            conn.execute('''INSERT INTO assistant_tasks(id,title,details,domain,kind,due_at,status,source,evidence,created_at,updated_at)
            VALUES(?,?,?,'school','commitment',?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            title=excluded.title,details=excluded.details,due_at=excluded.due_at,status=excluded.status,evidence=excluded.evidence,updated_at=excluded.updated_at''',
            (task_id,title,notes,due,status,source,url,now,now))
            result['updated' if old else 'created']+=1
            if completed and (not old or old[0]!='done'):result['completed_from_canvas']+=1
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    args=parser.parse_args();config=json.loads(args.config.read_text())
    from personal_assistant.bootstrap import configure
    settings=configure()
    from ultimate_assistant.onboarding import router_request
    folder=settings.user_data_dir/'school-sync';folder.mkdir(parents=True,exist_ok=True)
    statefile=folder/'state.json'
    previous=json.loads(statefile.read_text()) if statefile.exists() else {}
    try:
        snapshot=fetch_snapshot(config,router_request)
        result=import_snapshot(snapshot,settings.tasks_db,config['origin'])
        (folder/'canvas-snapshot.json').write_text(json.dumps(snapshot,indent=2))
        state={**result,'state':'ready','last_success':snapshot['observed_at'],'origin':config['origin']}
    except Exception as error:
        state={**previous,'state':'needs_attention','last_attempt':datetime.now(timezone.utc).isoformat(),
               'error':str(error)[:300] if isinstance(error,(ValueError,RuntimeError)) else type(error).__name__}
    temp=statefile.with_suffix('.tmp');temp.write_text(json.dumps(state,indent=2));temp.replace(statefile)
    print(json.dumps(state,indent=2))
    return 0 if state['state']=='ready' else 1


if __name__=='__main__':raise SystemExit(main())
