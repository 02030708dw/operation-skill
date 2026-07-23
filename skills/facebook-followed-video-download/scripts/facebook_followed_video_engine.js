#!/usr/bin/env node
/*
 * Multi-account Facebook video downloader for Hermes.
 *
 * Discovers public/exposed Facebook Reels and video links from configured
 * account pages, then downloads unseen videos into a configurable local folder.
 */

const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');
const WebSocket = require('ws');
const { spawn, spawnSync } = require('child_process');

const HOME = process.env.HOME || os.homedir() || process.cwd();
const HERMES_HOME = process.env.HERMES_HOME || path.join(HOME, '.hermes');
const DEFAULT_ACCOUNTS = path.join(HERMES_HOME, 'facebook-followed-video-download/accounts.txt');
const DEFAULT_COOKIES = process.env.FACEBOOK_FOLLOWED_COOKIES || process.env.FB_FOLLOWED_COOKIES || '';
const DEFAULT_DESKTOP = process.env.FACEBOOK_FOLLOWED_OUTPUT || process.env.FB_FOLLOWED_DESKTOP || path.join(HOME, 'Desktop', 'Facebook');
const DEFAULT_YTDLP = process.env.FACEBOOK_FOLLOWED_YTDLP || process.env.FB_FOLLOWED_YTDLP || process.env.YTDLP || 'yt-dlp';
const CDP_PORT = Number(process.env.FACEBOOK_FOLLOWED_CDP_PORT || process.env.FB_CDP_PORT || String(9300 + Math.floor(Math.random() * 500)));

function detectChrome() {
  const configured = process.env.FACEBOOK_FOLLOWED_CHROME || process.env.FB_FOLLOWED_CHROME;
  if (configured) return configured;
  const candidates = [];
  if (process.platform === 'win32') {
    for (const base of [process.env.PROGRAMFILES, process.env['PROGRAMFILES(X86)'], process.env.LOCALAPPDATA]) {
      if (base) {
        candidates.push(path.join(base, 'Google', 'Chrome', 'Application', 'chrome.exe'));
        candidates.push(path.join(base, 'Chromium', 'Application', 'chrome.exe'));
      }
    }
  } else if (process.platform === 'darwin') {
    candidates.push('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome');
    candidates.push('/Applications/Chromium.app/Contents/MacOS/Chromium');
  } else {
    candidates.push('/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser');
  }
  return candidates.find(candidate => fs.existsSync(candidate)) || (process.platform === 'win32' ? 'chrome.exe' : 'google-chrome');
}

const DEFAULT_CHROME = detectChrome();

function argValue(name, fallback = '') {
  const idx = process.argv.indexOf(name);
  return idx >= 0 && process.argv[idx + 1] ? process.argv[idx + 1] : fallback;
}

function hasFlag(name) {
  return process.argv.includes(name);
}

const mode = argValue('--mode', 'daily');
const accountsFile = argValue('--accounts', DEFAULT_ACCOUNTS);
const cookiesFile = argValue('--cookies', DEFAULT_COOKIES);
const desktopDir = argValue('--desktop', DEFAULT_DESKTOP);
const chromePath = argValue('--chrome', DEFAULT_CHROME);
const ytdlpPath = argValue('--yt-dlp', DEFAULT_YTDLP);
const dryRun = hasFlag('--dry-run');
const scrollRounds = Number(argValue('--scroll-rounds', mode === 'full' ? '80' : '8'));
const waitMs = Number(argValue('--wait-ms', '1400'));
const maxDownloads = Number(argValue('--max-downloads', mode === 'full' ? '0' : '30'));
let cdpId = 10;

function nextCdpId() {
  cdpId += 1;
  return cdpId;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function sanitizeFolderName(value) {
  return String(value || 'facebook')
    .replace(/[/:\\?%*"<>|]/g, '_')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 90) || 'facebook';
}

function stripQuery(url) {
  try {
    const parsed = new URL(url);
    if (parsed.pathname === '/watch/' && parsed.searchParams.get('v')) {
      return `https://www.facebook.com/watch/?v=${parsed.searchParams.get('v')}`;
    }
    if (parsed.pathname === '/video.php' && parsed.searchParams.get('v')) {
      return `https://www.facebook.com/video.php?v=${parsed.searchParams.get('v')}`;
    }
    if (parsed.pathname === '/profile.php' && parsed.searchParams.get('id')) {
      const root = `https://www.facebook.com/profile.php?id=${parsed.searchParams.get('id')}`;
      const sk = parsed.searchParams.get('sk');
      return sk ? `${root}&sk=${sk}` : root;
    }
    parsed.search = '';
    parsed.hash = '';
    return parsed.toString();
  } catch {
    return url.split('#')[0].split('?')[0];
  }
}

function normalizeVideoUrl(url) {
  const decoded = url.replace(/\\\//g, '/').replace(/&amp;/g, '&');
  const reel = decoded.match(/facebook\.com\/reel\/(\d+)/);
  if (reel) return `https://www.facebook.com/reel/${reel[1]}`;
  const watch = decoded.match(/facebook\.com\/watch\/\?v=(\d+)/);
  if (watch) return `https://www.facebook.com/watch/?v=${watch[1]}`;
  const videoPhp = decoded.match(/facebook\.com\/video\.php\?v=(\d+)/);
  if (videoPhp) return `https://www.facebook.com/video.php?v=${videoPhp[1]}`;
  const videos = decoded.match(/facebook\.com\/[^"' <]+\/videos\/(\d+)/);
  if (videos) return `https://www.facebook.com/watch/?v=${videos[1]}`;
  return stripQuery(decoded);
}

function videoKey(url) {
  const decoded = String(url || '').replace(/\\\//g, '/').replace(/&amp;/g, '&');
  const reel = decoded.match(/facebook\.com\/reel\/(\d+)/);
  if (reel) return reel[1];
  const watch = decoded.match(/facebook\.com\/watch\/\?v=(\d+)/);
  if (watch) return watch[1];
  const videoPhp = decoded.match(/facebook\.com\/video\.php\?v=(\d+)/);
  if (videoPhp) return videoPhp[1];
  const videos = decoded.match(/facebook\.com\/[^"' <]+\/videos\/(\d+)/);
  if (videos) return videos[1];
  const ytdlpArchive = decoded.match(/^facebook\s+(\d+)$/);
  if (ytdlpArchive) return ytdlpArchive[1];
  return decoded;
}

function normalizeAccountUrl(url) {
  const clean = stripQuery(url).replace(/\/$/, '');
  return clean;
}

function pageCandidates(accountUrl) {
  const base = normalizeAccountUrl(accountUrl);
  if (/\/reel\/\d+/.test(base) || /\/watch\/\?v=\d+/.test(base) || /\/video\.php\?v=\d+/.test(base)) {
    return [base];
  }
  if (/facebook\.com\/share\//.test(base)) {
    return [base];
  }
  if (/\/reels(?:_tab)?$/.test(base) || /[?&]sk=reels_tab\b/.test(base)) {
    const root = base.replace(/\/reels(?:_tab)?$/, '').replace(/[?&]sk=reels_tab\b/, '');
    if (/facebook\.com\/profile\.php\?id=/.test(root)) {
      return [base, `${root}&sk=videos`, root];
    }
    return [base, `${root}/videos/`, root];
  }
  if (/\/videos$/.test(base) || /[?&]sk=videos\b/.test(base)) {
    const root = base.replace(/\/videos$/, '').replace(/[?&]sk=videos\b/, '');
    if (/facebook\.com\/profile\.php\?id=/.test(root)) {
      return [base, `${root}&sk=reels_tab`, root];
    }
    return [base, `${root}/reels/`, root];
  }
  const peopleId = base.match(/facebook\.com\/people\/[^/]+\/(\d+)/);
  if (peopleId) {
    const profile = `https://www.facebook.com/profile.php?id=${peopleId[1]}`;
    return [`${profile}&sk=reels_tab`, `${profile}&sk=videos`, profile, base];
  }
  return [`${base}/reels/`, `${base}/videos/`, base];
}

function readAccounts(filePath) {
  const text = fs.readFileSync(filePath, 'utf8');
  return text.split(/\r?\n/).map(line => line.trim()).filter(line => line && !line.startsWith('#')).map(line => {
    const tab = line.split(/\t+/);
    if (tab.length >= 2) {
      return { folder: sanitizeFolderName(tab[0]), url: tab.slice(1).join('\t').trim() };
    }
    return { folder: sanitizeFolderName(line.replace(/^https?:\/\/(www\.)?facebook\.com\/?/i, '').split(/[/?#]/)[0]), url: line };
  });
}

function httpGet(pathname) {
  return new Promise((resolve, reject) => {
    http.get(`http://127.0.0.1:${CDP_PORT}${pathname}`, res => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          resolve(JSON.parse(data));
        } catch (err) {
          reject(err);
        }
      });
    }).on('error', reject);
  });
}

async function waitForChrome() {
  for (let i = 0; i < 30; i++) {
    try {
      await httpGet('/json/version');
      return;
    } catch {
      await sleep(500);
    }
  }
  throw new Error('Chrome CDP did not start');
}

async function cdpCall(ws, msg, timeoutMs = 20000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      ws.removeListener('message', handler);
      reject(new Error(`CDP timeout: ${msg.method}`));
    }, timeoutMs);
    const handler = data => {
      const parsed = JSON.parse(data);
      if (parsed.id === msg.id) {
        clearTimeout(timer);
        ws.removeListener('message', handler);
        resolve(parsed);
      }
    };
    ws.on('message', handler);
    ws.send(JSON.stringify(msg));
  });
}

async function connectTab() {
  let tabs = await httpGet('/json');
  let tab = tabs.find(candidate => candidate.type === 'page' && candidate.webSocketDebuggerUrl);
  if (!tab) {
    await httpGet('/json/new?about:blank');
    tabs = await httpGet('/json');
    tab = tabs.find(candidate => candidate.type === 'page' && candidate.webSocketDebuggerUrl);
  }
  if (!tab) {
    throw new Error('No controllable Chrome page target found');
  }
  const ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise(resolve => ws.on('open', resolve));
  await cdpCall(ws, { id: 1, method: 'Page.enable' });
  await cdpCall(ws, { id: 2, method: 'Network.enable' });
  return ws;
}

async function injectCookies(ws) {
  if (!cookiesFile) {
    return 0;
  }
  if (!fs.existsSync(cookiesFile)) {
    console.log(`Facebook cookies file not found, continuing without cookies: ${cookiesFile}`);
    return 0;
  }
  const lines = fs.readFileSync(cookiesFile, 'utf8').split(/\r?\n/).filter(line => line && !line.startsWith('#'));
  let count = 0;
  for (let i = 0; i < lines.length; i++) {
    const parts = lines[i].split('\t');
    if (parts.length < 7) continue;
    const [domain, , cookiePath, secure, , name, value] = parts;
    try {
      await cdpCall(ws, {
        id: 1000 + i,
        method: 'Network.setCookie',
        params: {
          domain: domain.startsWith('.') ? domain.slice(1) : domain,
          name,
          value,
          path: cookiePath || '/',
          secure: secure === 'TRUE',
          httpOnly: false,
          sameSite: 'Lax'
        }
      }, 5000);
      count++;
    } catch {}
  }
  return count;
}

async function discoverOnPage(ws, url) {
  console.log(`  掃描: ${url}`);
  const extractExpression = `
                (() => {
                  const found = new Set();
                  for (const a of Array.from(document.querySelectorAll('a[href]'))) {
                    const href = a.href || '';
                    if (href.includes('/reel/') || href.includes('/watch/?v=') || href.includes('/videos/') || href.includes('/video.php?v=')) found.add(href);
                  }
                  const html = document.documentElement.innerHTML;
                  for (const match of html.matchAll(/\\/reel\\/\\d+/g)) found.add(location.origin + match[0]);
                  for (const match of html.matchAll(/\\/watch\\/\\?v=\\d+/g)) found.add(location.origin + match[0]);
                  for (const match of html.matchAll(/\\/video\\.php\\?v=\\d+/g)) found.add(location.origin + match[0]);
                  return JSON.stringify(Array.from(found));
                })()
      `;
  const collected = new Set();
  let lastCount = 0;
  async function collectVisibleLinks() {
    const result = await cdpCall(ws, {
      id: nextCdpId(),
      method: 'Runtime.evaluate',
      params: {
        returnByValue: true,
        expression: extractExpression
      }
    }, 30000);
    if (result.result && result.result.exceptionDetails) {
      const details = result.result.exceptionDetails;
      throw new Error((details.exception && details.exception.description) || details.text || 'Runtime.evaluate failed');
    }
    const raw = JSON.parse(
      (result.result && result.result.result && result.result.result.value) || '[]'
    );
    for (const videoUrl of raw) collected.add(normalizeVideoUrl(videoUrl));
    if (collected.size > lastCount) {
      console.log(`    已收集: ${collected.size}`);
      lastCount = collected.size;
    }
  }

  try {
    await cdpCall(ws, { id: nextCdpId(), method: 'Page.navigate', params: { url } }, 8000);
  } catch {
    console.log('    導航較慢，繼續等待頁面內容');
  }
  await sleep(2500);
  await cdpCall(ws, { id: nextCdpId(), method: 'Page.reload' }, 8000).catch(() => {});
  await sleep(10000);
  await collectVisibleLinks();
  for (let i = 0; i < scrollRounds; i++) {
    await cdpCall(ws, {
      id: nextCdpId(),
      method: 'Runtime.evaluate',
      params: { expression: 'window.scrollTo(0, document.body.scrollHeight); document.body.scrollHeight', returnByValue: true }
    }, 10000).catch(() => {});
    await sleep(waitMs);
    await collectVisibleLinks();
  }
  return Array.from(collected).filter(videoUrl => /facebook\.com\/(reel\/\d+|watch\/\?v=\d+|video\.php\?v=\d+)/.test(videoUrl));
}

function readArchive(archivePath) {
  if (!fs.existsSync(archivePath)) return new Set();
  return new Set(fs.readFileSync(archivePath, 'utf8').split(/\r?\n/).map(line => line.trim()).filter(Boolean));
}

function readArchiveKeys(...archivePaths) {
  const keys = new Set();
  for (const archivePath of archivePaths) {
    if (!archivePath || !fs.existsSync(archivePath)) continue;
    for (const line of fs.readFileSync(archivePath, 'utf8').split(/\r?\n/)) {
      const trimmed = line.trim();
      if (trimmed) keys.add(videoKey(trimmed));
    }
  }
  return keys;
}

function appendArchive(archivePath, value) {
  fs.appendFileSync(archivePath, `${value}\n`);
}

function assertRunnable(command, label) {
  const result = spawnSync(command, ['--version'], { encoding: 'utf8', timeout: 15000 });
  if (result.error || (typeof result.status === 'number' && result.status !== 0)) {
    throw new Error(`Missing ${label}: ${command}`);
  }
}

function downloadVideo(url, outputDir, archivePath) {
  if (dryRun) {
    console.log(`  DRY-RUN: ${url}`);
    return true;
  }
  const args = [];
  if (cookiesFile && fs.existsSync(cookiesFile)) {
    args.push('--cookies', cookiesFile);
  }
  args.push(
    '--ignore-errors',
    '--no-warnings',
    '--no-playlist',
    '--download-archive', path.join(outputDir, '.yt-dlp-archive.txt'),
    '-f', 'hd/best',
    '--merge-output-format', 'mp4',
    '-o', path.join(outputDir, '%(upload_date)s_%(id)s_%(title).120B.%(ext)s'),
    url
  );
  const result = spawnSync(ytdlpPath, args, { encoding: 'utf8', timeout: 600000 });
  const output = `${result.stdout || ''}${result.stderr || ''}`.trim();
  if (result.status === 0 || output.includes('100%') || output.includes('has already been recorded in the archive')) {
    appendArchive(archivePath, url);
    console.log('    完成');
    return true;
  }
  const errorLine = output.split(/\r?\n/).find(line => line.includes('ERROR')) || output.split(/\r?\n/).slice(-1)[0] || '下載失敗';
  console.log(`    失敗: ${errorLine.slice(0, 220)}`);
  return false;
}

async function main() {
  if (!fs.existsSync(accountsFile)) throw new Error(`Missing accounts file: ${accountsFile}`);
  assertRunnable(chromePath, 'Chrome');
  if (!dryRun) assertRunnable(ytdlpPath, 'yt-dlp');
  const accounts = readAccounts(accountsFile);
  if (!accounts.length) throw new Error('No accounts configured');

  const profile = path.join(os.tmpdir(), `hermes_facebook_followed_${Date.now()}`);
  fs.mkdirSync(profile, { recursive: true });
  const chrome = spawn(chromePath, [
    `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${profile}`,
    '--headless=new',
    '--disable-gpu',
    '--no-first-run',
    '--no-default-browser-check',
    'about:blank'
  ], { stdio: 'ignore', detached: process.platform !== 'win32' });

  let ws;
  try {
    await waitForChrome();
    ws = await connectTab();
    const cookieCount = await injectCookies(ws);
    console.log(`Facebook cookies: ${cookieCount}`);
    console.log(`模式: ${mode === 'full' ? '首次全量' : '每日增量'}`);

    for (const account of accounts) {
      const outputDir = path.join(desktopDir, sanitizeFolderName(account.folder));
      if (!dryRun) fs.mkdirSync(outputDir, { recursive: true });
      const archivePath = path.join(outputDir, '.fb-video-urls.txt');
      const existingKeys = readArchiveKeys(archivePath, path.join(outputDir, '.yt-dlp-archive.txt'));
      const discovered = new Set();

      console.log(`\n=== ${account.folder} ===`);
      for (const page of pageCandidates(account.url)) {
        try {
          const urls = await discoverOnPage(ws, page);
          urls.forEach(videoUrl => discovered.add(videoUrl));
        } catch (err) {
          console.log(`  掃描失敗: ${err.message}`);
        }
      }

      const discoveredByKey = new Map();
      for (const videoUrl of Array.from(discovered)) {
        const key = videoKey(videoUrl);
        if (!discoveredByKey.has(key)) discoveredByKey.set(key, videoUrl);
      }
      const newUrls = Array.from(discoveredByKey.entries())
        .filter(([key]) => !existingKeys.has(key))
        .map(([, videoUrl]) => videoUrl);
      const selected = maxDownloads > 0 ? newUrls.slice(0, maxDownloads) : newUrls;
      console.log(`  找到影片: ${discovered.size}，待下載: ${selected.length}`);

      let ok = 0;
      for (const videoUrl of selected) {
        console.log(`  下載: ${videoUrl}`);
        if (downloadVideo(videoUrl, outputDir, archivePath)) ok++;
      }
      if (dryRun) console.log(`  預演: ${selected.length} 個待下載，未寫入檔案`);
      else console.log(`  成功: ${ok}/${selected.length}`);
    }
  } finally {
    if (ws) ws.close();
    try {
      if (process.platform === 'win32') chrome.kill();
      else process.kill(-chrome.pid);
    } catch {}
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch {}
  }
}

main().catch(err => {
  console.error(`錯誤: ${err.message}`);
  process.exit(1);
});
