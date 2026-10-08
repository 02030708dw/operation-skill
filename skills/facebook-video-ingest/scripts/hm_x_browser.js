// Human X login and authenticated scans use ordinary Chromium on a private display.
const {spawn}=require('child_process');
const engine=require('../../facebook-followed-video-download/scripts/facebook_followed_video_engine');
const shutdown=require('../../facebook-followed-video-download/scripts/facebook_browser_shutdown');
async function startBrowser(profile, dependencies={}){
 const launch=dependencies.spawn||spawn, start=dependencies.startBrowser||engine.startBrowser;
 const display=launch('/usr/bin/Xvfb',['-displayfd','3','-screen','0','1280x900x24','-nolisten','tcp'],{stdio:['ignore','ignore','ignore','pipe']});
 try{
  const number=await new Promise((resolve,reject)=>{
   const timer=setTimeout(()=>finish(Error('X_DISPLAY_UNAVAILABLE')),8000);
   function finish(error,value){clearTimeout(timer);display.removeListener('error',failed);display.removeListener('exit',failed);display.stdio[3].removeListener('data',ready);error?reject(error):resolve(value);}
   function failed(){finish(Error('X_DISPLAY_UNAVAILABLE'));}
   let output='';
   function ready(data){output+=String(data);if(!output.includes('\n'))return;const value=output.trim();/^\d+$/.test(value)?finish(null,value):failed();}
   display.once('error',failed);display.once('exit',failed);display.stdio[3].on('data',ready);
  });
  const browser=await start(profile,undefined,{headless:false,env:{DISPLAY:':'+number}});
  browser.xDisplay=display;
  browser.chrome.once('exit',()=>display.kill());
  return browser;
 }catch(error){display.kill();throw error;}
}
async function closeBrowser(browser){
 try{await shutdown.closeBrowser(browser);}
 finally{if(browser?.xDisplay)browser.xDisplay.kill();}
}
async function confirmPersistedSession(browser,profile,verify){
 await closeBrowser(browser);
 const reopened=await startBrowser(profile);
 try{return await verify(reopened);}
 finally{await closeBrowser(reopened);}
}
module.exports={startBrowser,closeBrowser,confirmPersistedSession};
