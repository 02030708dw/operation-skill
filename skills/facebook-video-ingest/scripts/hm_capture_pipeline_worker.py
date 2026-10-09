#!/usr/bin/env python3
"""One shared fair dispatcher, fenced region-local jobs and restart-safe stage receipts."""
from __future__ import annotations
import collections
import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import hm_server_worker as runner

REGIONS = ('ph', 'th', 'vn', 'id')
PLATFORMS = ('Facebook', 'YouTube', 'TikTok', 'X')
PREFIX = '/api/internal/capture/pipeline'


def atomic_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as output:
        json.dump(value, output, sort_keys=True); output.flush(); os.fsync(output.fileno())
    temporary.replace(path)


def backend_title(value, limit=300):
    """Keep titles within the Java API's UTF-16 code-unit limit."""
    chars=[]; units=0
    for char in str(value):
        width=2 if ord(char)>0xffff else 1
        if units+width>limit:break
        chars.append(char);units+=width
    return ''.join(chars)


def request(config, path, body):
    payload = dict(body, workerId=config['workerId'], storageNode=config['workerId'])
    req = urllib.request.Request(config['backendUrl'].rstrip('/')+PREFIX+path,
        json.dumps(payload).encode(), headers={'Content-Type':'application/json',
        'X-HM-Worker-Token':config['workerToken']}, method='POST')
    with urllib.request.urlopen(req, timeout=12) as response:
        return json.load(response).get('data')


class Rotation:
    """A successful claim always advances the region; empty capacity is borrowed."""
    def __init__(self, regions):
        self.regions = collections.deque(regions)
        self.platforms = {region:collections.deque(PLATFORMS) for region in regions}

    def candidates(self):
        for _ in range(len(self.regions)):
            region = self.regions[0]; self.regions.rotate(-1)
            queue = self.platforms[region]
            for _ in range(len(queue)):
                platform = queue[0]; queue.rotate(-1)
                yield region, platform


class DiscoveryTurn:
    """Share discovery slots fairly even while blogger checks stay busy."""
    def __init__(self):self.preferred='CHECK'
    def stages(self):
        other='X_IMPORT' if self.preferred=='CHECK' else 'CHECK'
        return (self.preferred,'DOWNLOAD',other,'MEDIA')
    def started(self,stage):
        if stage in ('CHECK','X_IMPORT'):
            self.preferred='X_IMPORT' if stage=='CHECK' else 'CHECK'


def slots(stage):
    total = min(8, max(1, int(os.getenv('HM_CAPTURE_SLOTS', '8'))))
    checks = min(2, total)
    return range(1, checks+1) if stage in ('CHECK','X_IMPORT') else range(checks+1, total+1)


def stage_lock(root, stage):
    if stage == 'MEDIA':
        import hm_media_capacity
        for number in range(1, hm_media_capacity.limit()+1):
            held = runner.lock_file(root/'locks'/f'PIPELINE-MEDIA-{number}.lock')
            if held: return number, held
        return None, None
    for number in slots(stage):
        held = runner.lock_file(root/'locks'/f'CAPTURE-{number}.lock')
        if held: return number, held
    return None, None


def terminate(child):
    if child.poll() is None:
        os.killpg(child.pid, signal.SIGTERM)
        try: child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL); child.wait()


def supervise():
    # The outer trusted runner holds PIPELINE-1 globally. Children inherit it,
    # so a replacement coordinator cannot race surviving children after a crash.
    root = Path(os.environ['HM_PIPELINE_GLOBAL_ROOT'])
    os.environ['HM_SERVER_STATE_DIR'] = str(root)
    configs = json.loads(Path(os.environ['HM_TENANT_CONFIG']).read_text())
    rotations = {stage:Rotation([r for r in REGIONS if r in configs]) for stage in ('CHECK','DOWNLOAD','X_IMPORT','MEDIA')}
    discovery_turn=DiscoveryTurn()
    active = []; stopping = False; states={};checked_at=0
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    try:
        while not stopping or active:
            if time.monotonic()-checked_at>5:
                checked_at=time.monotonic()
                for region in configs:
                    try:states[region]=request(configs[region],'/status',{})
                    except (OSError,ValueError):states[region]=None
                if states and all(s is not None and not s['enabled'] for s in states.values()):stopping=True
            for item in list(active):
                child, held, log, _, _, _ = item
                if child.poll() is not None:
                    held.close(); log.close(); active.remove(item)
            if stopping:
                time.sleep(.5); continue
            for stage in discovery_turn.stages():
                rotation=rotations[stage]
                slot, held = stage_lock(root, stage)
                if held is None: continue
                try:
                    for region, platform in rotation.candidates():
                        config = configs[region]
                        state=states.get(region)
                        if not state or not state['enabled'] or state.get('pauseClaims') or (stage in ('CHECK','X_IMPORT') and not state['acceptNew']):continue
                        if platform not in config.get('enabledPlatforms', ['Facebook']): continue
                        ready=[r for r in state.get('ready',[]) if r['stage']==stage and r['platform']==platform]
                        if 'ready' in state and not any(r['count']>0 for r in ready):continue
                        if config.get('capturePolicy') != 'PUBLIC_FIRST': continue
                        # Keep platform pressure bounded across checking AND downloading.
                        limit = 2 if platform == 'Facebook' else 1
                        if stage != 'MEDIA' and sum(x[3]==region and x[4]==platform and x[5]!='MEDIA' for x in active)>=limit: continue
                        cooldown = root/'tenants'/region/'public-cooldown'/(platform+'.json')
                        if stage!='MEDIA' and platform_cooling_down(cooldown,state.get('windowedMode',False)): continue
                        if stage=='DOWNLOAD':
                            import shutil
                            if not os.getenv('HM_EPHEMERAL_MEDIA_ROOT') and shutil.disk_usage(config['mediaRoot']).free < int(os.getenv('HM_MIN_FREE_DISK_BYTES',str(20*1024**3))): continue
                        try: job = request(config, f'/{stage}/claim', {'platform':platform,
                            'allowHistory':not any(s and s['enabled'] and s.get('realtimeWaiting',0)>0 for s in states.values())})
                        except (OSError, ValueError): continue
                        if not job:
                            for hint in ready:hint['count']=0
                            continue
                        for hint in ready:hint['count']=max(0,hint['count']-1)
                        directory = root/'tenants'/region/'pipeline'/job['jobNo']
                        directory.mkdir(parents=True, exist_ok=True)
                        spec = directory/(str(job['leaseVersion'])+'.json')
                        atomic_json(spec, dict(job, tenant=region))
                        environment = runner.worker_environment(dict(tenant=region,kind='PIPELINE',slot=slot,
                            dispatchId='pipeline-'+job['jobNo'], platform=platform))
                        # Isolate browser profiles by stage; MEDIA slot 1 must not
                        # reuse CHECK slot 1's temporary directory.
                        environment['TMPDIR'] = str(directory/'tmp')
                        environment['HM_PIPELINE_DEFER_MEDIA']='1'
                        environment['HM_MEDIA_COMPAT_ENABLED']='1'
                        Path(environment['TMPDIR']).mkdir(parents=True, exist_ok=True)
                        log = (directory/(str(job['leaseVersion'])+'.log')).open('ab')
                        inherited = [held.fileno()]
                        outer = os.getenv('HM_PIPELINE_GUARD_FD')
                        if outer: inherited.append(int(outer))
                        child = subprocess.Popen([sys.executable,__file__,'job',str(spec)], env=environment,
                            stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,pass_fds=tuple(inherited))
                        active.append((child,held,log,region,platform,stage));held=None
                        discovery_turn.started(stage)
                        break
                finally:
                    if held: held.close()
            time.sleep(1)
    finally:
        for child, held, log, *_ in active:
            terminate(child); held.close(); log.close()


def result_file(job_file):
    return job_file.with_suffix('.result.json')


def run_job(job_file):
    job_file = Path(job_file); job = json.loads(job_file.read_text())
    config = runner.tenant_config(job)
    heartbeat = {'leaseVersion':job['leaseVersion']}
    receipt = result_file(job_file)
    # The child executes one stage. This supervisor owns its fenced heartbeat.
    child = None
    try:
        request(config, '/jobs/'+job['jobNo']+'/heartbeat', heartbeat)
        if not receipt.exists():
            child = subprocess.Popen([sys.executable,__file__,'stage',str(job_file)])
        last_ack=time.monotonic(); last_heartbeat=last_ack
        while child is not None and child.poll() is None:
            time.sleep(.5)
            if time.monotonic()-last_heartbeat<25: continue
            last_heartbeat=time.monotonic()
            try:
                request(config, '/jobs/'+job['jobNo']+'/heartbeat', heartbeat); last_ack=time.monotonic()
            except urllib.error.HTTPError as error:
                if error.code in (401,403,409): return 2
            except OSError: pass
            if time.monotonic()-last_ack>85: return 2
        if not receipt.exists():
            atomic_json(receipt, dict(heartbeat,outcome='FAILED',errorCode='STAGE_PROCESS_FAILED'))
        payload=json.loads(receipt.read_text())
        # A lost acknowledgement retries the SAME durable receipt, never the work.
        while time.monotonic()-last_ack<85:
            try:
                request(config, '/jobs/'+job['jobNo']+'/complete', payload)
                if job.get('windowedMode'):
                    import shutil
                    if job['stage']=='DOWNLOAD' or (job.get('legacyAdopt') and payload.get('outcome')=='SUCCESS'):
                        media_dir=Path(config['mediaRoot'])/'pipeline'/job['subjectKey']
                        shutil.rmtree(media_dir,ignore_errors=True)
                    shutil.rmtree(job_file.parent,ignore_errors=True)
                return 0
            except urllib.error.HTTPError as error:
                if error.code in (400,401,403,409): return 2
            except OSError: pass
            time.sleep(2)
        return 2
    finally:
        if child is not None and child.poll() is None:
            # The stage shares our process group; killing just it does not stop
            # ffmpeg/Chromium. Exit the entire fenced group on lease loss.
            os.killpg(os.getpgrp(),signal.SIGTERM)


def execute_stage(job_file):
    # Chromium creates its SingletonSocket under TMPDIR (Unix path limit 108
    # bytes). Job/lease directories are too deep. Keep durable receipts/media
    # there, but give each stage a private short-lived, short browser temp root.
    previous = os.environ.get('TMPDIR')
    import hm_media_workspace as workspace
    enabled=bool(os.getenv('HM_EPHEMERAL_MEDIA_ROOT'))
    job_data=json.loads(Path(job_file).read_text()) if enabled else {}
    required=64*1024**2 if job_data.get('stage') in ('CHECK','X_IMPORT') else max(512*1024**2,int(job_data.get('download',{}).get('fileSize',0))*3+64*1024**2)
    admission=workspace.job(job_data['tenant'],job_data['jobNo'],required) if enabled else contextlib.nullcontext()
    try:
        with admission, tempfile.TemporaryDirectory(prefix='hp-', dir=workspace.temporary_root()) as temporary:
            os.environ['TMPDIR'] = temporary
            try:
                _execute_stage(job_file)
            finally:
                if previous is None: os.environ.pop('TMPDIR', None)
                else: os.environ['TMPDIR'] = previous
    except workspace.WorkspaceFailure as error:
        atomic_json(result_file(Path(job_file)),dict(leaseVersion=job_data['leaseVersion'],outcome='RESOURCE_WAIT' if error.retryable else 'FAILED',errorCode=str(error),failureStage='MEDIA_WORKSPACE'))



def platform_cooling_down(path, windowed):
    if not path.exists():return False
    saved=json.loads(path.read_text())
    # Legacy rate-limit receipts have no reasonCode. A single video's challenge
    # must not pause every other public video or source on that platform.
    return saved.get('until',0)>time.time() and (not windowed or saved.get('reasonCode') in (None,'RATE_LIMITED'))


def _execute_stage(job_file):
    import hm_public_capture as public
    job_file=Path(job_file);job=json.loads(job_file.read_text());config=dict(runner.tenant_config(job));config['legacyMediaRoot']=config.get('mediaRoot')
    if os.getenv('HM_JOB_MEDIA_ROOT'):config['mediaRoot']=os.environ['HM_JOB_MEDIA_ROOT']
    attempts=[];result={'leaseVersion':job['leaseVersion']}
    phase=job['stage']
    try:
        if job['stage']=='X_IMPORT':
            from hm_x_capture import discover as discover_post
            attempt={'stage':'DISCOVERY','mode':'PUBLIC'};attempts.append(attempt)
            entries=discover_post(job['sourceUrl'],10)
            attempt['result']='PASSED'
            result.update(outcome='SUCCESS',complete=True,validated=True,entries=entries)
        elif job['stage']=='CHECK':
            from hm_pipeline_discovery import check, public_check
            def discover(credentials):
                try:return check(job,credentials)
                except public.Failure as error:
                    # Accessible public videos may be discovered before a login
                    # wall. Keep them, but never convert that attempt to coverage.
                    if getattr(error,'entries',None):result['entries']=error.entries
                    raise
            from hm_x_capture import source as x_source, profile_check, local_profile_check
            if job['platform']=='X' and job.get('localDiscovery'):
                attempt={'stage':'DISCOVERY','mode':'AUTHENTICATED','sourceMode':'LOCAL_CHROME','metadataMode':'PUBLIC'};attempts.append(attempt)
                try:result.update(local_profile_check(job))
                except Exception as error:
                    attempt.update(result='FAILED',reasonCode=public.classify(error))
                    if getattr(error,'entries',None):result['entries']=error.entries
                    raise
                attempt['result']='PASSED'
            elif job['platform']=='X' and x_source(job['sourceUrl']).get('kind')=='profile':
                attempt={'stage':'DISCOVERY','mode':'AUTHENTICATED'};attempts.append(attempt)
                try:result.update(profile_check(job,config))
                except Exception as error:
                    attempt.update(result='FAILED',reasonCode=public.classify(error))
                    if getattr(error,'entries',None):result['entries']=error.entries
                    raise
                attempt['result']='PASSED'
            elif job.get('windowedMode'):
                attempt={'stage':'DISCOVERY','mode':'PUBLIC'};attempts.append(attempt)
                try:
                    discovered=public_check(job)
                except Exception as error:
                    attempt.update(result='FAILED',reasonCode=public.classify(error))
                    raise
                attempt.update(result='PASSED' if discovered['outcome']=='SUCCESS' else 'PARTIAL')
                if discovered.get('errorCode'):attempt['reasonCode']=discovered['errorCode']
                result.update(discovered)
            else:
                result.update(public.attempt(discover,config,job['platform'],attempts,'DISCOVERY'))
        elif job.get('legacyAdopt'):
            phase='REVIEW_STORAGE'
            result.update(outcome='SUCCESS',video=adopt_legacy_original(job,config))
        else:
            directory=Path(config['mediaRoot'])/'pipeline'/job['subjectKey']
            directory.mkdir(parents=True,exist_ok=True)
            saved=directory/('download.json' if job['stage']=='DOWNLOAD' else 'media.json')
            if job.get('manualRetry') and not os.getenv('HM_JOB_MEDIA_ROOT'):
                cleanup_pipeline_media(directory,saved)
                saved.unlink(missing_ok=True)
            video=None
            if saved.exists():
                candidate=json.loads(saved.read_text());path=Path(candidate.get('localPath') or '')
                if job.get('windowedMode') and candidate.get('reviewObjectKey'):
                    phase='REVIEW_STORAGE'
                    try:verify_review_receipt(candidate,job['tenant'])
                    except Exception:
                        if path.is_file() and digest(path)==candidate.get('fileSha256'):video=candidate
                        else:raise
                    else:
                        video=dict(candidate,localPath=None)
                        import hm_media_workspace
                        hm_media_workspace.verified()
                        atomic_json(saved,video)
                elif path.is_file() and digest(path)==candidate.get('fileSha256'):video=candidate
            if video is None and job.get('recoveryOnly'):
                cleanup_pipeline_media(directory,saved)
                raise public.Failure('RECEIPT_MISSING')
            if video is None and job['stage']=='DOWNLOAD':
                for candidate in job.get('reuseCandidates',[]):
                    try:
                        source=Path(candidate['localPath']).resolve(strict=True)
                        if not source.is_relative_to(Path(config['mediaRoot']).resolve()) or digest(source)!=candidate['fileSha256']:continue
                        # Own immutable copy: another review/delete queue may unlink
                        # the old ownership immediately after this check.
                        import shutil
                        target=directory/('reused'+source.suffix)
                        temporary=target.with_suffix('.copying');shutil.copyfile(source,temporary)
                        if digest(temporary)!=candidate['fileSha256']:
                            temporary.unlink(missing_ok=True);continue
                        temporary.replace(target)
                        video=dict(candidate,localPath=str(target),fileSize=target.stat().st_size,
                            platformVideoId=job['entry']['id'],originalUrl=job['entry']['url'],canonicalUrl=job['entry']['url'],
                            reused=True,attempts=[])
                        break
                    except (OSError,KeyError):continue
            if video is None and job['stage']=='DOWNLOAD':
                item=public.attempt(lambda credentials:public.download_item(job['platform'],job['entry'],directory,credentials),config,job['platform'],attempts,'DOWNLOAD')
                if item.get('status') in ('filtered-duration','unsupported'):
                    result.update(outcome='FILTERED',errorCode=item.get('errorCode','DURATION_FILTERED'))
                else:
                    path=Path(item['path']).resolve(strict=True)
                    if not path.is_relative_to(Path(config['mediaRoot']).resolve()):raise ValueError('REGION_PATH_INVALID')
                    video=dict(originalUrl=job['entry']['url'],canonicalUrl=job['entry']['url'],
                        platformVideoId=job['entry']['id'],title=backend_title(item.get('title') or job['entry'].get('title') or job['entry']['id']),
                        sourceName=(item.get('sourceName') or job['entry'].get('sourceName') or '').encode('utf-16-le')[:240].decode('utf-16-le','ignore') or None,
                        sourceHandle=item.get('sourceHandle') or job['entry'].get('sourceHandle'),
                        titleSource=job['entry'].get('titleSource','ORIGINAL'),titleStatus=job['entry'].get('titleStatus','AVAILABLE'),
                        localPath=str(path),fileName=path.name,fileSize=path.stat().st_size,fileSha256=digest(path),
                        durationSeconds=int(item.get('duration') or 0),expectAudio=bool(item.get('expectAudio')),attempts=attempts)
            elif video is None:
                import hm_media_compat as compat
                video=dict(job['download'])
                path=Path(video['localPath']).resolve(strict=True)
                if not path.is_relative_to(Path(config['mediaRoot']).resolve()) or digest(path)!=video['fileSha256']:raise ValueError('FILE_CHANGED')
                compat.prepare_record(video)
                if video.get('status')=='download-failed' or video.get('downloadStatus')=='DOWNLOAD_FAILED' or not video.get('mediaCompatibility'):raise public.Failure('MEDIA_COMPAT_FAILED')
                if video.get('expectAudio') and not any(s.get('codec_type')=='audio' for s in compat.probe(video['localPath'])['streams']):raise public.Failure('VALIDATION_FAILED')
                video['fileSha256']=digest(video['localPath'])
            if video:
                # Persisted downloads and media receipts may predate the title
                # limit fix; normalize them before either stage reports success.
                video['title']=backend_title(video.get('title') or video.get('platformVideoId') or job['subjectKey'])
                if job.get('windowedMode') and job['stage']=='DOWNLOAD':
                    try:
                        if not video.get('reviewObjectKey'):
                            phase='REVIEW_STORAGE'
                            video=store_review_original(video,job['tenant'],job['subjectKey'],saved)
                            atomic_json(saved,video)
                        result.update(outcome='SUCCESS',video=video)
                    finally:
                        # The verified bucket object is the source of truth. Failed
                        # uploads are terminal and must not strand large files.
                        if video.get('localPath') is None:cleanup_pipeline_media(directory,saved)
                    # Keep the verified R2 receipt for the shared persistence and
                    # completion below; localPath is already cleared by storage.
                if job['stage']=='MEDIA':
                    # Immutable canonical cache stays in pipeline/. Each ownership
                    # receives its own hard link, so review cleanup cannot destroy
                    # another owner's source or a later reuse of the same video.
                    import shutil
                    paths=dict(video.get('deliveryPaths') or {})
                    for task_id in job.get('taskIds',[]):
                        target=Path(config['mediaRoot'])/'executions'/('pipeline-'+job['subjectKey']+'-'+str(task_id))/'video.mp4'
                        target.parent.mkdir(parents=True,exist_ok=True)
                        if not target.is_file() or digest(target)!=video['fileSha256']:
                            temporary=target.with_suffix('.tmp')
                            temporary.unlink(missing_ok=True)
                            try:os.link(video['localPath'],temporary)
                            except OSError:shutil.copyfile(video['localPath'],temporary)
                            temporary.replace(target)
                        paths[str(task_id)]=str(target)
                    video['deliveryPaths']=paths
                atomic_json(saved,video);result.update(outcome='SUCCESS',video=video)
        result['attempts']=attempts
        if job.get('windowedMode') and result.get('errorCode')=='RATE_LIMITED':
            cooldown=Path(os.environ['HM_SERVER_STATE_DIR'])/'public-cooldown'/(job['platform']+'.json')
            atomic_json(cooldown,{'until':time.time()+max(1800,result.get('retryAfterSeconds',0)),'reasonCode':'RATE_LIMITED'})
    except Exception as error:
        if job.get('windowedMode') and job['stage']=='DOWNLOAD' and not os.getenv('HM_JOB_MEDIA_ROOT'):
            cleanup_pipeline_media(directory,saved)
        code=public.classify(error)
        if job['stage']=='X_IMPORT' and attempts:
            attempts[-1].update(result='FAILED',reasonCode=code)
        if job.get('windowedMode') and (job['stage']=='DOWNLOAD' or job.get('legacyAdopt')) and error.__class__.__name__=='StorageFailure':
            code=str(error) if str(error).replace('_','').isalnum() and len(str(error))<=80 else 'REVIEW_STORAGE_FAILED'
        result.update(outcome='RESOURCE_WAIT' if code=='MEDIA_WORKSPACE_BUSY' else 'RATE_LIMITED' if code=='RATE_LIMITED' else 'LOGIN_REQUIRED' if public.requires_login(code,attempts) else 'FAILED',errorCode=code,attempts=attempts)
        if job.get('windowedMode') and (job['stage']=='DOWNLOAD' or job.get('legacyAdopt')):
            result['failureStage']='VALIDATION' if code=='VIDEO_TRACK_MISSING' else phase
        delay=max(1800,public.retry_after(error) or 0)
        if code=='RATE_LIMITED':result['retryAfterSeconds']=delay
        if code in public.STOP and (not job.get('windowedMode') or code=='RATE_LIMITED'):
            cooldown=Path(os.environ['HM_SERVER_STATE_DIR'])/'public-cooldown'/(job['platform']+'.json')
            atomic_json(cooldown,{'until':time.time()+delay,'reasonCode':code})
    atomic_json(result_file(job_file),result)


def adopt_legacy_original(job, config):
    """Move a verified old download into review storage without fetching it again."""
    import hm_review_storage as storage
    directory=Path(config.get('legacyMediaRoot',config['mediaRoot'])).resolve()/'pipeline'/job['subjectKey']
    receipt_path=directory/'media.json'
    if receipt_path.exists():
        receipt=json.loads(receipt_path.read_text())
        if receipt.get('reviewObjectKey'):
            verify_review_receipt(receipt,job['tenant'])
            receipt['localPath']=None
            return receipt
    video=dict(job['download'])
    path=legacy_source_path(video,directory)
    video['localPath']=str(path)
    directory.mkdir(parents=True,exist_ok=True)
    receipt=store_review_original(video,job['tenant'],job['subjectKey'],receipt_path,protect=False)
    atomic_json(receipt_path,receipt)
    return receipt


def legacy_source_path(video, directory):
    import hm_review_storage as storage
    try:
        original=Path(video['localPath']).resolve(strict=True)
    except (KeyError,OSError):
        original=None
    if original is not None:
        if not original.is_relative_to(directory) or original.stat().st_size!=video.get('fileSize') or digest(original)!=video.get('fileSha256'):
            raise storage.StorageFailure('LEGACY_FILE_CHANGED')
        return original
    # Old media preparation sometimes moved the validated original within its
    # private video directory. Search only this key, and accept an exact hash.
    if directory.is_dir():
        for candidate in directory.rglob('*'):
            if not candidate.is_file() or candidate.stat().st_size!=video.get('fileSize'):continue
            resolved=candidate.resolve()
            if resolved.is_relative_to(directory) and digest(resolved)==video.get('fileSha256'):
                return resolved
    raise storage.StorageFailure('LEGACY_FILE_MISSING')


def verify_review_receipt(video, tenant):
    import hm_review_storage as storage
    region=tenant.upper();config=storage.configuration(region)
    key=video['reviewObjectKey'];storage.require_review_key(region,key)
    if video['reviewBucket']!=os.environ['CLOUDFLARE_R2_BUCKET']:
        raise storage.StorageFailure('BUCKET_MISMATCH')
    s3=storage.client();extra=storage.encryption(config,video['reviewKeyVersion'])
    storage.verify(storage.head(s3,video['reviewBucket'],key,extra),video)


def store_review_original(video, tenant, video_key, intent_path=None, protect=True):
    import hm_review_storage as storage
    import hm_media_compat as compat
    region=tenant.upper();config=storage.configuration(region)
    path=Path(video['localPath']).resolve(strict=True)
    info=compat.probe(str(path))
    if not any(stream.get('codec_type')=='video' for stream in info['streams']):
        raise storage.StorageFailure('VIDEO_TRACK_MISSING')
    video['sourceInspection']=compat.inspect_source(path,info,source_sha=video['fileSha256'])
    import hm_media_workspace
    hm_media_workspace.protect_source(path,video['fileSha256']) if protect else None
    name='source'+(path.suffix.lower() if path.suffix.lower() in ('.mp4','.webm','.mkv','.mov','.m4v') else '.bin')
    key=f"review/{region}/pipeline/{video_key}/{video['fileSha256'][:16]}/{name}"
    storage.require_review_key(region,key)
    bucket=os.environ['CLOUDFLARE_R2_BUCKET'];version=config['activeVersion']
    extra=storage.encryption(config,version);s3=storage.client()
    receipt=dict(video,reviewObjectKey=key,reviewKeyVersion=version,reviewBucket=bucket)
    if intent_path is not None:atomic_json(intent_path,receipt)
    existing=storage.head(s3,bucket,key,extra)
    if existing is None:
        from boto3.s3.transfer import TransferConfig
        s3.upload_file(str(path),bucket,key,ExtraArgs={**extra,'ContentType':'video/mp4' if name.endswith('.mp4') else 'application/octet-stream',
                       'Metadata':{'hm-sha256':video['fileSha256']}},Config=TransferConfig(max_concurrency=2))
    storage.verify(storage.head(s3,bucket,key,extra),receipt)
    receipt['localPath']=None
    import hm_media_workspace
    hm_media_workspace.verified()
    return receipt


def cleanup_pipeline_media(directory, receipt):
    import shutil
    for path in directory.iterdir():
        if path==receipt:continue
        if path.is_dir():shutil.rmtree(path)
        else:path.unlink(missing_ok=True)


def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):value.update(chunk)
    return value.hexdigest()


if __name__=='__main__':
    if len(sys.argv)==1:supervise()
    elif sys.argv[1]=='job':raise SystemExit(run_job(sys.argv[2]))
    elif sys.argv[1]=='stage':execute_stage(sys.argv[2])
