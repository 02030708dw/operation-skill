const {test} = require('node:test');
const assert = require('node:assert/strict');
const {parseCookies} = require('../scripts/google_cookie_import');
const row = (domain='.youtube.com', expiry='2000000000') => `${domain}\tTRUE\t/\tTRUE\t${expiry}\tSAPISID\tfixture`;
test('accepts private Netscape export, preserves HttpOnly and ignores expired rows', () => {
  const cookies=parseCookies('# Netscape HTTP Cookie File\n#HttpOnly_'+row()+'\n'+row('.google.com','1'),1000);
  assert.equal(cookies.length,1);assert.equal(cookies[0].httpOnly,true);assert.equal(cookies[0].secure,true);
});
test('rejects unrelated domains, deceptive suffixes, malformed rows and missing authentication', () => {
  for(const input of [row('.example.com'),row('.google.com.example.com'),row('youtube.com.evil'),row().replace('SAPISID','PREF'),'bad',row('.youtube.com','1')])
    assert.throws(()=>parseCookies(input,1000),/INVALID_GOOGLE_COOKIE/);
});
test('supports session cookies and Google subdomains', () => {
  const cookies=parseCookies(row('.accounts.google.com','0'),1000);
  assert.equal(cookies[0].expires,undefined);
});
