#!/usr/bin/env node
// Private transport for a human-operated Google login. No page text or cookies in stdout.
const fs=require('fs'),path=require('path'),readline=require('readline');
const shared=path.resolve(__dirname,'../../facebook-followed-video-download/scripts');
const engine=require(path.join(shared,'facebook_followed_video_engine'));
const interactive=require(path.join(shared,'facebook_interactive_browser'));
const {closeBrowser,confirmPersistedSession}=require(path.join(shared,'facebook_browser_shutdown'));
const hosts=['accounts.google.com','www.youtube.com','youtube.com','consent.youtube.com','consent.google.com'];
let seq=940000;
const call=(b,method,params)=>engine.cdpCall(b.ws,{id:seq++,method,params},10000);
const emit=value=>console.log('HM_ACCOUNT_RESULT '+JSON.stringify(value));
async function inspect(browser,profile){
 const location=await call(browser,'Runtime.evaluate',{expression:'location.href',returnByValue:true});
 const url=new URL(location.result.result.value);
 if(url.hostname==='accounts.google.com' && /challenge/.test(url.pathname)) return {state:'VERIFICATION_REQUIRED',reasonCode:'GOOGLE_VERIFICATION_REQUIRED'};
 await interactive.open(browser,'https://www.youtube.com/account');
 let info;
 for(let i=0;i<15;i++){
  await new Promise(r=>setTimeout(r,700));
  const result=await call(browser,'Runtime.evaluate',{returnByValue:true,expression:`JSON.stringify({host:location.hostname,ready:document.readyState,logged:!!(window.ytcfg && ytcfg.get('LOGGED_IN')),email:(document.body.innerText.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}/)||[])[0]||null})`});
  try{info=JSON.parse(result.result.result.value)}catch(_){continue}
  if(info.logged)break;
 }
 if(!info?.logged)return {state:info?.host==='accounts.google.com'?'LOGIN_REQUIRED':'COOLDOWN',reasonCode:info?.host==='accounts.google.com'?'GOOGLE_LOGIN_REQUIRED':'GOOGLE_SESSION_UNKNOWN'};
 const response=await call(browser,'Network.getAllCookies',{});
 const cookies=response.result.cookies.filter(c=>/^(?:.*\.)?(youtube|google)\.com$/.test(c.domain.replace(/^\./,'')));
 if(!cookies.length)return {state:'COOLDOWN',reasonCode:'GOOGLE_SESSION_UNKNOWN'};
 const rows=cookies.map(c=>[c.domain,c.domain.startsWith('.')?'TRUE':'FALSE',c.path,c.secure?'TRUE':'FALSE',c.session?'0':Math.max(0,Math.floor(c.expires)),c.name,c.value].join('\t'));
 fs.writeFileSync(path.join(profile,'youtube-cookies.txt'),'# Netscape HTTP Cookie File\n'+rows.join('\n')+'\n',{mode:0o600});
 const maskedAccount=info.email?info.email.slice(0,2)+'***@'+info.email.split('@')[1]:null;
 return {state:'LOGGED_IN',loginStatus:'LOGGED_IN',downloadStatus:'NOT_TESTED',maskedAccount,reasonCode:null};
}
async function main(){
 const profile=process.argv[2];interactive.privatePreferences(profile);
 const browser=await engine.startBrowser(profile);
 try{
  for await(const line of readline.createInterface({input:process.stdin})){
   let input;
   try{
    input=JSON.parse(line);
    if(input.action==='close')break;
    if(input.action==='browser'){await interactive.open(browser,'https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2Faccount');emit({state:'BROWSER_READY'});continue;}
    if(input.action==='view'){
     try{console.log('HM_BROWSER_RESULT '+JSON.stringify({requestId:input.requestId,...await interactive.command(browser,input,hosts)}));}
     catch(_){console.log('HM_BROWSER_RESULT '+JSON.stringify({requestId:input.requestId,error:'BROWSER_UNAVAILABLE'}));}continue;
    }
    if(!['verify','check','finish'].includes(input.action))throw Error('INVALID_ACTION');
    let result=await inspect(browser,profile);
    if(result.state==='LOGGED_IN'){
     result=await confirmPersistedSession(browser,profile,reopened=>inspect(reopened,profile));emit(result);return;
    }
    emit(result);
   }catch(_){emit({state:'COOLDOWN',reasonCode:'GOOGLE_SESSION_UNKNOWN'});if(browser.hmClosed)return;}
   finally{input=null;}
  }
 }finally{await closeBrowser(browser);}
}
if(require.main===module)main().catch(()=>{emit({state:'COOLDOWN',reasonCode:'GOOGLE_SESSION_UNKNOWN'});process.exitCode=1});
module.exports={hosts};
