const test=require('node:test'),assert=require('node:assert/strict'),{EventEmitter}=require('events');
const {startBrowser}=require('../scripts/hm_x_browser');
function fakeDisplay(){const display=new EventEmitter();display.stdio=[null,null,null,new EventEmitter()];display.killed=false;display.kill=()=>{display.killed=true;};return display;}
test('X uses ordinary Chromium on its own display and closes the display with Chromium',async()=>{
 const display=fakeDisplay(),chrome=new EventEmitter();let args;
 const browser=await startBrowser('/private/x-profile',{spawn:(file,flags)=>{assert.equal(file,'/usr/bin/Xvfb');assert.ok(flags.includes('-nolisten'));process.nextTick(()=>display.stdio[3].emit('data',Buffer.from('42\n')));return display;},startBrowser:async(...values)=>{args=values;return {chrome};}});
 assert.deepEqual(args,['/private/x-profile',undefined,{headless:false,env:{DISPLAY:':42'}}]);assert.equal(browser.xDisplay,display);
 chrome.emit('exit');assert.ok(display.killed);
});
test('failed Chromium startup releases the private display',async()=>{
 const display=fakeDisplay();await assert.rejects(startBrowser('/private/x-profile',{spawn:()=>{process.nextTick(()=>display.stdio[3].emit('data',Buffer.from('43\n')));return display;},startBrowser:async()=>{throw Error('CHROME_START');}}),/CHROME_START/);assert.ok(display.killed);
});
