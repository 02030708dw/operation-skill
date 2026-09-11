'use strict';
function clean(value) { return String(value || '').normalize('NFC').replace(/[\s\u0000-\u001f\u007f\u00a0]+/g, ' ').trim(); }
function missing(value) {
  const s = clean(value).toLowerCase();
  return !s || /^(?:facebook[ _-]*)?(?:video|reel)(?:[ _-]*\d+)?(?:\.(?:mp4|webm|mkv))?$/.test(s)
    || /^\d+$/.test(s) || /^(?:na|\d{8})_\d+_/.test(s) || ['未获取标题', '无标题'].includes(s);
}
function usable(value) {
  let s = clean(value);
  // Facebook often puts engagement counts before the actual caption.
  s = s.replace(/^(?:[\d.,KM]+\s*(?:views|reactions|likes|comments|shares)\s*[·|•-]?\s*)+/i, '').replace(/^\|\s*/, '');
  if (missing(s) || /^(?:facebook|log in|sign up|watch more|see more|videos|reels)$/i.test(s)) return null;
  if (/log in to continue|log into facebook|log in or sign up|sign up for facebook|confirm your identity|security check/i.test(s)) return null;
  return Array.from(s).join('').slice(0, /[\uD800-\uDBFF]/.test(s.charAt(299)) ? 299 : 300);
}
function choose(metadata = {}, page = {}) {
  for (const [value, source] of [[metadata.title, 'ORIGINAL'], [page.originalTitle, 'ORIGINAL'], [metadata.description, 'POST_TEXT'],
    [page.postText, 'POST_TEXT'], [page.description, 'PAGE_DESCRIPTION']]) {
    const title = usable(value);
    if (title) return { title, titleSource: source, titleStatus: 'AVAILABLE' };
  }
  return { title: null, titleSource: 'NONE', titleStatus: page.blocked ? 'ACCESS_REQUIRED' : page.noText ? 'NO_TEXT' : 'EXTRACTION_FAILED' };
}
// Executed in Chromium. Text is accepted only from a single-video story or a matching canonical page.
function pageSnapshot(expectedId) {
  const id = value => {
    const m = String(value || '').match(/\/(?:reel|videos)\/(?:[^/?]+\/)?(\d+)|[?&]v=(\d+)/);
    return m ? m[1] || m[2] : null;
  };
  const candidates = {};
  for (const anchor of document.querySelectorAll('a[href]')) {
    const key = id(anchor.href); if (!key || (expectedId && key !== expectedId)) continue;
    const story = anchor.closest('[role="article"],article'); if (!story) continue;
    const ids = new Set(Array.from(story.querySelectorAll('a[href]')).map(a => id(a.href)).filter(Boolean));
    if (ids.size !== 1) continue;
    const message = story.querySelector('[data-ad-preview="message"],[data-ad-comet-preview="message"]');
    if (message && message.innerText) candidates[key] = { postText: message.innerText.slice(0, 4000) };
  }
  let budget = 2000000, nodes = 0;
  for (const script of Array.from(document.querySelectorAll('script[type="application/json"]')).slice(0, 60)) {
    const text=String(script.textContent || ''); if(!text || text.length > budget) continue;
    budget-=text.length;
    try {
      const stack=[JSON.parse(text)];
      while(stack.length && nodes++ < 50000) {
        const node=stack.pop(); if(!node || typeof node!=='object') continue;
        if(node.__typename==='Video' && /^\d+$/.test(String(node.id)) && (!expectedId || String(node.id)===expectedId)) {
          const message=node.creation_story?.message?.text;
          if(typeof message==='string' && message.trim()) candidates[String(node.id)]={postText:message.slice(0,4000)};
        }
        for(const child of Object.values(node)) if(child && typeof child==='object') stack.push(child);
      }
    } catch {}
  }
  const meta = name => document.querySelector('meta[property="'+name+'"]')?.content || '';
  const canonical = document.querySelector('link[rel="canonical"]')?.href || meta('og:url') || location.href;
  const matched = expectedId && id(canonical) === expectedId && id(location.href) === expectedId;
  const body = (document.body?.innerText || '').slice(0, 6000);
  const blocked = /facebook\.com\/(login|checkpoint|challenge|two_factor)/i.test(location.href)
    || /log in to continue|confirm your identity|security check|登录以继续|验证你的身份/i.test(body);
  return { candidates, matched, blocked, ...(matched ? {
    ...(candidates[expectedId] || {}), originalTitle: meta('og:title'), description: meta('og:description'),
    noText: !!document.querySelector('video') && !!document.querySelector('[data-ad-preview="message"],[data-ad-comet-preview="message"]')
      && !document.querySelector('[data-ad-preview="message"],[data-ad-comet-preview="message"]').innerText.trim()
  } : {}) };
}
module.exports = { clean, missing, usable, choose, pageSnapshot };
