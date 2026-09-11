#!/usr/bin/env python3
"""One low-priority metadata repair, with durable result replay and no media download."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.error
import hm_server_worker as runner

BASE = '/api/internal/capture/titles'

def api(path, body):
    return runner.api(BASE + path, dict(body, workerId=os.environ['HM_WORKER_ID']))

def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.next')
    with tmp.open('w') as f:
        os.chmod(tmp, 0o600);json.dump(value, f, ensure_ascii=False);f.flush();os.fsync(f.fileno())
    tmp.replace(path)

def deliver(path):
    payload = json.loads(path.read_text())
    try:
        api('/'+payload['jobNo']+'/complete', payload)
    except urllib.error.HTTPError as error:
        if error.code == 409:
            # Superseded execution cannot overwrite a later edit; retain diagnostic result.
            path.rename(path.with_suffix('.stale'));return
        raise
    path.rename(path.with_suffix('.ack'))

def run():
    root = runner.state_root() / 'title-results';root.mkdir(parents=True, exist_ok=True)
    for result in root.glob('*.pending'):deliver(result)
    job = api('/claim', {})
    if not job:return
    name = job['jobNo']+'-'+str(job['executionVersion'])
    input_path=root/(name+'.input');output_path=root/(name+'.output')
    atomic(input_path, job)
    engine=Path(__file__).resolve().parents[2]/'facebook-followed-video-download/scripts/facebook_followed_video_engine.js'
    args=['node',str(engine),'--metadata-only-json',str(input_path),'--title-output',str(output_path)]
    if os.getenv('HM_FACEBOOK_PROFILE'):args += ['--browser-profile',os.environ['HM_FACEBOOK_PROFILE']]
    stopped=threading.Event();lost=threading.Event()
    def heartbeat():
        last=time.monotonic()
        while not stopped.wait(20):
            try:
                if not api('/'+job['jobNo']+'/check',{'executionVersion':job['executionVersion']})['active']:lost.set();return
                last=time.monotonic()
            except Exception:
                if time.monotonic()-last>100:lost.set();return
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    try:
        # Child stdout is not metadata: it can contain navigation diagnostics.
        child=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        deadline=time.monotonic()+160
        while child.poll() is None:
            if lost.is_set() or time.monotonic()>deadline:
                child.terminate()
                try:child.wait(timeout=10)
                except subprocess.TimeoutExpired:child.kill();child.wait()
                break
            time.sleep(1)
        if lost.is_set():return
        result=json.loads(output_path.read_text()) if output_path.exists() else {'title':None,'titleSource':'NONE','titleStatus':'NETWORK_ERROR' if time.monotonic()>=deadline else 'EXTRACTION_FAILED'}
        result.update(jobNo=job['jobNo'],executionVersion=job['executionVersion'])
        receipt=root/(name+'.pending');atomic(receipt,result);deliver(receipt)
    finally:
        stopped.set();thread.join(timeout=1);input_path.unlink(missing_ok=True);output_path.unlink(missing_ok=True)

if __name__ == '__main__':run()
