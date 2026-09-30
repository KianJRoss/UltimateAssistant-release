"""Account setup through the bundled Router's existing registries and login API."""
from __future__ import annotations
import hashlib
import json
import os
import uuid
from urllib.parse import quote, urlsplit
import httpx
from .settings import settings

NATIVE_SETUP_RELOAD_MARKER = '[[ASSISTANT_SETUP_RELOAD]]'


def router_request(method: str, path: str, payload=None):
    key = os.environ.get('HERALD_API_KEY')
    headers = {'Authorization': f'Bearer {key}'} if key else {}
    response = httpx.request(method, settings.herald_url.rstrip('/') + path,
                             json=payload, headers=headers, timeout=120)
    result = response.json()
    if not response.is_success:
        # Credential-bearing upstream error bodies must not enter app/chat logs.
        raise ValueError('Router request failed (HTTP ' + str(response.status_code) + '). Check account setup and retry.')
    return result


def _verification_file():
    return settings.user_settings_file.parent / 'provider-verification.json'


def _signature(account):
    return hashlib.sha256(json.dumps({k:account.get(k) for k in ('name','provider','config','secret_ref','enabled')},sort_keys=True).encode()).hexdigest()


def inventory() -> dict:
    providers = router_request('GET', '/auth/providers').get('providers', [])
    accounts = router_request('GET', '/accounts').get('accounts', [])
    backends = {b['name']:b for b in router_request('GET', '/backends').get('backends', []) if b.get('enabled',True)}
    statuses = {s['cli']:s['status'] for s in router_request('GET','/auth/status').get('clis',[])}
    capabilities = router_request('GET','/auth/capabilities').get('clis',{})
    path = _verification_file()
    verified = json.loads(path.read_text('utf-8')) if path.exists() else {}
    entries=[]
    for account in accounts:
        if not account.get('enabled',True): continue
        lanes = router_request('GET', '/accounts/'+quote(account['name'],safe='')+'/lanes').get('lanes',[])
        active_lanes = [l for l in lanes if l.get('enabled',True) and l.get('backend_name') in backends]
        if not active_lanes: continue
        kind=account['auth_kind']
        status=statuses.get(account['name'],'unverified') if kind=='cli_profile' else ('verified' if verified.get(account['name'])==_signature(account) else 'unverified')
        for lane in active_lanes:
            entries.append({'name':account['name'],'model':lane['backend_name'],'provider':account['provider'],
                            'kind':kind,'adapter':account.get('config',{}).get('cli_name',account['provider']),
                            'login':capabilities.get(account['name'],{}),'status':status})
    return {'providers':providers,'accounts':entries}


def connect(provider: str, name: str = '') -> str:
    state=inventory()
    choice=next((p for p in state['providers'] if p['id']==provider),None)
    if not choice: raise ValueError('Choose a login provider discovered by this Router.')
    if not choice['installed']: raise ValueError('Install the selected provider first.')
    existing=next((a for a in state['accounts'] if a['kind']=='cli_profile' and a['adapter']==provider and (not name or a['name']==name)),None)
    if existing: return existing['model']
    name=name.strip() or 'assistant-'+provider
    if any(a['name']==name for a in router_request('GET','/accounts')['accounts']):
        raise ValueError('That account name already exists; use Account Console to edit it.')
    path=quote(name,safe='')
    config={'cli_name':provider}
    router_request('POST','/backends',{'name':name,'backend_type':'cli','config':config,'capabilities':{'code':True,'reasoning':True},'enabled':True})
    router_request('POST','/accounts',{'name':name,'provider':choice['adapter'],'auth_kind':'cli_profile','config':config})
    router_request('POST','/accounts/'+path+'/lanes',{'name':'default','backend_name':name,'capabilities':{'code':True,'reasoning':True}})
    router_request('POST','/accounts/'+path+'/activate',{})
    return name


def save_api_key(name, provider, model, key, base_url=''):
    if not key.strip(): raise ValueError('Enter an API key in this secure form.')
    if base_url:
        parsed=urlsplit(base_url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or (parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in ('127.0.0.1','localhost','::1'))):
            raise ValueError('Use an HTTPS API base URL or a loopback HTTP endpoint without credentials or query parameters.')
    previous=next((a for a in router_request('GET','/accounts')['accounts'] if a['name']==name),None)
    if previous and previous['auth_kind']!='api_key':
        raise ValueError('That name belongs to another account type. Choose a different name.')
    from herald.router.secret_vault import SecretVault
    vault=SecretVault()
    artifact='assistant-api/'+uuid.uuid4().hex
    ref=vault.put(artifact,key.encode(),metadata={'kind':'api-key','provider':provider})
    config={'model_name':model,'provider':provider}
    if base_url: config['base_url']=base_url
    try:
        router_request('POST','/accounts',{'name':name,'provider':provider,'auth_kind':'api_key','config':config,'secret_ref':ref})
        router_request('POST','/accounts/'+quote(name,safe='')+'/activate',{})
    except Exception:
        # Retain the encrypted artifact if an account was saved before activation
        # failed; the user can retry activation without re-entering the key.
        raise ValueError('Account setup is blocked. The key is encrypted; retry activation from Account Console.') from None
    return {'status':'waiting','account':name,'detail':'API key saved in the local encrypted vault. Verify the account to test a small provider request.'}


def verify_account(name):
    account=next((a for a in router_request('GET','/accounts')['accounts'] if a['name']==name and a.get('enabled',True)),None)
    if not account: raise ValueError('Choose a connected account.')
    result=router_request('POST','/accounts/'+quote(name,safe='')+'/test',{})
    if not result.get('ok'): raise ValueError('Provider connection test failed.')
    path=_verification_file(); path.parent.mkdir(parents=True,exist_ok=True)
    records=json.loads(path.read_text('utf-8')) if path.exists() else {}
    records[name]=_signature(account)
    temporary=path.with_suffix('.pending.json'); temporary.write_text(json.dumps(records),'utf-8'); temporary.replace(path)
    return {'status':'completed','account':name,'detail':'A provider request returned successfully.'}


def setup_context() -> str:
    return ('Onboarding uses the bundled local Herald Router and provider CLI on this device. '
            'During setup, present yourself as the user\'s personal assistant, not as the underlying provider or a coding assistant. '
            'Lead setup from the automatic opening request without requiring the user to write setup instructions. '
            'The core setup is browser automation, vision, and Windows control MCPs, alongside the full native CLI tools. '
            'Inspect, configure, and verify these capabilities; guide the user through any necessary browser connection '
            'or desktop permission step, asking one simple question at a time. Let the native CLI choose the supported '
            'implementation from actual device discovery. Connections to external apps and accounts, including Google '
            'Workspace, Zotero, Obsidian, and school portals, are optional and must be set up only when the user asks. '
            'Do not make optional integrations a requirement for completing core setup, and do not initiate their '
            'account discovery, installations, or login flows merely because they appear in the catalog. '
            'Limit device discovery to relevant executables, this assistant\'s workspace and per-user settings, '
            'and documented provider configuration locations. Do not recursively scan unrelated development '
            'projects or search their private data for reusable setup. Use official supported tooling rather '
            'than reverse-engineering another application. Preserve unrelated files and configuration. '
            'Keep replies brief and understandable to a nontechnical user, and ask at most one simple question at a time. '
            'Use their answers to choose and configure the necessary tools; do not lead with an integration menu. '
            'Handle technical prerequisites yourself using supported provider CLIs or authenticated browser tools. '
            'Do not ask the user to create developer projects, install packages, edit settings, or supply OAuth client files '
            'before inspecting and attempting the supported setup path. Ask the user only for necessary preferences, '
            'sign-in, consent, MFA, or an account permission you cannot obtain yourself. If setup cannot proceed, '
            'describe the observed blocker in plain language instead of offering a technical checklist. '
            'Keep technical inventory, provider names, local paths, tool names and configuration details out of ordinary setup replies. '
            'Explain a technical detail only when it is necessary for the user\'s next action. '
            'Persist setup progress in this conversation: record completed steps with their observed verification, '
            'waiting steps with the precise OAuth/MFA action, and blocked steps with the actual error. '
            'Report only the individual capabilities actually verified by successful tool results. '
            'A successful task-list check does not verify memory, study, math, browser or any other tool. '
            'Available or discovered tools are not tested tools. Never broaden a verification claim to an entire tool group. '
            'Keep progress concise in normal language rather than showing a technical status report on every turn. '
            'Do not start ordinary replies with Setup Status or Setup Progress sections. '
            'On continuation read prior observations before doing discovery again. Installation/configuration alone '
            'is not verification: discover tools and perform a harmless read-only call. Optional connectors may '
            'be installed from official instructions using native CLI tools. Never assume accounts or portals exist. '
            'API keys must be entered only in the secure account form, never in chat. The Account Console exposes '
            'Herald account/login controls. G4F session capture and file import are credential operations handled '
            'outside the model: guide the user to those controls and never read HAR, cookie, or token contents.')
