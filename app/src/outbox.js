// Offline mutation queue. docs/02-offline-playback.md §4: playlist create,
// rename, reorder, add, remove, and item delete all write to IndexedDB
// immediately and must keep working fully offline; this is what replays them
// once the network is back.
//
// No idempotency-key ledger: every mutation below is already safe to replay
// as-is. Creates carry a client-generated id (`INSERT ... ON CONFLICT DO
// NOTHING`), renames/deletes are last-write-wins by nature, and the playlist
// items patch is `ON CONFLICT DO UPDATE`. Retrying the same call twice is a
// no-op the second time, so the outbox can just re-issue the original request
// in order rather than needing a bespoke sync protocol. See D-018.

import * as db from './db.svelte.js';
import * as api from './api.js';

const REPLAY = {
  playlist_create: (p) => api.createPlaylist(p.id, p.name),
  playlist_rename: (p) => api.renamePlaylist(p.id, p.name),
  playlist_delete: (p) => api.deletePlaylist(p.id),
  playlist_items_patch: (p) => api.patchPlaylistItems(p.playlist_id, p.upserts, p.removes),
  items_create: (p) => api.createItems(p.entries, p.playlist_id),
  item_delete: (p) => api.deleteItem(p.id),
};

export const enqueue = (kind, payload) =>
  db.put('outbox', { kind, payload, created_at: new Date().toISOString() });

let draining = false;

/**
 * Replays queued mutations in `seq` order, one at a time. Stops at the first
 * failure and leaves it and everything after it queued — order matters, so a
 * later mutation must never apply before an earlier one that is still stuck.
 */
export async function drainOnce() {
  if (draining) return;
  draining = true;
  try {
    for (;;) {
      // ponytail: re-reads the whole store each loop instead of a cursor.
      // Fine at the hundreds-of-rows scale a reorder session produces; revisit
      // with a cursor if the outbox ever grows unbounded.
      const [row] = await db.all('outbox');
      if (!row) return;
      try {
        await REPLAY[row.kind](row.payload);
      } catch {
        return; // still offline, or the server is down — try again later
      }
      await db.remove('outbox', row.seq);
    }
  } finally {
    draining = false;
  }
}
