"""Private regional X profiles. Listing leases never expose cookies to downloads."""
import fcntl
import json
import os
from pathlib import Path
import re
import time
from hm_facebook_account import recover_profile, CaptureLease


def account_config(config):
    value=config.get('xAccount')
    if not value:return None
    if not isinstance(value,dict) or not re.fullmatch(r'x-(ph|th|vn|id)',str(value.get('key',''))):raise ValueError('Invalid regional X account')
    return value


def account_root(account):
    root=Path(os.getenv('HM_X_ACCOUNT_ROOT','/opt/data/x-accounts'))/account['key']
    root.mkdir(parents=True,exist_ok=True,mode=0o700);root.chmod(0o700);return root


def acquire(account,shared=False):
    path=account_root(account)/'account.lock';handle=path.open('a+');path.chmod(0o600)
    try:fcntl.flock(handle,(fcntl.LOCK_SH if shared else fcntl.LOCK_EX)|fcntl.LOCK_NB);return handle
    except BlockingIOError:handle.close();return None


def acquire_capture(account):
    gate=acquire(account,shared=True)
    if gate is None:return None
    root=account_root(account);lane=(root/'capture.lock').open('a+')
    try:fcntl.flock(lane,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:lane.close();gate.close();return None
    recover_profile(root/'profile')
    return CaptureLease(gate,lane,root/'profile')


def read_state(account):
    file=account_root(account)/'state.json'
    return json.loads(file.read_text()) if file.exists() else dict(state='LOGIN_REQUIRED',version=0)


def save_result(config,tenant,result):
    account=account_config(config);root=account_root(account)
    if result['state'] not in ('LOGGED_IN','LOGIN_REQUIRED','VERIFICATION_REQUIRED','COOLDOWN'):raise ValueError('Invalid X state')
    with (root/'state-write.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        current=dict(state=result['state'],reasonCode=result.get('reasonCode'),checkedAt=int(time.time()),version=time.time_ns()//1000000,nextCheckAt=int(time.time())+1800 if result['state']=='COOLDOWN' else None)
        temp=root/'state.next';temp.write_text(json.dumps(current));temp.chmod(0o600);temp.replace(root/'state.json')
    return current
