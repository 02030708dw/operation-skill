const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('fs'),os=require('os'),path=require('path');
const engine=require('../scripts/facebook_followed_video_engine'),ui=require('../scripts/facebook_interactive_browser');
test('interactive input is bounded and does not accept URLs or executable commands',async()=>{
 for(const input of [{op:'navigate',url:'https://example.invalid'},{op:'key',key:'javascript:alert(1)'},{op:'pointer',event:'mousePressed',x:-1,y:1},{op:'pointer',event:'mousePressed',x:1,y:701},{op:'text',text:'a'.repeat(1001)}])assert.throws(()=>ui.validate(input));
 const old=engine.cdpCall,calls=[];engine.cdpCall=async(ws,request)=>{calls.push(request);return request.method==='Runtime.evaluate'?{result:{result:{value:'www.facebook.com'}}}:request.method==='Page.captureScreenshot'?{result:{data:'YQ=='}}:{result:{}}};
 try{
  const password='private-input-\";alert(1)';await ui.command({ws:{}},{op:'text',text:password});
  assert.equal(calls.at(-1).method,'Input.insertText');assert.equal(calls.at(-1).params.text,password);
  assert.ok(calls.filter(c=>c.method==='Runtime.evaluate').every(c=>!c.params.expression.includes(password)));
  assert.equal((await ui.command({ws:{}},{op:'snapshot'})).image,'YQ==');
  await ui.open({ws:{}});assert.equal(calls.at(-1).params.url,'https://www.facebook.com/login/');
 }finally{engine.cdpCall=old;}
});
test('official login profile disables password storage',()=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'hm-browser-'));
 try{ui.privatePreferences(dir);const file=path.join(dir,'Default/Preferences'),prefs=JSON.parse(fs.readFileSync(file));assert.equal(prefs.credentials_enable_service,false);assert.equal(prefs.profile.password_manager_enabled,false);assert.equal(fs.statSync(file).mode&0o777,0o600);}finally{fs.rmSync(dir,{recursive:true,force:true});}
});
