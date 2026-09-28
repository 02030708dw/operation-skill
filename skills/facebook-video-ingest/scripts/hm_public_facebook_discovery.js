// Public discovery runs in an isolated profile. Only selected IDs/URLs leave this helper.
const engine=require('../../facebook-followed-video-download/scripts/facebook_followed_video_engine');
async function main(){
 const [url,count,authorizedProfile]=process.argv.slice(2);const limit=Number(count);
 const profile=authorizedProfile||engine.createTemporaryProfile();let browser;
 try{
  browser=await engine.startBrowser(profile);const entries=new Map();let exhausted=true,empty=false;
  if(process.argv.includes('--pipeline-cursor')){
   const cursor=JSON.parse(process.argv[process.argv.indexOf('--pipeline-cursor')+1]);
   const pages=engine.pageCandidates(url),index=Number(cursor.candidate||0);
   if(!Number.isInteger(index)||index<0||index>=pages.length)throw new Error('Invalid cursor');
   const found=await engine.discoverOnPage(browser.ws,pages[index],new Set(),0,cursor.provider||null);
   const page=found.pipelinePage;
   if(!page || (!page.exhausted && page.end===page.start)){
    console.log('HM_PUBLIC_DISCOVERY '+JSON.stringify({errorCode:found.pipelineLoginGate?'LOGIN_REQUIRED':'DISCOVERY_INCOMPLETE',entries:found.urls.map(u=>({id:engine.videoKey(u),url:u})).filter(e=>/^\d+$/.test(e.id)).slice(0,limit)}));return;
   }
   const next=page.exhausted?{candidate:index+1}:{candidate:index,provider:{key:page.key,after:page.end}};
   const complete=page.exhausted && index===pages.length-1;
   console.log('HM_PUBLIC_DISCOVERY '+JSON.stringify({entries:page.urls.map(u=>({id:engine.videoKey(u),url:u})),sourceExhausted:complete,emptyConfirmed:complete&&!page.urls.length,cursor:next,validated:true}));return;
  }
  for(const page of engine.pageCandidates(url)){
   const found=await engine.discoverWithRecovery(browser,page,new Set(),limit);
   exhausted=exhausted && !!found.sourceExhausted;empty=empty || !!found.emptyConfirmed;
   for(const u of found.urls){const id=engine.videoKey(u);if(id)entries.set(id,{id,url:u});}
   if(entries.size>=limit){exhausted=false;break;}
  }
  console.log('HM_PUBLIC_DISCOVERY '+JSON.stringify({entries:[...entries.values()].slice(0,limit),sourceExhausted:exhausted,emptyConfirmed:empty&&entries.size===0}));
 }catch(e){
  const code=String(e.code||'').replace(/^FACEBOOK_/,'');
  console.log('HM_PUBLIC_DISCOVERY '+JSON.stringify({errorCode:['LOGIN_REQUIRED','VERIFICATION_REQUIRED','RATE_LIMITED','ACCOUNT_SUSPENDED','ACCESS_DENIED','DISCOVERY_INCOMPLETE'].includes(code)?code:'DISCOVERY_EMPTY'}));
 }finally{
  if(browser){try{browser.ws.close();}catch{}await engine.stopChrome(browser.chrome);}
  if(!authorizedProfile)engine.removeTree(profile);
 }
}
main().catch(()=>{console.log('HM_PUBLIC_DISCOVERY {"errorCode":"DOWNLOAD_FAILED"}');process.exitCode=1;});
