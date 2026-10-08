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


def page_result(entries, *, exhausted, validated, cursor, frontier, limit=PAGE):
    """A quota or familiar/pinned videos cannot prove the list is complete."""
    complete=bool(validated and exhausted)
    return dict(outcome='SUCCESS' if complete else 'INCOMPLETE',complete=complete,
        validated=bool(validated),entries=entries[:limit],cursor=cursor,
        evidence='SOURCE_EXHAUSTED' if exhausted else 'CONTINUATION_REQUIRED')


def check(job, credentials):
    if job['platform']=='X':
        from hm_x_capture import discover
        entries=discover(job['sourceUrl'],10)
        return dict(outcome='SUCCESS',complete=True,validated=True,entries=entries,
            evidence='VALIDATED_LATEST_TEN' if job.get('latestTen') else 'SINGLE_VIDEO_METADATA',
            scope='LATEST_TEN' if job.get('latestTen') else 'HISTORY')
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
        return dict(outcome='SUCCESS',complete=True,validated=True,entries=[single],
                    evidence='VALIDATED_LATEST_TEN' if job.get('latestTen') else 'SINGLE_VIDEO_METADATA',
                    scope='LATEST_TEN' if job.get('latestTen') else 'HISTORY')
    if job.get('latestTen'):
        # Only the provider's first ordered feed page can establish the current
        # top ten. Never resume an old cursor or infer emptiness from a short page.
        first=dict(job,latestTen=False,cursor={},knownVideoIds=[],pageLimit=10)
        page=check(first,credentials)
        if not page.get('validated') or not (len(page.get('entries') or []) >= 10 or page.get('complete')):
            raise public.Failure('DISCOVERY_INCOMPLETE')
        seen=set();latest=[]
        for item in page['entries']:
            if item['id'] not in seen:
                seen.add(item['id']);latest.append(item)
            if len(latest)==10:break
        if len(latest)<10 and not page.get('complete'):
            raise public.Failure('DISCOVERY_INCOMPLETE')
        return dict(outcome='SUCCESS',complete=True,validated=True,entries=latest,
                    evidence='VALIDATED_LATEST_TEN',scope='LATEST_TEN')
    cursor=job.get('cursor') or {}
    frontier=set(cursor.get('frontier',job.get('knownVideoIds') or []))
    limit=int(job.get('pageLimit') or PAGE)
    if job['platform']=='YouTube':
        return youtube(job['sourceUrl'],cursor,frontier,credentials,limit)
    if job['platform']=='TikTok':
        return tiktok(job['sourceUrl'],cursor,frontier,limit)
    return facebook(job['sourceUrl'],cursor,frontier,credentials,limit)


def youtube(url,cursor,frontier,credentials,limit=PAGE):
    url=public.normalize_url(url)[0]
    import yt_dlp
    offset=int(cursor.get('offset',0))
    with yt_dlp.YoutubeDL(dict(public.common_options(credentials),extract_flat='in_playlist',skip_download=True,
            playliststart=offset+1,playlistend=offset+limit+1,lazy_playlist=True)) as ydl:
        data=ydl.extract_info(url,download=False)
        if not data or data.get('entries') is None:raise public.Failure('EXTRACTION_ERROR')
        rows=list(itertools.islice(data['entries'],limit+1))
    entries=[]
    for item in rows:
        if not item or not re.fullmatch(r'[\w-]{11}',str(item.get('id',''))):raise public.Failure('EXTRACTION_ERROR')
        entries.append(dict(id=item['id'],url='https://www.youtube.com/watch?v='+item['id']))
    # The extra row is a lookahead and is fetched again on the next page.
    return page_result(entries[:limit],exhausted=len(rows)<=limit,validated=bool(data.get('id')),
        cursor=dict(offset=offset+limit,frontier=sorted(frontier)),frontier=frontier,limit=limit)


def facebook(url,cursor,frontier,credentials,limit=PAGE):
    if cursor.get('buffer'):
        entries=cursor['buffer'];saved=dict(cursor,buffer=entries[limit:])
        return page_result(entries[:limit],exhausted=bool(cursor.get('sourceExhausted')) and len(entries)<=limit,
            validated=True,cursor=saved,frontier=set(),limit=limit)
    profile=credentials.get('cookiesfrombrowser',('',None))[1] or ''
    helper=Path(public.__file__).with_name('hm_public_facebook_discovery.js')
    proc=subprocess.run(['node',str(helper),url,str(limit),profile,'--scroll-rounds','2' if limit==10 else '20',
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
    return page_result(entries[:limit],exhausted=bool(data.get('sourceExhausted')) and len(entries)<=limit,
        validated=True,cursor=dict(facebook=data['cursor'],buffer=entries[limit:],sourceExhausted=bool(data.get('sourceExhausted'))),frontier=set(),limit=limit)


def tiktok(url,cursor,frontier,limit=PAGE):
    import hm_tiktok_capture as tt
    import yt_dlp
    offset=int(cursor.get('offset',0));source=tt.source(url);engine=tt.provider()
    try:
        if cursor.get('browser'):raise tt.downloader.Failure('PROFILE_ID_UNAVAILABLE')
        def extract():
            with yt_dlp.YoutubeDL(dict(engine.options(),extract_flat='in_playlist',skip_download=True,
                    playliststart=offset+1,playlistend=offset+limit+1,lazy_playlist=True)) as ydl:
                data=ydl.extract_info(url,download=False)
                if not data or data.get('entries') is None:raise tt.downloader.Failure('EXTRACTION_ERROR')
                return list(itertools.islice(data['entries'],limit+1))
        rows=tt.downloader.network_call(extract)
        entries=[tt.downloader.entry_source(item,source) for item in rows]
        return page_result(entries[:limit],exhausted=len(rows)<=limit,validated=True,
            cursor=dict(offset=offset+limit,frontier=sorted(frontier)),frontier=frontier,limit=limit)
    except tt.downloader.Failure as error:
        if error.code not in ('PROFILE_ID_UNAVAILABLE','EXTRACTION_ERROR'):raise
    if limit==10:
        raise public.Failure('DISCOVERY_INCOMPLETE')
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
