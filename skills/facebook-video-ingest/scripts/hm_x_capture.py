"""Native X posts and bounded, authorized profile media discovery."""
import itertools
import re
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit

HOSTS = {'x.com', 'www.x.com', 'twitter.com', 'www.twitter.com', 'mobile.twitter.com'}
POST = re.compile(r'/(?:[A-Za-z0-9_]{1,15}|i/web)/status/([0-9]{1,20})(?:/video/[1-9][0-9]?)?/?')
PROFILE = re.compile(r'/([A-Za-z0-9_]{1,15})(?:/media)?/?')
RESERVED = {'i','home','explore','settings','messages','search','notifications','compose','login','logout','signup','tos','privacy','account','accounts','hashtag','share'}


def source(url):
    import hm_public_capture as public
    try:
        parsed = urlsplit(url.strip())
        match = POST.fullmatch(parsed.path)
        if parsed.scheme != 'https' or parsed.netloc not in HOSTS:
            raise ValueError('Invalid X post')
        if match:return {'id': match[1], 'url': 'https://x.com' + parsed.path.rstrip('/')}
        profile = PROFILE.fullmatch(parsed.path)
        if profile and profile[1].lower() not in RESERVED:
            return {'kind':'profile', 'handle':profile[1].lower(), 'url':'https://x.com/'+profile[1].lower()+'/media'}
        raise ValueError('Invalid X source')
    except (ValueError, AttributeError):
        raise public.Failure('UNSUPPORTED_CONTENT') from None


def videos(info):
    import hm_public_capture as public
    rows = list(itertools.islice(info.get('entries', []), 11)) if info.get('_type') == 'playlist' else [info]
    if not rows or len(rows) > 10:
        raise public.Failure('DISCOVERY_INCOMPLETE')
    for row in rows:
        if not row or not re.fullmatch(r'[0-9]{1,20}', str(row.get('id', ''))) or not row.get('formats'):
            raise public.Failure('UNSUPPORTED_CONTENT')
        if row.get('is_live') or row.get('live_status') in ('is_live', 'is_upcoming'):
            raise public.Failure('UNSUPPORTED_LIVE')
    return rows


def select_info(info, media_id):
    import hm_public_capture as public
    for row in videos(info):
        if str(row['id']) == str(media_id):
            return row
    raise public.Failure('EXTRACTION_ERROR')


def discover_profile(url, limit, profile):
    import hm_public_capture as public
    if not profile:raise public.Failure('LOGIN_REQUIRED')
    helper=Path(__file__).with_name('hm_x_profile_discovery.js')
    proc=subprocess.run(['node',str(helper),source(url)['url'],str(min(limit,10)),str(profile)],
        env=public.clean_environment(os.environ),capture_output=True,text=True,timeout=140)
    lines=[line for line in proc.stdout.splitlines() if line.startswith('HM_X_DISCOVERY ')]
    if not lines:raise public.Failure('NETWORK_ERROR')
    data=json.loads(lines[-1].split(' ',1)[1])
    entries=[];seen=set()
    for row in data.get('entries') or []:
        if not isinstance(row,dict) or not re.fullmatch(r'[0-9]{1,20}',str(row.get('id',''))):raise public.Failure('EXTRACTION_ERROR')
        post=source(row.get('url',''))
        if post.get('kind')=='profile':raise public.Failure('EXTRACTION_ERROR')
        identity=str(row['id'])
        if identity not in seen:entries.append(dict(id=identity,url=post['url']));seen.add(identity)
    code=data.get('errorCode')
    if code or not data.get('validated') or not (len(entries)>=min(limit,10) or data.get('sourceExhausted')):
        error=public.Failure(code if code in public.MESSAGES else 'DISCOVERY_INCOMPLETE');error.entries=entries;raise error
    return entries[:min(limit,10)]


def profile_check(job, config):
    import hm_public_capture as public
    import hm_x_account as accounts
    account=accounts.account_config(config)
    if not account:raise public.Failure('ACCOUNT_NOT_CONFIGURED')
    state=accounts.read_state(account)
    if state.get('state')!='LOGGED_IN' and not (state.get('state')=='COOLDOWN' and (state.get('nextCheckAt') or 0)<=time.time()):raise public.Failure('LOGIN_REQUIRED')
    lease=accounts.acquire_capture(account)
    if lease is None:raise public.Failure('ACCOUNT_BUSY')
    try:
        entries=discover_profile(job['sourceUrl'],10,lease.profile)
        return dict(outcome='SUCCESS',complete=True,validated=True,entries=entries,evidence='VALIDATED_LATEST_TEN',scope='LATEST_TEN')
    except public.Failure as error:
        if error.code in ('LOGIN_REQUIRED','VERIFICATION_REQUIRED','RATE_LIMITED','ACCOUNT_SUSPENDED'):
            accounts.save_result(config,'',dict(state='LOGIN_REQUIRED' if error.code=='LOGIN_REQUIRED' else 'COOLDOWN' if error.code=='RATE_LIMITED' else 'VERIFICATION_REQUIRED',reasonCode='X_'+error.code))
        raise
    finally:lease.close()


def local_profile_check(job):
    """Resolve public native media IDs from an ordered local-browser handoff."""
    import hm_public_capture as public
    feed=source(job['sourceUrl']);payload=job.get('localDiscovery') or {}
    urls=payload.get('urls')
    if feed.get('kind')!='profile' or payload.get('readFromTop') is not True or not isinstance(urls,list) or len(urls)>10:
        raise public.Failure('DISCOVERY_INCOMPLETE')
    previous=None;posts=set();entries=[];media=set()
    for url in urls:
        post=source(url)
        if post.get('kind') or url.split('/')[3].lower()!=feed['handle'] or post['id'] in posts or previous is not None and int(post['id'])>=previous:
            raise public.Failure('EXTRACTION_ERROR')
        posts.add(post['id']);previous=int(post['id'])
        try:resolved=discover(post['url'],10)
        except Exception as error:
            failure=public.Failure(public.classify(error));failure.entries=entries;raise failure from None
        for row in resolved:
            if row['id'] not in media:
                media.add(row['id']);entries.append(row)
            if len(entries)==10:
                return dict(outcome='SUCCESS',complete=True,validated=True,entries=entries,
                    evidence='VALIDATED_LATEST_TEN',scope='LATEST_TEN',discoveryMode='LOCAL_CHROME')
    failure=public.Failure('DISCOVERY_INCOMPLETE');failure.entries=entries;raise failure


def discover(url, limit, profile=None):
    import hm_public_capture as public
    import yt_dlp
    post = source(url)
    if post.get('kind')=='profile':return discover_profile(url,limit,profile)
    with yt_dlp.YoutubeDL(dict(public.common_options({}), skip_download=True, noplaylist=True)) as ydl:
        info = ydl.extract_info(re.sub(r'/video/[0-9]+$', '', post['url']), download=False)
    if not info:
        raise public.Failure('EXTRACTION_ERROR')
    rows = videos(info)
    selector=re.search(r'/video/([0-9]+)$',post['url'])
    if selector:
        index=int(selector[1])-1
        if index>=len(rows):raise public.Failure('UNSUPPORTED_CONTENT')
        rows=[rows[index]]
    if len(rows) > limit:
        raise public.Failure('DISCOVERY_INCOMPLETE')
    result={}
    for row in rows:
        handle=row.get('uploader_id') or info.get('uploader_id') or post['url'].split('/')[3]
        if handle in ('i','web'):handle=None
        title=row.get('description') or info.get('description') or row.get('title') or info.get('title')
        result[str(row['id'])]=dict(id=str(row['id']),url=post['url'],sourceName=row.get('uploader') or info.get('uploader') or handle,sourceHandle=handle,title=title or '未获取标题',titleSource='POST_TEXT' if title else 'NONE',titleStatus='AVAILABLE' if title else 'NO_TEXT')
    return list(result.values())
