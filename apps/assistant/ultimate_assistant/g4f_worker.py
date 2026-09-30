"""One native g4f process for one explicitly configured account."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--account',required=True)
    parser.add_argument('--port',type=int,required=True)
    args=parser.parse_args()
    accounts_file=Path(os.environ['HERALD_G4F_ACCOUNTS_FILE'])
    account=next(a for a in json.loads(accounts_file.read_text('utf-8'))['accounts'] if a['name']==args.account and a.get('enabled',True))
    root=(accounts_file.parent/account['dir']).resolve()
    if not root.is_relative_to(accounts_file.parent.resolve()): raise ValueError('Invalid account directory')
    root.mkdir(parents=True,exist_ok=True)
    os.chdir(root)
    (root/'har_and_cookies').mkdir(exist_ok=True)
    from g4f import Provider
    from g4f.cookies import set_cookies_dir,read_cookie_files,BROWSERS
    from g4f.config import AppConfig
    from g4f.api import run_api
    BROWSERS.clear()  # Use only deliberately imported account files, never other browser profiles.
    cookies=root/'har_and_cookies'; cookies.mkdir(exist_ok=True)
    set_cookies_dir(str(cookies));read_cookie_files(str(cookies))
    AppConfig.provider=Provider.__map__[account['provider']]
    AppConfig.gui=True
    AppConfig.debug=False
    run_api(host='127.0.0.1',port=args.port,debug=False)


if __name__=='__main__': main()
