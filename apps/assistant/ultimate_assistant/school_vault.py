"""Project observed Canvas metadata into the user's existing Ivy Tech vault structure."""
import json
from pathlib import Path
import re
from ultimate_assistant.study_brain import slug

def sync(snapshot,vault):
    vault=Path(vault).resolve()
    if not snapshot.get('complete') or not (vault/'.obsidian').is_dir():raise ValueError('Complete Canvas snapshot and existing vault required')
    written=[]
    def write(relative,fields,body):
        p=vault/relative
        if not p.resolve().is_relative_to(vault) or p.is_symlink():raise ValueError('Unsafe note path')
        p.parent.mkdir(parents=True,exist_ok=True)
        start='<!-- UA-Canvas:start -->';end='<!-- UA-Canvas:end -->'
        block=start+'\n'+body+'\n'+end
        if p.exists():
            text=p.read_text()
            if start not in text or end not in text:raise ValueError('Existing note is not managed; preserving it')
            first,rest=text.split(start,1);_,last=rest.split(end,1)
            for key in ('due','canvas_status','observed_at'):
                if key in fields:first=re.sub(r'^'+key+r':.*$',key+': '+json.dumps(fields[key]),first,flags=re.M)
            text=first+block+last
        else:text='---\n'+'\n'.join(k+': '+json.dumps(v) for k,v in fields.items())+'\n---\n\n'+block+'\n\n## My notes\n\n'
        temp=p.with_suffix('.tmp');temp.write_text(text);temp.replace(p);written.append(str(p))
    for course in snapshot['courses']:
        if not course.get('complete'):raise ValueError('Incomplete course')
        code=(course.get('code') or str(course['course_id'])).split('-')[0]
        title=course['name'];course_note=code+' - '+slug(title)
        write(Path('Ivy Tech/Courses')/(course_note+'.md'),{'type':'course','course_id':str(course['course_id']),'status':'Current',
              'term':course.get('term') or 'Current Canvas enrollment','ivylearn_url':snapshot['origin']+'/courses/'+str(course['course_id']),'tags':['course'],'observed_at':snapshot['observed_at']},
              '# '+title+'\n\n[IvyLearn course]('+snapshot['origin']+'/courses/'+str(course['course_id'])+')\n\nStudy sessions and review cards link back to this course. Enrollment and deadlines come from Canvas; understanding comes from actual answers.')
        for a in course['assignments']:
            if a.get('published') is False:continue
            sub=a.get('submission') or {};state=sub.get('workflow_state','unknown')
            status='Submitted' if sub.get('submitted_at') else 'Not Started'
            body='# '+a['name']+'\n\n[Open assignment]('+a['html_url']+')\n\nCanvas reports: '+state+'.'
            if 'external_tool' in (a.get('submission_types') or []):body+=' External-tool completion may lag in Canvas; verify in the linked tool.'
            write(Path('Ivy Tech/Assignments')/(code+' - '+slug(a['name'])+' - '+str(a['id'])+'.md'),
                  {'type':'assignment','course':'[[Ivy Tech/Courses/'+course_note+']]','due':a.get('due_at'),'status':status,'canvas_status':state,
                   'priority':'Medium','source':'IvyLearn','source_url':a['html_url'],'points_possible':a.get('points_possible'),'tags':['assignment'],'observed_at':snapshot['observed_at']},body)
    return {'notes_updated':len(written),'vault':str(vault)}
