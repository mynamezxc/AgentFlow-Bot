// Pure helpers for keeping the page's copy of the workspace current without refetching it: delta merging, the poll schedule,
// a small markdown cache and an optional IndexedDB copy of the last state (so a reload paints before the network answers).

/** Collections that are only partly loaded (the newest messages per channel); records older than the loaded part are ignored. */
const WINDOWED = {messages: m => m.channel};

const stamp = v => { const t = Date.parse(v || ''); return Number.isNaN(t) ? 0 : t; };

/**
 * Apply a /api/state?since= response to `data` in place.
 * delta = {rev, changes:{kind:[record]}, deleted:{kind:[id]}, settings?, runtime}. Returns {changed, kinds} where `changed`
 * is true when any record, deletion or setting differs (runtime changes are reported separately by the caller's signature).
 */
export function mergeState(data, delta) {
  const kinds = new Set();
  for (const [kind, records] of Object.entries(delta.changes || {})) {
    if (!records.length) continue;
    const list = data[kind] ||= [];
    const index = new Map(list.map((item, i) => [item.id, i]));
    let oldest = null;
    if (WINDOWED[kind]) {   // oldest loaded created_at per channel
      oldest = new Map();
      for (const item of list) { const c = WINDOWED[kind](item), t = stamp(item.created_at); if (!oldest.has(c) || t < oldest.get(c)) oldest.set(c, t); }
    }
    for (const record of records) {
      const at = index.get(record.id);
      if (at !== undefined) { list[at] = record; kinds.add(kind); continue; }
      if (oldest) {   // a patched old message that was never loaded must not appear at the bottom
        const first = oldest.get(WINDOWED[kind](record));
        if (first !== undefined && stamp(record.created_at) < first) continue;
      }
      index.set(record.id, list.length);
      list.push(record);
      kinds.add(kind);
    }
  }
  for (const [kind, ids] of Object.entries(delta.deleted || {})) {
    if (!ids.length || !data[kind]) continue;
    const gone = new Set(ids), before = data[kind].length;
    data[kind] = data[kind].filter(item => !gone.has(item.id));
    if (data[kind].length !== before) kinds.add(kind);
  }
  if (delta.settings) { data.settings = delta.settings; kinds.add('settings'); }
  if (delta.rev) data.rev = delta.rev;
  if (delta.runtime) data.runtime = delta.runtime;
  return {changed: kinds.size > 0, kinds: [...kinds]};
}

/** Put older messages (oldest first, as /api/messages returns them) in front of the loaded ones; returns how many were new. */
export function prependMessages(data, older) {
  const have = new Set((data.messages || []).map(m => m.id));
  const fresh = (older || []).filter(m => m && !have.has(m.id));
  if (fresh.length) data.messages = [...fresh, ...(data.messages || [])];
  return fresh.length;
}

/**
 * Milliseconds until the next poll, or null while the tab is hidden (the caller polls again when it becomes visible).
 * active: a run is working; sinceChange: ms since the last response that changed anything; failures: consecutive failed polls.
 */
export function pollDelay({hidden = false, active = false, sinceChange = Infinity, failures = 0} = {}) {
  if (hidden) return null;
  if (failures > 0) return Math.min(30000, 2000 * 2 ** Math.min(failures, 5));
  if (active) return 1500;
  if (sinceChange < 30000) return 2000;
  if (sinceChange < 180000) return 5000;
  return 10000;
}

/** Cache of rendered markdown keyed by message id + version (and the lookups it depends on); least recently used entries go first. */
export function createFormatCache(limit = 600) {
  const map = new Map();
  let deps = '';
  return {
    /** Anything the output depends on besides the text (agent names, artifact lists...). A new value empties the cache. */
    setDeps(value) { if (value !== deps) { deps = value; map.clear(); } },
    get(key, compute) {
      if (map.has(key)) { const value = map.get(key); map.delete(key); map.set(key, value); return value; }
      const value = compute();
      map.set(key, value);
      if (map.size > limit) map.delete(map.keys().next().value);
      return value;
    },
    get size() { return map.size; },
    clear() { map.clear(); },
  };
}

// ---- last-state copy in IndexedDB (every call is best effort) -----------------------------------------------------------
const DB = 'agentflow-cache', STORE = 'state';
const open = () => new Promise((resolve, reject) => {
  if (typeof indexedDB === 'undefined') return reject(new Error('no indexedDB'));
  const request = indexedDB.open(DB, 1);
  request.onupgradeneeded = () => request.result.createObjectStore(STORE);
  request.onsuccess = () => resolve(request.result);
  request.onerror = () => reject(request.error);
});

export async function loadCachedState(key) {
  try {
    const db = await open();
    return await new Promise(resolve => {
      const request = db.transaction(STORE).objectStore(STORE).get(key);
      request.onsuccess = () => { db.close(); resolve(request.result || null); };
      request.onerror = () => { db.close(); resolve(null); };
    });
  } catch { return null; }
}

export async function saveCachedState(key, state) {
  try {
    const db = await open();
    await new Promise(resolve => {
      const tx = db.transaction(STORE, 'readwrite');
      tx.objectStore(STORE).put(state, key);
      tx.oncomplete = tx.onerror = tx.onabort = () => { db.close(); resolve(); };
    });
  } catch { /* the cache is an optimisation only */ }
}

/** The state worth keeping (only records and flags; the runtime block holds no credentials), or null while it is incomplete. */
export function cacheable(data) {
  return data && data.rev && data.agents ? {...data, cached_at: Date.now()} : null;
}
