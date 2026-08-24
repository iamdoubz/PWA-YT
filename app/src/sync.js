// Multi-device convergence, pull half. The push half needs no code of its
// own — every offline mutation already replays through the same idempotent
// REST endpoints (outbox.js, D-018); this is what lets a device learn what
// *another* device signed into the same account did.
//
// Last-write-wins by `updated_at`; a tombstone beats an update at the same
// timestamp. See 03-data-model.md §5.

import * as db from './db.svelte.js';
import * as api from './api.js';

function wins(incoming, local) {
  if (!local) return true;
  // A local row with no `updated_at` (an optimistic write that forgot to
  // stamp one) must still lose to a real one — `"2026-..." > undefined` is
  // `false` in JS, so without this check that row would never sync-update
  // again, permanently stuck with whatever the optimistic write guessed.
  if (!local.updated_at) return true;
  if (incoming.updated_at !== local.updated_at) return incoming.updated_at > local.updated_at;
  return !!incoming.deleted_at && !local.deleted_at;
}

async function applyItems(rows, onTombstone) {
  for (const row of rows) {
    const local = await db.get('items', row.id);
    if (!wins(row, local)) continue;
    if (row.deleted_at) {
      // `items` keeps no tombstone rows locally (forget() hard-deletes) — a
      // remote delete follows the same local convention.
      await db.remove('items', row.id);
      await onTombstone?.(row.id);
    } else {
      await db.put('items', row);
    }
  }
}

async function applyPlaylists(rows) {
  for (const row of rows) {
    const local = await db.get('playlists', row.id);
    if (wins(row, local)) await db.put('playlists', row);
  }
}

async function applyPlaylistItems(rows) {
  for (const row of rows) {
    const local = await db.get('playlist_items', [row.playlist_id, row.item_id]);
    if (wins(row, local)) await db.put('playlist_items', row);
  }
}

let syncing = false;

/**
 * `onTombstone(itemId)` is how a remote delete reaches OPFS — this module
 * only touches IndexedDB, so App.svelte supplies the side effect (purge the
 * worker's media, revoke object URLs, stop playback if it was playing).
 */
export async function pullOnce(onTombstone) {
  if (syncing) return null;
  syncing = true;
  try {
    const cursor = (await db.getMeta('sync_cursor')) ?? '';
    const result = await api.sync(cursor);
    await applyItems(result.items, onTombstone);
    await applyPlaylists(result.playlists);
    await applyPlaylistItems(result.playlist_items);
    await db.setMeta('sync_cursor', result.cursor);
    return result;
  } finally {
    syncing = false;
  }
}
