"""Bounded anonymous Chromium discovery. Never reads an existing browser profile."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import urlparse, parse_qs

class BrowserFailure(Exception):
    def __init__(self, code, evidence=None):
        self.code = code
        self.evidence = evidence or {}
        super().__init__(code)


def snapshot_expression(account):
    # Only canonical links and bounded status signals leave the page context.
    return r'''(() => {
      const account = ACCOUNT.toLowerCase(), entries = new Map();
      const add = raw => { try {
        const u = new URL(raw, location.href);
        const m = u.pathname.match(/^\/@([^/]+)\/(video|photo)\/(\d+)\/?$/);
        if (u.hostname === 'www.tiktok.com' && m && m[1].toLowerCase() === account)
          entries.set(m[3], {id:m[3],url:'https://www.tiktok.com'+u.pathname.replace(/\/$/,'')});
      } catch {} };
      for (const a of document.querySelectorAll('a[href]')) add(a.href);
      let visited = 0, privateAccount = false, secUid = null;
      const walk = value => {
        if (!value || typeof value !== 'object' || ++visited > 50000) return;
        if (typeof value.uniqueId === 'string' && value.uniqueId.toLowerCase() === account && typeof value.secUid === 'string') secUid = value.secUid;
        const author = value.author?.uniqueId || value.author?.unique_id;
        if (typeof author === 'string' && author.toLowerCase() === account && /^\d+$/.test(String(value.id)) && value.video)
          add('https://www.tiktok.com/@'+author+'/video/'+value.id);
        for (const child of Object.values(value)) if (typeof child === 'object') walk(child);
      };
      for (const id of ['__UNIVERSAL_DATA_FOR_REHYDRATION__','SIGI_STATE']) {
        try {
          const data = JSON.parse(document.getElementById(id)?.textContent || '{}');
          if (data.__DEFAULT_SCOPE__?.['webapp.user-detail']?.statusCode === 10222) privateAccount = true;
          walk(data);
        } catch {}
      }
      const visible = el => !!(el && el.getBoundingClientRect().width && el.getBoundingClientRect().height && getComputedStyle(el).visibility !== 'hidden');
      const challenge = [...document.querySelectorAll('[id*="captcha"], [class*="captcha-verify"], iframe[src*="captcha"]')].some(visible);
      const text = (document.body?.innerText || '').slice(0,30000);
      return {entries:[...entries.values()].slice(0,1000),
        challenge:challenge || /verify (?:that )?you(?:'re| are) human|drag the slider|complete the puzzle|security verification/i.test(text),
        login:location.pathname.startsWith('/login') || privateAccount || (!entries.size && /log in to (?:continue|view this|see this)/i.test(text)),
        unavailable:!entries.size && /couldn.t find this account|account not found|access denied/i.test(text),
        host:location.hostname, ready:document.readyState, secUid};
    })()'''.replace('ACCOUNT', json.dumps(account), 1)


def pagination_page(payload, response_url, account, sec_uid):
    """Accept only the current profile's own post-list pagination, not recommendations."""
    url = urlparse(response_url)
    query = parse_qs(url.query)
    if url.hostname not in ('www.tiktok.com', 'tiktok.com') or url.path != '/api/post/item_list/': return None
    if not sec_uid or query.get('secUid') != [sec_uid]: return None
    if payload.get('statusCode') != 0 or not isinstance(payload.get('itemList'), list): return None
    more = payload.get('hasMore')
    if type(more) not in (bool, int) or more not in (False, True, 0, 1): return None
    entries = []
    for item in payload['itemList']:
        author = item.get('author', {}).get('uniqueId', '')
        identity = str(item.get('id', ''))
        if author.lower() != account.lower() or not identity.isdigit() or not item.get('video'): return None
        entries.append(dict(id=identity, url='https://www.tiktok.com/@'+author+'/video/'+identity))
    cursor = str(payload.get('cursor', ''))
    start = query.get('cursor', ['0'])[0]
    if not cursor.isdigit() or not start.isdigit(): return None
    return dict(start=start, cursor=cursor, entries=entries, exhausted=not bool(more))


def chromium_binary():
    candidates = [os.getenv('TIKTOK_CHROMIUM_BIN'), shutil.which('chromium'), shutil.which('chromium-browser'),
                  shutil.which('google-chrome'), '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome']
    for name in candidates:
        if name and Path(name).is_file(): return name
    raise BrowserFailure('BROWSER_UNAVAILABLE')


class Chromium:
    def __init__(self, temp_root, deadline):
        self.temp_root, self.deadline = temp_root, deadline
        self.process = self.ws = self.profile = None
        self.serial = 0
        self.document_status = None
        self.rate_limited = False
        self.post_responses = {}
        self.post_pages = {}
        self.post_entries = {}
        self.post_template = None
    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0: raise BrowserFailure('DISCOVERY_INCOMPLETE')
        return max(.1, min(5, remaining))
    def __enter__(self):
        try:
            import websocket
            binary = chromium_binary()
            self.profile = tempfile.mkdtemp(prefix='tiktok-anonymous-', dir=self.temp_root)
            args = [binary, '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
                    '--disable-extensions', '--disable-background-networking', '--lang=en-US',
                    '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
                    '--user-data-dir='+self.profile, 'about:blank']
            if os.getenv('TIKTOK_CHROMIUM_NO_SANDBOX') == '1': args.insert(1, '--no-sandbox')
            self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, start_new_session=os.getenv("HM_PIPELINE_DEFER_MEDIA") != "1")
            active = Path(self.profile) / 'DevToolsActivePort'
            while not active.exists():
                self.remaining()
                if self.process.poll() is not None: raise BrowserFailure('BROWSER_UNAVAILABLE')
                time.sleep(.1)
            port = int(active.read_text().splitlines()[0])
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open('http://127.0.0.1:'+str(port)+'/json/list', timeout=self.remaining()) as response:
                pages = json.load(response)
            target = next(x['webSocketDebuggerUrl'] for x in pages if x.get('type') == 'page')
            self.ws = websocket.create_connection(target, timeout=self.remaining(), suppress_origin=True,
                                                  http_no_proxy=['localhost','127.0.0.1'])
            self.call('Page.enable'); self.call('Network.enable')
            return self
        except BrowserFailure:
            self.__exit__(None, None, None); raise
        except Exception:
            self.__exit__(None, None, None); raise BrowserFailure('BROWSER_UNAVAILABLE') from None
    def __exit__(self, *args):
        if self.ws:
            try: self.ws.close()
            except Exception: pass
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try: self.process.wait(timeout=3)
            except subprocess.TimeoutExpired: self.process.kill(); self.process.wait()
        if self.profile: shutil.rmtree(self.profile, ignore_errors=True)
    def call(self, method, params=None):
        self.serial += 1; request_id = self.serial
        self.ws.settimeout(self.remaining())
        self.ws.send(json.dumps({'id':request_id,'method':method,'params':params or {}}))
        while True:
            self.ws.settimeout(self.remaining())
            value = json.loads(self.ws.recv())
            if value.get('method') == 'Network.responseReceived':
                event = value.get('params', {}); response = event.get('response', {})
                host = urlparse(response.get('url','')).hostname or ''
                if host == 'tiktok.com' or host.endswith('.tiktok.com'):
                    if event.get('type') == 'Document': self.document_status = response.get('status')
                    if response.get('status') == 429: self.rate_limited = True
                    if urlparse(response.get('url','')).path == '/api/post/item_list/' and response.get('status') == 200:
                        self.post_responses[event['requestId']] = response['url']
            if value.get('id') == request_id:
                if 'error' in value: raise BrowserFailure('EXTRACTION_ERROR')
                return value.get('result', {})
    def snapshot(self, account):
        result = self.call('Runtime.evaluate', {'expression':snapshot_expression(account),'returnByValue':True})
        if result.get('exceptionDetails'): raise BrowserFailure('EXTRACTION_ERROR')
        snapshot = result.get('result', {}).get('value') or {}
        for request_id, response_url in list(self.post_responses.items()):
            try:
                response = self.call('Network.getResponseBody', {'requestId':request_id})
                if response.get('base64Encoded'): continue
                page = pagination_page(json.loads(response['body']), response_url, account, snapshot.get('secUid'))
                if page:
                    self.post_template = response_url
                    self.post_pages[page['start']] = page
                    for entry in page['entries']: self.post_entries[entry['id']] = entry
                del self.post_responses[request_id]
            except (BrowserFailure, ValueError, KeyError): pass
        # A terminal page only proves exhaustion after every preceding cursor
        # from the initial page was observed. Missing network pages stay incomplete.
        cursor = '0'; visited = set(); exhausted = False
        while cursor in self.post_pages and cursor not in visited:
            visited.add(cursor); page = self.post_pages[cursor]
            if page['exhausted']: exhausted = True; break
            cursor = page['cursor']
        snapshot['entries'] = list({e['id']:e for e in snapshot.get('entries', [])+list(self.post_entries.values())}.values())
        snapshot['sourceExhausted'] = exhausted
        return snapshot
    def fetch_page(self, account, cursor, sec_uid):
        # A fresh, authorized anonymous page supplies the request template. Only
        # the public pagination cursor is persisted, never cookies or signatures.
        if not self.post_template or not str(cursor).isdigit(): raise BrowserFailure('DISCOVERY_INCOMPLETE')
        expression = r"""(async () => {
          const u = new URL(TEMPLATE); u.searchParams.set('cursor', CURSOR);
          const response = await fetch(u.href, {credentials:'same-origin'});
          return {status:response.status, url:u.href, body:await response.text()};
        })()""".replace('TEMPLATE',json.dumps(self.post_template),1).replace('CURSOR',json.dumps(str(cursor)),1)
        result=self.call('Runtime.evaluate',{'expression':expression,'awaitPromise':True,'returnByValue':True})
        value=result.get('result',{}).get('value') or {}
        if value.get('status') == 429: raise BrowserFailure('RATE_LIMITED')
        if value.get('status') != 200: raise BrowserFailure('NETWORK_ERROR')
        try: page=pagination_page(json.loads(value['body']),value['url'],account,sec_uid)
        except (KeyError, ValueError): page=None
        if not page or page['start'] != str(cursor): raise BrowserFailure('DISCOVERY_INCOMPLETE')
        return page

    def scroll(self):
        self.call('Runtime.evaluate', {'expression':'window.scrollBy(0, Math.max(innerHeight, 800))'})


def discover(source, limit, temp_root, browser_factory=Chromium, timeout=90, pause=2):
    deadline = time.monotonic() + min(timeout, 90)
    evidence = {'method':'ANONYMOUS_CHROMIUM','httpStatus':None,'scrolls':0,'challengeVisible':False,'loginRequired':False}
    entries = {}; stagnant = 0
    try:
        with browser_factory(temp_root, deadline) as browser:
            navigation = browser.call('Page.navigate', {'url':source['url']})
            if navigation.get('errorText'): raise BrowserFailure('NETWORK_ERROR', evidence)
            for step in range(11):
                remaining = deadline-time.monotonic()
                if remaining <= 0: break
                time.sleep(min(pause, remaining))
                snapshot = browser.snapshot(source['account'])
                evidence.update(httpStatus=browser.document_status, challengeVisible=bool(snapshot.get('challenge')),
                                loginRequired=bool(snapshot.get('login')), scrolls=step)
                if browser.rate_limited: raise BrowserFailure('RATE_LIMITED', evidence)
                if snapshot.get('challenge'): raise BrowserFailure('VERIFICATION_REQUIRED', evidence)
                if snapshot.get('login'): raise BrowserFailure('LOGIN_REQUIRED', evidence)
                if snapshot.get('unavailable') or browser.document_status in (401,403,404): raise BrowserFailure('ACCESS_DENIED', evidence)
                if snapshot.get('host') not in ('www.tiktok.com','tiktok.com'): raise BrowserFailure('EXTRACTION_ERROR', evidence)
                before = len(entries)
                for item in snapshot.get('entries', []):
                    # Parse the canonical URL locally: importing "download"
                    # may resolve to the YouTube provider in the shared Worker.
                    try:
                        parsed = urlparse(item['url'])
                        import re
                        match = re.fullmatch(r'/@([^/]+)/(video|photo)/(\d+)/?', parsed.path)
                        if parsed.hostname not in ('www.tiktok.com', 'tiktok.com') or not match: continue
                        link = dict(account=match[1],kind=match[2],id=match[3],url='https://www.tiktok.com'+parsed.path.rstrip('/'))
                        if link['kind'] in ('video','photo') and link['account'].lower() == source['account'].lower():
                            entries[link['id']] = {'id':link['id'],'url':link['url']}
                    except (ValueError, KeyError, TypeError): continue
                    if len(entries) >= limit: break
                if snapshot.get('sourceExhausted') and len(entries) < limit:
                    return {'entries':list(entries.values()),'complete':True,'sourceExhausted':True,'emptyConfirmed':not entries,'evidence':evidence}
                if len(entries) >= limit:
                    return {'entries':list(entries.values())[:limit], 'complete':True, 'evidence':evidence}
                stagnant = stagnant + 1 if len(entries) == before else 0
                if stagnant >= 3 or step == 10: break
                browser.scroll()
    except BrowserFailure as error:
        if error.code != 'DISCOVERY_INCOMPLETE':
            if not error.evidence: error.evidence = evidence
            raise
    except Exception:
        raise BrowserFailure('EXTRACTION_ERROR', evidence) from None
    if not entries: raise BrowserFailure('DISCOVERY_EMPTY', evidence)
    return {'entries':list(entries.values())[:limit], 'complete':False, 'evidence':evidence}


def discover_page(source, cursor, temp_root, browser_factory=Chromium, timeout=90, pause=2):
    """One validated provider page; resumes beyond any UI list/scroll quota."""
    start=str((cursor or {}).get('providerCursor','0'))
    if not start.isdigit(): raise BrowserFailure('DISCOVERY_INCOMPLETE')
    deadline=time.monotonic()+min(timeout,90)
    with browser_factory(temp_root,deadline) as browser:
        navigation=browser.call('Page.navigate',{'url':source['url']})
        if navigation.get('errorText'): raise BrowserFailure('NETWORK_ERROR')
        for step in range(11):
            remaining=deadline-time.monotonic()
            if remaining<=0: break
            time.sleep(min(pause,remaining));snapshot=browser.snapshot(source['account'])
            if browser.rate_limited: raise BrowserFailure('RATE_LIMITED')
            if snapshot.get('challenge'): raise BrowserFailure('VERIFICATION_REQUIRED')
            if snapshot.get('login'): raise BrowserFailure('LOGIN_REQUIRED')
            if snapshot.get('unavailable') or browser.document_status in (401,403,404): raise BrowserFailure('ACCESS_DENIED')
            if snapshot.get('host') not in ('www.tiktok.com','tiktok.com'): raise BrowserFailure('EXTRACTION_ERROR')
            if browser.post_template and snapshot.get('secUid'):
                page=browser.post_pages.get(start)
                if page is None: page=browser.fetch_page(source['account'],start,snapshot['secUid'])
                if not page['exhausted'] and page['cursor']==start: raise BrowserFailure('DISCOVERY_INCOMPLETE')
                return dict(entries=page['entries'],sourceExhausted=page['exhausted'],
                    emptyConfirmed=page['exhausted'] and not page['entries'],providerCursor=page['cursor'])
            browser.scroll()
    raise BrowserFailure('DISCOVERY_INCOMPLETE')
