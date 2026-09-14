const {test} = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const engine = require('../scripts/facebook_followed_video_engine');
const {closeBrowser, confirmPersistedSession} = require('../scripts/facebook_browser_shutdown');
function browser() {
  const chrome = new EventEmitter();chrome.exitCode=null;chrome.signalCode=null;
  return {chrome, ws:{close(){}}};
}
test('persists with Browser.close before reopening; never signals a healthy browser', async () => {
  const saved={call:engine.cdpCall,start:engine.startBrowser,stop:engine.stopChrome};
  const first=browser(), second=browser(), events=[];
  engine.cdpCall=async(ws,request)=>{
    assert.equal(request.method,'Browser.close');const b=ws===first.ws?first:second;
    events.push(b===first?'flush-first':'flush-second');b.chrome.exitCode=0;b.chrome.emit('exit',0);return {};
  };
  engine.stopChrome=async()=>{throw Error('Should not force stop');};
  engine.startBrowser=async(profile)=>{assert.equal(profile,'private-profile');assert.ok(first.hmClosed);events.push('reopen');return second;};
  try {
    const result=await confirmPersistedSession(first,'private-profile',async(b)=>{assert.equal(b,second);events.push('verify');return {state:'AVAILABLE'};});
    assert.equal(result.state,'AVAILABLE');assert.deepEqual(events,['flush-first','reopen','verify','flush-second']);
    await closeBrowser(first);assert.equal(events.length,4);
  } finally {engine.cdpCall=saved.call;engine.startBrowser=saved.start;engine.stopChrome=saved.stop;}
});
test('timeout cannot claim a saved session or reopen an unflushed profile', async () => {
  const saved={call:engine.cdpCall,stop:engine.stopChrome};let forced=0;
  engine.cdpCall=async()=>({});engine.stopChrome=async()=>{forced++;};
  try {await assert.rejects(closeBrowser(browser(),5),/SESSION_PERSIST_FAILED/);assert.equal(forced,1);}
  finally {engine.cdpCall=saved.call;engine.stopChrome=saved.stop;}
});
test('CDP disconnect after graceful exit is accepted, process signal is not', async () => {
  const saved=engine.cdpCall;
  try {
    for(const signaled of [false,true]) {
      const b=browser();
      engine.cdpCall=async()=>{if(signaled)b.chrome.signalCode='SIGTERM';else b.chrome.exitCode=0;b.chrome.emit('exit');throw Error('CDP disconnected');};
      if(signaled)await assert.rejects(closeBrowser(b),/SESSION_PERSIST_FAILED/);else await closeBrowser(b);
    }
  }finally{engine.cdpCall=saved;}
});
