// A fresh official media-page visit per scan; no private response bodies are logged.
const engine=require('../../facebook-followed-video-download/scripts/facebook_followed_video_engine');
const {startBrowser,closeBrowser}=require('./hm_x_browser');
const {parseFeed}=require('./hm_x_feed');
let seq=970000;
const call=(b,method,params={})=>engine.cdpCall(b.ws,{id:seq++,method,params},10000);
async function discover(url,limit,profile){
 const u=new URL(url);const match=/^\/([A-Za-z0-9_]{1,15})\/media$/.exec(u.pathname);
 if(u.origin!=='https://x.com'||u.search||u.hash||!match||!Number.isInteger(limit)||limit<1||limit>10)throw Error('INVALID_SOURCE');
 const handle=match[1],browser=await startBrowser(profile);
 const pending=new Set(),finished=new Set(),entries=new Map();let validated=false,sourceExhausted=false,errorCode;
 const event=data=>{try{
  const e=JSON.parse(data),p=e.params||{};
  if(e.method==='Network.responseReceived'){
   const response=new URL(p.response.url);
   if(response.origin==='https://x.com'&&/^\/i\/api\/graphql\/[^/]+\/UserMedia$/.test(response.pathname)){
    if(p.response.status===429)errorCode='RATE_LIMITED';
    else if([401,403].includes(p.response.status))errorCode='LOGIN_REQUIRED';
    else if(p.response.status===200)pending.add(p.requestId);
   }
  }
  if(e.method==='Network.loadingFinished'&&pending.has(p.requestId))finished.add(p.requestId);
 }catch(_){}};
 browser.ws.on('message',event);
 try{
  await call(browser,'Network.enable');await call(browser,'Page.navigate',{url});
  const deadline=Date.now()+90000;let scrolls=0,lastScroll=Date.now();
  while(Date.now()<deadline){
   for(const id of [...finished]){
    finished.delete(id);pending.delete(id);
    const response=await call(browser,'Network.getResponseBody',{requestId:id});
    const body=response.result.base64Encoded?Buffer.from(response.result.body,'base64').toString():response.result.body;
    const page=parseFeed(JSON.parse(body),handle);
    if(page.errorCode)errorCode=page.errorCode;
    if(page.validated){validated=true;sourceExhausted=page.sourceExhausted;for(const row of page.entries)entries.set(row.id,row);}
   }
   if(errorCode||validated&&(entries.size>=limit||sourceExhausted))break;
   const location=await call(browser,'Runtime.evaluate',{returnByValue:true,expression:'JSON.stringify({path:location.pathname,logged:!!document.querySelector("[data-testid=SideNav_AccountSwitcher_Button]"),challenge:!!document.querySelector("iframe[src*=captcha],input[name=challenge_response]")})'});
   const state=JSON.parse(location.result.result.value);
   if(state.challenge){errorCode='VERIFICATION_REQUIRED';break;}
   if(/\/(?:i\/flow\/login|i\/jf\/onboarding|login)/.test(state.path)){errorCode='LOGIN_REQUIRED';break;}
   if(validated&&scrolls<12&&Date.now()-lastScroll>1800){
    await call(browser,'Runtime.evaluate',{expression:'window.scrollTo(0,document.body.scrollHeight)',returnByValue:true});scrolls++;lastScroll=Date.now();
   }
   await new Promise(r=>setTimeout(r,250));
  }
  const ordered=[...entries.values()].sort((a,b)=>BigInt(a.postId)>BigInt(b.postId)?-1:BigInt(a.postId)<BigInt(b.postId)?1:0).slice(0,limit).map(({postId,...row})=>row);
  return {entries:ordered,validated,sourceExhausted,errorCode:errorCode||(!validated||ordered.length<limit&&!sourceExhausted?'DISCOVERY_INCOMPLETE':null)};
 }finally{browser.ws.removeListener('message',event);await closeBrowser(browser);}
}
if(require.main===module)discover(process.argv[2],Number(process.argv[3]),process.argv[4]).then(result=>console.log('HM_X_DISCOVERY '+JSON.stringify(result))).catch(()=>{console.log('HM_X_DISCOVERY {"errorCode":"NETWORK_ERROR"}');process.exitCode=1});
module.exports={discover};
