const assert = require('node:assert/strict');
const {test} = require('node:test');
const {parseCookies, importCookies, verifyIdentity} = require('../scripts/facebook_cookie_import');
test('accepts extension Cookie string, pins scope and persists only allowed session cookies', () => {
  const cookies = parseCookies('c_user=123456; xs=example%3Avalue; datr=device; unrelated=secret;', 0);
  assert.deepEqual(cookies.map(c=>c.name), ['c_user', 'xs', 'datr']);
  assert.ok(cookies.every(c=>c.domain === '.facebook.com' && c.path === '/' && c.secure && c.expires === 2592000));
  assert.equal(cookies.find(c=>c.name === 'xs').httpOnly, true);
});
test('rejects access tokens, missing auth cookies, duplicate identity, injections and oversized data', () => {
  for(const value of ['EAAAA-example-token', '', 'c_user=123456', 'xs=only', 'c_user=x; xs=y', 'c_user=123456; c_user=654321; xs=test', 'c_user=123456; xs=test\r\nHost: evil', 'c_user=123456; xs="bad"', 'c_user=123456; xs='+'a'.repeat(17000)]) {
    assert.throws(()=>parseCookies(value), /^Error: INVALID_FACEBOOK_COOKIE$/);
  }
});
test('verifies imported identity; never echoes browser text or Cookie and never treats checkpoint as success', async () => {
  for(const state of ['AVAILABLE','VERIFICATION_REQUIRED','LOGIN_REQUIRED']) {
    const calls=[];
    const cdp=async (_,call)=>{calls.push(call);return {result:{cookies:[{name:'c_user',value:'123456'},{name:'xs',value:'private'}]}};};
    const result=await importCookies({ws:{}}, 'c_user=123456; xs=private', async()=>({state,errorText:'private',reasonCode:state==='VERIFICATION_REQUIRED'?'FACEBOOK_CHECKPOINT':null}), cdp);
    assert.equal(result.state,state);assert.ok(!JSON.stringify(result).includes('private'));
    assert.equal(calls[0].method,'Network.setCookies');
  }
  const mismatch=await importCookies({ws:{}}, 'c_user=123456; xs=private', async()=>({state:'AVAILABLE'}),async()=>({result:{cookies:[{name:'c_user',value:'654321'}]}}));
  assert.equal(mismatch.reasonCode,'FACEBOOK_COOKIE_ACCOUNT_MISMATCH');
  let calls=0;
  const invalid=await importCookies({}, 'EAAAA',async()=>{calls++;});
  assert.equal(invalid.reasonCode,'INVALID_FACEBOOK_COOKIE');assert.equal(calls,0);
});

test('identity remains pinned after an interactive security checkpoint', async () => {
  const result = await verifyIdentity({ws:{}}, {state:'AVAILABLE'}, '123456', async()=>({result:{cookies:[{name:'c_user',value:'654321'},{name:'xs',value:'session'}]}}));
  assert.equal(result.reasonCode, 'FACEBOOK_COOKIE_ACCOUNT_MISMATCH');
});
