// One IndexedDB store: local_media, "do I actually have this?", per device,
// never synced. docs/03-data-model.md §4.
//
// ponytail: raw IndexedDB rather than the `idb` package. One store with
// get/put/delete is ~15 lines; a dependency for that is not worth it. Revisit
// when there are six stores, indexes and a version migration to run.

const NAME = 'tarmac';
const VERSION = 1;
const STORE = 'local_media';

function open() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(NAME, VERSION);
    req.onupgradeneeded = () => req.result.createObjectStore(STORE, { keyPath: 'item_id' });
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function run(mode, fn) {
  const db = await open();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      const req = fn(tx.objectStore(STORE));
      tx.oncomplete = () => resolve(req?.result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

export const all = () => run('readonly', (s) => s.getAll());
export const put = (row) => run('readwrite', (s) => s.put(row));
export const remove = (id) => run('readwrite', (s) => s.delete(id));
