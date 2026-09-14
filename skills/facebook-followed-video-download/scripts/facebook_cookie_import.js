// Cookie strings are private input, never persisted outside the staged browser profile.
const engine = require('./facebook_followed_video_engine');
const {perform} = require('./facebook_session');
let sequence = 180000;
const allowed = new Set(['c_user', 'xs', 'datr', 'sb', 'fr', 'wd', 'presence', 'locale', 'ps_l', 'ps_n', 'dpr']);
function parseCookies(raw, now = Date.now()) {
  if (typeof raw !== 'string' || !raw.trim() || raw.length > 16384 || /[^\x20-\x7e]/.test(raw)) throw Error('INVALID_FACEBOOK_COOKIE');
  const seen = new Set(), cookies = [];
  for (const part of raw.split(';')) {
    if (!part.trim()) continue;
    const match = part.trim().match(/^([A-Za-z_][A-Za-z0-9_]*)=([^;\s]*)$/);
    if (!match || seen.has(match[1])) throw Error('INVALID_FACEBOOK_COOKIE');
    const [, name, value] = match;
    seen.add(name);
    // Only Facebook session cookies can be set, never arbitrary domains/URLs.
    if (!allowed.has(name)) continue;
    if (!value || value.length > 4096 || /["\\]/.test(value)) throw Error('INVALID_FACEBOOK_COOKIE');
    cookies.push({name, value, domain: '.facebook.com', path: '/', secure: true,
      httpOnly: ['xs', 'datr', 'sb', 'fr'].includes(name), sameSite: 'Lax',
      expires: Math.floor(now / 1000) + 30 * 86400});
  }
  const user = cookies.find(c => c.name === 'c_user');
  if (!user || !/^\d{5,30}$/.test(user.value) || !cookies.some(c => c.name === 'xs')) throw Error('INVALID_FACEBOOK_COOKIE');
  return cookies;
}
async function verifyIdentity(browser, result, expected, cdp = engine.cdpCall) {
  if (result.state !== 'AVAILABLE' || !expected) return result;
  const current = await cdp(browser.ws, {id: sequence++, method: 'Network.getCookies', params: {urls: ['https://www.facebook.com/']}});
  if (!current.result.cookies.some(c => c.name === 'c_user' && c.value === expected) || !current.result.cookies.some(c => c.name === 'xs' && c.value))
    return {state: 'LOGIN_REQUIRED', reasonCode: 'FACEBOOK_COOKIE_ACCOUNT_MISMATCH'};
  return result;
}
async function importCookies(browser, raw, verify = perform, cdp = engine.cdpCall) {
  let cookies;
  try {cookies = parseCookies(raw);} catch (_) {return {state: 'LOGIN_REQUIRED', reasonCode: 'INVALID_FACEBOOK_COOKIE'};}
  raw = null;
  const expected = cookies.find(c => c.name === 'c_user').value;
  try {
    const set = await cdp(browser.ws, {id: sequence++, method: 'Network.setCookies', params: {cookies}});
    if (set.error) throw Error('Cookie import failed');
    cookies = null;
    await cdp(browser.ws, {id: sequence++, method: 'Emulation.setDeviceMetricsOverride', params: {width: 1100, height: 700, deviceScaleFactor: 1, mobile: false}});
    const result = await verify(browser, {action: 'verify'});
    const checked = await verifyIdentity(browser, result, expected, cdp);
    return {state: checked.state, reasonCode: checked.reasonCode || null};
  } catch (_) {return {state: 'COOLDOWN', reasonCode: 'SESSION_CHECK_FAILED'};}
  finally {cookies = null;}
}
module.exports = {parseCookies, importCookies, verifyIdentity};
