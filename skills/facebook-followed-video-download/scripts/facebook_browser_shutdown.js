// Chrome must flush its cookie database before a verified profile can be promoted.
const engine = require('./facebook_followed_video_engine');
let sequence = 810000;
function exited(chrome) {return chrome.exitCode !== null || !!chrome.signalCode;}
async function closeBrowser(browser, timeoutMs = 8000) {
  if (!browser || browser.hmClosed) return;
  const chrome = browser.chrome;
  let timer, onExit;
  const exit = new Promise(resolve => {
    if (exited(chrome)) return resolve();
    onExit = resolve;
    chrome.once('exit', onExit);
    timer = setTimeout(resolve, timeoutMs);
  });
  try {
    if (!exited(chrome)) {
      try {await engine.cdpCall(browser.ws, {id: sequence++, method: 'Browser.close', params: {}}, Math.min(timeoutMs, 5000));}
      catch (_) { /* Chrome may close CDP before sending the acknowledgement. */ }
    }
    await exit;
    if (!exited(chrome) || chrome.exitCode !== 0) {
      if (!exited(chrome)) await engine.stopChrome(chrome);
      throw Error('SESSION_PERSIST_FAILED');
    }
  } finally {
    clearTimeout(timer);
    if (onExit) chrome.removeListener('exit', onExit);
    browser.ws.close();
    browser.hmClosed = true;
  }
}
async function confirmPersistedSession(browser, profile, verify) {
  await closeBrowser(browser);
  const reopened = await engine.startBrowser(profile);
  try {return await verify(reopened);}
  finally {await closeBrowser(reopened);}
}
module.exports = {closeBrowser, confirmPersistedSession};
