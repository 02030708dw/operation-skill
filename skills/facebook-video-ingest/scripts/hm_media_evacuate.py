#!/usr/bin/env python3
"""Legacy media evacuation. No age-based deletion and no R2 delete operations."""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import hm_review_storage as storage
import hm_media_compat as media
import hm_media_jobs as jobs
import hm_server_worker as runner

MEDIA={'.mp4','.m4v','.webm','.mkv','.mov','.avi','.ts','.m4a','.part','.m4s'}
METADATA={'.json','.lock','.ytdl'}

@contextlib.contextmanager
def maintenance(apply):
    """Reserve idle lanes first, then wait for running work to finish naturally."""
    with contextlib.ExitStack() as stack:
        if apply:
            root=Path(os.environ.get('HM_SERVER_STATE_DIR','/opt/data/server'))/'locks'
            root.mkdir(parents=True,exist_ok=True);pending=[]
            for kind,count in [('CAPTURE',int(os.getenv('HM_CAPTURE_SLOTS','8'))),('UPLOAD',1),('DELETE',1),('GENERATION',1)]:
                for slot in range(1,count+1):pending.append(stack.enter_context((root/f'{kind}-{slot}.lock').open('a')))
            deadline=time.monotonic()+7200
            while pending:
                for handle in pending[:]:
                    try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB);pending.remove(handle)
                    except BlockingIOError:pass
                if pending:
                    if time.monotonic()>deadline:raise RuntimeError('自然排空超时，未清理媒体')
                    time.sleep(.25)
        yield

def opened():
    inodes=set()
    for directory in Path('/proc').glob('[0-9]*/fd'):
        for entry in directory.glob('*'):
            try:s=entry.stat();inodes.add((s.st_dev,s.st_ino))
            except (OSError,PermissionError):continue
    return inodes

def archive(path,region,call):
    source=path.resolve(strict=True);initial=source.stat()
    sha=media.sha256(source)
    config=storage.configuration(region);version=config['activeVersion'];bucket=os.environ['CLOUDFLARE_R2_BUCKET']
    key=f'review/{region}/legacy-retained/{sha}/source.mp4';s3=storage.client();extra=storage.encryption(config,version)
    existing=storage.head(s3,bucket,key,extra)
    if not existing:
        from boto3.s3.transfer import TransferConfig
        s3.upload_file(str(source),bucket,key,ExtraArgs={**extra,'Metadata':{'hm-sha256':sha}},Config=TransferConfig(max_concurrency=1))
    receipt={'fileSize':initial.st_size,'fileSha256':sha}
    head=storage.head(s3,bucket,key,extra);storage.verify(head,receipt)
    if jobs.remote_sha256(s3,bucket,key,head['ETag'],extra,lambda:None)!=sha:raise RuntimeError('R2 原件内容不一致')
    current=source.stat()
    if (current.st_ino,current.st_size,current.st_mtime_ns)!=(initial.st_ino,initial.st_size,initial.st_mtime_ns) or media.sha256(source)!=sha:raise RuntimeError('原文件发生变化')
    ack=call('confirm',{'localPath':str(source),'sha256':sha,'fileSize':initial.st_size,'objectKey':key,'keyVersion':version})
    if not ack.get('archived'):raise RuntimeError('原件对象引用未保存')
    if (current.st_dev,current.st_ino) in opened():raise RuntimeError('原件仍被进程使用')
    latest=source.stat()
    if (latest.st_ino,latest.st_size,latest.st_mtime_ns)!=(initial.st_ino,initial.st_size,initial.st_mtime_ns):raise RuntimeError('清理前原文件发生变化')
    source.unlink()
    return initial.st_blocks*512

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--region',choices=['ph','th','vn','id'],required=True);p.add_argument('--apply',action='store_true');p.add_argument('--limit',type=int,default=50)
    args=p.parse_args()
    if args.limit<=0:raise ValueError('limit must be positive')
    config=runner.tenant_config({'tenant':args.region});region=args.region.upper()
    os.environ.update(HM_BACKEND_URL=config['backendUrl'],HM_WORKER_TOKEN=config['workerToken'],
                      HM_WORKER_ID=config['workerId'],HM_R2_KEY_PREFIX=config['r2Prefix'],
                      HM_REVIEW_KEY_FILE=config['reviewKeyFile'])
    def call(action,body):return runner.api('/api/internal/capture/local-media/'+action,dict(body,workerId=config['workerId']))
    with maintenance(args.apply):evacuate(args,region,call)

def evacuate(args,region,call,roots=None):
    state=call('inventory',{})
    if state['running']:raise RuntimeError('存在运行任务，请先自然排空并保持领取暂停')
    blocked={str(p) for p in state.get('blockedPaths',[]) if p};busy=opened()
    report={'region':region,'apply':args.apply,'candidates':0,'releasedBytes':0,'retained':0,'errors':{}}
    if roots is None:roots=[Path('/opt/data/regions')/args.region/'media',Path('/opt/data/media-compat-backups')/region]
    for root in roots:
        for path in sorted(root.rglob('*')):
            if path.suffix.lower() not in MEDIA or not path.is_file():continue
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):raise RuntimeError('媒体目录存在链接')
            info=path.stat()
            if str(path) in blocked or (info.st_dev,info.st_ino) in busy:
                report['retained']+=1;continue
            report['candidates']+=1
            if args.apply:
                try:report['releasedBytes']+=archive(path,region,call)
                except Exception as error:
                    report['retained']+=1;name=type(error).__name__;report['errors'][name]=report['errors'].get(name,0)+1
            if report['candidates']>=args.limit:break
        if report['candidates']>=args.limit:break
    remaining=[p for root in roots for p in root.rglob('*') if p.is_file() and p.suffix.lower() not in METADATA]
    # Unknown extensions remain visible and block quota activation; they are never silently deleted.
    report['remainingFiles']=len(remaining)
    report['remainingBytes']=sum(p.stat().st_blocks*512 for p in remaining)
    report['metadataBytes']=sum(p.stat().st_blocks*512 for root in roots for p in root.rglob('*') if p.is_file() and p.suffix.lower() in METADATA)
    print(json.dumps(report))
    return report
if __name__=='__main__':main()
