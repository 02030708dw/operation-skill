const test = require('node:test');
const assert = require('node:assert/strict');
const fs=require('fs'),os=require('os'),path=require('path'),{spawnSync}=require('child_process');
test('a truncated journal line cannot swallow the next completed result',()=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'hm-stat-journal-'));
 try {
  fs.writeFileSync(path.join(dir,'statistics.jsonl'),'{"truncated":');
  const engine=path.resolve(__dirname,'../scripts/facebook_followed_video_engine.js');
  const code=`process.argv.push('--result-json',${JSON.stringify(path.join(dir,'download.json'))});const engine=require(${JSON.stringify(engine)});engine.persistStatistics({status:'downloaded',platformVideoId:'123'});`;
  const result=spawnSync(process.execPath,['-e',code],{encoding:'utf8'});assert.equal(result.status,0,result.stderr);
  const rows=fs.readFileSync(path.join(dir,'statistics.jsonl'),'utf8').trimEnd().split('\n');assert.equal(rows.length,2);
  const completed=JSON.parse(rows[1]);assert.equal(completed.statistics.outcome,'SUCCESS');assert.ok(completed.statistics.attemptId);assert.ok(completed.statistics.occurredAt);
 }finally{fs.rmSync(dir,{recursive:true,force:true});}
});
