"""Regional public-first capture. Durable per-item receipts precede backend callbacks."""
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse, parse_qs

import facebook_video_ingest as ingest
import hm_facebook_account as facebook
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'youtube-video-downloader/scripts'))
import hm_google_account as google
from download import Logger, save, resolve_ffmpeg, normalize_url

AUTH_ENV = ('HM_GOOGLE_COOKIES','HM_FACEBOOK_PROFILE','HM_FACEBOOK_ACCOUNT_STATE',
            'FACEBOOK_FOLLOWED_COOKIES','FB_FOLLOWED_COOKIES','YTDLP_COOKIES','YTDLP_COOKIES_FROM_BROWSER')
STOP = {'RATE_LIMITED','VERIFICATION_REQUIRED','ACCOUNT_SUSPENDED'}
MESSAGES = {
    'LOGIN_REQUIRED':'该内容需要登录；没有可用登录会话时无法下载。',
    'ACCOUNT_NOT_CONFIGURED':'该内容需要登录，本地区未配置账号。',
    'ACCOUNT_UNAVAILABLE':'该内容需要登录，本地区账号未登录或会话已失效。',
    'ACCOUNT_BUSY':'该内容需要登录，账号正在维护或使用，本次不等待。',
    'VERIFICATION_REQUIRED':'平台要求安全验证，已停止本批次，请在官方页面完成验证。',
    'ACCOUNT_SUSPENDED':'平台提示账号受限，已停止本批次。',
    'RATE_LIMITED':'平台请求受限，已停止本批次并等待冷却。',
    'NETWORK_ERROR':'请求超时或网络异常，本次尝试已结束。',
    'DISCOVERY_EMPTY':'来源页未发现可下载视频，未能枚举本次视频列表。',
    'DOWNLOAD_FAILED':'未获得可验证的视频文件，请检查链接或查看平台访问情况。',
    'VIDEO_BUSY':'该视频已有下载正在进行，请稍后重试。',
}


def current_tenant():
    tenant=os.environ.get('HM_CAPTURE_TENANT')
    if tenant not in ('ph','th','vn','id'):raise ingest.PipelineError('Invalid execution tenant')
    return tenant


class Failure(Exception):
    def __init__(self, code):
        self.code=code
        super().__init__(MESSAGES.get(code,MESSAGES['DOWNLOAD_FAILED']))


def classify(error):
    if isinstance(error,Failure): return error.code
    text=str(error).lower()
    if any(x in text for x in ('429','too many requests','rate limit','temporarily blocked','try again later')): return 'RATE_LIMITED'
    if any(x in text for x in ('captcha','not a bot','checkpoint','challenge','verify your identity','security check','verification required')): return 'VERIFICATION_REQUIRED'
    if any(x in text for x in ('account suspended','account has been suspended','account disabled')): return 'ACCOUNT_SUSPENDED'
    if any(x in text for x in ('sign in','log in','login','registered users','cookies','private video','members-only','age-restricted')): return 'LOGIN_REQUIRED'
    if any(x in text for x in ('timed out','timeout','connection reset','unable to download webpage','network is unreachable')): return 'NETWORK_ERROR'
    return 'DOWNLOAD_FAILED'


def clean_environment(env):
    result=dict(env)
    for key in AUTH_ENV: result.pop(key,None)
    result['HM_PUBLIC_CAPTURE']='1'
    return result


@contextlib.contextmanager
def authenticated_session(config,platform):
    provider=google if platform=='YouTube' else facebook
    account=provider.account_config(config)
    if not account: raise Failure('ACCOUNT_NOT_CONFIGURED')
    current=provider.read_state(account)
    if current.get('state') not in ('AVAILABLE','LOGGED_IN'): raise Failure('ACCOUNT_UNAVAILABLE')
    lease=provider.acquire_capture(account)
    if lease is None: raise Failure('ACCOUNT_BUSY')
    try:
        verified=provider.prepare_capture(config,current_tenant(),lease)
        if verified.get('state')!='AVAILABLE':
            reason=str(verified.get('reasonCode','')).replace('FACEBOOK_','').replace('GOOGLE_','')
            if reason in STOP:raise Failure(reason)
            if verified.get('state')=='VERIFICATION_REQUIRED':raise Failure('VERIFICATION_REQUIRED')
            raise Failure('ACCOUNT_UNAVAILABLE')
        yield ({'cookiefile':str(lease.profile/'youtube-cookies.txt')} if platform=='YouTube'
               else {'cookiesfrombrowser':('chrome',str(lease.profile),None,None)})
    finally: lease.close()


def account_outcome(config,platform,code):
    provider=google if platform=='YouTube' else facebook
    if not provider.account_config(config):return
    prefix='GOOGLE_' if platform=='YouTube' else 'FACEBOOK_'
    try:
        if platform=='YouTube':
            if code is None or code in ('LOGIN_REQUIRED','VERIFICATION_REQUIRED','RATE_LIMITED'):
                google.download_result(config,current_tenant(),prefix+code if code else None)
        elif code in ('LOGIN_REQUIRED','VERIFICATION_REQUIRED','RATE_LIMITED','ACCOUNT_SUSPENDED'):
            facebook.capture_restriction(config,current_tenant(),prefix+code)
    except Exception: pass  # Durable account state is reconciled by later account checks.


def attempt(operation,config,platform,attempts,stage):
    """Only explicit login requirements permit one authenticated supplement."""
    public={'stage':stage,'mode':'PUBLIC'};attempts.append(public)
    try:
        result=operation({});public['result']='PASSED';return result
    except Exception as error:
        code=classify(error);public.update(result='FAILED',reasonCode=code)
        if code!='LOGIN_REQUIRED':raise Failure(code)
    auth={'stage':stage,'mode':'AUTHENTICATED','result':'NOT_ATTEMPTED'};attempts.append(auth)
    try:
        with authenticated_session(config,platform) as credentials:
            auth['result']='RUNNING'
            try:
                result=operation(credentials)
            except Exception as error:
                code=classify(error);auth.update(result='FAILED',reasonCode=code)
                account_outcome(config,platform,code);raise Failure(code)
            auth['result']='PASSED'
            if stage=='DOWNLOAD' and isinstance(result,dict) and result.get('status')=='downloaded':account_outcome(config,platform,None)
            return result
    except Exception as error:
        code=classify(error);auth.setdefault('reasonCode',code)
        raise Failure(code) from None


def common_options(credentials):
    runtime={name:{'path':shutil.which(name)} for name in ('node','deno') if shutil.which(name)}
    return dict(logger=Logger(),quiet=True,no_warnings=True,noprogress=True,socket_timeout=25,
                retries=0,extractor_retries=0,fragment_retries=0,cachedir=False,ignoreerrors=False,
                js_runtimes=runtime,**credentials)


def single_source(platform,url):
    if platform=='YouTube':
        normalized,vid=normalize_url(url)
        return {'id':vid,'url':normalized} if vid else None
    p=urlparse(url);parts=p.path.strip('/').split('/')
    if p.hostname in ('fb.watch','www.fb.watch') or ('videos' in parts and any(x.isdigit() for x in parts[parts.index('videos')+1:])) or 'reel' in parts or p.path.startswith('/share/') or p.path.rstrip('/') in ('/watch','/video.php') and parse_qs(p.query).get('v'):
        vid=next((x for x in reversed(parts) if x.isdigit()),None) or parse_qs(p.query).get('v',[None])[0]
        return {'id':vid or hashlib.sha256(url.encode()).hexdigest()[:24],'url':url}
    return None


def discover(platform,url,limit,credentials):
    if platform=='YouTube':
        import yt_dlp
        with yt_dlp.YoutubeDL(dict(common_options(credentials),extract_flat='in_playlist',skip_download=True,playlistend=limit)) as ydl:
            data=ydl.extract_info(url,download=False)
            entries=[{'id':x['id'],'url':'https://www.youtube.com/watch?v='+x['id']} for x in data.get('entries',[]) if x and re.fullmatch(r'[\w-]{11}',x.get('id',''))]
    else:
        helper=Path(__file__).with_name('hm_public_facebook_discovery.js')
        profile=credentials.get('cookiesfrombrowser',('',None))[1] or ''
        proc=subprocess.run(['node',str(helper),url,str(limit),profile,'--scroll-rounds','8'],env=clean_environment(os.environ),capture_output=True,text=True,timeout=240)
        lines=[x for x in proc.stdout.splitlines() if x.startswith('HM_PUBLIC_DISCOVERY ')]
        if not lines:raise Failure('NETWORK_ERROR' if proc.returncode else 'DISCOVERY_EMPTY')
        result=json.loads(lines[-1].split(' ',1)[1])
        if result.get('errorCode'):raise Failure(result['errorCode'])
        entries=result['entries']
    if not entries:raise Failure('DISCOVERY_EMPTY')
    return list({e['id']:e for e in entries}.values())[:limit]


def download_item(platform,item,output,credentials):
    import yt_dlp
    from yt_dlp.postprocessor.common import PostProcessor
    captured=[]
    class CaptureFile(PostProcessor):
        def run(self,info):
            path=Path(info['filepath'])
            if not path.is_file() or path.stat().st_size==0:raise Failure('DOWNLOAD_FAILED')
            captured.append(dict(id=str(info['id']),url=item['url'],title=info.get('title') or str(info['id']),
                upload_date=info.get('upload_date'),duration=info.get('duration'),path=str(path),bytes=path.stat().st_size))
            return [],info
    options=dict(common_options(credentials),noplaylist=True,continuedl=True,overwrites=False,
        ffmpeg_location=resolve_ffmpeg(output),merge_output_format='mp4',
        format='bv*[height<=1920][vcodec^=avc1]+ba[acodec^=mp4a]/b[height<=1920][ext=mp4]/bv*[height<=1920]+ba/b[height<=1920]',
        windowsfilenames=True,outtmpl=str(output/'%(id)s.%(ext)s'))
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.add_post_processor(CaptureFile(ydl),when='after_move')
        info=ydl.extract_info(item['url'],download=False)
        if (info.get('duration') or 0)>1200:return dict(item,status='filtered-duration')
        ydl.process_ie_result(info,download=True)
    if not captured:raise Failure('DOWNLOAD_FAILED')
    return dict(captured[-1],status='downloaded')


def run_download(platform,url,limit,output,report_path,config,on_item=lambda *_:None):
    output.mkdir(parents=True,exist_ok=True);receipts=output/'.public-items';receipts.mkdir(exist_ok=True)
    report=json.loads(report_path.read_text()) if report_path.exists() else dict(capturePolicy='PUBLIC_FIRST',platform=platform,attempts=[],results=[],discovered=None,startedAt=int(time.time()*1000))
    if report.get('finishedAt'):return report
    # Partial manifests survive worker/acknowledgement failure; never replay completed items.
    entries=report.get('entries')
    if entries is None:
        try:
            single=single_source(platform,url)
            entries=[single] if single else attempt(lambda c:discover(platform,url,limit,c),config,platform,report['attempts'],'DISCOVERY')
            report.update(entries=entries,discovered=len(entries));save(report_path,report)
        except Exception as error:
            report['errorCode']=classify(error);entries=[]
    legacy_ids=set()
    archives=[output/'.download-archive.txt'] if platform=='YouTube' else list(output.parent.rglob('.yt-dlp-archive.txt'))
    prefix='youtube ' if platform=='YouTube' else 'facebook '
    for archive in archives:
        if archive.is_file():legacy_ids.update(line[len(prefix):].strip() for line in archive.read_text().splitlines() if line.startswith(prefix))
    processed={x.get('sourceId',x['id']) for x in report['results']}
    for entry in entries:
        if entry['id'] in processed:continue
        if report['results']:time.sleep(5)
        key=hashlib.sha256(entry['id'].encode()).hexdigest();receipt=receipts/(key+'.json')
        item=dict(entry,sourceId=entry['id'],attempts=[])
        with (receipts/(key+'.lock')).open('a') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                if receipt.exists():
                    item.update(json.loads(receipt.read_text()),status='skipped',attempts=[])
                elif entry['id'] in legacy_ids:
                    previous=output/'.items'/(entry['id']+'.json')
                    if previous.is_file():item.update(json.loads(previous.read_text()))
                    item['status']='skipped'
                else:
                    item.update(attempt(lambda c:download_item(platform,entry,output,c),config,platform,item['attempts'],'DOWNLOAD'))
                    if item['status']=='downloaded':
                        save(receipt,item)
                        save(receipts/(hashlib.sha256(item['id'].encode()).hexdigest()+'.json'),item)
            except BlockingIOError:item.update(status='failed',errorCode='VIDEO_BUSY')
            except Exception as error:item.update(status='failed',errorCode=classify(error))
        report['results'].append(item);save(report_path,report);on_item(item)
        if item.get('errorCode') in STOP:
            report['errorCode']=item['errorCode'];break
    counts={name:sum(x['status']==name for x in report['results']) for name in ('downloaded','skipped','failed','filtered-duration')}
    counts['unattempted']=None if report['discovered'] is None else max(0,report['discovered']-len(report['results']))
    report.update(counts=counts,finishedAt=int(time.time()*1000))
    public=[a for a in report['attempts']+sum((i.get('attempts',[]) for i in report['results']),[]) if a['mode']=='PUBLIC']
    rates=any(a.get('reasonCode')=='RATE_LIMITED' for a in public)
    passed=any(a.get('result')=='PASSED' and a.get('stage')=='DOWNLOAD' for a in public)
    failures=[a.get('reasonCode') for a in public if a.get('result')=='FAILED']
    report['publicDownload']=dict(status='COOLDOWN' if rates else 'PASSED' if passed else 'FAILED' if failures else 'NOT_TESTED',
        reasonCode='RATE_LIMITED' if rates else None if passed else next(iter(failures),None),observedAt=int(time.time()*1000),retryAfterSeconds=1800 if rates else None)
    if rates:
        gate=Path(os.environ.get('HM_SERVER_STATE_DIR',output))/'public-cooldown';gate.mkdir(exist_ok=True,parents=True)
        save(gate/(platform+'.json'),{'until':int(time.time())+1800})
    save(report_path,report);return report


def video_record(item):
    if item['status']=='filtered-duration':return None
    path=Path(item['path']) if item.get('path') else None
    good=item['status'] in ('downloaded','skipped') and path and path.is_file()
    if item['status']=='skipped' and not good:return None
    digest=None
    if good:
        hasher=hashlib.sha256()
        with path.open('rb') as media:
            for chunk in iter(lambda:media.read(1024*1024),b''):hasher.update(chunk)
        digest=hasher.hexdigest()
    published=None
    if item.get('upload_date'):
        try:published=dt.datetime.strptime(item['upload_date'],'%Y%m%d').strftime('%Y-%m-%dT00:00:00')
        except ValueError:pass
    return dict(platformVideoId=item['id'],originalUrl=item['url'],canonicalUrl=item['url'],title=item.get('title') or item['id'],
        titleSource='ORIGINAL' if item.get('title') else 'NONE',titleStatus='AVAILABLE' if item.get('title') else 'PENDING',
        localPath=str(path) if good else None,fileName=path.name if good else None,fileSize=path.stat().st_size if good else None,sha256=digest,
        durationSeconds=round(item['duration']) if item.get('duration') else None,publishedAt=published,
        status='downloaded' if good else 'download-failed',errorCode=None if good else 'PUBLIC_'+item.get('errorCode','DOWNLOAD_FAILED'),
        error=None if good else MESSAGES.get(item.get('errorCode'),MESSAGES['DOWNLOAD_FAILED']),attempts=item.get('attempts',[]))


def log_text(report):
    lines=['下载策略：公开优先，明确需要登录时使用可用账号补试一次。']
    for a in report['attempts']:
        lines.append('发现列表 · '+('匿名' if a['mode']=='PUBLIC' else '账号补试')+' · '+a['result']+' '+MESSAGES.get(a.get('reasonCode'),''))
    for item in report['results']:
        lines.append(item['id']+' · '+item['status']+' '+MESSAGES.get(item.get('errorCode'),''))
        for a in item.get('attempts',[]):lines.append('  '+('匿名' if a['mode']=='PUBLIC' else '账号补试')+' · '+a['result']+' '+MESSAGES.get(a.get('reasonCode'),''))
    if report.get('errorCode'):lines.append(MESSAGES.get(report['errorCode'],MESSAGES['DOWNLOAD_FAILED']))
    c=report['counts'];lines.append(f"成功 {c['downloaded']}；跳过 {c['skipped']}；失败 {c['failed']}；未尝试 "+('未知（未能枚举）' if c['unattempted'] is None else str(c['unattempted'])))
    return '\n'.join(lines)


def execute(args,job):
    platform=job.get('platform','Facebook');execution=str(job['executionId'])
    tenant=current_tenant()
    config=json.loads(Path(os.environ['HM_TENANT_CONFIG']).read_text())[tenant]
    if config.get('capturePolicy')!='PUBLIC_FIRST':raise ingest.PipelineError('Capture policy mismatch')
    folder=args.state_dir.expanduser().resolve()/ingest.state_segment(execution);folder.mkdir(parents=True,exist_ok=True)
    output=Path(os.environ['FACEBOOK_FOLLOWED_OUTPUT'])/platform
    backend=ingest.normalize_backend(args.backend)
    pump=ingest.HeartbeatPump(backend,args.worker_token,args.worker_id,execution,args.heartbeat_seconds)
    def record(item):
        video=video_record(item)
        if video:ingest.record_video(backend,args.worker_token,args.worker_id,execution,video,download_status='DOWNLOADED' if video['status']=='downloaded' else 'DOWNLOAD_FAILED',upload_status='PENDING')
    try:
        ingest.heartbeat(backend,args.worker_token,args.worker_id,execution,5);pump.start()
        report=run_download(platform,job['sourceUrl'],args.count,output,folder/'public-download.json',config,record)
        for item in report['results']:record(item)
        failed=report.get('errorCode') or report['counts']['failed']
        state='PARTIAL' if failed and (report['counts']['downloaded'] or report['counts']['skipped']) else 'FAILED' if failed else 'COMPLETED'
        report.update(status=state,executionId=execution,skill='public-first-capture')
        code=report.get('errorCode') or next((x.get('errorCode') for x in report['results'] if x['status']=='failed'),None)
        log=log_text(report);save(folder/'result.json',report);(folder/'worker.log').write_text(log)
        ingest.complete(backend,args.worker_token,args.worker_id,execution,state,report,log,'PUBLIC_'+code if code else None,MESSAGES.get(code) if code else None)
        return 0,report
    finally:pump.stop()
