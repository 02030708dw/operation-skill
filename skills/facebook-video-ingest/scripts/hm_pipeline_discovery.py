"""Bounded discovery pages with explicit completion evidence (never size == success)."""
from __future__ import annotations
import itertools
import json
import os
from pathlib import Path
import re
import subprocess

import hm_public_capture as public

PAGE = 50


def page_result(entries, *, exhausted, validated, cursor, frontier):
    """A quota or familiar/pinned videos cannot prove the list is complete."""
    complete=bool(validated and exhausted)
    return dict(outcome='SUCCESS' if complete else 'INCOMPLETE',complete=complete,
        validated=bool(validated),entries=entries[:PAGE],cursor=cursor,
        evidence='SOURCE_EXHAUSTED' if exhausted else 'CONTINUATION_REQUIRED')


def check(job, credentials):
    single=public.single_source(job['platform'],job['sourceUrl'])
    if single:
        # Metadata inspection confirms accessibility; a syntactically valid URL alone isn't a check.
        if job['platform']=='TikTok':
            from hm_tiktok_capture import provider, source
            provider().inspect(source(single['url']))
        else:
            import yt_dlp
            with yt_dlp.YoutubeDL(dict(public.common_options(credentials),skip_download=True,noplaylist=True)) as ydl:
                info=ydl.extract_info(single['url'],download=False)
                if not info or not info.get('id'):raise public.Failure('EXTRACTION_ERROR')
                single['id']=str(info['id'])
                if job['platform']=='Facebook':
                    if not single['id'].isdigit():raise public.Failure('EXTRACTION_ERROR')
                    single['url']='https://www.facebook.com/watch/?v='+single['id']
        return dict(outcome='SUCCESS',complete=True,validated=True,entries=[single],evidence='SINGLE_VIDEO_METADATA')
    cursor=job.get('cursor') or {}
    frontier=set(cursor.get('frontier',job.get('knownVideoIds') or []))
    if job['platform']=='YouTube':
        return youtube(job['sourceUrl'],cursor,frontier,credentials)
    if job['platform']=='TikTok':
        return tiktok(job['sourceUrl'],cursor,frontier)
    return facebook(job['sourceUrl'],cursor,frontier,credentials)


def youtube(url,cursor,frontier,credentials):
    url=public.normalize_url(url)[0]
    import yt_dlp
    offset=int(cursor.get('offset',0))
    with yt_dlp.YoutubeDL(dict(public.common_options(credentials),extract_flat='in_playlist',skip_download=True,
            playliststart=offset+1,playlistend=offset+PAGE+1,lazy_playlist=True)) as ydl:
        data=ydl.extract_info(url,download=False)
        if not data or data.get('entries') is None:raise public.Failure('EXTRACTION_ERROR')
        rows=list(itertools.islice(data['entries'],PAGE+1))
    entries=[]
    for item in rows:
        if not item or not re.fullmatch(r'[\w-]{11}',str(item.get('id',''))):raise public.Failure('EXTRACTION_ERROR')
        entries.append(dict(id=item['id'],url='https://www.youtube.com/watch?v='+item['id']))
    # The extra row is a lookahead and is fetched again on the next page.
    return page_result(entries[:PAGE],exhausted=len(rows)<=PAGE,validated=bool(data.get('id')),
        cursor=dict(offset=offset+PAGE,frontier=sorted(frontier)),frontier=frontier)


def facebook(url,cursor,frontier,credentials):
    if cursor.get('buffer'):
        entries=cursor['buffer'];saved=dict(cursor,buffer=entries[PAGE:])
        return page_result(entries[:PAGE],exhausted=bool(cursor.get('sourceExhausted')) and len(entries)<=PAGE,
            validated=True,cursor=saved,frontier=set())
    profile=credentials.get('cookiesfrombrowser',('',None))[1] or ''
    helper=Path(public.__file__).with_name('hm_public_facebook_discovery.js')
    proc=subprocess.run(['node',str(helper),url,str(PAGE),profile,'--scroll-rounds','20',
        '--pipeline-cursor',json.dumps(cursor.get('facebook') or {})],
        env=public.clean_environment(os.environ),capture_output=True,text=True,timeout=240)
    lines=[line for line in proc.stdout.splitlines() if line.startswith('HM_PUBLIC_DISCOVERY ')]
    if not lines:raise public.Failure('NETWORK_ERROR')
    data=json.loads(lines[-1].split(' ',1)[1])
    if data.get('errorCode'):
        failure=public.Failure(data['errorCode']);failure.entries=data.get('entries') or [];raise failure
    if not data.get('validated') or not isinstance(data.get('cursor'),dict):raise public.Failure('DISCOVERY_INCOMPLETE')
    entries=data.get('entries') or []
    if any(not str(entry.get('id','')).isdigit() for entry in entries):raise public.Failure('EXTRACTION_ERROR')
    return page_result(entries[:PAGE],exhausted=bool(data.get('sourceExhausted')) and len(entries)<=PAGE,
        validated=True,cursor=dict(facebook=data['cursor'],buffer=entries[PAGE:],sourceExhausted=bool(data.get('sourceExhausted'))),frontier=set())


def tiktok(url,cursor,frontier):
    import hm_tiktok_capture as tt
    import yt_dlp
    offset=int(cursor.get('offset',0));source=tt.source(url);engine=tt.provider()
    try:
        if cursor.get('browser'):raise tt.downloader.Failure('PROFILE_ID_UNAVAILABLE')
        def extract():
            with yt_dlp.YoutubeDL(dict(engine.options(),extract_flat='in_playlist',skip_download=True,
                    playliststart=offset+1,playlistend=offset+PAGE+1,lazy_playlist=True)) as ydl:
                data=ydl.extract_info(url,download=False)
                if not data or data.get('entries') is None:raise tt.downloader.Failure('EXTRACTION_ERROR')
                return list(itertools.islice(data['entries'],PAGE+1))
        rows=tt.downloader.network_call(extract)
        entries=[tt.downloader.entry_source(item,source) for item in rows]
        return page_result(entries[:PAGE],exhausted=len(rows)<=PAGE,validated=True,
            cursor=dict(offset=offset+PAGE,frontier=sorted(frontier)),frontier=frontier)
    except tt.downloader.Failure as error:
        if error.code not in ('PROFILE_ID_UNAVAILABLE','EXTRACTION_ERROR'):raise
    import browser_discovery
    if cursor.get('buffer'):
        entries=cursor['buffer'];saved=dict(cursor,buffer=entries[PAGE:])
        return page_result(entries[:PAGE],exhausted=bool(cursor.get('sourceExhausted')) and len(entries)<=PAGE,validated=True,cursor=saved,frontier=set())
    try:
        result=browser_discovery.discover_page(source,cursor,engine.browser_temp_root)
    except browser_discovery.BrowserFailure as error:
        raise public.Failure(error.code) from None
    entries=list(cursor.get('buffer',[]))+result['entries']
    saved=dict(providerCursor=result['providerCursor'],frontier=sorted(frontier),buffer=entries[PAGE:],
        browser=True,sourceExhausted=bool(result['sourceExhausted']))
    return page_result(entries[:PAGE],exhausted=bool(result['sourceExhausted']) and len(entries)<=PAGE,
        validated=True,cursor=saved,frontier=set())
