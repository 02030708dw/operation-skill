"""VN Google session storage. Credentials remain in the regional private volume."""
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.request
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'facebook-video-ingest/scripts'))
from hm_facebook_account import recover_profile, CaptureLease


def account_config(config):
    account=config.get('googleAccount')
    if not account:return None
    if not isinstance(account,dict) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}',str(account.get('key',''))):raise ValueError('Invalid Google account')
    return account


def account_root(account):
    root=Path(os.getenv('HM_GOOGLE_ACCOUNT_ROOT','/opt/data/google-accounts'))/account['key']
    root.mkdir(parents=True,exist_ok=True,mode=0o700);root.chmod(0o700);return root


def acquire(account,shared=False):
    path=account_root(account)/'account.lock';handle=path.open('a+');path.chmod(0o600)
    try:fcntl.flock(handle,fcntl.LOCK_SH|fcntl.LOCK_NB if shared else fcntl.LOCK_EX|fcntl.LOCK_NB);return handle
    except BlockingIOError:handle.close();return None


def acquire_capture(account):
    gate=acquire(account,shared=True)
    if gate is None:return None
    root=account_root(account);lane=(root/'capture.lock').open('a+')
    try:fcntl.flock(lane,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:lane.close();gate.close();return None
    return CaptureLease(gate,lane,root/'profile')


def read_state(account):
    file=account_root(account)/'state.json'
    return json.loads(file.read_text()) if file.exists() else dict(state='LOGIN_REQUIRED',loginStatus='LOGIN_REQUIRED',downloadStatus='NOT_TESTED',version=0)


def report(config,tenant,state):
    request=urllib.request.Request(config['backendUrl'].rstrip('/')+'/api/internal/server/google-account/status',data=json.dumps(dict(state,tenant=tenant,accountKey=account_config(config)['key'])).encode(),method='POST',headers={'Content-Type':'application/json','X-HM-Worker-Token':config['workerToken']})
    with urllib.request.urlopen(request,timeout=15) as response:
        if json.load(response).get('code')!=200:raise RuntimeError('Google state acknowledgement failed')


def save_result(config,tenant,result):
    account=account_config(config);root=account_root(account)
    with (root/'state-write.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        old=read_state(account)
        state=result['state'];login=result.get('loginStatus', 'LOGIN_REQUIRED' if state=='LOGIN_REQUIRED' else 'VERIFICATION_REQUIRED' if state=='VERIFICATION_REQUIRED' else old.get('loginStatus','UNKNOWN'))
        current=dict(state=state,loginStatus=login,downloadStatus=result.get('downloadStatus','FAILED' if state in ('LOGIN_REQUIRED','VERIFICATION_REQUIRED') else old.get('downloadStatus','NOT_TESTED')),maskedAccount=result.get('maskedAccount',old.get('maskedAccount')),reasonCode=result.get('reasonCode'),version=max(old.get('version',0)+1,time.time_ns()//1000000),checkedAt=int(time.time()),nextCheckAt=int(time.time())+1800 if state=='COOLDOWN' else None)
        tmp=root/'state.next';tmp.write_text(json.dumps(current));tmp.chmod(0o600);tmp.replace(root/'state.json')
    report(config,tenant,current);return current


def prepare_capture(config,tenant,lease):
    value=read_state(account_config(config))
    if value.get('version'):report(config,tenant,value)
    allowed=value['state'] in ('LOGGED_IN','AVAILABLE') or (value['state']=='COOLDOWN' and (value.get('nextCheckAt') or 0)<=time.time())
    if allowed and (lease.profile/'youtube-cookies.txt').is_file():return dict(value,state='AVAILABLE')
    return value if not allowed else save_result(config,tenant,dict(state='LOGIN_REQUIRED',reasonCode='GOOGLE_LOGIN_REQUIRED'))


def download_result(config,tenant,code=None):
    state={'GOOGLE_LOGIN_REQUIRED':'LOGIN_REQUIRED','GOOGLE_VERIFICATION_REQUIRED':'VERIFICATION_REQUIRED','GOOGLE_RATE_LIMITED':'COOLDOWN'}.get(code)
    if code and state is None:return  # Item failures do not invalidate the login.
    save_result(config,tenant,dict(state=state or 'AVAILABLE',reasonCode=code,downloadStatus='FAILED' if code else 'PASSED',**({} if code else {'loginStatus':'LOGGED_IN'})))
