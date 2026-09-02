const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
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
  publishedAtFromMetadata,
  waitForChrome,
} = require('../scripts/facebook_followed_video_engine.js');
const { parseRunLog, renderReport } = require('../scripts/facebook_followed_video_report.js');

function reel(id) {
  return `https://www.facebook.com/reel/${id}`;
}

test('per-video event line is structured and includes batch progress', () => {
  const video = {
    status: 'downloaded',
    canonicalUrl: reel(123),
    localPath: '/tmp/video.mp4',
  };

  const line = videoResultEventLine('creator-one', video, 3, 30);
  assert.ok(line.startsWith(VIDEO_RESULT_EVENT_PREFIX));
  assert.deepEqual(
    JSON.parse(line.slice(VIDEO_RESULT_EVENT_PREFIX.length)),
    {
      schemaVersion: '1.0',
      event: 'video-result',
      source: 'creator-one',
      completed: 3,
      total: 30,
      video,
    },
  );
});

test('normalizes current public Facebook video URL formats', () => {
  assert.equal(
    normalizeVideoUrl('https://www.facebook.com/example/videos/12345/?ref=share'),
    'https://www.facebook.com/watch/?v=12345',
  );
  assert.equal(
    normalizeVideoUrl('https://www.facebook.com/share/r/AbCdEf/?mibextid=x'),
    'https://www.facebook.com/share/r/AbCdEf/',
  );
  assert.equal(
    normalizeVideoUrl('https://fb.watch/ZyX987/?ref=foo'),
    'https://fb.watch/ZyX987/',
  );
  for (const url of [
    'https://www.facebook.com/reel/12345',
    'https://www.facebook.com/watch/?v=12345',
    'https://www.facebook.com/share/v/AbCdEf/',
    'https://fb.watch/ZyX987/',
  ]) assert.equal(isSupportedVideoUrl(url), true);
});

test('direct video and share URLs are scanned as a single candidate', () => {
  const share = 'https://www.facebook.com/share/r/AbCdEf/';
  assert.deepEqual(pageCandidates(share), ['https://www.facebook.com/share/r/AbCdEf']);
  const watch = 'https://fb.watch/ZyX987/';
  assert.deepEqual(pageCandidates(watch), ['https://fb.watch/ZyX987']);
});

test('discovery errors prioritize access and repeated CDP timeout', () => {
  assert.equal(
    classifyDiscoveryFailure([
      { code: ERROR_CODES.CDP_TIMEOUT, message: 'timeout' },
      { code: ERROR_CODES.ACCESS_REQUIRED, message: 'login' },
    ], false).code,
    ERROR_CODES.ACCESS_REQUIRED,
  );
  assert.equal(
    classifyDiscoveryFailure([
      { code: ERROR_CODES.CDP_TIMEOUT, message: 'timeout' },
    ], false).code,
    ERROR_CODES.CDP_TIMEOUT,
  );
  assert.equal(
    classifyDiscoveryFailure([], true).code,
    ERROR_CODES.LAYOUT_UNSUPPORTED,
  );
  assert.equal(
    classifyDiscoveryFailure([], false).code,
    ERROR_CODES.DISCOVERY_EMPTY,
  );
});

test('bounded browser extraction expression compiles without whole-page HTML', () => {
  const expression = discoveryExpression();
  assert.doesNotThrow(() => new Function(`return ${expression};`));
  assert.equal(expression.includes('document.documentElement.innerHTML'), false);
  assert.equal(expression.includes('scriptBudget = 2000000'), true);
});

test('bounded browser extraction expression executes and decodes escaped URLs', () => {
  const expression = discoveryExpression();
  const document = {
    body: { innerText: 'Public Facebook page' },
    querySelector: () => null,
    querySelectorAll: selector => {
      if (selector === 'a[href]') {
        return [{ href: String.raw`https:\/\/www.facebook.com\/reel\/12345` }];
      }
      if (selector === 'script') {
        return [{ textContent: String.raw`{"url":"https:\/\/fb.watch\/AbCdEf\/"}` }];
      }
      return [];
    },
  };
  const location = {
    href: 'https://www.facebook.com/example/reels/',
    origin: 'https://www.facebook.com',
  };
  const evaluate = new Function('document', 'location', `return (${expression});`);
  const snapshot = JSON.parse(evaluate(document, location));

  assert.deepEqual(snapshot.urls, [
    'https://www.facebook.com/reel/12345',
    'https://fb.watch/AbCdEf',
  ]);
});

test('reads Chrome dynamic DevToolsActivePort and waits for its endpoint', async () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'fb-cdp-port-'));
  const server = http.createServer((request, response) => {
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({ Browser: 'Chrome/test' }));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  try {
    fs.writeFileSync(
      path.join(temporary, 'DevToolsActivePort'),
      `${port}\n/devtools/browser/test\n`,
      'utf8',
    );
    assert.equal(readDevToolsActivePort(temporary), port);
    assert.equal(await waitForChrome({ exitCode: null }, temporary, []), port);
  } finally {
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});

test('classifies Chrome early exit with bounded startup detail', async () => {
  await assert.rejects(
    waitForChrome({ exitCode: 1 }, os.tmpdir(), ['profile lock\n']),
    error => {
      assert.equal(error.code, ERROR_CODES.CHROME_START);
      assert.equal(error.message.includes('profile lock'), true);
      return true;
    },
  );
});

test('rebuilds the CDP page session once after Runtime.evaluate timeout', async () => {
  let attempts = 0;
  let oldClosed = false;
  let cookiesInjected = false;
  const browser = {
    port: 12345,
    ws: { close: () => { oldClosed = true; } },
  };
  const recovered = await discoverWithRecovery(
    browser,
    'https://www.facebook.com/example/reels/',
    new Set(),
    10,
    {
      discoverOnPage: async () => {
        attempts++;
        if (attempts === 1) {
          const error = new Error('timeout');
          error.code = ERROR_CODES.CDP_TIMEOUT;
          throw error;
        }
        return { urls: [reel(123)], layoutUnsupported: false };
      },
      connectTab: async (port, forceNew) => {
        assert.equal(port, 12345);
        assert.equal(forceNew, true);
        return { close() {} };
      },
      injectCookies: async () => { cookiesInjected = true; },
    },
  );
  assert.equal(attempts, 2);
  assert.equal(oldClosed, true);
  assert.equal(cookiesInjected, true);
  assert.deepEqual(recovered.urls, [reel(123)]);
});

test('returns stable CDP_RUNTIME_TIMEOUT after recovery also times out', async () => {
  const browser = { port: 12345, ws: { close() {} } };
  await assert.rejects(
    discoverWithRecovery(browser, 'https://www.facebook.com/example/', new Set(), 0, {
      discoverOnPage: async () => {
        const error = new Error('timeout');
        error.code = ERROR_CODES.CDP_TIMEOUT;
        throw error;
      },
      connectTab: async () => ({ close() {} }),
      injectCookies: async () => {},
    }),
    error => {
      assert.equal(error.code, ERROR_CODES.CDP_TIMEOUT);
      return true;
    },
  );
});

test('exact timestamp takes precedence over upload date', () => {
  const timestamp = Date.UTC(2026, 7, 29, 14, 35, 42) / 1000;

  assert.deepEqual(
    publishedAtFromMetadata({ timestamp, upload_date: '20260829' }),
    {
      publishedAt: '2026-08-29T14:35:42',
      publishedAtPrecision: 'SECOND',
    },
  );
});

test('release timestamp supplies seconds when timestamp is invalid', () => {
  const releaseTimestamp = Date.UTC(2026, 7, 29, 9, 8, 7) / 1000;

  assert.deepEqual(
    publishedAtFromMetadata({
      timestamp: 'not-a-timestamp',
      release_timestamp: String(releaseTimestamp),
      upload_date: '20260829',
    }),
    {
      publishedAt: '2026-08-29T09:08:07',
      publishedAtPrecision: 'SECOND',
    },
  );
});

test('upload date is a date-precision fallback only', () => {
  assert.deepEqual(
    publishedAtFromMetadata({ upload_date: '20260829' }),
    {
      publishedAt: '2026-08-29T00:00:00',
      publishedAtPrecision: 'DATE',
    },
  );
});

test('invalid publish metadata returns no publish time', () => {
  assert.deepEqual(
    publishedAtFromMetadata({ timestamp: Infinity, upload_date: '20260230' }),
    { publishedAt: null, publishedAtPrecision: null },
  );
});

test('first daily run selects only the latest ten videos', () => {
  const newestFirst = Array.from({ length: 14 }, (_, index) => reel(200 - index));

  assert.deepEqual(
    selectVideoUrls(newestFirst, new Set(), 'daily', 10),
    newestFirst.slice(0, 10),
  );
});

test('later daily run with no updates falls back to the latest ten videos', () => {
  const archivedNewestFirst = Array.from({ length: 14 }, (_, index) => reel(300 - index));
  const archived = new Set(archivedNewestFirst.map(videoKey));

  assert.deepEqual(
    selectVideoUrls(archivedNewestFirst, archived, 'daily', 10),
    archivedNewestFirst.slice(0, 10),
  );
});

test('later daily run selects two updates without filling eight older videos', () => {
  const updates = [reel(302), reel(301)];
  const archivedBoundary = reel(300);
  const olderUnarchived = [reel(299), reel(298)];

  assert.deepEqual(
    selectVideoUrls(
      [...updates, archivedBoundary, ...olderUnarchived],
      new Set([videoKey(archivedBoundary)]),
      'daily',
      10,
    ),
    updates,
  );
});

test('later daily run selects every update even when more than ten exist', () => {
  const updates = Array.from({ length: 30 }, (_, index) => reel(600 - index));
  const archivedBoundary = reel(500);

  assert.deepEqual(
    selectVideoUrls(
      [...updates, archivedBoundary],
      new Set([videoKey(archivedBoundary)]),
      'daily',
      10,
    ),
    updates,
  );
});

test('updates from one page prevent another page from adding fallback videos', () => {
  const updates = [reel(702), reel(701)];
  const archivedBoundary = reel(700);
  const archived = new Set([videoKey(archivedBoundary), videoKey(reel(699))]);

  assert.deepEqual(
    selectDailyVideoUrls(
      [[...updates, archivedBoundary], [reel(699), reel(698)]],
      archived,
      10,
    ),
    updates,
  );
});

test('full import still excludes archived videos', () => {
  const discovered = [reel(3), reel(2), reel(1)];

  assert.deepEqual(
    selectVideoUrls(discovered, new Set([videoKey(reel(2))]), 'full', 0),
    [reel(3), reel(1)],
  );
});

test('archive-only video is classified without replacing a retained local file', () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'fb-archive-test-'));
  const url = reel(987654321);
  const archivePath = path.join(temporary, '.yt-dlp-archive.txt');
  try {
    fs.writeFileSync(archivePath, 'facebook 987654321\n', 'utf8');
    assert.equal(isYtDlpArchived(temporary, url), true);
    assert.deepEqual(
      existingVideoState(temporary, url, '987654321'),
      { status: 'archived-existing', localPath: null },
    );

    const retained = path.join(temporary, '20260902_987654321_creator.mp4');
    fs.writeFileSync(retained, 'video', 'utf8');
    assert.deepEqual(
      existingVideoState(temporary, url, '987654321'),
      { status: 'local-existing', localPath: retained },
    );
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});

test('daily report recognizes the recent-window selection summary', () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'fb-report-test-'));
  const logPath = path.join(temporary, 'daily.log');
  try {
    fs.writeFileSync(
      logPath,
      '=== creator ===\n找到影片: 16，本次選取: 10\n成功: 10/10\n',
      'utf8',
    );
    const parsed = parseRunLog(logPath);
    const creator = parsed.byFolder.get('creator');
    assert.equal(creator.found, 16);
    assert.equal(creator.pending, 10);
    assert.equal(creator.ok, 10);
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});

test('daily report treats archive-only fallback as completed with no new video', () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'fb-report-archive-test-'));
  const logPath = path.join(temporary, 'daily.log');
  try {
    fs.writeFileSync(
      logPath,
      '=== creator ===\n找到影片: 12，本次選取: 10\n已歸檔跳過: 10\n成功: 0/0\n',
      'utf8',
    );
    const parsed = parseRunLog(logPath);
    const rendered = renderReport([{ folder: 'creator', url: reel(1) }], parsed);
    assert.equal(rendered.json.totalNew, 0);
    assert.equal(rendered.json.totalFailures, 0);
    assert.equal(rendered.json.totalArchivedExisting, 10);
    assert.equal(rendered.json.accounts[0].state, '無新增，已歸檔 10');
    assert.match(rendered.markdown, /沒有新影片/);
    assert.doesNotMatch(rendered.markdown, /有處理失敗/);
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});
