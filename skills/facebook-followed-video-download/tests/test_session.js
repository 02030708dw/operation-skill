const test=require('node:test');
const assert=require('node:assert/strict');
const {totp}=require('../scripts/facebook_session');
const {classifyDownloadError}=require('../scripts/facebook_followed_video_engine');
test('standard two-factor reference vector',()=>{
  assert.equal(totp('GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ',59000),'287082');
});
test('access checks and temporary failures have different retry policies',()=>{
  assert.equal(classifyDownloadError('only available for registered users'),'FACEBOOK_ACCESS_REQUIRED');
  assert.equal(classifyDownloadError('HTTP Error 503'),'FACEBOOK_NETWORK_ERROR');
  assert.equal(classifyDownloadError('Read timed out'),'FACEBOOK_NETWORK_ERROR');
  assert.equal(classifyDownloadError('Unsupported URL'),'FACEBOOK_DOWNLOAD_UNSUPPORTED');
});
