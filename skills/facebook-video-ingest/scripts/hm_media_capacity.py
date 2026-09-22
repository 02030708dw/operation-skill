"""Shared, fail-closed compatibility capacity; temporary boosts retire on backlog drain."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time


def config_path():
    return Path(os.environ.get('HM_MEDIA_CAPACITY_FILE', '/opt/data/media-compat-capacity.json'))


def configuration():
    try:
        value = json.loads(config_path().read_text())
        if not isinstance(value, dict): return {'limit': 1}
        if value.get('limit') not in (1, 2) or isinstance(value.get('limit'), bool): return {'limit': 1}
        return value
    except (OSError, ValueError, TypeError):
        return {'limit': 1}


def limit():
    return configuration()['limit']


def restore_if_drained(observed, pending, expected_regions):
    if (observed.get('limit') != 2 or not observed.get('untilBacklogCleared')
            or not observed.get('runId') or set(pending) != set(expected_regions) or not pending
            or any(v.get('backlog') != 0 for v in pending.values())):
        return False
    path = config_path()
    with Path(str(path)+'.control.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if configuration() != observed: return False
        value = dict(observed, limit=1, restoredAt=int(time.time()), restoreReason='APPROVED_BACKLOG_DRAINED')
        fd, name = tempfile.mkstemp(prefix=path.name+'.', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(value, output); output.flush(); os.fsync(output.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
    return True


def try_lock(path):
    handle = path.open('a')
    try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close(); return None
    return handle


@contextlib.contextmanager
def slot(path, *, blocking=False):
    """Slot one retains the legacy filename so old and new Workers cannot overlap it."""
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    secondary = Path(str(path)+'.2')
    held = None
    while held is None:
        with Path(str(path)+'.admission').open('a') as admission:
            fcntl.flock(admission, fcntl.LOCK_EX)
            slots = limit()
            if slots == 1:
                # On 2 -> 1, drain a running secondary before admitting new work.
                barrier = try_lock(secondary)
                if barrier:
                    try: held = try_lock(path)
                    finally: barrier.close()
            else:
                held = try_lock(path) or try_lock(secondary)
        if held is not None or not blocking: break
        time.sleep(.1)
    try: yield held
    finally:
        if held: held.close()


def encoder_slot():
    return slot(os.environ.get('HM_MEDIA_ENCODER_LOCK', '/tmp/hm-media-encoder.lock'), blocking=True)
