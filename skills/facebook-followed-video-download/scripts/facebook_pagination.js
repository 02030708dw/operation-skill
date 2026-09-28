// Conservative proof from the requested profile's primary connection only.
// Captions, suggested feeds and scroll stagnation are never completion evidence.
const CONNECTIONS = ['timeline_list_feed_units', 'all_videos', 'video_list'];
function identity(raw) {
  try {
    const u = new URL(raw);
    if (!/(^|\.)facebook\.com$/.test(u.hostname)) return null;
    return u.pathname === '/profile.php' ? u.searchParams.get('id') : u.pathname.split('/').filter(Boolean)[0]?.toLowerCase();
  } catch { return null; }
}
function parsePage(payload, source, variables = {}) {
  if (!payload || payload.errors?.length) return [];
  const expected = identity(source);
  if (!expected) return [];
  const result = [];
  for (const node of [payload.data?.node, payload.data?.user, payload.data?.page]) {
    if (!node || !['Page', 'User'].includes(node.__typename)) continue;
    if (identity(node.url) !== expected && String(node.id) !== expected) continue;
    for (const name of CONNECTIONS) {
      const connection = node[name], info = connection?.page_info;
      if (!Array.isArray(connection?.edges) || typeof info?.has_next_page !== 'boolean') continue;
      if (info.has_next_page && typeof info.end_cursor !== 'string') continue;
      // Only an explicitly addressed first page or a saved cursor can join a chain.
      if (!Object.hasOwn(variables, 'cursor') && !Object.hasOwn(variables, 'after')) continue;
      const start = variables.cursor ?? variables.after ?? null;
      if (start !== null && typeof start !== 'string') continue;
      const urls = new Set(); let visited = 0;
      function walk(value) {
        if (!value || typeof value !== 'object' || ++visited > 50000) return;
        if (value.__typename === 'Video' && /^\d+$/.test(String(value.id))) urls.add('https://www.facebook.com/watch/?v='+value.id);
        for (const item of Object.values(value)) if (typeof item === 'object') walk(item);
      }
      connection.edges.forEach(walk);
      result.push({key: String(node.id)+':'+name, start, end: info.end_cursor ?? null,
        exhausted: !info.has_next_page, urls: [...urls], empty: connection.edges.length === 0});
    }
  }
  return result;
}
class PaginationProof {
  constructor() { this.chains = new Map(); this.urls = new Set(); }
  add(pages) {
    for (const page of pages) {
      if (!this.chains.has(page.key)) this.chains.set(page.key, new Map());
      this.chains.get(page.key).set(page.start, page);
      page.urls.forEach(url => this.urls.add(url));
    }
  }
  get exhausted() {
    if (!this.chains.size) return false;
    for (const chain of this.chains.values()) {
      let cursor = null, complete = false; const visited = new Set();
      while (chain.has(cursor) && !visited.has(cursor)) {
        visited.add(cursor); const page = chain.get(cursor);
        if (page.exhausted) { complete = true; break; }
        cursor = page.end;
      }
      if (!complete) return false;
    }
    return true;
  }
}
module.exports = {parsePage, PaginationProof};
