"""Bounded anonymous Chromium discovery. Never reads an existing browser profile."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import urlparse

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
      let visited = 0, privateAccount = false;
      const walk = value => {
        if (!value || typeof value !== 'object' || ++visited > 50000) return;
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
        host:location.hostname, ready:document.readyState};
    })()'''.replace('ACCOUNT', json.dumps(account), 1)


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
                                            stderr=subprocess.DEVNULL, start_new_session=True)
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
            if value.get('id') == request_id:
                if 'error' in value: raise BrowserFailure('EXTRACTION_ERROR')
                return value.get('result', {})
    def snapshot(self, account):
        result = self.call('Runtime.evaluate', {'expression':snapshot_expression(account),'returnByValue':True})
        if result.get('exceptionDetails'): raise BrowserFailure('EXTRACTION_ERROR')
        return result.get('result', {}).get('value') or {}
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
                    from download import normalize_url
                    try:
                        link = normalize_url(item['url'])
                        if link['kind'] in ('video','photo') and link['account'].lower() == source['account'].lower():
                            entries[link['id']] = {'id':link['id'],'url':link['url']}
                    except (ValueError, KeyError, TypeError): continue
                    if len(entries) >= limit: break
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
