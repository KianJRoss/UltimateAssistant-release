"""Open provider-owned sign-in pages from the existing native login session."""
import re
import threading
import webbrowser
from urllib.parse import urlsplit

_opened={}
_lock=threading.Lock()
_HOSTS={'accounts.google.com','antigravity.google','aistudio.google.com','auth.openai.com','chatgpt.com','platform.openai.com','claude.ai','console.anthropic.com','platform.claude.com'}


def login_urls(output):
    # PTY screens can wrap long OAuth URLs at the terminal edge.
    lines=[line.strip().strip('\u2502\u2503').strip() for line in output.splitlines()]
    for index,line in enumerate(lines):
        match=re.search(r'https://[^\s<>"\x1b\u2502\u2503]+',line)
        if not match:continue
        candidate=match.group(0)
        previous=line
        cursor=index+1
        while match.end()==len(line) and len(previous)>=70 and cursor<len(lines):
            following=lines[cursor]
            if not re.fullmatch(r"[A-Za-z0-9._~:/?#@!$&()*+,;=%+-]+",following):break
            candidate+=following;previous=following;cursor+=1
        yield candidate.rstrip(".,;)' ")


def prepare_login_link(result,account,operation):
    result=dict(result)
    with _lock:
        if operation in ('start','cancel'):_opened.pop(account,None)
        if not result.get('running'):return result
        for candidate in login_urls(result.get('output','')):
            parsed=urlsplit(candidate)
            if parsed.hostname not in _HOSTS or parsed.username or parsed.password:continue
            result['login_url']=candidate
            if _opened.get(account)!=candidate:
                try:
                    result['browser_opened']=bool(webbrowser.open(candidate,new=2))
                    if result['browser_opened']:_opened[account]=candidate
                except Exception:result['browser_opened']=False
            else:result['browser_opened']=True
            break
    return result
