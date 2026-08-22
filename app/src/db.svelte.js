// The client's local truth. `items` is the catalogue mirror that makes a
// zero-network boot possible; `local_media` is this device's private answer to
// "do I actually have the bytes?" and never syncs. docs/03-data-model.md §4.
//
// ponytail: raw IndexedDB rather than the `idb` package. Two stores with
// getAll/put/delete is ~20 lines; a dependency for that is not worth it.
// Revisit when playlists and the outbox arrive and there is a real migration.

const NAME = 'pwa-yt';
const VERSION = 3;

// store name -> keyPath
const STORES = {
  items: 'id',
  local_media: 'item_id',
  meta: 'key', // last sync, last verification sweep
};

function open() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(NAME, VERSION);
    req.onupgradeneeded = () => {
      for (const [store, keyPath] of Object.entries(STORES)) {
        if (!req.result.objectStoreNames.contains(store)) {
          req.result.createObjectStore(store, { keyPath });
        }
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function run(store, mode, fn) {
  const db = await open();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(store, mode);
      const req = fn(tx.objectStore(store));
      tx.oncomplete = () => resolve(req?.result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

export const all = (store) => run(store, 'readonly', (s) => s.getAll());

/**
 * Rows arriving here are usually Svelte `$state` proxies, and neither
 * IndexedDB nor postMessage can structured-clone a Proxy — both throw
 * DataCloneError. Snapshotting at this boundary, once, means no call site can
 * get it wrong. It is also why this file is `.svelte.js`: `$state.snapshot` is
 * a rune and is not available in a plain module.
 */
export const put = (store, row) =>
  run(store, 'readwrite', (s) => s.put($state.snapshot(row)));

export const remove = (store, key) => run(store, 'readwrite', (s) => s.delete(key));

export const getMeta = async (key) =>
  (await run('meta', 'readonly', (s) => s.get(key)))?.value ?? null;
export const setMeta = (key, value) => put('meta', { key, value });
