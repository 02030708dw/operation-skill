const test = require('node:test');
const assert = require('node:assert/strict');
const {parsePage, PaginationProof} = require('../scripts/facebook_pagination');
const url = 'https://www.facebook.com/example/videos/';
function payload(more, end, id = '123') {
  return {data:{node:{id,__typename:'Page',url:'https://www.facebook.com/example/',all_videos:{
    edges:[{node:{__typename:'Video',id:'456'}}],page_info:{has_next_page:more,end_cursor:end}}}}};
}
test('only a complete chain bound to the requested profile proves exhaustion', () => {
  const proof = new PaginationProof();
  proof.add(parsePage(payload(false, null), url, {cursor:'next'}));
  assert.equal(proof.exhausted,false);
  proof.add(parsePage(payload(true, 'next'), url, {cursor:null}));
  assert.equal(proof.exhausted,true);
  assert.deepEqual([...proof.urls], ['https://www.facebook.com/watch/?v=456']);
});
test('foreign profiles, recommendations, missing request cursor and body text cannot prove exhaustion', () => {
  assert.deepEqual(parsePage(payload(false,null), 'https://www.facebook.com/other', {cursor:null}),[]);
  assert.deepEqual(parsePage(payload(false,null), url, {}),[]);
  assert.deepEqual(parsePage({bodyText:'no more videos',page_info:{has_next_page:false}},url,{cursor:null}),[]);
  const p = payload(false,null);p.data.node.recommendations=p.data.node.all_videos;delete p.data.node.all_videos;
  assert.deepEqual(parsePage(p,url,{cursor:null}),[]);
});
test('server errors and invalid pagination are incomplete', () => {
  const p=payload(false,null);p.errors=[{message:'network'}];
  assert.deepEqual(parsePage(p,url,{cursor:null}),[]);
  const invalid=payload('false',null);
  assert.deepEqual(parsePage(invalid,url,{cursor:null}),[]);
});
