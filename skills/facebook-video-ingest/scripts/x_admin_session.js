// Human-operated official X login. Credentials remain in the regional Chrome profile.
const readline=require('readline');
const engine=require('../../facebook-followed-video-download/scripts/facebook_followed_video_engine');
const interactive=require('../../facebook-followed-video-download/scripts/facebook_interactive_browser');
const {startBrowser,closeBrowser,confirmPersistedSession}=require('./hm_x_browser');
const hosts=['x.com','www.x.com','twitter.com','api.x.com'];let seq=980000;
const call=(b,method,params)=>engine.cdpCall(b.ws,{id:seq++,method,params},10000);
const emit=value=>console.log('HM_ACCOUNT_RESULT '+JSON.stringify(value));
async function inspect(browser){
 await interactive.open(browser,'https://x.com/home');
 for(let i=0;i<30;i++){
  await new Promise(r=>setTimeout(r,500));
  const response=await call(browser,'Runtime.evaluate',{returnByValue:true,expression:'JSON.stringify({host:location.hostname,path:location.pathname,logged:!!document.querySelector("[data-testid=SideNav_AccountSwitcher_Button]"),challenge:!!document.querySelector("iframe[src*=captcha],input[name=challenge_response]")})'});
  const state=JSON.parse(response.result.result.value);
  if(!hosts.includes(state.host))return {state:'VERIFICATION_REQUIRED',reasonCode:'X_VERIFICATION_REQUIRED'};
  if(state.logged)return {state:'LOGGED_IN',reasonCode:null};
  if(state.challenge)return {state:'VERIFICATION_REQUIRED',reasonCode:'X_VERIFICATION_REQUIRED'};
  if(/\/(?:i\/flow\/login|i\/jf\/onboarding|login)/.test(state.path))return {state:'LOGIN_REQUIRED',reasonCode:'X_LOGIN_REQUIRED'};
 }
 return {state:'COOLDOWN',reasonCode:'X_SESSION_UNKNOWN'};
}
async function main(){
 const profile=process.argv[2];interactive.privatePreferences(profile);const browser=await startBrowser(profile);
 try{
  for await(const line of readline.createInterface({input:process.stdin})){
   let input;
   try{
    input=JSON.parse(line);if(input.action==='close')break;
    if(input.action==='browser'){await interactive.open(browser,'https://x.com/i/flow/login');emit({state:'BROWSER_READY'});continue;}
    if(input.action==='view'){
     try{console.log('HM_BROWSER_RESULT '+JSON.stringify({requestId:input.requestId,...await interactive.command(browser,input,hosts)}));}
     catch(_){console.log('HM_BROWSER_RESULT '+JSON.stringify({requestId:input.requestId,error:'BROWSER_UNAVAILABLE'}));}continue;
    }
    if(!['verify','check','finish'].includes(input.action))throw Error('INVALID_ACTION');
    let result=await inspect(browser);
    if(result.state==='LOGGED_IN'){result=await confirmPersistedSession(browser,profile,reopened=>inspect(reopened));emit(result);return;}
    emit(result);
   }catch(_){emit({state:'COOLDOWN',reasonCode:'X_SESSION_UNKNOWN'});if(browser.hmClosed)return;}
   finally{input=null;}
  }
 }finally{await closeBrowser(browser);}
}
if(require.main===module)main().catch(()=>{emit({state:'COOLDOWN',reasonCode:'X_SESSION_UNKNOWN'});process.exitCode=1});
module.exports={hosts};
