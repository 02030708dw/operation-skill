"""Anonymous native X video posts, including multiple videos in one post."""
import itertools
import re
from urllib.parse import urlsplit

HOSTS = {'x.com', 'www.x.com', 'twitter.com', 'www.twitter.com', 'mobile.twitter.com'}
POST = re.compile(r'/(?:[A-Za-z0-9_]{1,15}|i/web)/status/([0-9]{1,20})(?:/video/[1-9][0-9]?)?/?')


def source(url):
    import hm_public_capture as public
    try:
        parsed = urlsplit(url.strip())
        match = POST.fullmatch(parsed.path)
        if parsed.scheme != 'https' or parsed.netloc not in HOSTS or not match:
            raise ValueError('Invalid X post')
        return {'id': match[1], 'url': 'https://x.com' + parsed.path.rstrip('/')}
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


def discover(url, limit):
    import hm_public_capture as public
    import yt_dlp
    post = source(url)
    with yt_dlp.YoutubeDL(dict(public.common_options({}), skip_download=True, noplaylist=True)) as ydl:
        info = ydl.extract_info(post['url'], download=False)
    if not info:
        raise public.Failure('EXTRACTION_ERROR')
    rows = videos(info)
    if len(rows) > limit:
        raise public.Failure('DISCOVERY_INCOMPLETE')
    return list({str(row['id']): {'id': str(row['id']), 'url': post['url']} for row in rows}.values())
