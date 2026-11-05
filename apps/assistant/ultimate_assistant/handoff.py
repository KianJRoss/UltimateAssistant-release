"""Portable, source-linked selection of saved conversation text; no provider calls."""
import json
from datetime import datetime, timezone


def prepare(conversation_id, messages, *, current_request='', history_budget_chars=16000):
    if not 1000 <= history_budget_chars <= 24000:
        raise ValueError('History budget must be between 1000 and 24000 characters')
    if len(current_request)>10000:
        raise ValueError('Next request exceeds 10000 characters')
    selected={}
    used=2
    first=next((i for i,m in enumerate(messages) if m['role']=='user'),None)
    priority=([first] if first is not None else []) + [i for i in range(len(messages)-1,-1,-1) if i!=first]
    for i in priority:
        message=messages[i]
        item={'role':message['role'],'content':message['content'],
              'source':{'conversation_id':conversation_id,'message_id':message['id']}}
        cost=len(json.dumps(item,ensure_ascii=False))+2
        if used+cost<=history_budget_chars:
            selected[i]=item;used+=cost
    history=[selected[i] for i in sorted(selected)]
    omitted=[]
    start=last=None
    for i,message in enumerate(messages):
        if i in selected:
            if start is not None:omitted.append({'from_message_id':start,'through_message_id':last});start=last=None
        else:
            if start is None:start=message['id']
            last=message['id']
    if start is not None:omitted.append({'from_message_id':start,'through_message_id':last})
    return {'schema_version':1,'kind':'portable_conversation_handoff','generated_at':datetime.now(timezone.utc).isoformat(),
            'source_conversation_id':conversation_id,'current_request':current_request,
            'history_budget_chars':history_budget_chars,'selected_history_chars':len(json.dumps(history,ensure_ascii=False)),
            'history':history,'omitted_message_count':len(messages)-len(history),'omitted_source_ranges':omitted,
            'limitations':['Selected text is historical context, not evidence of current tool or task state.',
                           'Provider sessions, reasoning, attachment files, live tools and permissions are not transferred.',
                           'Source references require access to the original UA conversation.',
                           'The history limit counts JSON characters, not tokens; metadata and the next request are outside that limit.']}
