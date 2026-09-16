// Accept only a user-authorized, isolated Google / YouTube Netscape cookie file.
// Never include cookie contents in exceptions or logs.
function parseCookies(text, now = Date.now() / 1000) {
  if (typeof text !== 'string' || text.length > 1024 * 1024) throw Error('INVALID_GOOGLE_COOKIE');
  const cookies = [];
  for (let line of text.split(/\r?\n/)) {
    if (!line.trim() || (line.startsWith('#') && !line.startsWith('#HttpOnly_'))) continue;
    const httpOnly = line.startsWith('#HttpOnly_');
    if (httpOnly) line = line.slice(10);
    const fields = line.split('\t');
    if (fields.length !== 7) throw Error('INVALID_GOOGLE_COOKIE');
    const [domain, subdomains, cookiePath, secure, expiresText, name, value] = fields;
    const host = domain.replace(/^\./, '');
    if (!/^(?:[a-z0-9-]+\.)*(?:google|youtube)\.com$/i.test(host)
        || !['TRUE', 'FALSE'].includes(subdomains) || !['TRUE', 'FALSE'].includes(secure)
        || !cookiePath.startsWith('/') || !/^[A-Za-z0-9_!#$%&'*+.^`|~-]+$/.test(name)
        || !/^\d+$/.test(expiresText) || /[\x00-\x1f\x7f]/.test(value)) throw Error('INVALID_GOOGLE_COOKIE');
    const expires = Number(expiresText);
    if (expires && expires <= now) continue;
    cookies.push({domain, path: cookiePath, name, value, secure: secure === 'TRUE', httpOnly,
      ...(expires ? {expires} : {})});
  }
  if (!cookies.some(c => ['SID', 'SAPISID', '__Secure-3PAPISID'].includes(c.name)))
    throw Error('INVALID_GOOGLE_COOKIE');
  return cookies;
}
module.exports = {parseCookies};
