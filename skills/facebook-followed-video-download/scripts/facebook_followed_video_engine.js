#!/usr/bin/env node
/*
 * Multi-account Facebook video downloader for Hermes.
 *
 * Discovers public/exposed Facebook Reels and video links from configured
 * account pages, then downloads unseen videos into a configurable local folder.
 */

const fs = require('fs');
const crypto = require('crypto');
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
const CONFIGURED_CDP_PORT = Number(process.env.FACEBOOK_FOLLOWED_CDP_PORT || process.env.FB_CDP_PORT || '0');
const SKILL_VERSION = '1.7.1';
const VIDEO_RESULT_EVENT_PREFIX = '__HM_VIDEO_RESULT__:';
const ERROR_CODES = {
  CHROME_START: 'CHROME_CDP_START_FAILED',
  CDP_TIMEOUT: 'CDP_RUNTIME_TIMEOUT',
  ACCESS_REQUIRED: 'FACEBOOK_ACCESS_REQUIRED',
  DISCOVERY_EMPTY: 'FACEBOOK_DISCOVERY_EMPTY',
  LAYOUT_UNSUPPORTED: 'FACEBOOK_LAYOUT_UNSUPPORTED'
};

function codedError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

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
const scrollRounds = Number(argValue('--scroll-rounds', '80'));
const waitMs = Number(argValue('--wait-ms', '1400'));
const firstRunLimit = Number(argValue(
  '--first-run-limit',
  argValue('--max-downloads', mode === 'full' ? '0' : '10')
));
const maxDurationSeconds = Number(argValue('--max-duration-seconds', '0'));
const resultJsonPath = argValue('--result-json', '');
const browserProfileDir = argValue('--browser-profile', '');
const emitVideoResultEvents = process.env.HM_VIDEO_RESULT_EVENTS === '1';
let cdpId = 10;

function nextCdpId() {
  cdpId += 1;
  return cdpId;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function videoResultEventLine(source, video, completed, total) {
  return `${VIDEO_RESULT_EVENT_PREFIX}${JSON.stringify({
    schemaVersion: '1.0',
    event: 'video-result',
    source,
    completed,
    total,
    video,
  })}`;
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
  const decoded = String(url || '').replace(/\\\//g, '/').replace(/&amp;/g, '&');
  const reel = decoded.match(/facebook\.com\/reel\/(\d+)/);
  if (reel) return `https://www.facebook.com/reel/${reel[1]}`;
  const watch = decoded.match(/facebook\.com\/watch\/\?v=(\d+)/);
  if (watch) return `https://www.facebook.com/watch/?v=${watch[1]}`;
  const videoPhp = decoded.match(/facebook\.com\/video\.php\?v=(\d+)/);
  if (videoPhp) return `https://www.facebook.com/video.php?v=${videoPhp[1]}`;
  const videos = decoded.match(/facebook\.com\/[^"' <]+\/videos\/(\d+)/);
  if (videos) return `https://www.facebook.com/watch/?v=${videos[1]}`;
  const share = decoded.match(/facebook\.com\/share\/(r|v)\/([^?&#/]+)/i);
  if (share) return `https://www.facebook.com/share/${share[1].toLowerCase()}/${share[2]}/`;
  const shortWatch = decoded.match(/(?:www\.)?fb\.watch\/([^?&#/]+)/i);
  if (shortWatch) return `https://fb.watch/${shortWatch[1]}/`;
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
  const share = decoded.match(/facebook\.com\/share\/(?:r|v)\/([^?&#/]+)/i);
  if (share) return `share:${share[1]}`;
  const shortWatch = decoded.match(/(?:www\.)?fb\.watch\/([^?&#/]+)/i);
  if (shortWatch) return `fb.watch:${shortWatch[1]}`;
  const ytdlpArchive = decoded.match(/^facebook\s+(\d+)$/);
  if (ytdlpArchive) return ytdlpArchive[1];
  return decoded;
}

function isSupportedVideoUrl(url) {
  return /(?:facebook\.com\/(?:reel\/\d+|watch\/\?v=\d+|video\.php\?v=\d+|[^"' <]+\/videos\/\d+|share\/(?:r|v)\/[^?&#/]+)|fb\.watch\/[^?&#/]+)/i.test(String(url || ''));
}

function normalizeAccountUrl(url) {
  const clean = stripQuery(url).replace(/\/$/, '');
  return clean;
}

function pageCandidates(accountUrl) {
  const base = normalizeAccountUrl(accountUrl);
  if (isSupportedVideoUrl(base)) {
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

function httpGet(port, pathname) {
  return new Promise((resolve, reject) => {
    http.get(`http://127.0.0.1:${port}${pathname}`, res => {
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

function boundedChromeDiagnostic(chunks) {
  return chunks.join('').replace(/[\r\n]+/g, ' ').trim().slice(-1600);
}

function readDevToolsActivePort(profile) {
  const activePortFile = path.join(profile, 'DevToolsActivePort');
  if (!fs.existsSync(activePortFile)) return 0;
  try {
    const value = fs.readFileSync(activePortFile, 'utf8').split(/\r?\n/)[0].trim();
    const parsed = Number(value);
    return Number.isInteger(parsed) && parsed > 0 && parsed < 65536 ? parsed : 0;
  } catch {
    return 0;
  }
}

async function waitForChrome(chrome, profile, stderrChunks) {
  let port = CONFIGURED_CDP_PORT > 0 ? CONFIGURED_CDP_PORT : 0;
  for (let i = 0; i < 60; i++) {
    if (chrome.exitCode !== null) {
      const detail = boundedChromeDiagnostic(stderrChunks);
      throw codedError(
        ERROR_CODES.CHROME_START,
        `Chrome exited before CDP became ready${detail ? `: ${detail}` : ''}`
      );
    }
    if (!port) port = readDevToolsActivePort(profile);
    try {
      if (port) {
        await httpGet(port, '/json/version');
        return port;
      }
    } catch {
      // Chrome may write DevToolsActivePort before the endpoint starts listening.
    }
    await sleep(500);
  }
  const detail = boundedChromeDiagnostic(stderrChunks);
  throw codedError(
    ERROR_CODES.CHROME_START,
    `Chrome CDP did not start within 30 seconds${detail ? `: ${detail}` : ''}`
  );
}

async function cdpCall(ws, msg, timeoutMs = 20000) {
  return new Promise((resolve, reject) => {
    let timer;
    const cleanup = () => {
      clearTimeout(timer);
      ws.removeListener('message', handler);
      ws.removeListener('close', closed);
      ws.removeListener('error', closed);
    };
    const closed = () => {
      cleanup();
      reject(new Error(`CDP connection closed: ${msg.method}`));
    };
    const handler = data => {
      let parsed;
      try {
        parsed = JSON.parse(data);
      } catch {
        return;
      }
      if (parsed.id === msg.id) {
        cleanup();
        if (parsed.error) reject(new Error(parsed.error.message || `CDP failed: ${msg.method}`));
        else resolve(parsed);
      }
    };
    timer = setTimeout(() => {
      cleanup();
      reject(codedError(
        msg.method === 'Runtime.evaluate' ? ERROR_CODES.CDP_TIMEOUT : 'CDP_TIMEOUT',
        `CDP timeout: ${msg.method}`
      ));
    }, timeoutMs);
    ws.on('message', handler);
    ws.once('close', closed);
    ws.once('error', closed);
    ws.send(JSON.stringify(msg));
  });
}

async function connectTab(port, forceNew = false) {
  let tabs = await httpGet(port, '/json');
  let tab;
  if (forceNew) {
    const created = await httpGet(port, '/json/new?about:blank').catch(() => null);
    if (created && created.type === 'page' && created.webSocketDebuggerUrl) {
      tab = created;
    }
    tabs = await httpGet(port, '/json');
  }
  if (!tab) {
    const candidates = tabs.filter(candidate => candidate.type === 'page' && candidate.webSocketDebuggerUrl);
    tab = forceNew ? candidates[candidates.length - 1] : candidates[0];
  }
  if (!tab) {
    await httpGet(port, '/json/new?about:blank');
    tabs = await httpGet(port, '/json');
    tab = tabs.find(candidate => candidate.type === 'page' && candidate.webSocketDebuggerUrl);
  }
  if (!tab) {
    throw new Error('No controllable Chrome page target found');
  }
  const ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('CDP WebSocket open timeout')), 10000);
    ws.once('open', () => {
      clearTimeout(timer);
      resolve();
    });
    ws.once('error', error => {
      clearTimeout(timer);
      reject(error);
    });
  });
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

function discoveryExpression() {
  return `
    (() => {
      const found = new Set();
      const add = value => {
        const href = String(value || '').replace(/\\\\\\\//g, '/');
        if (
          href.includes('/reel/') || href.includes('/watch/?v=')
          || href.includes('/videos/') || href.includes('/video.php?v=')
          || href.includes('/share/r/') || href.includes('/share/v/')
          || href.includes('fb.watch/')
        ) found.add(href);
      };
      for (const anchor of Array.from(document.querySelectorAll('a[href]'))) add(anchor.href);
      for (const selector of ['link[rel="canonical"]', 'meta[property="og:url"]']) {
        const element = document.querySelector(selector);
        if (element) add(element.href || element.content);
      }
      add(location.href);
      const patterns = [
        /https?:\\/\\/(?:www\\.)?facebook\\.com\\/(?:reel\\/\\d+|watch\\/\\?v=\\d+|[^"' <]+\\/videos\\/\\d+|video\\.php\\?v=\\d+|share\\/(?:r|v)\\/[^"' <\\/?&#]+)/gi,
        /https?:\\/\\/fb\\.watch\\/[^"' <\\/?&#]+/gi,
        /\\/reel\\/\\d+/g,
        /\\/watch\\/\\?v=\\d+/g,
        /\\/video\\.php\\?v=\\d+/g,
        /\\/share\\/(?:r|v)\\/[^"' <\\/?&#]+/g
      ];
      let scriptBudget = 2000000;
      for (const script of Array.from(document.querySelectorAll('script')).slice(0, 60)) {
        if (scriptBudget <= 0) break;
        const rawText = String(script.textContent || '').slice(0, Math.min(100000, scriptBudget));
        const text = rawText.replace(/\\\\\\\//g, '/');
        scriptBudget -= rawText.length;
        for (const pattern of patterns) {
          pattern.lastIndex = 0;
          let match;
          let matches = 0;
          while ((match = pattern.exec(text)) && matches++ < 200) {
            add(match[0].startsWith('/') ? location.origin + match[0] : match[0]);
          }
        }
      }
      return JSON.stringify({
        urls: Array.from(found).slice(0, 2000),
        finalUrl: location.href,
        title: String(document.title || '').slice(0, 500),
        bodyText: String(document.body ? document.body.innerText : '').slice(0, 6000),
        videoElements: document.querySelectorAll('video').length
      });
    })()
      `;
}

async function discoverOnPage(ws, url, stopKeys = new Set(), minimumItems = 0) {
  console.log(`  掃描: ${url}`);
  const extractExpression = discoveryExpression();
  const collected = new Set();
  let lastCount = 0;
  let stagnantRounds = 0;
  let reachedKnownVideo = false;
  let layoutUnsupported = false;
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
    const snapshot = JSON.parse(
      (result.result && result.result.result && result.result.result.value) || '{}'
    );
    const accessText = `${snapshot.finalUrl || ''} ${snapshot.title || ''} ${snapshot.bodyText || ''}`;
    if (
      /facebook\.com\/(?:login|checkpoint|challenge|recover|two_factor)/i.test(accessText)
      || /(?:log in to continue|login to continue|confirm your identity|security check|temporarily blocked|請登入|登录以继续|確認你的身分|验证你的身份)/i.test(accessText)
    ) {
      throw codedError(
        ERROR_CODES.ACCESS_REQUIRED,
        'Facebook requires login, verification, or an access check for this page'
      );
    }
    if (Number(snapshot.videoElements || 0) > 0 && !(snapshot.urls || []).length) {
      layoutUnsupported = true;
    }
    for (const rawVideoUrl of snapshot.urls || []) {
      const videoUrl = normalizeVideoUrl(rawVideoUrl);
      if (!isSupportedVideoUrl(videoUrl)) continue;
      collected.add(videoUrl);
      if (stopKeys.has(videoKey(videoUrl))) reachedKnownVideo = true;
    }
    if (collected.size > lastCount) {
      console.log(`    已收集: ${collected.size}`);
      stagnantRounds = 0;
      lastCount = collected.size;
    } else {
      stagnantRounds += 1;
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
    if (reachedKnownVideo || (minimumItems > 0 && collected.size >= minimumItems) || stagnantRounds >= 3) break;
    await cdpCall(ws, {
      id: nextCdpId(),
      method: 'Runtime.evaluate',
      params: { expression: 'window.scrollTo(0, document.body.scrollHeight); document.body.scrollHeight', returnByValue: true }
    }, 10000).catch(() => {});
    await sleep(waitMs);
    await collectVisibleLinks();
  }
  return {
    urls: Array.from(collected).filter(isSupportedVideoUrl),
    layoutUnsupported
  };
}

async function discoverWithRecovery(
  browser,
  url,
  stopKeys,
  minimumItems,
  adapters = { discoverOnPage, connectTab, injectCookies }
) {
  try {
    return await adapters.discoverOnPage(browser.ws, url, stopKeys, minimumItems);
  } catch (err) {
    if (err.code !== ERROR_CODES.CDP_TIMEOUT) throw err;
    console.log('    CDP 執行逾時，重建頁面工作階段後重試一次');
    try { browser.ws.close(); } catch {}
    browser.ws = await adapters.connectTab(browser.port, true);
    await adapters.injectCookies(browser.ws);
    try {
      return await adapters.discoverOnPage(browser.ws, url, stopKeys, minimumItems);
    } catch (retryError) {
      if (retryError.code === ERROR_CODES.CDP_TIMEOUT) {
        throw codedError(
          ERROR_CODES.CDP_TIMEOUT,
          'Facebook page Runtime.evaluate timed out after rebuilding the CDP session'
        );
      }
      throw retryError;
    }
  }
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

function appendArchiveOnce(archivePath, value) {
  const key = videoKey(value);
  if (!readArchiveKeys(archivePath).has(key)) appendArchive(archivePath, value);
}

function isYtDlpArchived(outputDir, url) {
  return readArchiveKeys(path.join(outputDir, '.yt-dlp-archive.txt')).has(videoKey(url));
}

function existingVideoState(outputDir, url, platformVideoId) {
  const localPath = findDownloadedFile(outputDir, platformVideoId, '');
  if (localPath) return { status: 'local-existing', localPath };
  if (isYtDlpArchived(outputDir, url)) {
    return { status: 'archived-existing', localPath: null };
  }
  return null;
}

/**
 * Facebook's Reels/videos pages expose links newest-first. The first daily run
 * is limited to the requested recent window. Later daily runs take every URL
 * before the first archived boundary; when that update set is empty, they
 * return the requested recent window. Full imports remain incremental.
 */
function selectVideoUrls(discoveredUrls, existingKeys, runMode, limit) {
  const unique = new Map();
  for (const videoUrl of discoveredUrls) {
    const key = videoKey(videoUrl);
    if (!unique.has(key)) unique.set(key, videoUrl);
  }
  const entries = Array.from(unique.entries());
  if (runMode !== 'daily') {
    const candidates = entries
      .filter(([key]) => !existingKeys.has(key))
      .map(([, videoUrl]) => videoUrl);
    return limit > 0 ? candidates.slice(0, limit) : candidates;
  }
  return selectDailyVideoUrls(
    [entries.map(([, videoUrl]) => videoUrl)],
    existingKeys,
    limit
  );
}

function selectDailyVideoUrls(discoveredPages, existingKeys, limit) {
  const allRecent = new Map();
  for (const pageUrls of discoveredPages) {
    for (const videoUrl of pageUrls) {
      const key = videoKey(videoUrl);
      if (!allRecent.has(key)) allRecent.set(key, videoUrl);
    }
  }
  const recentWindow = () => {
    const recent = Array.from(allRecent.values());
    return limit > 0 ? recent.slice(0, limit) : recent;
  };
  if (existingKeys.size === 0) return recentWindow();

  const updates = [];
  const updateKeys = new Set();
  for (const pageUrls of discoveredPages) {
    for (const videoUrl of pageUrls) {
      const key = videoKey(videoUrl);
      if (existingKeys.has(key)) break;
      if (!updateKeys.has(key)) {
        updateKeys.add(key);
        updates.push(videoUrl);
      }
    }
  }
  return updates.length ? updates : recentWindow();
}

function assertRunnable(command, label) {
  const result = spawnSync(command, ['--version'], { encoding: 'utf8', timeout: 15000 });
  if (result.error || (typeof result.status === 'number' && result.status !== 0)) {
    throw new Error(`Missing ${label}: ${command}`);
  }
}

function sha256File(filePath) {
  const hash = crypto.createHash('sha256');
  const descriptor = fs.openSync(filePath, 'r');
  const buffer = Buffer.allocUnsafe(1024 * 1024);
  try {
    while (true) {
      const bytes = fs.readSync(descriptor, buffer, 0, buffer.length, null);
      if (!bytes) break;
      hash.update(buffer.subarray(0, bytes));
    }
  } finally {
    fs.closeSync(descriptor);
  }
  return hash.digest('hex');
}

function findDownloadedFile(outputDir, platformVideoId, output) {
  const marker = String(output || '').split(/\r?\n/)
    .find(line => line.startsWith('__HERMES_FILE__:'));
  if (marker) {
    const candidate = marker.slice('__HERMES_FILE__:'.length).trim();
    if (candidate && fs.existsSync(candidate) && fs.statSync(candidate).isFile()) return candidate;
  }
  if (!fs.existsSync(outputDir)) return '';
  const needle = `_${platformVideoId}_`;
  const candidates = fs.readdirSync(outputDir)
    .filter(name => name.includes(needle) && name.toLowerCase().endsWith('.mp4'))
    .map(name => path.join(outputDir, name))
    .filter(candidate => {
      try { return fs.statSync(candidate).isFile(); } catch { return false; }
    })
    .sort((left, right) => fs.statSync(right).mtimeMs - fs.statSync(left).mtimeMs);
  return candidates[0] || '';
}

function baseVideoResult(account, url) {
  return {
    platform: 'Facebook',
    source: account.folder,
    sourceUrl: account.url,
    platformVideoId: videoKey(url),
    originalUrl: url,
    canonicalUrl: normalizeVideoUrl(url),
    localPath: null,
    fileName: null,
    fileSize: null,
    sha256: null,
    durationSeconds: null,
    publishedAt: null,
    publishedAtPrecision: null,
    status: 'pending',
    error: null
  };
}

function ytdlpSessionArgs() {
  if (cookiesFile && fs.existsSync(cookiesFile)) return ['--cookies', cookiesFile];
  if (browserProfileDir) return ['--cookies-from-browser', `chrome:${browserProfileDir}`];
  return [];
}

function publishedAtFromMetadata(metadata = {}) {
  for (const rawTimestamp of [metadata.timestamp, metadata.release_timestamp]) {
    if (rawTimestamp === null || rawTimestamp === undefined || rawTimestamp === '') continue;
    const timestamp = Number(rawTimestamp);
    if (!Number.isFinite(timestamp) || timestamp <= 0) continue;
    const value = new Date(timestamp * 1000);
    if (!Number.isFinite(value.getTime())) continue;
    return {
      publishedAt: value.toISOString().slice(0, 19),
      publishedAtPrecision: 'SECOND'
    };
  }

  const uploadDate = String(metadata.upload_date || '');
  if (/^\d{8}$/.test(uploadDate)) {
    const year = Number(uploadDate.slice(0, 4));
    const month = Number(uploadDate.slice(4, 6));
    const day = Number(uploadDate.slice(6, 8));
    const value = new Date(Date.UTC(year, month - 1, day));
    if (
      value.getUTCFullYear() === year
      && value.getUTCMonth() === month - 1
      && value.getUTCDate() === day
    ) {
      return {
        publishedAt: `${uploadDate.slice(0, 4)}-${uploadDate.slice(4, 6)}-${uploadDate.slice(6, 8)}T00:00:00`,
        publishedAtPrecision: 'DATE'
      };
    }
  }

  return { publishedAt: null, publishedAtPrecision: null };
}

function probeVideoMetadata(url) {
  if (dryRun) return {};
  const result = spawnSync(ytdlpPath, [
    ...ytdlpSessionArgs(),
    '--force-ipv4', '--socket-timeout', '60', '--retries', '2',
    '--no-warnings', '--no-playlist', '--skip-download',
    '--dump-single-json', url
  ], { encoding: 'utf8' });
  try {
    const metadata = JSON.parse(String(result.stdout || '').trim());
    const duration = Number(metadata.duration);
    const publishTime = publishedAtFromMetadata(metadata);
    return {
      durationSeconds: Number.isFinite(duration) && duration >= 0 ? Math.round(duration) : null,
      ...publishTime,
      title: metadata.title || null
    };
  } catch {
    return {};
  }
}

function completedVideoResult(item, downloadedPath) {
  const stat = fs.statSync(downloadedPath);
  item.localPath = path.resolve(downloadedPath);
  item.fileName = path.basename(downloadedPath);
  item.fileSize = stat.size;
  item.sha256 = sha256File(downloadedPath);
  item.status = 'downloaded';
  return item;
}

function downloadVideo(account, url, outputDir, archivePath) {
  const item = baseVideoResult(account, url);
  if (dryRun) {
    console.log(`  DRY-RUN: ${url}`);
    item.status = 'preview';
    return item;
  }
  const existing = existingVideoState(outputDir, url, item.platformVideoId);
  if (existing && existing.status === 'archived-existing') {
    item.status = 'archived-existing';
    console.log('    跳過: 已在下載歸檔中，本地檔案已清理');
    return item;
  }
  const metadata = probeVideoMetadata(url);
  item.durationSeconds = metadata.durationSeconds === undefined ? null : metadata.durationSeconds;
  item.publishedAt = metadata.publishedAt === undefined ? null : metadata.publishedAt;
  item.publishedAtPrecision = metadata.publishedAtPrecision === undefined ? null : metadata.publishedAtPrecision;
  item.title = metadata.title === undefined ? null : metadata.title;
  if (maxDurationSeconds && item.durationSeconds !== null && item.durationSeconds > maxDurationSeconds) {
    appendArchiveOnce(archivePath, url);
    item.status = 'filtered-duration';
    item.error = `duration ${item.durationSeconds}s exceeds ${maxDurationSeconds}s limit`;
    console.log(`    跳過: 時長 ${item.durationSeconds}s 超過 ${maxDurationSeconds}s`);
    return item;
  }
  if (existing && existing.status === 'local-existing') {
    appendArchiveOnce(archivePath, url);
    console.log('    復用本地檔案');
    return completedVideoResult(item, existing.localPath);
  }
  const args = [];
  args.push(...ytdlpSessionArgs());
  if (maxDurationSeconds) {
    args.push('--match-filter', `duration <= ${maxDurationSeconds}`);
  }
  args.push(
    '--force-ipv4',
    '--socket-timeout', '60',
    '--retries', '2',
    '--ignore-errors',
    '--no-warnings',
    '--no-playlist',
    '--download-archive', path.join(outputDir, '.yt-dlp-archive.txt'),
    '-f', 'hd/best',
    '--merge-output-format', 'mp4',
    '--print', 'after_move:__HERMES_FILE__:%(filepath)s',
    '-o', path.join(outputDir, '%(upload_date)s_%(id)s_%(title).120B.%(ext)s'),
    url
  );
  const result = spawnSync(ytdlpPath, args, { encoding: 'utf8' });
  const output = `${result.stdout || ''}${result.stderr || ''}`.trim();
  const downloadedPath = findDownloadedFile(outputDir, item.platformVideoId, output);
  if ((result.status === 0 || output.includes('100%')) && downloadedPath) {
    appendArchiveOnce(archivePath, url);
    console.log('    完成');
    return completedVideoResult(item, downloadedPath);
  }
  const errorLine = output.split(/\r?\n/).find(line => line.includes('ERROR')) || output.split(/\r?\n/).slice(-1)[0] || '下載失敗';
  if (/does not pass filter|duration.*(?:larger|greater|longer)/i.test(output)) {
    appendArchiveOnce(archivePath, url);
    item.status = 'filtered-duration';
    item.error = `video exceeds ${maxDurationSeconds}s duration limit`;
    console.log(`    跳過: 時長超過 ${maxDurationSeconds}s`);
    return item;
  }
  console.log(`    失敗: ${errorLine.slice(0, 220)}`);
  item.status = 'download-failed';
  item.error = errorLine.slice(0, 500);
  return item;
}

async function stopChrome(chrome) {
  if (!chrome) return;
  try {
    if (process.platform === 'win32') {
      spawnSync('taskkill', ['/pid', String(chrome.pid), '/T', '/F'], {
        stdio: 'ignore', windowsHide: true
      });
    } else {
      process.kill(-chrome.pid, 'SIGTERM');
    }
  } catch {
    try { chrome.kill(); } catch {}
  }
  await sleep(300);
}

function removeTree(directory) {
  if (!fs.existsSync(directory)) return;
  fs.rmdirSync(directory, { recursive: true, maxRetries: 3, retryDelay: 100 });
}

async function startBrowser(profile) {
  const activePortFile = path.join(profile, 'DevToolsActivePort');
  let lastError;
  for (let attempt = 1; attempt <= 2; attempt++) {
    try { fs.unlinkSync(activePortFile); } catch {}
    const stderrChunks = [];
    let stderrSize = 0;
    const chrome = spawn(chromePath, [
      `--remote-debugging-port=${CONFIGURED_CDP_PORT > 0 ? CONFIGURED_CDP_PORT : 0}`,
      `--user-data-dir=${profile}`,
      attempt === 1 ? '--headless=new' : '--headless',
      '--disable-gpu',
      '--no-first-run',
      '--no-default-browser-check',
      'about:blank'
    ], {
      stdio: ['ignore', 'ignore', 'pipe'],
      detached: process.platform !== 'win32',
      windowsHide: true
    });
    if (chrome.stderr) {
      chrome.stderr.on('data', chunk => {
        if (stderrSize >= 6400) return;
        const value = String(chunk).slice(0, 6400 - stderrSize);
        stderrChunks.push(value);
        stderrSize += value.length;
      });
    }
    try {
      const port = await waitForChrome(chrome, profile, stderrChunks);
      const ws = await connectTab(port);
      return { chrome, port, ws };
    } catch (err) {
      lastError = err.code
        ? err
        : codedError(ERROR_CODES.CHROME_START, String(err.message || err));
      await stopChrome(chrome);
      if (attempt === 1) console.log('Chrome CDP 啟動失敗，清理本次程序後重試一次');
    }
  }
  throw lastError || codedError(ERROR_CODES.CHROME_START, 'Chrome CDP did not start');
}

function classifyDiscoveryFailure(scanErrors, layoutUnsupported) {
  const priorities = [ERROR_CODES.ACCESS_REQUIRED, ERROR_CODES.CDP_TIMEOUT];
  for (const code of priorities) {
    const match = scanErrors.find(item => item.code === code);
    if (match) return match;
  }
  if (layoutUnsupported) {
    return {
      code: ERROR_CODES.LAYOUT_UNSUPPORTED,
      message: 'Facebook page contains video elements but its current layout is not supported'
    };
  }
  return {
    code: ERROR_CODES.DISCOVERY_EMPTY,
    message: 'Facebook public page returned no discoverable video links'
  };
}

async function main() {
  if (!fs.existsSync(accountsFile)) throw new Error(`Missing accounts file: ${accountsFile}`);
  assertRunnable(chromePath, 'Chrome');
  if (!dryRun) assertRunnable(ytdlpPath, 'yt-dlp');
  const accounts = readAccounts(accountsFile);
  if (!accounts.length) throw new Error('No accounts configured');

  const temporaryProfile = !browserProfileDir;
  const profile = browserProfileDir || path.join(os.tmpdir(), `hermes_facebook_followed_${Date.now()}`);
  const startedAt = new Date().toISOString();
  const runResult = {
    schemaVersion: '1.0',
    skill: 'facebook-followed-video-download',
    skillVersion: SKILL_VERSION,
    mode: dryRun ? 'dry-run' : 'execute',
    status: 'running',
    startedAt,
    completedAt: null,
    sources: []
  };
  fs.mkdirSync(profile, { recursive: true, mode: 0o700 });
  let browser;
  try {
    browser = await startBrowser(profile);
    const cookieCount = await injectCookies(browser.ws);
    console.log(`Facebook cookies: ${cookieCount}`);
    console.log(`Facebook CDP port: ${browser.port}`);
    console.log(`Facebook browser session: ${temporaryProfile ? 'ephemeral-public' : 'user-authorized-isolated-profile'}`);
    console.log(`模式: ${mode === 'full' ? '首次全量' : '每日增量'}`);

    for (const account of accounts) {
      const outputDir = path.join(desktopDir, sanitizeFolderName(account.folder));
      if (!dryRun) fs.mkdirSync(outputDir, { recursive: true });
      const archivePath = path.join(outputDir, '.fb-video-urls.txt');
      const existingKeys = readArchiveKeys(archivePath, path.join(outputDir, '.yt-dlp-archive.txt'));
      const discovered = new Set();
      const discoveredPages = [];
      const scanErrors = [];
      let layoutUnsupported = false;
      const firstDailyRun = mode === 'daily' && existingKeys.size === 0;

      console.log(`\n=== ${account.folder} ===`);
      for (const page of pageCandidates(account.url)) {
        try {
          const discovery = await discoverWithRecovery(
            browser,
            page,
            firstDailyRun ? new Set() : existingKeys,
            firstDailyRun ? firstRunLimit : 0
          );
          discoveredPages.push(discovery.urls);
          discovery.urls.forEach(videoUrl => discovered.add(videoUrl));
          layoutUnsupported = layoutUnsupported || discovery.layoutUnsupported;
        } catch (err) {
          const code = err.code || 'FACEBOOK_SCAN_FAILED';
          scanErrors.push({ code, message: String(err.message || err) });
          console.log(`  掃描失敗 [${code}]: ${err.message}`);
        }
      }

      const selected = mode === 'daily'
        ? selectDailyVideoUrls(discoveredPages, existingKeys, firstRunLimit)
        : selectVideoUrls(Array.from(discovered), existingKeys, mode, firstRunLimit);
      console.log(`  找到影片: ${discovered.size}，本次選取: ${selected.length}`);

      const discoveryFailure = discovered.size === 0
        ? classifyDiscoveryFailure(scanErrors, layoutUnsupported)
        : null;
      if (discoveryFailure) {
        console.log(`  發現失敗 [${discoveryFailure.code}]: ${discoveryFailure.message}`);
      }

      const sourceResult = {
        name: account.folder,
        url: account.url,
        outputDir: path.resolve(outputDir),
        discovered: discovered.size,
        selected: selected.length,
        succeeded: 0,
        failed: discoveryFailure ? 1 : 0,
        filteredDuration: 0,
        archivedExisting: 0,
        errorCode: discoveryFailure ? discoveryFailure.code : null,
        error: discoveryFailure ? discoveryFailure.message : null,
        videos: []
      };
      for (const videoUrl of selected) {
        console.log(`  下載: ${videoUrl}`);
        const item = downloadVideo(account, videoUrl, outputDir, archivePath);
        sourceResult.videos.push(item);
        if (emitVideoResultEvents) {
          console.log(videoResultEventLine(
            account.folder,
            item,
            sourceResult.videos.length,
            selected.length,
          ));
        }
        if (item.status === 'filtered-duration') sourceResult.filteredDuration++;
        else if (item.status === 'archived-existing') sourceResult.archivedExisting++;
        else if (item.status === 'downloaded' || item.status === 'preview') sourceResult.succeeded++;
        else sourceResult.failed++;
      }
      if (dryRun) console.log(`  預演: ${selected.length} 個待下載，未寫入檔案`);
      else {
        if (sourceResult.archivedExisting > 0) {
          console.log(`  已歸檔跳過: ${sourceResult.archivedExisting}`);
        }
        const actionable = selected.length
          - sourceResult.filteredDuration
          - sourceResult.archivedExisting;
        console.log(`  成功: ${sourceResult.succeeded}/${actionable}`);
      }
      runResult.sources.push(sourceResult);
    }
    const failures = runResult.sources.reduce((sum, source) => sum + source.failed, 0);
    const successes = runResult.sources.reduce((sum, source) => sum + source.succeeded, 0);
    runResult.status = failures === 0 ? 'completed' : (successes > 0 ? 'partial' : 'failed');
    runResult.completedAt = new Date().toISOString();
    if (resultJsonPath) {
      fs.mkdirSync(path.dirname(resultJsonPath), { recursive: true });
      fs.writeFileSync(resultJsonPath, `${JSON.stringify(runResult, null, 2)}\n`, 'utf8');
    }
    return runResult;
  } finally {
    if (browser && browser.ws) {
      try { browser.ws.close(); } catch {}
    }
    if (browser) await stopChrome(browser.chrome);
    if (temporaryProfile) {
      try { removeTree(profile); } catch {}
    }
  }
}

async function runMain() {
  try {
    const result = await main();
    if (result.status === 'failed') process.exitCode = 1;
  } catch (err) {
    console.error(`錯誤: [${err.code || 'PIPELINE_ERROR'}] ${err.message}`);
    if (resultJsonPath) {
      const failed = {
        schemaVersion: '1.0',
        skill: 'facebook-followed-video-download',
        skillVersion: SKILL_VERSION,
        mode: dryRun ? 'dry-run' : 'execute',
        status: 'failed',
        startedAt: null,
        completedAt: new Date().toISOString(),
        sources: [],
        errorCode: err.code || null,
        error: String(err.message || err)
      };
      try {
        fs.mkdirSync(path.dirname(resultJsonPath), { recursive: true });
        fs.writeFileSync(resultJsonPath, `${JSON.stringify(failed, null, 2)}\n`, 'utf8');
      } catch {}
    }
    process.exit(1);
  }
}

if (require.main === module) runMain();

module.exports = {
  ERROR_CODES,
  VIDEO_RESULT_EVENT_PREFIX,
  classifyDiscoveryFailure,
  discoveryExpression,
  discoverWithRecovery,
  existingVideoState,
  isSupportedVideoUrl,
  isYtDlpArchived,
  normalizeVideoUrl,
  pageCandidates,
  readDevToolsActivePort,
  selectDailyVideoUrls,
  selectVideoUrls,
  videoKey,
  videoResultEventLine,
  waitForChrome,
  publishedAtFromMetadata,
};
