"""Local gateway for explicitly configured, separate native g4f workers."""
from __future__ import annotations
import json
import os
import time
from pathlib import Path
import httpx
from fastapi import FastAPI,HTTPException,Request
app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)


def accounts():
    p=Path(os.environ['ULTIMATE_ASSISTANT_G4F_STATE_FILE'])
    return json.loads(p.read_text('utf-8')).get('accounts',[]) if p.exists() else []


@app.get('/v1/models')
def models():
    return {'object':'list','data':[{'id':a['name'],'object':'model','owned_by':'g4f'} for a in accounts() if a.get('enabled',True)]}


@app.post('/v1/chat/completions')
async def chat(request:Request):
    body=await request.json()
    account=next((a for a in accounts() if a['name']==body.get('model') and a.get('enabled',True)),None)
    if not account: raise HTTPException(404,'Choose an enabled g4f account')
    endpoint=account['url']
    from urllib.parse import urlsplit
    if urlsplit(endpoint).hostname!='127.0.0.1': raise HTTPException(500,'Invalid local worker endpoint')
    payload={**body,'model':account['model'],'provider':account['provider'],'stream':False,'conversation_id':None,'parent_message_id':None}
    try:
        async with httpx.AsyncClient(timeout=120,trust_env=False) as client:
            response=await client.post(endpoint+'/chat/completions',json=payload)
            response.raise_for_status()
            result=response.json()
        if not result.get('choices') or result.get('error'): raise ValueError('Provider returned no result')
        return result
    except Exception:
        # Never proxy credential-bearing provider errors or response bodies.
        raise HTTPException(502,'The g4f provider request failed. Check this account session and model, then retry.') from None
