"""Source-linked reading progress and understanding evidence, projected into Obsidian.

Obsidian's existing Spaced Repetition plugin owns card scheduling. This store tracks
reading and observed answers; it never manufactures a completed review or mastery.
"""
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

def now():return datetime.now(timezone.utc).isoformat()
def slug(value):return re.sub(r'[^\w .-]','_',value).strip(' .')[:90] or 'Reading'
def chunks(text,maximum=900):
    parts=[]
    for paragraph in re.split(r'\n\s*\n',text.strip()):
        while len(paragraph)>maximum:
            end=paragraph.rfind(' ',0,maximum)
            if end<1:end=maximum
            parts.append(paragraph[:end]);paragraph=paragraph[end:].lstrip()
        if paragraph.strip():parts.append(paragraph.strip())
    return parts

class StudyBrain:
    def __init__(self,database,vault):
        self.database=Path(database);self.vault=Path(vault).resolve()
        if not (self.vault/'.obsidian').is_dir():raise ValueError('Configure the existing Obsidian vault')
        self.database.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.db()) as c,c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS reading_sources(id TEXT PRIMARY KEY,title TEXT,url TEXT,course TEXT,locator TEXT,chunks TEXT,captured TEXT);
            CREATE TABLE IF NOT EXISTS reading_sessions(id TEXT PRIMARY KEY,source_id TEXT,phase TEXT,cursor INTEGER,pending TEXT,created TEXT,updated TEXT);
            CREATE TABLE IF NOT EXISTS reading_events(id TEXT PRIMARY KEY,session_id TEXT,kind TEXT,topic TEXT,question TEXT,answer TEXT,outcome TEXT,evidence TEXT,created TEXT);
            ''')
    def db(self):
        c=sqlite3.connect(self.database,timeout=15);c.row_factory=sqlite3.Row;return c
    def capture(self,title,url,course,locator,text):
        from urllib.parse import urlsplit,urlunsplit
        parsed=urlsplit(url)
        if parsed.scheme not in ('https','http') or parsed.username or parsed.password:raise ValueError('Expected a source URL without credentials')
        url=urlunsplit((parsed.scheme,parsed.netloc,parsed.path,'',''))
        if not text.strip() or len(text)>200000:raise ValueError('Capture one actual reading section (1–200000 characters)')
        ident=hashlib.sha256((url+'\0'+locator+'\0'+text).encode()).hexdigest()[:24]
        with closing(self.db()) as c,c:
            c.execute('INSERT OR IGNORE INTO reading_sources VALUES(?,?,?,?,?,?,?)',
                      (ident,title,url,course,locator,json.dumps(chunks(text)),now()))
        return {'source_id':ident,'chunks':len(chunks(text)),'title':title,'url':url,'locator':locator}
    def start(self,source_id):
        with closing(self.db()) as c,c:
            if not c.execute('SELECT 1 FROM reading_sources WHERE id=?',(source_id,)).fetchone():raise ValueError('Unknown reading source')
            ident=uuid.uuid4().hex
            c.execute('INSERT INTO reading_sessions VALUES(?,?,?,0,NULL,?,?)',(ident,source_id,'precheck',now(),now()))
        self.export(ident);return self.state(ident)
    def state(self,session_id):
        with closing(self.db()) as c:
            s=c.execute('SELECT * FROM reading_sessions WHERE id=?',(session_id,)).fetchone()
            if not s:raise ValueError('Unknown reading session')
            source=c.execute('SELECT * FROM reading_sources WHERE id=?',(s['source_id'],)).fetchone()
            events=[dict(r) for r in c.execute('SELECT * FROM reading_events WHERE session_id=? ORDER BY created,id',(session_id,))]
        parts=json.loads(source['chunks']);s=dict(s)
        return {**s,'title':source['title'],'course':source['course'],'url':source['url'],'locator':source['locator'],
                'total_chunks':len(parts),'next_text':parts[s['cursor']] if s['cursor']<len(parts) else None,'events':events,
                'completion_is_not_understanding':True}
    def ask(self,session_id,question,topic,request_id):
        if not question.strip() or len(question)>1500:raise ValueError('Ask one bounded question')
        state=self.state(session_id)
        if state['pending']:raise ValueError('Wait for current narration to finish before asking')
        with closing(self.db()) as c,c:
            c.execute('INSERT OR IGNORE INTO reading_events VALUES(?,?,?,?,?,?,?,?,?)',
                (request_id,session_id,'question',topic,question,'','','',now()))
        self.export(session_id);return {'question_id':request_id,'question':question,'reveal_answer':False}
    def answer(self,session_id,question_id,answer,outcome,evidence):
        if outcome not in ('explained','partly','not_yet'):raise ValueError('Use explained, partly, or not_yet')
        if not answer.strip() or not evidence.strip():raise ValueError('An actual answer and assessment evidence are required')
        with closing(self.db()) as c,c:
            row=c.execute('SELECT * FROM reading_events WHERE id=? AND session_id=? AND kind=?',(question_id,session_id,'question')).fetchone()
            if not row:raise ValueError('Unknown question')
            if row['answer']:
                if row['answer']!=answer:raise ValueError('This question already has a recorded answer')
                return self.state(session_id)
            c.execute('UPDATE reading_events SET answer=?,outcome=?,evidence=? WHERE id=?',(answer,outcome,evidence,question_id))
            c.execute("UPDATE reading_sessions SET phase='reading',updated=? WHERE id=? AND phase IN ('precheck','checkpoint')",(now(),session_id))
        self.export(session_id);return self.state(session_id)
    def prepare(self,session_id):
        state=self.state(session_id)
        if state['phase'] in ('precheck','checkpoint'):raise ValueError('Ask and assess a short understanding question before continuing')
        if not state['next_text']:return {'state':'finished'}
        key=state['pending'] or f"reading:{session_id}:{state['cursor']}"
        with closing(self.db()) as c,c:c.execute('UPDATE reading_sessions SET pending=? WHERE id=?',(key,session_id))
        return {'delivery_key':key,'text':state['next_text'],'cursor':state['cursor'],'state':'ready'}
    def acknowledge(self,session_id,delivery_key,playback_state):
        state=self.state(session_id)
        if state['pending']!=delivery_key:raise ValueError('Playback does not match the pending passage')
        if playback_state!='completed':return {'advanced':False,'state':playback_state,'cursor':state['cursor']}
        with closing(self.db()) as c,c:
            cursor=state['cursor']+1
            phase='finished' if cursor>=state['total_chunks'] else 'checkpoint' if cursor%3==0 else 'reading'
            c.execute('UPDATE reading_sessions SET cursor=?,pending=NULL,phase=?,updated=? WHERE id=? AND pending=?',
                (cursor,phase,now(),session_id,delivery_key))
        self.export(session_id);return {'advanced':True,'cursor':cursor}
    def note(self,session_id,text,request_id):
        self.state(session_id)
        if not text.strip():raise ValueError('A note must contain content')
        with closing(self.db()) as c,c:c.execute('INSERT OR IGNORE INTO reading_events VALUES(?,?,?,?,?,?,?,?,?)',
            (request_id,session_id,'note','',text,'','','',now()))
        return self.export(session_id)
    def path(self,relative):
        p=self.vault/relative
        if not p.resolve().is_relative_to(self.vault) or any(q.is_symlink() for q in [p,*p.parents] if q!=self.vault.parent):
            raise ValueError('Vault writes must stay inside the configured vault without symlinks')
        p.parent.mkdir(parents=True,exist_ok=True);return p
    def export(self,session_id):
        s=self.state(session_id)
        relative=Path('Ivy Tech/Class Notes/Reading Sessions')/(slug(s['title'])+' - '+session_id[:8]+'.md')
        p=self.path(relative)
        body=[f"## Reading session",f"Source: [{s['title']}]({s['url']})",f"Location: {s['locator']}",
              f"Playback completed: {s['cursor']} / {s['total_chunks']} passages",f"Session phase: {s['phase']}",
              'Understanding is assessed from answers below; finishing playback alone does not establish it.','']
        for e in s['events']:
            if e['kind']=='note':body+=['### Captured note',e['question'],'']
            else:body+=['### '+(e['topic'] or 'Understanding check'),'Question: '+e['question'],
                        'Your answer: '+(e['answer'] or '(waiting)'), 'Assessment: '+(e['outcome'] or 'unassessed'),
                        'Evidence: '+(e['evidence'] or '(none yet)'),'']
        start='<!-- UA-reading:start -->';end='<!-- UA-reading:end -->'
        generated=start+'\n'+'\n'.join(body)+'\n'+end
        if p.exists():
            text=p.read_text()
            if start not in text or end not in text:raise ValueError('Existing note has no managed block; leaving it unchanged')
            before,rest=text.split(start,1);_,after=rest.split(end,1);text=before+generated+after
        else:
            text='---\ntype: class-note\ncourse: '+json.dumps(s['course'])+'\ndate: '+now()[:10]+'\ntopic: '+json.dumps(s['title'])+'\ntags: [class-note, reading]\n---\n\n'+generated+'\n\n## My notes\n\n'
        temp=p.with_suffix('.tmp');temp.write_text(text);temp.replace(p)
        return {'note':str(p),'session_id':session_id}
    def flashcard(self,session_id,question,answer,request_id):
        s=self.state(session_id)
        if not question.strip() or not answer.strip() or '\n' in question or '\n' in answer or '::' in question:
            raise ValueError('Use one-line question and answer for the installed review plugin')
        p=self.path(Path('Ivy Tech/Class Notes/Review Cards')/(slug(s['course'])+'.md'))
        marker='<!-- UA-card:'+hashlib.sha256(request_id.encode()).hexdigest()[:24]+' -->'
        text=p.read_text() if p.exists() else '# '+s['course']+' review\n\n#flashcards/'+slug(s['course']).replace(' ','-')+'\n\n'
        if marker in text:return {'note':str(p),'already_exists':True}
        text+='\n'+marker+'\n'+question+'::'+answer+'\n\nSource: ['+s['title']+']('+s['url']+') · '+s['locator']+'\n'
        temp=p.with_suffix('.tmp');temp.write_text(text);temp.replace(p)
        return {'note':str(p),'scheduler':'Obsidian Spaced Repetition','review_performed':False}
