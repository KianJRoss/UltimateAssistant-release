"""Spoken study in the existing Obsidian vault, through the ordinary device tool route."""
import json
from pathlib import Path
import time
import uuid
from mcp.server.fastmcp import FastMCP
from ultimate_assistant import voice_tools
from ultimate_assistant.school_sync import content_value
from ultimate_assistant.settings import settings
from ultimate_assistant.study_brain import StudyBrain

mcp=FastMCP('spoken-study')

def brain():
    config=json.loads((settings.user_settings_file.parent/'study-config.json').read_text())
    return StudyBrain(settings.user_data_dir/'study/reading.sqlite3',config['vault'])

@mcp.tool()
def capture_open_reading(tab_id:str,course:str) -> dict:
    """Capture the actual book page currently open in the connected browser. Norton's content
    iframe is supported. No navigation, form submission, or vendor-practice answers are performed.
    The source/position stays attached to questions and notes. Use the actual course name/code.
    """
    from ultimate_assistant.onboarding import router_request
    config=json.loads((settings.user_settings_file.parent/'study-config.json').read_text())
    code='''(()=>{let d=document;const f=document.querySelector('iframe#iframe-content');
    if(f){if(!f.contentDocument)throw new Error('Book iframe unavailable');d=f.contentDocument;}
    const root=d.querySelector('article,[role="document"],main')||d.body;
    const text=root.innerText;if(!text||!text.trim())throw new Error('No readable book text');
    return {title:d.title||document.title,url:location.href,locator:f?new URL(f.src).pathname:location.pathname,text};})()'''
    r=content_value(router_request('POST','/tools/run',{'project':config['project'],'part':config['part'],
        'name':config['kapture_instance']+'.evaluate','arguments':{'tabId':tab_id,'code':code,'timeout':15000}}))
    source=r['value']
    return brain().capture(source['title'],source['url'],course,source['locator'],source['text'])

@mcp.tool()
def start_guided_reading(source_id:str) -> dict:
    """Start at a short pre-reading question. The next_text is actual source material for grounding
    your question. Ask one question, wait for the user's real answer, assess it, then begin reading.
    """
    return brain().start(source_id)

@mcp.tool()
def reading_state(session_id:str) -> dict:
    """Reading position, source, captured notes and actual understanding evidence; resumes after restart."""
    return brain().state(session_id)

@mcp.tool()
def ask_reading_question(session_id:str,question:str,topic:str,request_id:str) -> dict:
    """Ask one SOURCE-GROUNDED question aloud; wait for the learner before explaining the answer.
    Use it before reading and at checkpoints. Do not supply answers to graded course questions.
    """
    result=brain().ask(session_id,question,topic,request_id)
    result['voice']=voice_tools.speak_to_user(question,'study-question:'+request_id,listen=True)
    return result

@mcp.tool()
def record_reading_answer(session_id:str,question_id:str,answer:str,outcome:str,evidence:str) -> dict:
    """After the learner answers, record their actual words and assessment (explained/partly/not_yet).
    Explain gaps conversationally, then continue. Never equate playback with understanding.
    This writes their Obsidian study note; it does not claim a spaced-repetition review occurred.
    """
    return brain().answer(session_id,question_id,answer,outcome,evidence)

@mcp.tool()
def read_next_passages(session_id:str,passages:int=1) -> dict:
    """Read up to three actual passages aloud, recording progress ONLY after completed playback.
    Pauses at understanding checkpoints. After a checkpoint, ask one question and wait for the
    user. Accepted/failed/interrupted audio never advances the reading position.
    """
    store=brain();results=[]
    for _ in range(max(1,min(passages,3))):
        passage=store.prepare(session_id)
        if passage['state']=='finished':break
        key=passage['delivery_key']
        result=voice_tools.speak_to_user(passage['text'],key,listen=True,reading_session=session_id)
        if result.get('state')=='busy':return {'state':'busy','advanced':False}
        if result.get('state')=='queued':
            return {'state':'queued_for_current_voice_turn','advanced':False,
                    'instruction':'The current voice reply will read this source passage. Do not speak a second copy or assess understanding before the user answers.',
                    'session':store.state(session_id)}
        deadline=time.monotonic()+150
        while result.get('state') in ('accepted','speaking') and time.monotonic()<deadline:
            time.sleep(.3);result=voice_tools.spoken_delivery_status(key)
        if result.get('state')=='completed':
            # The device acknowledgement has already committed progress.
            results.append({'advanced':True,'cursor':store.state(session_id)['cursor']})
        else:results.append(store.acknowledge(session_id,key,result.get('state','unknown')))
        if result.get('state')!='completed':break
        if store.state(session_id)['phase'] in ('checkpoint','finished'):break
    return {'playback':results,'session':store.state(session_id)}

@mcp.tool()
def capture_study_note(session_id:str,note:str,request_id:str) -> dict:
    """Save a useful explanation, connection, learner question or note in the active Obsidian study note."""
    return brain().note(session_id,note,request_id)

@mcp.tool()
def add_reading_review_card(session_id:str,question:str,answer:str,request_id:str) -> dict:
    """Append a source-grounded Question::Answer card tagged for the vault's EXISTING Spaced
    Repetition plugin. Preserve its scheduling comments and the user's edits. No invented review.
    """
    return brain().flashcard(session_id,question,answer,request_id)

if __name__=='__main__':mcp.run()
