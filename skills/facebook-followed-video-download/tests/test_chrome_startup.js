const assert = require('node:assert/strict');
const childProcess = require('node:child_process');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const enginePath = path.join(__dirname, '../scripts/facebook_followed_video_engine.js');

function loadEngine(t, options = {}) {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'fb-chrome-startup-'));
  t.after(() => fs.rmSync(temporary, { recursive: true, force: true }));
  const chrome = path.join(temporary, '浏览器 with spaces.exe');
  fs.writeFileSync(chrome, 'placeholder executable');
  const accounts = path.join(temporary, 'accounts.txt');
  fs.writeFileSync(accounts, 'creator\thttps://www.facebook.com/example/reels/\n');
  const calls = [];
  const processes = {
    spawnSync(command, args, settings) {
      calls.push({ kind: 'sync', command, args, settings });
      if (options.native) return childProcess.spawnSync(command, args, settings);
      return { status: 0, stdout: 'version', stderr: '' };
    },
    spawn(command, args, settings) {
      calls.push({ kind: 'spawn', command, args, settings });
      if (options.native) return childProcess.spawn(command, args, settings);
      const child = new EventEmitter();
      child.stderr = new EventEmitter();
      child.kill = () => {};
      if (options.spawnError) {
        child.exitCode = null;
        process.nextTick(() => child.emit('error', new Error('EACCES: cannot execute Chrome')));
      } else {
        child.pid = 987654;
        // Stop before any Facebook request while exercising the real main/start/retry path.
        child.exitCode = 1;
      }
      return child;
    },
  };
  const sandbox = {
    module: { exports: {} }, Buffer,
    console: { log() {}, error() {} },
    setTimeout: options.native ? setTimeout : callback => setTimeout(callback, 0),
    clearTimeout,
    process: {
      platform: options.platform || 'win32',
      env: options.native ? process.env : { PATH: temporary },
      argv: ['node', enginePath, '--accounts', accounts, '--chrome', chrome,
        ...(options.execute ? [] : ['--dry-run'])],
    },
    require(name) {
      if (name === 'child_process') return processes;
      if (name === 'os') return { ...os, tmpdir: () => temporary };
      if (name === 'ws') return require('../scripts/node_modules/ws');
      return require(name);
    },
  };
  vm.runInNewContext(fs.readFileSync(enginePath, 'utf8'), sandbox, { filename: enginePath });
  return { engine: sandbox.module.exports, chrome, temporary, calls };
}

test('Windows preview and capture enter only isolated headless Chrome, including retry', async t => {
  for (const execute of [false, true]) {
    const { engine, chrome, calls } = loadEngine(t, { execute });
    await assert.rejects(engine.main(), error => error.code === engine.ERROR_CODES.CHROME_START);
    const chromeCalls = calls.filter(call => call.command === chrome);
    assert.equal(chromeCalls.length, 2, 'only the two headless startup attempts may launch Chrome');
    for (const call of chromeCalls) {
      assert.equal(call.kind, 'spawn');
      assert.ok(call.args.some(arg => arg === '--headless=new' || arg === '--headless'));
      assert.ok(call.args.some(arg => arg.startsWith('--user-data-dir=')));
      assert.ok(call.args.includes('--no-first-run'));
      assert.ok(call.args.includes('--no-default-browser-check'));
      assert.equal(call.settings.windowsHide, true);
      assert.equal(call.args.includes('--version'), false);
    }
    assert.equal(calls.some(call => call.command === 'yt-dlp' && call.args[0] === '--version'), execute);
  }
});

test('Windows preflight inspects executable paths and PATH without starting a process', t => {
  const { engine, chrome, calls } = loadEngine(t);
  for (let attempt = 0; attempt < 3; attempt++) {
    assert.equal(engine.assertChromeAvailable(chrome), chrome);
    assert.equal(engine.assertChromeAvailable(path.basename(chrome)), chrome);
    assert.equal(engine.assertChromeAvailable(path.basename(chrome, '.exe')), chrome);
  }
  assert.equal(calls.length, 0);
});

test('missing Chrome and directories fail without a browser fallback', t => {
  const { engine, chrome, temporary, calls } = loadEngine(t);
  fs.unlinkSync(chrome);
  assert.throws(() => engine.assertChromeAvailable(chrome), /Missing Chrome/);
  assert.throws(() => engine.assertChromeAvailable(temporary), /Missing Chrome/);
  assert.equal(calls.length, 0);
});

test('an installed but unlaunchable Chrome reports CDP failure without an unhandled error', async t => {
  const { engine, chrome, calls } = loadEngine(t, { spawnError: true });
  await assert.rejects(engine.main(), error =>
    error.code === engine.ERROR_CODES.CHROME_START && /EACCES/.test(error.message));
  assert.equal(calls.filter(call => call.command === chrome).length, 2);
  assert.equal(calls.some(call => call.kind === 'sync'), false);
});

test('macOS and Linux retain the existing Chrome version probe', t => {
  for (const platform of ['darwin', 'linux']) {
    const { engine, chrome, calls } = loadEngine(t, { platform });
    assert.equal(engine.assertChromeAvailable(chrome), chrome);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].kind, 'sync');
    assert.equal(calls[0].command, chrome);
    assert.deepEqual(Array.from(calls[0].args), ['--version']);
  }
});

test('native Windows Chrome connects to CDP with no normal-browser version launch', {
  skip: process.platform !== 'win32', timeout: 90000,
}, async t => {
  const { engine, temporary, calls } = loadEngine(t, { native: true });
  const executable = engine.detectChrome();
  for (let attempt = 0; attempt < 3; attempt++) engine.assertChromeAvailable(executable);
  assert.equal(calls.length, 0);
  const profile = path.join(temporary, 'isolated-native-profile');
  fs.mkdirSync(profile);
  let browser;
  try {
    browser = await engine.startBrowser(profile, executable);
    assert.ok(browser.port > 0);
    assert.equal(browser.ws.readyState, 1);
    const launches = calls.filter(call => call.command === executable);
    assert.ok(launches.length > 0);
    assert.ok(launches.every(call => call.kind === 'spawn'
      && call.args.some(arg => arg.startsWith('--headless'))
      && call.args.includes(`--user-data-dir=${profile}`)
      && !call.args.includes('--version')));
  } finally {
    if (browser) {
      browser.ws.close();
      await engine.stopChrome(browser.chrome);
    }
  }
});
