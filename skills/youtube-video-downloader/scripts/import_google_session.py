#!/usr/bin/env python3
"""Import an explicitly authorized private cookie file into a selected region.

Keep credentials in private stdin, verify the persisted browser session, and let
the existing regional maintenance lease protect downloads and login replacement.
"""
import argparse
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cookies', type=Path, required=True)
    parser.add_argument('--tenant', choices=['ph','th','vn','id'], default='vn')
    args = parser.parse_args()
    os.umask(0o077)
    source = args.cookies.resolve()
    if not source.is_file() or source.stat().st_mode & 0o077 or source.stat().st_size > 1024 * 1024:
        parser.error('Cookie input must be a private file (0600), at most 1 MiB')
    script = Path(__file__).resolve().parents[2] / 'facebook-video-ingest/scripts/hm_facebook_web_session.py'
    child = subprocess.Popen([sys.executable, str(script), args.tenant, 'google'],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
    try:
        child.stdin.write(json.dumps({'action': 'cookies', 'cookies': source.read_text()}) + '\n')
        child.stdin.flush()
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            if not select.select([child.stdout], [], [], 1)[0]:
                continue
            line = child.stdout.readline()
            if not line:
                break
            result = json.loads(line)
            safe = {k: result[k] for k in ('phase', 'state', 'reasonCode') if k in result}
            print(json.dumps(safe), flush=True)
            if result.get('phase') == 'SUCCEEDED':
                return 0
            if result.get('phase') in ('FAILED', 'WAITING_BROWSER', 'WAITING_VERIFICATION', 'WAITING_CODE', 'EXPIRED'):
                return 1
        print(json.dumps({'phase': 'FAILED', 'reasonCode': 'SESSION_CHECK_FAILED'}))
        return 1
    finally:
        if child.poll() is None:
            try:
                child.stdin.write('{"action":"close"}\n'); child.stdin.flush()
                child.wait(timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait()
        child.stdin.close(); child.stdout.close()


if __name__ == '__main__':
    raise SystemExit(main())
