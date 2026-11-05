"""Use the user's preferred spoken interface for clock reminders."""
from datetime import datetime,timezone
import hashlib
import json
import time

def show(element):
    from ultimate_assistant.settings import settings
    from ultimate_assistant import canvas
    try:preferences=json.loads(settings.user_settings_file.read_text())
    except (OSError,ValueError):preferences={}
    instance=preferences.get('preferred_voice_instance')
    if preferences.get('reminder_interface')!='voice' or not instance:
        return canvas.store.show(element)
    history=settings.user_data_dir/'spoken-reminder-delivery.json'
    try:last=json.loads(history.read_text())
    except (OSError,ValueError):last={}
    if time.time()-float(last.get('completed_at',0))<900:
        raise RuntimeError('Spoken reminders are coalesced; retained until the quiet interval ends')
    from ultimate_assistant.onboarding import router_request
    from ultimate_assistant.school_sync import content_value
    data=element.get('data') or {}
    covers=data.get('covers') or {}
    if covers:
        labels=list(covers.values())
        text=f"You have {len(labels)} things coming up. First, {labels[0]}. Would you like to work through what's due?"
    else:
        text=str(data.get('question') or element.get('title') or '')
        if not text.endswith('?'):text+=' Would you like to work through it together?'
    payload={'project':settings.herald_project,'part':settings.herald_part}
    def call(tool,args):
        return content_value(router_request('POST','/tools/run',{
            **payload,'name':instance+'.'+tool,'arguments':args}))
    status=call('voice_status',{})
    if status.get('state') not in ('idle','error'):
        raise RuntimeError('Preferred spoken interface is busy; reminder retained for next tick')
    # Stable across retries, but a changed escalation/question gets a fresh delivery.
    key='life:'+hashlib.sha256((str(element['id'])+text).encode()).hexdigest()[:40]
    result=call('speak_to_user',{'text':text,'delivery_key':key,'listen':True})
    deadline=time.monotonic()+90
    while result.get('state') in ('accepted','queued','speaking') and time.monotonic()<deadline:
        time.sleep(.5);result=call('spoken_delivery_status',{'delivery_key':key})
    if result.get('state')!='completed':
        raise RuntimeError('Spoken reminder was not accepted; previous reminder state retained')
    history.parent.mkdir(parents=True,exist_ok=True)
    pending=history.with_suffix('.tmp');pending.write_text(json.dumps({'completed_at':time.time(),'key':key}));pending.replace(history)
    return result

def escalation_speech(text,key):
    """The preferred interface already spoke this reminder; avoid a second phone announcement."""
    from ultimate_assistant.settings import settings
    try:preferences=json.loads(settings.user_settings_file.read_text())
    except (OSError,ValueError):preferences={}
    if preferences.get('reminder_interface')=='voice':return {'state':'preferred_interface_owns_speech'}
    from ultimate_assistant.phone.announce import announce
    return announce(text,key)
