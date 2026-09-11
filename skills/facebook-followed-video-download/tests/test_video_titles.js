const test=require('node:test');const assert=require('node:assert/strict');
const titles=require('../scripts/facebook_video_titles');
test('placeholder title falls back to caption and preserves real original',()=>{
 assert.equal(titles.choose({title:'Video',description:'สนามฟุตบอลวันนี้'}).titleSource,'POST_TEXT');
 assert.equal(titles.choose({title:'Final match',description:'body'}).title,'Final match');
 for(const s of ['Video','Facebook video','123456','20260911_123456_Video.mp4']) assert.equal(titles.usable(s),null);
 assert.equal(titles.usable('52K views · 2.6K reactions | Real caption'),'Real caption');
});
test('missing, access and genuine no text remain distinct',()=>{
 assert.equal(titles.choose({title:'Video'}).titleStatus,'EXTRACTION_FAILED');
 assert.equal(titles.choose({}, {blocked:true}).titleStatus,'ACCESS_REQUIRED');
 assert.equal(titles.choose({}, {noText:true}).titleStatus,'NO_TEXT');
 assert.equal(titles.choose({title:'Video games'}).title,'Video games');
});
test('only one-video articles can supply captions and page metadata requires identity',()=>{
 const anchor=(key,story)=>({href:'https://www.facebook.com/reel/'+key,closest:()=>story});
 const single={querySelectorAll:()=>[anchor('111')],querySelector:()=>({innerText:'Caption A'})};
 const mixed={querySelectorAll:()=>[anchor('111'),anchor('222')],querySelector:()=>({innerText:'Wrong caption'})};
 global.location={href:'https://www.facebook.com/reel/111'};
 global.document={body:{innerText:'post'},querySelectorAll:()=>[anchor('111',single),anchor('222',mixed)],querySelector:(s)=>s.includes('canonical')?{href:'https://www.facebook.com/reel/222'}:null};
 const result=titles.pageSnapshot('111');assert.equal(result.candidates['111'].postText,'Caption A');assert.equal(result.matched,false);assert.equal(result.description,undefined);
 delete global.document;delete global.location;
});
