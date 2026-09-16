const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('fs'),os=require('os'),path=require('path'),vm=require('vm'),crypto=require('crypto');
const {createRequire}=require('module');
const enginePath=path.resolve(__dirname,'../scripts/facebook_followed_video_engine.js');
function load(t, options={}) {
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'hm-phase1-'));t.after(()=>fs.rmSync(dir,{recursive:true,force:true}));
 const realRequire=createRequire(enginePath),calls=[];
 const child={...require('child_process'),spawnSync:(...args)=>{calls.push(args);if(options.spawn)return options.spawn(...args);throw Error('Unexpected external process');}};
 const context={require:n=>n==='child_process'?child:realRequire(n),module:{exports:{}},__dirname:path.dirname(enginePath),
  process:{env:{HOME:dir,...options.env},argv:['node','engine','--desktop',dir,'--accounts',path.join(dir,'accounts.txt'),'--result-json',path.join(dir,'download.json')],pid:process.pid,platform:process.platform},
  console:{log(){},error(){}},Buffer,URL,setTimeout,clearTimeout};
 fs.writeFileSync(path.join(dir,'accounts.txt'),'creator https://www.facebook.com/creator\n');
 vm.createContext(context);vm.runInContext(fs.readFileSync(enginePath,'utf8'),context);
 return {dir,calls,engine:context.module.exports,context};
}
test('verified local file and complete metadata cause zero extractor calls',t=>{
 const {dir,calls,engine}=load(t);const local=path.join(dir,'20260916_123_video.mp4');fs.writeFileSync(local,'verified media');
 const item={status:'downloaded',platformVideoId:'123',title:'Original caption',durationSeconds:15,publishedAt:'2026-09-16T00:00:00',publishedAtPrecision:'DATE',fileSize:14,sha256:crypto.createHash('sha256').update('verified media').digest('hex')};
 engine.cacheVideo(dir,item);const result=engine.downloadVideo({folder:'creator'},'https://www.facebook.com/reel/123',dir,path.join(dir,'.fb-video-urls.txt'));
 assert.equal(result.cacheHit,true);assert.equal(result.title,'Original caption');assert.equal(calls.length,0);
 fs.writeFileSync(local,'tampered media');assert.throws(()=>engine.readCachedVideo(dir,item,local),e=>e.code==='LOCAL_MEDIA_INTEGRITY_FAILED');
 assert.equal(engine.downloadVideo({folder:'creator'},'https://www.facebook.com/reel/123',dir,path.join(dir,'.fb-video-urls.txt')).errorCode,'LOCAL_MEDIA_INTEGRITY_FAILED');
 assert.equal(calls.length,0);
});
test('metadata 429 stops before media download and later video attempts',t=>{
 const {engine,calls,dir}=load(t,{spawn:()=>({status:1,stdout:'',stderr:'ERROR: HTTP Error 429: Too Many Requests'})});
 const account={folder:'creator'},archive=path.join(dir,'.fb-video-urls.txt');
 assert.equal(engine.downloadVideo(account,'https://www.facebook.com/reel/123',dir,archive).errorCode,'FACEBOOK_RATE_LIMITED');
 assert.equal(engine.downloadVideo(account,'https://www.facebook.com/reel/456',dir,archive).errorCode,'FACEBOOK_RATE_LIMITED');assert.equal(calls.length,1);
});
test('account state changed by another lane gates next navigation without sending CDP',async t=>{
 const state=path.join(os.tmpdir(),'hm-account-state-'+process.pid+'.json');t.after(()=>fs.rmSync(state,{force:true}));
 fs.writeFileSync(state,JSON.stringify({state:'COOLDOWN',reasonCode:'FACEBOOK_RATE_LIMITED'}));
 const {engine}=load(t,{env:{HM_FACEBOOK_ACCOUNT_STATE:state}});
 await assert.rejects(engine.cdpCall({send(){assert.fail('must not navigate');}},{method:'Page.navigate',params:{url:'https://www.facebook.com/'}}),e=>e.code==='FACEBOOK_RATE_LIMITED');
});
test('an existing session cooldown is not a new rate limit and does not extend the deadline',t=>{
 const state=path.join(os.tmpdir(),'hm-existing-cooldown-'+process.pid+'.json');t.after(()=>fs.rmSync(state,{force:true}));
 const original=JSON.stringify({state:'COOLDOWN',reasonCode:'SESSION_CHECK_INCONCLUSIVE',nextCheckAt:123456});fs.writeFileSync(state,original);
 const {engine,dir,calls}=load(t,{env:{HM_FACEBOOK_ACCOUNT_STATE:state}});
 const result=engine.downloadVideo({folder:'creator'},'https://www.facebook.com/reel/123',dir,path.join(dir,'.fb-video-urls.txt'));
 assert.equal(result.errorCode,'FACEBOOK_ACCOUNT_COOLDOWN');assert.equal(calls.length,0);assert.equal(fs.readFileSync(state,'utf8'),original);
});
test('private video 403 remains item scoped; network errors retain retry classification',t=>{
 const {engine}=load(t);assert.equal(engine.classifyDownloadError('HTTP Error 403'),'FACEBOOK_ACCESS_REQUIRED');
 assert.equal(engine.classifyDownloadError('HTTP Error 503'),'FACEBOOK_NETWORK_ERROR');
 assert.equal(engine.classifyDownloadError('login required'),'FACEBOOK_LOGIN_REQUIRED');
 assert.equal(engine.accessCode('confirm your identity'),'FACEBOOK_VERIFICATION_REQUIRED');
});
test('discovery account stop prevents remaining entrypoints and all downloads',async t=>{
 const {context,engine}=load(t);
 vm.runInContext(`assertChromeAvailable=()=>'/fake/chrome';assertRunnable=()=>{};
 startBrowser=async()=>({ws:{close(){}},chrome:{}});stopChrome=async()=>{};injectCookies=async()=>0;
 let visits=0;discoverWithRecovery=async()=>{visits++;throw codedError('FACEBOOK_RATE_LIMITED','restricted');};
 downloadVideo=()=>{throw Error('must not download');};`,context);
 const result=await engine.main();assert.equal(result.status,'failed');assert.equal(result.errorCode,'FACEBOOK_RATE_LIMITED');
 assert.equal(vm.runInContext('visits',context),1);assert.equal(result.sources[0].videos.length,0);
});
test('success before a later restriction remains partial and does not attempt a third video',async t=>{
 const {context,engine}=load(t);
 vm.runInContext(`assertChromeAvailable=()=>'/fake/chrome';assertRunnable=()=>{};
 startBrowser=async()=>({ws:{close(){}},chrome:{}});stopChrome=async()=>{};injectCookies=async()=>0;
 discoverWithRecovery=async()=>({urls:['https://www.facebook.com/reel/123','https://www.facebook.com/reel/456','https://www.facebook.com/reel/789']});
 let downloads=0;downloadVideo=(account,url)=>{downloads++;if(downloads===2){stopAccount('FACEBOOK_RATE_LIMITED');return {status:'download-failed',errorCode:'FACEBOOK_RATE_LIMITED'};}
 return {status:'downloaded',title:'Existing title'};};cacheVideo=()=>{};`,context);
 const result=await engine.main();assert.equal(result.status,'partial');assert.equal(result.errorCode,'FACEBOOK_RATE_LIMITED');
 assert.equal(vm.runInContext('downloads',context),2);assert.equal(result.sources[0].videos.length,2);
});
test('healthy discovery navigates once without an unconditional refresh',async t=>{
 const {context}=load(t);
 vm.runInContext(`sleep=async()=>{};let methods=[];
 rawCdpCall=async(ws,msg)=>{
   methods.push(msg.method);
   return {result:{result:{value:msg.method==='Runtime.evaluate'?JSON.stringify({urls:['https://www.facebook.com/reel/123']}):null}}};
 };`,context);
 await vm.runInContext(`discoverOnPage({},'https://www.facebook.com/creator',new Set(['123']),1)`,context);
 assert.equal(vm.runInContext(`methods.filter(x=>x==='Page.navigate').length`,context),1);
 assert.equal(vm.runInContext(`methods.filter(x=>x==='Page.reload').length`,context),0);
});
test('process metrics survive truncated writes and omit untrusted errors',t=>{
 const {dir}=load(t);const journal=path.join(dir,'process-metrics.jsonl');fs.writeFileSync(journal,'{"broken":');
 const metrics=require('../scripts/capture_observability').createMetrics(path.join(dir,'download.json'));
 metrics.record('metadata','failure',15,'Cookie: secret');metrics.record('empty_check','count');
 const rows=fs.readFileSync(journal,'utf8').trim().split('\n').slice(1).map(JSON.parse);
 assert.equal(rows.length,2);assert.equal(rows[0].errorCode,null);assert.equal(metrics.summary().stages.metadata.failures,1);
 assert.equal(fs.readFileSync(journal,'utf8').includes('secret'),false);
});

test('generic retry is a page failure and cannot pause the account',t=>{
 const {engine,calls,dir}=load(t);
 assert.equal(engine.accessCode('Something went wrong. Please try again later.'),null);
 assert.equal(engine.classifyDownloadError('ERROR: Please try again later'),'FACEBOOK_NETWORK_ERROR');
 assert.throws(()=>engine.assertPageAccess({gateText:'Please try again later'}),e=>e.code==='FACEBOOK_NETWORK_ERROR');
 assert.equal(calls.length,0);
 assert.doesNotThrow(()=>engine.assertAccess());
 const rows=fs.readFileSync(path.join(dir,'process-metrics.jsonl'),'utf8').trim().split('\n').map(JSON.parse);
 assert.equal(rows[0].evidence.signal,'GENERIC_RETRY');
});
test('explicit restrictions still stop the account and persist only safe evidence',t=>{
 const {engine,dir}=load(t);
 assert.throws(()=>engine.assertPageAccess({gateText:"You're temporarily blocked from using this feature. Cookie: secret",urls:['video']}),e=>e.code==='FACEBOOK_RATE_LIMITED');
 assert.throws(()=>engine.assertAccess(),e=>e.code==='FACEBOOK_RATE_LIMITED');
 const raw=fs.readFileSync(path.join(dir,'process-metrics.jsonl'),'utf8');
 assert.equal(raw.includes('secret'),false);assert.ok(raw.includes('EXPLICIT_RATE_LIMIT'));assert.ok(raw.includes('PAGE_DIALOG'));
 assert.equal(engine.accessEvidence('HTTP Error 429').signal,'HTTP_429');
});
test('creator paths and healthy captions are not account restriction signals',t=>{
 const {engine}=load(t);
 assert.equal(engine.pageAccessEvidence({finalUrl:'https://www.facebook.com/challenge_creator/',bodyText:'we limit how often',urls:['video']}),null);
 assert.equal(engine.pageAccessEvidence({bodyText:'Please log into Facebook',videoElements:1}),null);
 assert.equal(engine.pageAccessEvidence({finalUrl:'https://www.facebook.com/checkpoint/123?secret=x'}).signal,'CHECKPOINT_PATH');
 assert.equal(engine.pageAccessEvidence({finalUrl:'https://www.facebook.com/login.php'}).code,'FACEBOOK_LOGIN_REQUIRED');
 assert.equal(engine.pageAccessEvidence({finalUrl:'https://other.example/checkpoint/'}),null);
});
test('metrics evidence rejects arbitrary values and strips extra properties',()=>{
 const m=require('../scripts/capture_observability').createMetrics();
 assert.throws(()=>m.evidence({source:'secret',signal:'HTTP_429',code:'FACEBOOK_RATE_LIMITED'}));
 m.evidence({source:'EXTRACTOR',signal:'HTTP_429',code:'FACEBOOK_RATE_LIMITED',cookie:'secret'});
 assert.equal(m.summary().accessEvidence.length,1);assert.equal(JSON.stringify(m.summary()).includes('secret'),false);
});
