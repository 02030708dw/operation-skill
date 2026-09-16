// Public discovery runs in an isolated profile. Only selected IDs/URLs leave this helper.
const engine=require('../../facebook-followed-video-download/scripts/facebook_followed_video_engine');
async function main(){
 const [url,count,authorizedProfile]=process.argv.slice(2);const limit=Number(count);
 const profile=authorizedProfile||engine.createTemporaryProfile();let browser;
 try{
  browser=await engine.startBrowser(profile);const entries=new Map();
  for(const page of engine.pageCandidates(url)){
   const found=await engine.discoverWithRecovery(browser,page,new Set(),limit);
   for(const u of found.urls){const id=engine.videoKey(u);if(id)entries.set(id,{id,url:u});}
   if(entries.size>=limit)break;
  }
  console.log('HM_PUBLIC_DISCOVERY '+JSON.stringify({entries:[...entries.values()].slice(0,limit)}));
 }catch(e){
  const code=String(e.code||'').replace(/^FACEBOOK_/,'');
  console.log('HM_PUBLIC_DISCOVERY '+JSON.stringify({errorCode:['LOGIN_REQUIRED','VERIFICATION_REQUIRED','RATE_LIMITED','ACCOUNT_SUSPENDED'].includes(code)?code:'DISCOVERY_EMPTY'}));
 }finally{
  if(browser){try{browser.ws.close();}catch{}await engine.stopChrome(browser.chrome);}
  if(!authorizedProfile)engine.removeTree(profile);
 }
}
main().catch(()=>{console.log('HM_PUBLIC_DISCOVERY {"errorCode":"DOWNLOAD_FAILED"}');process.exitCode=1;});
