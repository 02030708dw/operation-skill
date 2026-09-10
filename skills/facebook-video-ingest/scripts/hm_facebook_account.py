#!/usr/bin/env python3
"""Administrator-only account maintenance; no secrets are accepted in argv."""
from __future__ import annotations
import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import time
import urllib.request


def account_config(config: dict) -> dict | None:
    account = config.get('facebookAccount')
    if not account:
        return None
    if not isinstance(account, dict) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', str(account.get('key', ''))):
        raise ValueError('Invalid configured Facebook account')
    return account


def account_root(account: dict) -> Path:
    root = Path(os.getenv('HM_FACEBOOK_ACCOUNT_ROOT', '/opt/data/facebook-accounts')) / account['key']
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root


def acquire(account: dict, shared: bool = False):
    root = account_root(account)
    handle = (root / 'account.lock').open('a+')
    os.chmod(root / 'account.lock', 0o600)
    try:
        fcntl.flock(handle, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        return handle
    except BlockingIOError:
        handle.close()
        return None


class CaptureLease:
    def __init__(self, gate, lane, profile):
        self.gate, self.lane, self.profile = gate, lane, profile

    def filenos(self):
        return (self.gate.fileno(), self.lane.fileno())

    def close(self):
        self.lane.close()
        self.gate.close()


def acquire_capture(account: dict):
    """Capture readers exclude login maintenance; each reader owns a browser lane."""
    count = account.get('concurrency', 1)
    if type(count) is not int or count not in (1, 2):
        raise ValueError('Invalid account concurrency')
    gate = acquire(account, shared=True)
    if gate is None:
        return None
    root = account_root(account)
    for index in range(count):
        path = root / f'capture-{index}.lock'
        lane = path.open('a+'); path.chmod(0o600)
        try:
            fcntl.flock(lane, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lane.close()
            continue
        return CaptureLease(gate, lane, root / f'capture-profile-{index}')
    gate.close()
    return None


def prepare_capture(config: dict, tenant: str, lease: CaptureLease) -> dict:
    """Copy the stable login session; never open its directory from two processes."""
    account = account_config(config)
    root = account_root(account)
    # Serialize state checks/reports so a later successful check cannot erase a
    # concurrent login failure. Waiting here holds no global capture slot.
    path = root / 'verify.lock'
    with path.open('a+') as guard:
        path.chmod(0o600)
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Requeue instead of waiting beyond the dispatch lease while another
            # lane performs its bounded network verification.
            return {'state': 'BUSY'}
        previous = read_state(account)
        if previous['state'] != 'AVAILABLE' and not (
                previous['state'] == 'COOLDOWN' and (previous.get('nextCheckAt') or 0) <= time.time()):
            report(config, tenant, previous) if previous.get('version', 0) else None
            return previous
        recover_profile(lease.profile)
        if lease.profile.exists():
            shutil.rmtree(lease.profile)
        source = root / 'profile'
        if source.exists():
            # Cache and Chromium process locks are not part of the login session.
            shutil.copytree(source, lease.profile, ignore=shutil.ignore_patterns(
                'Singleton*', 'DevToolsActivePort', 'Cache', 'Code Cache', 'GPUCache',
                'ShaderCache', 'GrShaderCache', 'Crashpad'))
        else:
            lease.profile.mkdir(mode=0o700)
        lease.profile.chmod(0o700)
        return verify(config, tenant, profile=lease.profile)


def read_state(account: dict) -> dict:
    path=account_root(account)/'state.json'
    return json.loads(path.read_text()) if path.exists() else {'state':'LOGIN_REQUIRED','version':0}


def report(config: dict, tenant: str, state: dict) -> None:
    account=account_config(config)
    request=urllib.request.Request(config['backendUrl'].rstrip('/')+'/api/internal/server/facebook-account/status',
        data=json.dumps(dict(state, accountKey=account['key'],tenant=tenant)).encode(), method='POST',
        headers={'Content-Type':'application/json','X-HM-Worker-Token':config['workerToken']})
    with urllib.request.urlopen(request,timeout=15) as response:
        result=json.load(response)
        if result.get('code')!=200: raise RuntimeError('Account status acknowledgement failed')


def recover_profile(profile: Path, proc_root: Path | None = None) -> None:
    """With account.lock held, reap only abandoned browsers for this exact account."""
    proc=proc_root if proc_root is not None else Path('/proc')
    if not proc.is_dir():
        return
    expected=('--user-data-dir='+str(profile.resolve())).encode()
    def matches(pid):
        try:
            entry=proc/str(pid)
            args=(entry/'cmdline').read_bytes().split(b'\0')
            return entry.stat().st_uid==os.getuid() and expected in args and not any(a.startswith(b'--type=') for a in args)
        except (OSError,PermissionError):
            return False
    for entry in proc.iterdir():
        if not entry.name.isdigit() or not matches(entry.name): continue
        pid=int(entry.name)
        try:
            grouped=os.getpgid(pid)==pid
            (os.killpg if grouped else os.kill)(pid,signal.SIGTERM)
            deadline=time.monotonic()+5
            while matches(pid) and time.monotonic()<deadline: time.sleep(0.1)
            if matches(pid): (os.killpg if grouped else os.kill)(pid,signal.SIGKILL)
        except ProcessLookupError:
            pass
    # Container hostnames change across recreation. Chrome refuses stale host locks.
    for name in ('SingletonLock','SingletonSocket','SingletonCookie','DevToolsActivePort'):
        path=profile/name
        if path.is_symlink() or path.is_file(): path.unlink(missing_ok=True)


def verify(config: dict, tenant: str, credentials: dict | None = None, profile: Path | None = None) -> dict:
    """Caller must hold account.lock throughout verification and capture."""
    os.umask(0o077)
    account=account_config(config)
    root=account_root(account)
    profile=profile if profile is not None else root/'profile'; profile.mkdir(exist_ok=True,mode=0o700)
    recover_profile(profile)
    script=Path(__file__).resolve().parents[2]/'facebook-followed-video-download/scripts/facebook_session.js'
    payload={'profile':str(profile),'action':'login' if credentials else 'verify'}
    payload.update(credentials or {})
    try:
        result=subprocess.run(['node',str(script)],input=json.dumps(payload),text=True,capture_output=True,timeout=180 if credentials else 90)
        outcome=json.loads(result.stdout.strip().splitlines()[-1])
        state=outcome['state']
        reason=outcome.get('reasonCode') or outcome.get('reason')
        if credentials and outcome.get('errorText'): print(json.dumps({'loginDiagnostic':outcome['errorText']}),flush=True)
        if state not in {'AVAILABLE','LOGIN_REQUIRED','VERIFICATION_REQUIRED','COOLDOWN'}: raise ValueError('state')
    except (ValueError,IndexError,OSError,subprocess.TimeoutExpired):
        state='COOLDOWN'
        reason='SESSION_CHECK_FAILED'
        recover_profile(profile)
    marker=profile/'.hermes-login-enabled'
    if state=='AVAILABLE': marker.write_text('Server-authorized session\n');marker.chmod(0o600)
    else: marker.unlink(missing_ok=True)
    old=read_state(account)
    current={'state':state,'reasonCode':reason,'version':max(int(old.get('version',0))+1,time.time_ns()//1000000),
             'checkedAt':int(time.time()),'nextCheckAt':int(time.time())+300 if state=='COOLDOWN' else None}
    tmp=root/'state.tmp';tmp.write_text(json.dumps(current));tmp.chmod(0o600);tmp.replace(root/'state.json')
    report(config,tenant,current)
    return current


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['login','status','revalidate','release-source'])
    parser.add_argument('--tenant',required=True,choices=['ph','th','vn','id'])
    parser.add_argument('--task-no')
    parser.add_argument('--stdin-json',action='store_true',help='Receive credentials privately over stdin')
    args=parser.parse_args()
    config=json.loads(Path(os.environ['HM_TENANT_CONFIG']).read_text())[args.tenant]
    account=account_config(config)
    if args.action=='release-source':
        if not re.fullmatch(r'C-[A-Za-z0-9-]+',args.task_no or ''): raise ValueError('Invalid task number')
        request=urllib.request.Request(config['backendUrl'].rstrip('/')+'/api/internal/server/capture-access/'+args.task_no+'/release',data=b'{}',method='POST',headers={'Content-Type':'application/json','X-HM-Worker-Token':config['workerToken']})
        with urllib.request.urlopen(request,timeout=15) as response: json.load(response)
        print('Source access gate released');return
    if account is None:
        print(json.dumps({'state':'NOT_CONFIGURED'}));return
    if args.action=='status':
        print(json.dumps(read_state(account)));return
    lock=acquire(account)
    if lock is None: raise RuntimeError('Account busy; wait for the current capture to finish')
    try:
        credentials=None
        if args.action=='login':
            credentials=json.load(sys.stdin) if args.stdin_json else {
                'username':input('Facebook account: '),'password':getpass.getpass('Password: '),
                'twoFactorSecret':getpass.getpass('Two-factor secret (optional): ')}
        print(json.dumps(verify(config,args.tenant,credentials)))
    finally: lock.close()

if __name__=='__main__':
    try: main()
    except Exception as error:
        # Never include credential-bearing subprocess output or request bodies.
        print('Account maintenance failed: '+type(error).__name__,file=sys.stderr)
        raise SystemExit(1)
