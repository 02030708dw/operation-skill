// Human-operated official login page. No credential autofill or challenge solving.
const fs=require('fs'),path=require('path');
const engine=require('./facebook_followed_video_engine');
let seq=500000;
const WIDTH=1100,HEIGHT=700;
function privatePreferences(profile){
 const dir=path.join(profile,'Default');fs.mkdirSync(dir,{recursive:true,mode:0o700});
 const file=path.join(dir,'Preferences');let prefs={};try{prefs=JSON.parse(fs.readFileSync(file,'utf8'))}catch(_){}
 prefs.credentials_enable_service=false;prefs.profile={...prefs.profile,password_manager_enabled:false};
 fs.writeFileSync(file,JSON.stringify(prefs),{mode:0o600});fs.chmodSync(file,0o600);
}
function validate(command){
 const op=command.op;
 if(!['snapshot','pointer','scroll','text','key','reload'].includes(op))throw Error('INVALID_BROWSER_INPUT');
 if(op==='pointer'){
  if(!['mousePressed','mouseReleased','mouseMoved'].includes(command.event))throw Error('INVALID_BROWSER_INPUT');
  for(const key of ['x','y'])if(!Number.isFinite(command[key])||command[key]<0||command[key]>(key==='x'?WIDTH:HEIGHT))throw Error('INVALID_BROWSER_INPUT');
 }
 if(op==='scroll' && (!Number.isFinite(command.deltaY)||Math.abs(command.deltaY)>1000))throw Error('INVALID_BROWSER_INPUT');
 if(op==='text' && (typeof command.text!=='string'||!command.text.length||command.text.length>1000))throw Error('INVALID_BROWSER_INPUT');
 if(op==='key' && !['Enter','Tab','Backspace','Delete','Escape','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End','SelectAll'].includes(command.key))throw Error('INVALID_BROWSER_INPUT');
 return command;
}
async function call(browser,method,params){return engine.cdpCall(browser.ws,{id:seq++,method,params},10000)}
async function open(browser){
 await call(browser,'Emulation.setDeviceMetricsOverride',{width:WIDTH,height:HEIGHT,deviceScaleFactor:1,mobile:false});
 await call(browser,'Page.navigate',{url:'https://www.facebook.com/login/'});
}
async function command(browser,input){
 validate(input);
 const current=await call(browser,'Runtime.evaluate',{expression:'location.hostname',returnByValue:true});
 if(!['www.facebook.com','facebook.com'].includes(current.result.result.value))throw Error('UNSUPPORTED_LOGIN_PAGE');
 if(input.op==='pointer')await call(browser,'Input.dispatchMouseEvent',{type:input.event,x:input.x,y:input.y,button:input.event==='mouseMoved'?'none':'left',buttons:input.event==='mousePressed'||input.drag?1:0,clickCount:input.event==='mouseMoved'?0:1});
 else if(input.op==='scroll')await call(browser,'Input.dispatchMouseEvent',{type:'mouseWheel',x:WIDTH/2,y:HEIGHT/2,deltaX:0,deltaY:input.deltaY});
 else if(input.op==='text')await call(browser,'Input.insertText',{text:input.text});
 else if(input.op==='key'){
  const codes={Enter:13,Tab:9,Backspace:8,Delete:46,Escape:27,ArrowLeft:37,ArrowUp:38,ArrowRight:39,ArrowDown:40,Home:36,End:35,SelectAll:65};
  const params={key:input.key==='SelectAll'?'a':input.key,windowsVirtualKeyCode:codes[input.key],modifiers:input.key==='SelectAll'?2:0};
  await call(browser,'Input.dispatchKeyEvent',{...params,type:'keyDown'});await call(browser,'Input.dispatchKeyEvent',{...params,type:'keyUp'});
 }else if(input.op==='reload')await call(browser,'Page.reload',{});
 if(input.op!=='snapshot')return {accepted:true};
 const frame=await call(browser,'Page.captureScreenshot',{format:'jpeg',quality:65,captureBeyondViewport:false});
 if(frame.result.data.length>2000000)throw Error('FRAME_TOO_LARGE');
 return {image:frame.result.data,width:WIDTH,height:HEIGHT};
}
module.exports={privatePreferences,validate,open,command};
