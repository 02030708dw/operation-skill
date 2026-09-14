const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs=require('fs'),os=require('os'),path=require('path');
const engine=require('../scripts/facebook_followed_video_engine');
const {closeBrowser,confirmPersistedSession}=require('../scripts/facebook_browser_shutdown');
test('real Chrome retains a cookie through verification reopen and capture profile copy', {skip:process.env.HM_TEST_REAL_CHROME!=='1'}, async()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'hm-cookie-durable-'));
  const profile=path.join(root,'profile');fs.mkdirSync(profile);
  let first,copy;let id=820000;
  const cookies=async browser=>(await engine.cdpCall(browser.ws,{id:id++,method:'Network.getCookies',params:{urls:['https://example.invalid/']}})).result.cookies;
  try {
    first=await engine.startBrowser(profile);
    await engine.cdpCall(first.ws,{id:id++,method:'Network.setCookies',params:{cookies:[{name:'hm_persistence_probe',value:'non-secret-fixture',domain:'.example.invalid',path:'/',secure:true,expires:Math.floor(Date.now()/1000)+86400}]}});
    assert.equal((await cookies(first)).length,1);
    await confirmPersistedSession(first,profile,async reopened=>{
      assert.equal((await cookies(reopened)).filter(c=>c.name==='hm_persistence_probe').length,1);
      return {state:'AVAILABLE'};
    });
    const capture=path.join(root,'capture');fs.cpSync(profile,capture,{recursive:true});
    copy=await engine.startBrowser(capture);
    assert.equal((await cookies(copy)).filter(c=>c.name==='hm_persistence_probe').length,1);
  } finally {
    if(first)await closeBrowser(first);
    if(copy)await closeBrowser(copy);
    fs.rmSync(root,{recursive:true,force:true});
  }
});
