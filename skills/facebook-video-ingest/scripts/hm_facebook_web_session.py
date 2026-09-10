#!/usr/bin/env python3
"""A bounded administrator session; private stdin and safe JSON-lines stdout."""
import fcntl
import json
import os
import queue
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import hm_facebook_account as accounts


def emit(**value):
    print(json.dumps(value), flush=True)


def maintenance_locks():
    handles = []
    try:
        root = Path(os.getenv('HM_SERVER_STATE_DIR', '/opt/data/server')) / 'locks'
        for kind, count in [('CAPTURE', int(os.getenv('HM_CAPTURE_SLOTS', '8'))), ('UPLOAD', 1), ('DELETE', 1), ('GENERATION', 1)]:
            for slot in range(1, count + 1):
                root.mkdir(parents=True, exist_ok=True)
                handle = (root / f'{kind}-{slot}.lock').open('a+')
                handles.append(handle)
                try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: raise RuntimeError('Busy')
        for config in json.loads(Path(os.environ['HM_TENANT_CONFIG']).read_text()).values():
            account = accounts.account_config(config)
            if account:
                handle = accounts.acquire(account)
                if handle is None: raise RuntimeError('Busy')
                handles.append(handle)
        return handles
    except Exception:
        for handle in handles: handle.close()
        return None


def busy():
    handles = maintenance_locks()
    if handles is None: return True
    for handle in handles: handle.close()
    return False


def promote(root, staging):
    """Switch only after a verified login; preserve the former session privately."""
    active = root / 'profile'
    if active.exists(): active.replace(root / ('profile.previous.' + uuid.uuid4().hex))
    staging.replace(active)
    marker = active / '.hermes-login-enabled'
    marker.write_text('Server-authorized session\n'); marker.chmod(0o600)


def save_result(config, tenant, result):
    account = accounts.account_config(config); root = accounts.account_root(account)
    old = accounts.read_state(account)
    current = dict(state=result['state'], reasonCode=result.get('reasonCode'),
                   version=max(int(old.get('version', 0)) + 1, time.time_ns() // 1000000),
                   checkedAt=int(time.time()), nextCheckAt=int(time.time()) + 300 if result['state'] == 'COOLDOWN' else None)
    temp = root / 'state.tmp'; temp.write_text(json.dumps(current)); temp.chmod(0o600); temp.replace(root / 'state.json')
    for attempt in range(3):
        try: accounts.report(config, tenant, current); return
        except Exception:
            if attempt == 2: raise
            time.sleep(1 if attempt == 0 else 5)


def run(tenant):
    os.umask(0o077)
    config = json.loads(Path(os.environ['HM_TENANT_CONFIG']).read_text())[tenant]
    account = accounts.account_config(config)
    if not account: emit(phase='FAILED', reasonCode='ACCOUNT_NOT_CONFIGURED'); return
    lock = accounts.acquire(account)
    if lock is None: emit(phase='FAILED', reasonCode='ACCOUNT_BUSY'); return
    root = accounts.account_root(account); staging = None; child = None
    def stop_child():
        nonlocal child
        if child and child.poll() is None:
            try: child.stdin.write('{"action":"close"}\n'); child.stdin.flush(); child.wait(timeout=12)
            except (OSError, subprocess.TimeoutExpired): child.kill(); child.wait()
        accounts.recover_profile(staging or root / 'profile')
        child = None
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(InterruptedError()))
    try:
        initial = json.loads(sys.stdin.readline())
        if initial.get('action') not in ('login', 'verify', 'browser'): raise ValueError('Invalid initial action')
        human = initial['action'] == 'browser'
        if initial['action'] in ('login', 'browser'):
            staging = root / ('login.' + uuid.uuid4().hex); staging.mkdir(mode=0o700)
        profile = staging or root / 'profile'; profile.mkdir(exist_ok=True, mode=0o700)
        accounts.recover_profile(profile)
        script = Path(__file__).resolve().parents[2] / 'facebook-followed-video-download/scripts/facebook_admin_session.js'
        child = subprocess.Popen(['node', str(script), str(profile)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True)
        child.stdin.write(json.dumps(initial) + '\n'); child.stdin.flush(); initial = None
        events = queue.Queue(maxsize=16)
        def read_lines(source, stream):
            try:
                for line in stream: events.put((source, line))
            finally: events.put((source, None))
        for source, stream in [('input', sys.stdin), ('browser', child.stdout)]:
            threading.Thread(target=read_lines, args=(source, stream), daemon=True).start()
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            try: source, line = events.get(timeout=1)
            except queue.Empty: continue
            if line is None:
                if source == 'input': return
                raise RuntimeError('Browser exited')
            if source == 'input':
                command = json.loads(line)
                if command.get('action') == 'close': return
                if command.get('action') not in ('code', 'check', 'finish', 'view'): raise ValueError('Invalid follow-up')
                child.stdin.write(json.dumps(command) + '\n'); child.stdin.flush(); command = None
            elif line.startswith('HM_BROWSER_RESULT '):
                emit(browser=json.loads(line.removeprefix('HM_BROWSER_RESULT ')))
            elif line.startswith('HM_ACCOUNT_RESULT '):
                result = json.loads(line.removeprefix('HM_ACCOUNT_RESULT '))
                if result['state'] == 'BROWSER_READY':
                    emit(phase='WAITING_BROWSER', reasonCode=None); continue
                if result['state'] == 'AVAILABLE':
                    stop_child()
                    if staging: promote(root, staging); staging = None
                    save_result(config, tenant, result)
                    emit(phase='SUCCEEDED', state='AVAILABLE', reasonCode=None); return
                if human:
                    emit(phase='WAITING_BROWSER', reasonCode=result.get('reasonCode')); continue
                if result['state'] == 'VERIFICATION_REQUIRED' and result.get('reasonCode') != 'FACEBOOK_ACCOUNT_SUSPENDED':
                    emit(phase='WAITING_CODE' if result.get('requiresCode') else 'WAITING_VERIFICATION',
                         state=result['state'], reasonCode=result.get('reasonCode')); continue
                stop_child()
                # Failed replacement must never destroy a previously usable account.
                if staging is None: save_result(config, tenant, result)
                emit(phase='FAILED', state=result['state'], reasonCode=result.get('reasonCode')); return
        emit(phase='EXPIRED', reasonCode='LOGIN_SESSION_EXPIRED')
    except Exception:
        emit(phase='FAILED', reasonCode='SESSION_CHECK_FAILED')
    finally:
        stop_child()
        if staging: shutil.rmtree(staging, ignore_errors=True)
        lock.close()


if __name__ == '__main__':
    if sys.argv[1:] == ['busy']: emit(busy=busy())
    elif sys.argv[1:] == ['restart-guard']:
        handles = maintenance_locks()
        emit(busy=handles is None)
        if handles is not None:
            try:
                if select.select([sys.stdin], [], [], 90)[0]: sys.stdin.readline()
            finally:
                for handle in handles: handle.close()
    elif len(sys.argv) == 2 and sys.argv[1] in ('ph', 'th', 'vn', 'id'): run(sys.argv[1])
    else: raise SystemExit(2)
