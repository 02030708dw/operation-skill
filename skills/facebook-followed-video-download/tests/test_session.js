const test=require('node:test');
const assert=require('node:assert/strict');
const {totp,loginReason,waitForPage}=require('../scripts/facebook_session');
const {EventEmitter}=require('events');
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

test("login diagnostics return safe reason codes",()=>{ assert.equal(loginReason("The password is incorrect"),"FACEBOOK_PASSWORD_REJECTED"); assert.equal(loginReason("The email address isn’t public"),"FACEBOOK_LOGIN_NOT_COMPLETED"); });

test('login waits through a replaced navigation context and an unloaded document',async()=>{
  const ws=new EventEmitter();let calls=0;
  ws.send=raw=>{
    const {id}=JSON.parse(raw);calls++;
    queueMicrotask(()=>ws.emit('message',JSON.stringify(calls===1
      ? {id,error:{message:'Execution context was destroyed'}}
      : {id,result:{result:{value:calls>=3}}})));
  };
  await waitForPage(ws);
  assert.equal(calls,3);
  assert.equal(ws.listenerCount('message'),0);
});
