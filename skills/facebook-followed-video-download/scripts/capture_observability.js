'use strict';
const fs = require('fs'), path = require('path'), crypto = require('crypto');

// Deliberately accepts counters and safe codes only, never URLs, captions or cookies.
function createMetrics(resultPath = '') {
  const attemptId = crypto.randomBytes(16).toString('hex');
  const totals = {};
  const accessEvidence = [];
  function record(stage, outcome, elapsedMs = 0, errorCode = null, count = 1, evidence = null) {
    if (!/^[a-z_]{1,40}$/.test(stage) || !['success', 'failure', 'count'].includes(outcome)) throw new Error('Invalid metric');
    const row = { schemaVersion: 1, attemptId, occurredAt: new Date().toISOString(), stage, outcome,
      elapsedMs: Math.max(0, Math.round(elapsedMs)), count,
      errorCode: /^[A-Z][A-Z0-9_]{0,79}$/.test(errorCode || '') ? errorCode : null };
    if (evidence) row.evidence = evidence;
    const total = totals[stage] || (totals[stage] = { count: 0, failures: 0, elapsedMs: 0 });
    total.count += count; total.failures += outcome === 'failure' ? 1 : 0; total.elapsedMs += row.elapsedMs;
    if (resultPath) {
      fs.mkdirSync(path.dirname(resultPath), { recursive: true });
      const fd = fs.openSync(path.join(path.dirname(resultPath), 'process-metrics.jsonl'), 'a+', 0o600);
      try {
        const size = fs.fstatSync(fd).size, tail = Buffer.alloc(1);
        if (size) { fs.readSync(fd, tail, 0, 1, size - 1); if (tail[0] !== 10) fs.writeSync(fd, '\n'); }
        fs.writeSync(fd, JSON.stringify(row) + '\n'); fs.fsyncSync(fd);
      } finally { fs.closeSync(fd); }
    }
    return row;
  }
  function evidence(value) {
    if (!value || !['EXTRACTOR','PAGE_DIALOG','PAGE_BODY','PAGE_URL'].includes(value.source)
      || !['ACCOUNT_SUSPENDED','HTTP_429','EXPLICIT_RATE_LIMIT','IDENTITY_CHECK','LOGIN_REQUIRED','CHECKPOINT_PATH','LOGIN_PATH','GENERIC_RETRY'].includes(value.signal)
      || !['FACEBOOK_ACCOUNT_SUSPENDED','FACEBOOK_RATE_LIMITED','FACEBOOK_VERIFICATION_REQUIRED','FACEBOOK_LOGIN_REQUIRED','FACEBOOK_NETWORK_ERROR'].includes(value.code)) throw Error('Invalid access evidence');
    const safe = {source:value.source, signal:value.signal};
    const row = record('access_signal', 'failure', 0, value.code, 1, safe);
    if (accessEvidence.length < 16) accessEvidence.push({...safe, errorCode:row.errorCode, occurredAt:row.occurredAt});
  }
  function sync(stage, action) {
    const start = Date.now();
    try { const result = action(); record(stage, 'success', Date.now()-start); return result; }
    catch (error) { record(stage, 'failure', Date.now()-start, error.code); throw error; }
  }
  async function asyncCall(stage, action) {
    const start = Date.now();
    try { const result = await action(); record(stage, 'success', Date.now()-start); return result; }
    catch (error) { record(stage, 'failure', Date.now()-start, error.code); throw error; }
  }
  return { record, evidence, sync, asyncCall, summary: () => ({ schemaVersion: 1, attemptId, stages: totals, accessEvidence }) };
}
module.exports = { createMetrics };
