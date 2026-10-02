"""One host-wide admission budget backed by a bounded filesystem, never per tenant.

Activation is explicit. A plain directory on the root filesystem is rejected.
Completed R2-backed jobs are disposable; unverified originals remain protected.
"""
from __future__ import annotations
import contextlib
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time

CAPACITY = 5 * 1024**3
MARGIN = 32 * 1024**2

class WorkspaceFailure(RuntimeError):
    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.retryable = retryable

def root():
    value = os.getenv('HM_EPHEMERAL_MEDIA_ROOT')
    if not value:
        return None
    path = Path(value).resolve(strict=True)
    config = json.loads((path/'.budget.json').read_text())
    filesystem = os.statvfs(path)
    if config.get('capacityBytes') != CAPACITY or filesystem.f_blocks * filesystem.f_frsize > CAPACITY:
        raise WorkspaceFailure('MEDIA_WORKSPACE_UNBOUNDED')
    return path

def bytes_used(path, browser_temporary=False):
    total, seen = 0, set()
    for item in path.rglob('*'):
        try: info = item.lstat()
        except FileNotFoundError: continue  # Chromium removes its temporary files concurrently.
        if stat.S_ISLNK(info.st_mode):
            if not browser_temporary or item.name not in {'SingletonLock','SingletonSocket','SingletonCookie'}:
                raise WorkspaceFailure('MEDIA_WORKSPACE_LINK')
            # Browser lock metadata is counted without following its target.
            total += info.st_blocks * 512
        elif stat.S_ISREG(info.st_mode):
            identity = (info.st_dev, info.st_ino)
            if identity not in seen:
                seen.add(identity); total += info.st_blocks * 512
    return total

def browser_directory(workspace, lease):
    name = lease.get('browserTemporary')
    if not name:return None
    if Path(name).name != name or not name.startswith('b-'):
        raise WorkspaceFailure('MEDIA_WORKSPACE_LINK')
    path = workspace/'t'/name
    if path.is_symlink():raise WorkspaceFailure('MEDIA_WORKSPACE_LINK')
    return path

def lease_bytes(workspace, name, lease):
    folder = workspace/'work'/name
    temporary = browser_directory(workspace, lease)
    return (bytes_used(folder) if folder.exists() else 0) + (bytes_used(temporary, True) if temporary and temporary.exists() else 0)

def process_identity(pid):
    try:
        # Linux starttime protects against PID reuse. Non-Linux is only used by tests/dev.
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
    except (OSError, IndexError):
        try: os.kill(pid, 0); return 'alive'
        except ProcessLookupError: return None
        except PermissionError: return 'alive'

def journal(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.budget-')
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output); output.flush(); os.fsync(output.fileno())
        os.chmod(name, 0o600); os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)

@contextlib.contextmanager
def lock(path):
    with (path/'.admission.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX); yield

def key_for(tenant, job):
    if tenant.lower() not in ('ph','th','vn','id'):
        raise WorkspaceFailure('MEDIA_WORKSPACE_TENANT')
    return tenant.lower() + '-' + hashlib.sha256(str(job).encode()).hexdigest()[:24]

@contextlib.contextmanager
def job(tenant, identifier, required):
    workspace = root()
    if workspace is None:
        yield None; return
    required = int(required)
    if required <= 0 or required + MARGIN > CAPACITY:
        raise WorkspaceFailure('MEDIA_EXCEEDS_WORKSPACE_LIMIT')
    key = key_for(tenant, identifier)
    directory = workspace/'work'/key
    (workspace/'work').mkdir(exist_ok=True); (workspace/'t').mkdir(exist_ok=True)
    lease_file = workspace/'.leases.json'
    identity = process_identity(os.getpid())
    with lock(workspace):
        leases = json.loads(lease_file.read_text()) if lease_file.exists() else {}
        existing = leases.get(key)
        if existing and existing.get('active',True) and process_identity(existing['pid']) == existing['identity']:
            raise WorkspaceFailure('MEDIA_WORKSPACE_BUSY', True)
        if existing:
            old_temporary = browser_directory(workspace, existing)
            if old_temporary:shutil.rmtree(old_temporary,ignore_errors=True)
        reserved = 0
        for name, lease in list(leases.items()):
            if name == key: continue
            folder = workspace/'work'/name
            active = lease.get('active',True) and process_identity(lease['pid']) == lease['identity']
            if not active:
                temporary = browser_directory(workspace, lease)
                if temporary:shutil.rmtree(temporary,ignore_errors=True)
                lease.pop('browserTemporary',None)
            if not active and (lease.get('verified') or not (folder/'.source-protected.json').exists()):
                shutil.rmtree(folder,ignore_errors=True); del leases[name]
            else:
                reserved += max(int(lease['reserved']),lease_bytes(workspace,name,lease))
        occupied = bytes_used(directory) if directory.exists() else 0
        requested = max(required,occupied)
        available = os.statvfs(workspace).f_bavail * os.statvfs(workspace).f_frsize
        if reserved + requested + MARGIN > CAPACITY or available < max(0,requested-occupied)+MARGIN:
            raise WorkspaceFailure('MEDIA_WORKSPACE_BUSY', True)
        directory.mkdir(mode=0o700,exist_ok=True)
        # Keep Chromium's Unix socket path short, but fenced by the same job lease.
        browser_temporary = Path(tempfile.mkdtemp(prefix='b-',dir=workspace/'t'))
        leases[key] = dict(pid=os.getpid(),identity=identity,reserved=requested,verified=False,active=True,createdAt=time.time(),browserTemporary=browser_temporary.name)
        journal(lease_file,leases)
    previous = os.environ.get('HM_JOB_MEDIA_ROOT')
    previous_temporary = os.environ.get('HM_JOB_TEMP_ROOT')
    os.environ['HM_JOB_MEDIA_ROOT'] = str(directory)
    os.environ['HM_JOB_TEMP_ROOT'] = str(browser_temporary)
    try:
        yield directory
    finally:
        if previous is None: os.environ.pop('HM_JOB_MEDIA_ROOT',None)
        else: os.environ['HM_JOB_MEDIA_ROOT']=previous
        if previous_temporary is None:os.environ.pop('HM_JOB_TEMP_ROOT',None)
        else:os.environ['HM_JOB_TEMP_ROOT']=previous_temporary
        with lock(workspace):
            leases = json.loads(lease_file.read_text())
            lease = leases.get(key)
            if lease and lease['pid']==os.getpid() and lease['identity']==identity:
                shutil.rmtree(browser_temporary,ignore_errors=True)
                lease.pop('browserTemporary',None)
                # Source verification is explicitly recorded before unlinking.
                verified = (directory/'.r2-verified').exists()
                protected = (directory/'.source-protected.json').exists()
                if verified or not protected:
                    shutil.rmtree(directory); del leases[key]
                else:
                    lease['reserved']=bytes_used(directory); lease['verified']=False;lease['active']=False
                journal(lease_file,leases)

def reserve_input(size):
    """Reserve input plus merge/output growth before transport; grow atomically."""
    value=os.getenv('HM_JOB_MEDIA_ROOT')
    if not value or not size:return
    required=int(size)*3+64*1024**2
    if required+MARGIN>CAPACITY:raise WorkspaceFailure('MEDIA_EXCEEDS_WORKSPACE_LIMIT')
    workspace=root();key=Path(value).name
    with lock(workspace):
        leases=json.loads((workspace/'.leases.json').read_text());current=leases[key]
        if required<=current['reserved']:return
        reserved=sum(max(int(lease['reserved']),lease_bytes(workspace,name,lease)) for name,lease in leases.items() if name!=key)
        if reserved+required+MARGIN>CAPACITY:raise WorkspaceFailure('MEDIA_WORKSPACE_BUSY',True)
        current['reserved']=required;journal(workspace/'.leases.json',leases)

def download_progress(progress):
    size=progress.get('total_bytes') or progress.get('total_bytes_estimate') or progress.get('downloaded_bytes')
    reserve_input(size)

def storage_failure(error):
    """Map exhausted media filesystem errors without exposing process output."""
    if not os.getenv('HM_EPHEMERAL_MEDIA_ROOT'):return None
    full=getattr(error,'errno',None) in (errno.ENOSPC,errno.EDQUOT)
    stderr=getattr(error,'stderr',b'') or b''
    message=(stderr.decode('utf-8','replace') if isinstance(stderr,bytes) else str(stderr))+' '+str(error)
    if not full and 'no space left on device' not in message.lower() and 'disk quota exceeded' not in message.lower():return None
    current=os.getenv('HM_JOB_MEDIA_ROOT')
    if current and bytes_used(Path(current))+MARGIN>=CAPACITY:
        return WorkspaceFailure('MEDIA_EXCEEDS_WORKSPACE_LIMIT')
    return WorkspaceFailure('MEDIA_WORKSPACE_BUSY',True)

def verified():
    value=os.getenv('HM_JOB_MEDIA_ROOT')
    if value:
        path=Path(value); (path/'.r2-verified').touch()
        workspace=root()
        with lock(workspace):
            leases=json.loads((workspace/'.leases.json').read_text())
            for lease in leases.values():
                if lease['pid']==os.getpid() and lease['identity']==process_identity(os.getpid()):lease['verified']=True
            journal(workspace/'.leases.json',leases)

def protect_source(path, sha):
    value=os.getenv('HM_JOB_MEDIA_ROOT')
    if value:
        directory=Path(value).resolve(); source=Path(path).resolve()
        if not source.is_relative_to(directory):raise WorkspaceFailure('MEDIA_WORKSPACE_SOURCE_PATH')
        journal(directory/'.source-protected.json',dict(path=str(source.relative_to(directory)),sha256=sha))

def temporary_root():
    workspace=root()
    if workspace is None:return '/tmp'
    target=Path(os.environ.get('HM_JOB_TEMP_ROOT') or workspace/'t');target.mkdir(exist_ok=True);return str(target)
