"""User-owned g4f account configuration and local service lifecycle."""
from __future__ import annotations
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import threading
import uuid
from pathlib import Path
from urllib.parse import quote
import httpx
from .settings import settings
from .onboarding import router_request

_catalog_cache=None
_processes=[]
_lock=threading.RLock()


def accounts_file():
    return Path(os.environ.get('HERALD_G4F_ACCOUNTS_FILE',str(settings.user_settings_file.parent/'router/g4f/accounts.json')))


def load_accounts():
    p=accounts_file()
    return json.loads(p.read_text('utf-8')).get('accounts',[]) if p.exists() else []


def catalog():
    # Provider import builds a model cache. Keep it outside the user's other
    # g4f installations and outside this application's program directory.
    global _catalog_cache
    with _lock:
        if _catalog_cache is not None:return _catalog_cache
        root=accounts_file().parent/'catalog'
        (root/'har_and_cookies').mkdir(parents=True,exist_ok=True)
        code="import json; from g4f import Provider; print(json.dumps({'providers':[{'id':p.__name__,'needs_auth':bool(getattr(p,'needs_auth',False)),'working':bool(getattr(p,'working',True))} for p in Provider.__providers__]}))"
        result=subprocess.run([sys.executable,'-c',code],cwd=root,capture_output=True,text=True,timeout=60,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:raise ValueError('Optional g4f provider discovery failed')
        _catalog_cache=json.loads(result.stdout.strip().splitlines()[-1])
        return _catalog_cache


def inventory():
    records=[]
    state_file=accounts_file().parent/'runtime.json'
    runtime=json.loads(state_file.read_text('utf-8')).get('accounts',[]) if state_file.exists() else []
    running=bool(_processes) and all(p.poll() is None for p in _processes)
    for a in load_accounts():
        root=accounts_file().parent/a['dir']/'har_and_cookies'
        saved=any(root.glob('*.har')) or any(root.glob('*.json'))
        worker=next((r for r in runtime if r['name']==a['name']),{})
        records.append({k:a.get(k) for k in ('name','provider','model','email_hint','enabled') } | {'session_saved':bool(saved),'status':'unverified' if saved else 'waiting',
                       'ui_url':worker.get('url','').removesuffix('/v1') if running else ''})
    return {'accounts':records, 'gateway_url':'http://127.0.0.1:'+str(port())+'/v1'}


def port(): return int(os.environ.get('ULTIMATE_ASSISTANT_G4F_PORT','18795'))


def _write(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.pending-'+uuid.uuid4().hex)
    temp.write_text(json.dumps(data),'utf-8');temp.replace(path)


def create_account(name,provider,model,email_hint=''):
    if provider not in {p['id'] for p in catalog()['providers']}: raise ValueError('Choose an installed g4f provider')
    if provider=='OpenaiChat' and not email_hint.strip(): raise ValueError('Enter the ChatGPT account email or masked email for protected identity verification')
    with _lock:
        accounts=load_accounts()
        if any(a['name']==name for a in accounts): raise ValueError('That g4f account name already exists')
        if any(a['name']=='g4f-'+name for a in router_request('GET','/accounts')['accounts']):
            raise ValueError('That Router account name already exists')
        directory=hashlib.sha256(name.encode()).hexdigest()[:24]
        accounts.append({'name':name,'provider':provider,'model':model,'email_hint':email_hint,'dir':directory,'enabled':True})
        _write(accounts_file(),{'accounts':accounts})
        restart()
        register_account(accounts[-1])
        backend='g4f-'+name
    return {'status':'waiting','account':name,'model':backend,'detail':'Local g4f worker started. Import or capture its session, then verify a provider request.'}


def register_account(account):
    backend='g4f-'+account['name']
    router_request('POST','/backends',{'name':backend,'backend_type':'browser_session','config':{'gateway_url':inventory()['gateway_url'],'model':account['name']},'capabilities':{'reasoning':True},'pool_name':'g4f-gateway'})
    router_request('POST','/accounts',{'name':backend,'provider':'g4f','auth_kind':'browser_session','config':{'g4f_account':account['name'],'model':account['model'],'session_revision':account.get('session_revision','')},'enabled':account.get('enabled',True)})
    router_request('POST','/accounts/'+quote(backend,safe='')+'/lanes',{'name':'default','backend_name':backend,'model':account['model'],'capabilities':{'reasoning':True}})


def import_session(name,filename,payload):
    account=next((a for a in load_accounts() if a['name']==name),None)
    if not account: raise ValueError('Choose a g4f account')
    if len(payload)>2*1024*1024: raise ValueError('Session export exceeds 2 MiB')
    data=json.loads(payload)
    suffix='.har' if filename.lower().endswith('.har') else '.json'
    if suffix=='.har':
        if not isinstance(data,dict) or not isinstance(data.get('log',{}).get('entries'),list): raise ValueError('Select a valid HAR export')
    elif not isinstance(data,list) or not all(isinstance(c,dict) and all(k in c for k in ('name','value','domain')) for c in data):
        raise ValueError('Select a valid cookie JSON export')
    from herald.router.secret_vault import SecretVault
    from herald.router.g4f_capture import _atomic_materialize
    root=accounts_file().parent/account['dir']/'har_and_cookies'
    SecretVault().put('g4f-import/'+uuid.uuid4().hex,payload,metadata={'kind':'session-export','account':name})
    _atomic_materialize(root/('session'+suffix),payload)
    with _lock:
        accounts=load_accounts()
        for item in accounts:
            if item['name']==name:item['session_revision']=uuid.uuid4().hex
        _write(accounts_file(),{'accounts':accounts})
    restart()
    register_account(next(a for a in accounts if a['name']==name))
    return {'status':'waiting','detail':'Session saved locally. Verify a provider request to confirm it works.'}


def stop():
    with _lock:
        for process in reversed(_processes):
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        _processes.clear()


def _spawn(command,env,cwd):
    p=subprocess.Popen(command,env=env,cwd=cwd,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    _processes.append(p);return p


def _wait(process,url):
    for _ in range(120):
        if process.poll() is not None: raise ValueError('Local g4f service exited during startup')
        try:
            r=httpx.get(url,timeout=1,trust_env=False)
            if r.is_success:return
        except httpx.HTTPError:pass
        time.sleep(.25)
    raise ValueError('Local g4f startup timed out')


def start():
    with _lock:
        if _processes and all(p.poll() is None for p in _processes):return
        stop()
        accounts=[a for a in load_accounts() if a.get('enabled',True)]
        if not accounts:return
        try:
            with socket.socket() as probe:probe.bind(('127.0.0.1',port()))
            env={**os.environ,'HERALD_G4F_ACCOUNTS_FILE':str(accounts_file())}
            state=[]
            for a in accounts:
                with socket.socket() as probe:probe.bind(('127.0.0.1',0));worker_port=probe.getsockname()[1]
                process=_spawn([sys.executable,'-m','ultimate_assistant.g4f_worker','--account',a['name'],'--port',str(worker_port)],env,str(Path(__file__).resolve().parents[1]))
                url='http://127.0.0.1:'+str(worker_port)+'/v1'
                _wait(process,url+'/models')
                state.append({**a,'url':url})
            state_file=accounts_file().parent/'runtime.json';_write(state_file,{'accounts':state})
            env['ULTIMATE_ASSISTANT_G4F_STATE_FILE']=str(state_file)
            process=_spawn([sys.executable,'-m','uvicorn','ultimate_assistant.g4f_gateway:app','--host','127.0.0.1','--port',str(port()),'--no-access-log'],env,str(Path(__file__).resolve().parents[1]))
            _wait(process,'http://127.0.0.1:'+str(port())+'/v1/models')
        except Exception:
            stop();raise


def restart():
    stop();start()
    # Restart reloads credential files, including protected captures. Require
    # a new provider request before reporting these sessions as verified.
    from .onboarding import _verification_file
    path=_verification_file()
    if path.exists():
        records=json.loads(path.read_text('utf-8'))
        for account in load_accounts():records.pop('g4f-'+account['name'],None)
        _write(path,records)

