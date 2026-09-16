"""YouTube provider adapter for HM's existing lease and video-result contracts."""
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import facebook_video_ingest as ingest
import hm_google_account as accounts
from download import save

CODES={'authentication_required':'GOOGLE_LOGIN_REQUIRED','rate_limited':'GOOGLE_RATE_LIMITED','verification_required':'GOOGLE_VERIFICATION_REQUIRED','download_error':'YOUTUBE_DOWNLOAD_FAILED'}


def video_record(item):
    vid=item['id'];url='https://www.youtube.com/watch?v='+vid
    path=Path(item['path']) if item.get('path') else None
    downloaded=item.get('status') in ('downloaded','skipped') and path and path.is_file()
    if item.get('status')=='skipped' and not downloaded:return None
    if item.get('status')=='filtered-duration':return None
    published=None
    if item.get('upload_date'):
        try:published=dt.datetime.strptime(item['upload_date'],'%Y%m%d').strftime('%Y-%m-%dT00:00:00')
        except ValueError:pass
    return dict(platformVideoId=vid,originalUrl=url,canonicalUrl=url,title=item.get('title') or vid,
        localPath=str(path) if downloaded else None,fileName=path.name if downloaded else None,
        fileSize=path.stat().st_size if downloaded else None,durationSeconds=round(item['duration']) if item.get('duration') else None,
        publishedAt=published,status='downloaded' if downloaded else 'download-failed',
        errorCode=None if downloaded else CODES.get(item.get('kind'),'YOUTUBE_DOWNLOAD_FAILED'),
        error=None if downloaded else 'YouTube 视频下载失败')


def report_code(report):
    kind=report.get('stopped_reason') or report.get('error',{}).get('kind')
    return CODES.get(kind) or next((CODES.get(i.get('kind'),'YOUTUBE_DOWNLOAD_FAILED') for i in report.get('results',[]) if i.get('status')=='failed'),None)


def execute(args,job):
    if os.getenv('HM_CAPTURE_TENANT')!='vn':raise ingest.PipelineError('YouTube is only enabled in VN')
    backend=ingest.normalize_backend(args.backend);execution=str(job['executionId'])
    folder=args.state_dir.expanduser().resolve()/ingest.state_segment(execution);folder.mkdir(parents=True,exist_ok=True)
    report_path=folder/'youtube-download.json'
    output=Path(os.environ['FACEBOOK_FOLLOWED_OUTPUT'])/'YouTube'
    config=json.loads(Path(os.environ['HM_TENANT_CONFIG']).read_text())['vn']
    pump=ingest.HeartbeatPump(backend,args.worker_token,args.worker_id,execution,args.heartbeat_seconds)
    child=None
    try:
        ingest.heartbeat(backend,args.worker_token,args.worker_id,execution,5);pump.start()
        report=json.loads(report_path.read_text()) if report_path.is_file() else {}
        # A completed receipt is authoritative even when its completion callback was lost.
        if not report.get('finished_at'):
            command=[sys.executable,str(Path(__file__).with_name('download.py')),job['sourceUrl'],'--limit',str(args.count),'--output',str(output),'--cookies',os.environ['HM_GOOGLE_COOKIES'],'--report',str(report_path),'--max-duration-seconds','1200','--height','1920']
            child=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=False)
            observed=set()
            deadline=time.monotonic()+3600
            while child.poll() is None:
                if time.monotonic()>deadline:child.terminate();raise ingest.PipelineError('YouTube 下载超时','YOUTUBE_DOWNLOAD_FAILED')
                if report_path.is_file():
                    snapshot=json.loads(report_path.read_text())
                    for item in snapshot.get('results',[]):
                        key=(item['id'],item['status'])
                        if key in observed:continue
                        video=video_record(item)
                        if video:
                            try:
                                ingest.record_video(backend,args.worker_token,args.worker_id,execution,video,download_status='DOWNLOADED' if video['status']=='downloaded' else 'DOWNLOAD_FAILED',upload_status='PENDING')
                            except ingest.BackendError:continue
                        observed.add(key)
                    pump.update(min(85,10+round(75*len(observed)/max(snapshot.get('discovered',1),1))))
                time.sleep(2)
            report=json.loads(report_path.read_text()) if report_path.is_file() else {'error':{'kind':'download_error'},'results':[]}
        records=[video for item in report.get('results',[]) if (video:=video_record(item))]
        # Reconcile every durable receipt idempotently, including archive skips after an interrupted callback.
        for video in records:
            ingest.record_video(backend,args.worker_token,args.worker_id,execution,video,download_status='DOWNLOADED' if video['status']=='downloaded' else 'DOWNLOAD_FAILED',upload_status='PENDING')
        code=report_code(report)
        if code:accounts.download_result(config,'vn',code)
        elif any(item.get('status')=='downloaded' for item in report.get('results',[])):accounts.download_result(config,'vn')
        counts=report.get('counts') or {key:sum(i.get('status')==key for i in report.get('results',[])) for key in ('downloaded','skipped','failed')}
        counts.setdefault('unattempted',max(0,report.get('discovered',0)-len(report.get('results',[]))))
        status='COMPLETED' if not code else 'PARTIAL' if counts.get('downloaded') else 'FAILED'
        result=dict(skill='youtube-video-downloader',executionId=execution,status=status,counts=counts,report=str(report_path))
        save(folder/'result.json',result)
        ingest.complete(backend,args.worker_token,args.worker_id,execution,status,result,'',code,'YouTube 下载受阻，请查看账号状态或失败视频' if code else None)
        return 0,result  # Business failure is terminal; only callback/lease failure retries orchestration.
    except ingest.BackendError:
        raise  # Keep durable receipts and recover on the existing lease retry path.
    except Exception as error:
        code=getattr(error,'error_code','YOUTUBE_DOWNLOAD_FAILED')
        result=dict(status='FAILED',executionId=execution,errorCode=code)
        ingest.complete(backend,args.worker_token,args.worker_id,execution,'FAILED',result,'',code,'YouTube 执行失败，请检查服务器运行环境')
        return 0,result
    finally:
        pump.stop()
        if child and child.poll() is None:
            child.terminate()
            try:child.wait(timeout=10)
            except subprocess.TimeoutExpired:child.kill();child.wait()
