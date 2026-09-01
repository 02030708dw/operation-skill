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
const CDP_PORT = Number(process.env.FACEBOOK_FOLLOWED_CDP_PORT || process.env.FB_CDP_PORT || String(9300 + Math.floor(Math.random() * 500)));
const SKILL_VERSION = '1.6.2';
const VIDEO_RESULT_EVENT_PREFIX = '__HM_VIDEO_RESULT__:';

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

async function discoverOnPage(ws, url, stopKeys = new Set(), minimumItems = 0) {
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
  let stagnantRounds = 0;
  let reachedKnownVideo = false;
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
    for (const rawVideoUrl of raw) {
      const videoUrl = normalizeVideoUrl(rawVideoUrl);
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

function appendArchiveOnce(archivePath, value) {
  const key = videoKey(value);
  if (!readArchiveKeys(archivePath).has(key)) appendArchive(archivePath, value);
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
  const metadata = probeVideoMetadata(url);
  item.durationSeconds = metadata.durationSeconds ?? null;
  item.publishedAt = metadata.publishedAt ?? null;
  item.publishedAtPrecision = metadata.publishedAtPrecision ?? null;
  item.title = metadata.title ?? null;
  if (maxDurationSeconds && item.durationSeconds !== null && item.durationSeconds > maxDurationSeconds) {
    appendArchiveOnce(archivePath, url);
    item.status = 'filtered-duration';
    item.error = `duration ${item.durationSeconds}s exceeds ${maxDurationSeconds}s limit`;
    console.log(`    跳過: 時長 ${item.durationSeconds}s 超過 ${maxDurationSeconds}s`);
    return item;
  }
  const cachedPath = findDownloadedFile(outputDir, item.platformVideoId, '');
  if (cachedPath) {
    appendArchiveOnce(archivePath, url);
    console.log('    復用本地檔案');
    return completedVideoResult(item, cachedPath);
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
    console.log(`Facebook browser session: ${temporaryProfile ? 'ephemeral-public' : 'user-authorized-isolated-profile'}`);
    console.log(`模式: ${mode === 'full' ? '首次全量' : '每日增量'}`);

    for (const account of accounts) {
      const outputDir = path.join(desktopDir, sanitizeFolderName(account.folder));
      if (!dryRun) fs.mkdirSync(outputDir, { recursive: true });
      const archivePath = path.join(outputDir, '.fb-video-urls.txt');
      const existingKeys = readArchiveKeys(archivePath, path.join(outputDir, '.yt-dlp-archive.txt'));
      const discovered = new Set();
      const discoveredPages = [];
      const firstDailyRun = mode === 'daily' && existingKeys.size === 0;

      console.log(`\n=== ${account.folder} ===`);
      for (const page of pageCandidates(account.url)) {
        try {
          const urls = await discoverOnPage(
            ws,
            page,
            firstDailyRun ? new Set() : existingKeys,
            firstDailyRun ? firstRunLimit : 0
          );
          discoveredPages.push(urls);
          urls.forEach(videoUrl => discovered.add(videoUrl));
        } catch (err) {
          console.log(`  掃描失敗: ${err.message}`);
        }
      }

      const selected = mode === 'daily'
        ? selectDailyVideoUrls(discoveredPages, existingKeys, firstRunLimit)
        : selectVideoUrls(Array.from(discovered), existingKeys, mode, firstRunLimit);
      console.log(`  找到影片: ${discovered.size}，本次選取: ${selected.length}`);

      const discoveryFailed = discovered.size === 0;
      if (discoveryFailed) {
        console.log('  發現失敗: Facebook 公開頁面沒有返回可解析的影片連結');
      }

      const sourceResult = {
        name: account.folder,
        url: account.url,
        outputDir: path.resolve(outputDir),
        discovered: discovered.size,
        selected: selected.length,
        succeeded: 0,
        failed: discoveryFailed ? 1 : 0,
        filteredDuration: 0,
        error: discoveryFailed
          ? 'Facebook public page returned no discoverable video links'
          : null,
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
        else if (item.status === 'downloaded' || item.status === 'preview') sourceResult.succeeded++;
        else sourceResult.failed++;
      }
      if (dryRun) console.log(`  預演: ${selected.length} 個待下載，未寫入檔案`);
      else console.log(`  成功: ${sourceResult.succeeded}/${selected.length}`);
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
    if (ws) ws.close();
    try {
      if (process.platform === 'win32') chrome.kill();
      else process.kill(-chrome.pid);
    } catch {}
    if (temporaryProfile) {
      try { fs.rmSync(profile, { recursive: true, force: true }); } catch {}
    }
  }
}

async function runMain() {
  try {
    const result = await main();
    if (result.status === 'failed') process.exitCode = 1;
  } catch (err) {
    console.error(`錯誤: ${err.message}`);
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
  VIDEO_RESULT_EVENT_PREFIX,
  selectDailyVideoUrls,
  selectVideoUrls,
  videoKey,
  videoResultEventLine,
  publishedAtFromMetadata,
};
