#!/usr/bin/env node
/*
 * Build a concise Hermes-readable report for the followed Facebook downloader.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');

const HOME = process.env.HOME || os.homedir() || process.cwd();
const HERMES_HOME = process.env.HERMES_HOME || path.join(HOME, '.hermes');
const DEFAULT_ACCOUNTS = path.join(HERMES_HOME, 'facebook-followed-video-download/accounts.txt');
const DEFAULT_DESKTOP = process.env.FACEBOOK_FOLLOWED_OUTPUT || process.env.FB_FOLLOWED_DESKTOP || path.join(HOME, 'Desktop', 'Facebook');
const DEFAULT_REPORTS = process.env.FACEBOOK_FOLLOWED_REPORTS || process.env.FB_FOLLOWED_REPORTS || path.join(HERMES_HOME, 'facebook-followed-video-download/reports');

function argValue(name, fallback = '') {
  const idx = process.argv.indexOf(name);
  return idx >= 0 && process.argv[idx + 1] ? process.argv[idx + 1] : fallback;
}

function hasFlag(name) {
  return process.argv.includes(name);
}

const mode = argValue('--mode', 'daily');
const accountsFile = argValue('--accounts', DEFAULT_ACCOUNTS);
const desktopDir = argValue('--desktop', DEFAULT_DESKTOP);
const reportsDir = argValue('--reports-dir', DEFAULT_REPORTS);
const runLog = argValue('--run-log', '');
const status = Number(argValue('--status', '0'));
const shouldPrint = hasFlag('--print');

function sanitizeFolderName(value) {
  return String(value || 'facebook')
    .replace(/[/:\\?%*"<>|]/g, '_')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 90) || 'facebook';
}

function readAccounts(filePath) {
  const text = fs.readFileSync(filePath, 'utf8');
  return text.split(/\r?\n/)
    .map(line => line.trim())
    .filter(line => line && !line.startsWith('#'))
    .map(line => {
      const tab = line.split(/\t+/);
      if (tab.length >= 2) {
        return { folder: sanitizeFolderName(tab[0]), url: tab.slice(1).join('\t').trim() };
      }
      const inferred = line.replace(/^https?:\/\/(www\.)?facebook\.com\/?/i, '').split(/[/?#]/)[0];
      return { folder: sanitizeFolderName(inferred), url: line };
    });
}

function formatBytes(bytes) {
  if (!bytes) return '0B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  const decimals = unit <= 1 ? 0 : 1;
  return `${value.toFixed(decimals)}${units[unit]}`;
}

function countLines(filePath) {
  if (!fs.existsSync(filePath)) return 0;
  return fs.readFileSync(filePath, 'utf8').split(/\r?\n/).map(line => line.trim()).filter(Boolean).length;
}

function folderSnapshot(folder) {
  const outputDir = path.join(desktopDir, sanitizeFolderName(folder));
  if (!fs.existsSync(outputDir)) {
    return {
      exists: false,
      mp4Count: 0,
      mp4Bytes: 0,
      urlArchiveCount: 0,
      ytdlpArchiveCount: 0,
      outputDir
    };
  }
  let mp4Count = 0;
  let mp4Bytes = 0;
  for (const entry of fs.readdirSync(outputDir)) {
    const filePath = path.join(outputDir, entry);
    let stat;
    try {
      stat = fs.statSync(filePath);
    } catch {
      continue;
    }
    if (stat.isFile() && entry.toLowerCase().endsWith('.mp4')) {
      mp4Count++;
      mp4Bytes += stat.size;
    }
  }
  return {
    exists: true,
    mp4Count,
    mp4Bytes,
    urlArchiveCount: countLines(path.join(outputDir, '.fb-video-urls.txt')),
    ytdlpArchiveCount: countLines(path.join(outputDir, '.yt-dlp-archive.txt')),
    outputDir
  };
}

function parseRunLog(filePath) {
  const parsed = { byFolder: new Map(), errors: [], rawPath: filePath || '' };
  if (!filePath || !fs.existsSync(filePath)) return parsed;
  const text = fs.readFileSync(filePath, 'utf8');
  let current = null;
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    const header = line.match(/^===\s+(.+?)\s+===$/);
    if (header) {
      current = {
        folder: header[1],
        found: null,
        pending: null,
        ok: null,
        total: null,
        downloads: [],
        failures: [],
        scanFailures: []
      };
      parsed.byFolder.set(current.folder, current);
      continue;
    }
    if (/^錯誤:/.test(line) || /^Error:/.test(line) || /\bERROR\b/.test(line)) {
      parsed.errors.push(line.slice(0, 260));
    }
    if (!current) continue;
    let match = line.match(/找到影片:\s*(\d+).*?待下載:\s*(\d+)/);
    if (match) {
      current.found = Number(match[1]);
      current.pending = Number(match[2]);
      continue;
    }
    match = line.match(/成功:\s*(\d+)\/(\d+)/);
    if (match) {
      current.ok = Number(match[1]);
      current.total = Number(match[2]);
      continue;
    }
    match = line.match(/下載:\s*(https?:\/\/\S+)/);
    if (match) {
      current.downloads.push(match[1]);
      continue;
    }
    if (/掃描失敗:/.test(line)) current.scanFailures.push(line.slice(0, 220));
    if (/失敗:/.test(line)) current.failures.push(line.slice(0, 220));
  }
  return parsed;
}

function localTimestamp(date = new Date()) {
  const pad = value => String(value).padStart(2, '0');
  return [
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`,
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  ].join(' ');
}

function safeCell(value) {
  return String(value).replace(/\|/g, '\\|');
}

function renderReport(accounts, parsed) {
  const now = new Date();
  const rows = [];
  let totalNew = 0;
  let totalPending = 0;
  let totalFailures = 0;

  for (const account of accounts) {
    const run = parsed.byFolder.get(account.folder);
    const snap = folderSnapshot(account.folder);
    const ok = run && Number.isFinite(run.ok) ? run.ok : null;
    const pending = run && Number.isFinite(run.pending) ? run.pending : null;
    const found = run && Number.isFinite(run.found) ? run.found : null;
    const failures = run ? run.failures.length + run.scanFailures.length : 0;
    totalNew += ok || 0;
    totalPending += pending || 0;
    totalFailures += failures;
    let state = '未掃描到摘要';
    if (ok !== null && pending !== null && pending > ok) state = '有下載失敗';
    else if (ok !== null && ok > 0) state = `新增 ${ok}`;
    else if (pending === 0) state = '無新影片';
    else if (status !== 0) state = '檢查可能失敗';
    rows.push({
      folder: account.folder,
      found: found === null ? '-' : found,
      pending: pending === null ? '-' : pending,
      ok: ok === null ? '-' : ok,
      files: snap.mp4Count,
      size: formatBytes(snap.mp4Bytes),
      archive: `${snap.urlArchiveCount}/${snap.ytdlpArchiveCount}`,
      state,
      outputDir: snap.outputDir,
      failures: run ? [...run.scanFailures, ...run.failures] : []
    });
  }

  const result = status === 0 ? '成功' : `有錯誤 (exit ${status})`;
  const title = mode === 'daily' ? 'Facebook 博主影片每日檢查報告' : 'Facebook 博主影片首次下載報告';
  const lines = [];
  lines.push(`# ${title}`);
  lines.push('');
  lines.push(`- 時間: ${localTimestamp(now)}`);
  lines.push(`- 模式: ${mode === 'daily' ? '每日新增檢查' : '首次全量下載'}`);
  lines.push(`- 結果: ${result}`);
  lines.push(`- 本次新下載: ${totalNew}`);
  lines.push(`- 本次待下載: ${totalPending}`);
  lines.push(`- 報告給 Hermes: ${totalNew > 0 ? `今天新增 ${totalNew} 個影片。` : '今天沒有新影片。'}`);
  if (runLog) lines.push(`- 原始日誌: ${runLog}`);
  lines.push('');
  lines.push('| 博主 | 本次找到 | 待下載 | 成功 | 桌面檔案 | 大小 | URL/yt-dlp 記錄 | 狀態 |');
  lines.push('|---|---:|---:|---:|---:|---:|---:|---|');
  for (const row of rows) {
    lines.push(`| ${safeCell(row.folder)} | ${row.found} | ${row.pending} | ${row.ok} | ${row.files} | ${row.size} | ${row.archive} | ${safeCell(row.state)} |`);
  }

  const uniqueErrors = Array.from(new Set([...parsed.errors, ...rows.flatMap(row => row.failures)]));
  if (uniqueErrors.length || totalFailures) {
    lines.push('');
    lines.push('## 需要注意');
    for (const err of uniqueErrors.slice(0, 12)) lines.push(`- ${err}`);
    if (uniqueErrors.length > 12) lines.push(`- 另外還有 ${uniqueErrors.length - 12} 條錯誤，請看原始日誌。`);
  }

  lines.push('');
  lines.push('## Hermes 回報口徑');
  if (status !== 0) {
    lines.push('每日檢查已執行，但下載腳本回報錯誤；請告訴使用者本次新增數、受影響博主，以及原始日誌路徑。');
  } else if (totalNew > 0) {
    lines.push(`每日檢查完成，本次新增下載 ${totalNew} 個影片；列出有新增的博主即可。`);
  } else {
    lines.push('每日檢查完成，沒有新影片；用一句中文回報即可。');
  }

  return {
    markdown: `${lines.join('\n')}\n`,
    json: {
      title,
      time: localTimestamp(now),
      mode,
      status,
      result,
      totalNew,
      totalPending,
      totalFailures,
      runLog,
      accounts: rows
    }
  };
}

function main() {
  if (!fs.existsSync(accountsFile)) throw new Error(`Missing accounts file: ${accountsFile}`);
  fs.mkdirSync(reportsDir, { recursive: true });
  const accounts = readAccounts(accountsFile);
  const parsed = parseRunLog(runLog);
  const rendered = renderReport(accounts, parsed);
  const stamp = localTimestamp().replace(/[-:]/g, '').replace(' ', '-');
  const datedMd = path.join(reportsDir, `${stamp}-${mode}.md`);
  const datedJson = path.join(reportsDir, `${stamp}-${mode}.json`);
  fs.writeFileSync(datedMd, rendered.markdown, 'utf8');
  fs.writeFileSync(datedJson, `${JSON.stringify(rendered.json, null, 2)}\n`, 'utf8');
  fs.writeFileSync(path.join(reportsDir, 'latest.md'), rendered.markdown, 'utf8');
  fs.writeFileSync(path.join(reportsDir, 'latest.json'), `${JSON.stringify(rendered.json, null, 2)}\n`, 'utf8');
  if (shouldPrint) process.stdout.write(rendered.markdown);
}

main();
