const test=require('node:test'),assert=require('node:assert/strict');
const {parseFeed}=require('../scripts/hm_x_feed');
function item(id,media,changes={}){return {tweet_results:{result:{rest_id:id,core:{user_results:{result:{core:{screen_name:'Creator'}}}},legacy:{id_str:id,extended_entities:{media:media.map(id_str=>({id_str,type:'video',video_info:{variants:[{}]}}))},...changes}}}};}
const feed=instructions=>({data:{user:{result:{core:{screen_name:'Creator'},timeline:{timeline:{instructions}}}}}});
test('native media identities survive grid duplicates and multi-video posts; quotes and ads never enter the queue',()=>{
 const normal=item('123',['456','789']);normal.tweet_results.result.quoted_status_result={result:item('999',['999']).tweet_results.result};
 const ad={...item('124',['800']),promotedMetadata:{}};
 const picture=item('125',[]);picture.tweet_results.result.legacy.extended_entities.media=[{id_str:'801',type:'photo'}];
 const result=parseFeed(feed([{type:'TimelineAddEntries',entries:[{content:{items:[normal,normal,ad,picture,item('126',['802'],{retweeted_status_id_str:'1'})].map(itemContent=>({item:{itemContent}}))}},{content:{cursorType:'Bottom',value:'more'}}]}]),'creator');
 assert.deepEqual(result.entries.map(e=>e.id),['456','789']);assert.ok(result.validated);assert.equal(result.sourceExhausted,false);
});
test('module pagination does not fabricate exhaustion and malformed payloads do not prove an empty feed',()=>{
 const page=parseFeed(feed([{type:'TimelineAddToModule',moduleItems:[{item:{itemContent:item('123',['456'])}}]}]),'creator');
 assert.deepEqual(page.entries.map(e=>e.id),['456']);assert.equal(page.sourceExhausted,false);
 assert.equal(parseFeed({data:{user:{result:{}}}},'creator').validated,false);
 assert.equal(parseFeed(feed([{type:'TimelineAddEntries',entries:[]},{type:'TimelineTerminateTimeline',direction:'Bottom'}]),'creator').sourceExhausted,true);
 assert.deepEqual(parseFeed({errors:[{code:88,message:'private response'}]},'creator'),{errorCode:'RATE_LIMITED'});
});
