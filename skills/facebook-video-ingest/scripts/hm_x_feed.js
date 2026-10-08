// Parse only the requested user's media timeline, excluding ads, quotes and retweets.
function parseFeed(payload, handle) {
 const errors=payload?.errors||[];
 if(errors.some(e=>[32,89,215].includes(e.code)))return {errorCode:'LOGIN_REQUIRED'};
 if(errors.some(e=>e.code===88))return {errorCode:'RATE_LIMITED'};
 if(errors.some(e=>[64,326].includes(e.code)))return {errorCode:'VERIFICATION_REQUIRED'};
 const result=payload?.data?.user?.result;
 if(result?.__typename==='UserUnavailable')return {errorCode:'ACCESS_DENIED'};
 const owner=result?.core?.screen_name||result?.legacy?.screen_name;
 if(owner&&owner.toLowerCase()!==handle.toLowerCase())return {validated:false,entries:[],sourceExhausted:false};
 const timelines=[];
 function walk(value){
  if(!value||typeof value!=='object')return;
  if(Array.isArray(value.instructions)){timelines.push(value.instructions);return;}
  for(const child of Object.values(value))walk(child);
 }
 walk(result);
 if(!timelines.length)return {validated:false,entries:[],sourceExhausted:false};
 const entries=new Map();let bottom=false,terminated=false,addEntries=false,sawTarget=false;
 function item(content){
  if(content?.promotedMetadata)return;
  let tweet=content?.tweet_results?.result;
  if(tweet?.__typename==='TweetWithVisibilityResults')tweet=tweet.tweet;
  const legacy=tweet?.legacy;
  const author=tweet?.core?.user_results?.result;
  const name=author?.core?.screen_name||author?.legacy?.screen_name;
  if(!legacy||name?.toLowerCase()!==handle.toLowerCase()||legacy.retweeted_status_result||legacy.retweeted_status_id_str)return;
  sawTarget=true;
  const post=tweet.rest_id||legacy.id_str;
  if(!/^[0-9]{1,20}$/.test(post))return;
  for(const media of legacy.extended_entities?.media||[]){
   if(media.type!=='video'||!media.video_info?.variants?.length||!/^[0-9]{1,20}$/.test(media.id_str||''))continue;
   entries.set(media.id_str,{id:media.id_str,url:`https://x.com/${handle}/status/${post}`,postId:post});
  }
 }
 for(const instructions of timelines)for(const instruction of instructions){
  if(instruction.type==='TimelineTerminateTimeline'&&instruction.direction==='Bottom')terminated=true;
  if(instruction.type==='TimelineReplaceEntry'){
   const cursor=instruction.entry?.content;
   if(cursor?.cursorType==='Bottom'&&cursor.value)bottom=true;
  }
  if(instruction.type==='TimelineAddToModule'){
   addEntries=true;
   for(const child of instruction.moduleItems||[])item(child.item?.itemContent);
   // Module additions alone cannot prove the timeline has ended.
   if(!terminated)bottom=true;
  }
  if(instruction.type!=='TimelineAddEntries')continue;
  addEntries=true;
  for(const entry of instruction.entries||[]){
   const cursor=entry.content?.itemContent||entry.content;
   if(cursor?.cursorType==='Bottom'&&cursor.value)bottom=true;
   item(entry.content?.itemContent);
   for(const child of entry.content?.items||[])item(child.item?.itemContent);
  }
 }
 return {validated:addEntries&&(sawTarget||owner?.toLowerCase()===handle.toLowerCase()),entries:[...entries.values()],sourceExhausted:terminated||(addEntries&&!bottom)};
}
module.exports={parseFeed};
