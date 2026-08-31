const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const {
  VIDEO_RESULT_EVENT_PREFIX,
  selectDailyVideoUrls,
  selectVideoUrls,
  videoKey,
  videoResultEventLine,
  publishedAtFromMetadata,
} = require('../scripts/facebook_followed_video_engine.js');
const { parseRunLog } = require('../scripts/facebook_followed_video_report.js');

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
