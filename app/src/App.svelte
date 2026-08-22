<script>
  import { onMount } from 'svelte';
  import { generateKeyBetween, generateNKeysBetween } from 'fractional-indexing';
  import * as db from './db.svelte.js';
  import * as api from './api.js';
  import { API } from './api.js';
  import { net } from './net.svelte.js';
  import * as outbox from './outbox.js';
  import { uuid7 } from './id.js';

  const ACTIVE = ['queued', 'fetching', 'transforming', 'ready'];
  const STAGE = {
    queued: 'queued',
    fetching: 'downloading on server',
    transforming: 'converting',
    ready: 'sending to device',
  };

  let items = $state([]); // catalogue mirror, from IndexedDB
  let media = $state({}); // item_id -> local_media row
  let urls = $state({}); // item_id -> { audio, art } object URLs
  let jobs = $state({}); // item_id -> latest job
  let progress = $state({}); // item_id -> 0..1 while pulling into OPFS
  let errors = $state({});

  let urlInput = $state('');
  let plan = $state(null);
  let planError = $state(null);
  let resolving = $state(false);
  // Set while `/resolve` is streaming a playlist: entries arrive one line at a
  // time so a 400-entry playlist starts rendering long before the last one.
  let playlistImport = $state(null);

  let view = $state('library'); // 'library' | 'playlists'
  let playlists = $state([]); // catalogue mirror, from IndexedDB
  let playlistItems = $state([]); // raw playlist_items rows (incl. tombstones)
  let openPlaylistId = $state(null); // which playlist's detail view is open
  let newPlaylistName = $state('');
  let editingPlaylistId = $state(null);
  let editingName = $state('');
  // Which playlist (if any) the currently-playing track's next/prev cycles
  // through. null means "cycle through everything downloaded", the v0.2
  // behaviour.
  let queuePlaylistId = $state(null);

  // Sent with every add. Stored per item server-side, so re-adding the same
  // track at a different bitrate genuinely re-pulls it at that bitrate.
  let profile = $state({ audio_codec: 'aac', audio_bitrate: 192, save_artwork: true });

  let persisted = $state(null);
  let moveSupported = $state(null);
  let mediaActions = $state({});
  let sweep = $state(null); // { checked, total } while the verification sweep runs
  let storage = $state(null); // { usage, quota }
  let lastSync = $state(null);
  let lastVerify = $state(null);
  let shellCached = $state(false);
  let playingId = $state(null);
  let paused = $state(true);
  let at = $state(0);
  let duration = $state(0);
  let booted = $state(false);

  let audio; // ONE element for the app's lifetime. FM-5: a fresh element per
  // track loses the iOS gesture unlock and playback dies once backgrounded.
  let worker;
  let poll = null;
  const inFlight = new Set();

  const playing = $derived(items.find((i) => i.id === playingId) ?? null);
  // "downloaded" means the bytes are here, not that a row once claimed they were.
  const downloaded = $derived(items.filter((i) => media[i.id]?.state === 'present'));
  const verified = $derived(downloaded.filter((i) => media[i.id]?.verified_at).length);

  const activePlaylists = $derived(
    playlists.filter((p) => !p.deleted_at).sort((a, b) => a.created_at.localeCompare(b.created_at)),
  );
  const openPlaylist = $derived(activePlaylists.find((p) => p.id === openPlaylistId) ?? null);

  // Rows (not resolved to items), sorted by fractional position, tombstones
  // dropped. The one place every playlist-ordering read goes through.
  function orderedPlaylistRows(playlistId) {
    return playlistItems
      .filter((pi) => pi.playlist_id === playlistId && !pi.deleted_at)
      .sort((a, b) => a.position.localeCompare(b.position));
  }
  function orderedPlaylistItems(playlistId) {
    return orderedPlaylistRows(playlistId)
      .map((pi) => items.find((i) => i.id === pi.item_id))
      .filter(Boolean);
  }
  const openPlaylistTracks = $derived(openPlaylistId ? orderedPlaylistItems(openPlaylistId) : []);

  const importSelected = $derived(
    playlistImport ? playlistImport.entries.filter((e) => !playlistImport.deselected[e.source_key]) : [],
  );
  const importSelectedBytes = $derived(
    importSelected.reduce((sum, e) => sum + (e.estimated_bytes ?? 0), 0),
  );

  // Next/previous cycles the open playlist's *live* order when playing from
  // one, so a reorder made mid-playback (the whole point of offline mutation)
  // takes effect immediately rather than only on the next play.
  function currentQueue() {
    if (!queuePlaylistId) return downloaded;
    return orderedPlaylistItems(queuePlaylistId).filter((i) => media[i.id]?.state === 'present');
  }

  onMount(async () => {
    // The boot path. Local reads only — no fetch, no await on anything that
    // could hang. FM-2 is the most commonly missed failure mode and it is
    // missed exactly here.
    const [catalogue, local, pls, plItems] = await Promise.all([
      db.all('items'),
      db.all('local_media'),
      db.all('playlists'),
      db.all('playlist_items'),
    ]);
    items = catalogue.sort((a, b) => b.added_at.localeCompare(a.added_at));
    for (const row of local) media[row.item_id] = row;
    playlists = pls;
    playlistItems = plItems;
    booted = true;

    navigator.storage.persisted().then((v) => (persisted = v));
    shellCached = !!navigator.serviceWorker?.controller;
    db.getMeta('last_sync').then((v) => (lastSync = v));
    db.getMeta('last_verify').then((v) => (lastVerify = v));
    refreshStorage();

    // Object URLs are built for every downloaded item up front, not on click.
    // Reading OPFS is async, and awaiting it inside a click handler breaks the
    // iOS user-gesture chain, so play() would be rejected.
    //
    // ponytail: read straight from OPFS rather than mirroring artwork into an
    // IndexedDB blob store as FM-7 suggests. Both are local, this needs no
    // second copy of the bytes, and it happens after first paint. Revisit at
    // v0.2 when a sweep has thousands of items to open.
    for (const row of local) resolveUrls(row);

    worker = new Worker(new URL('./opfs-worker.js', import.meta.url), { type: 'module' });
    worker.onmessage = onWorkerMessage;

    setupMediaSession();

    // Both optional, both failable, both strictly after first paint. FM-6 says
    // verify lazily — first paint uses the catalogue's claimed state and the
    // sweep corrects it a moment later, rather than blocking boot on N stat
    // calls that would each have to succeed before anything appeared.
    queueMicrotask(() => {
      startPolling();
      runSweep();
      outbox.drainOnce();
    });

    // The other trigger for draining queued offline mutations — reconnect can
    // happen with the app already open and idle, not just at boot.
    window.addEventListener('online', () => outbox.drainOnce());
  });

  async function refreshStorage() {
    if (!navigator.storage?.estimate) return;
    const { usage, quota } = await navigator.storage.estimate();
    storage = { usage, quota };
  }

  // ------------------------------------------------------------------ adding
  //
  // Resolving and importing brand-new content is not an offline operation —
  // there is nothing to download without a network — so unlike everything in
  // the "playlists" section below, none of this goes through the outbox. A
  // failure here just means "try again", not "queue for later".

  async function doResolve() {
    if (!urlInput.trim()) return;
    resolving = true;
    planError = null;
    plan = null;
    playlistImport = null;
    try {
      await api.streamResolve(urlInput.trim(), $state.snapshot(profile), {
        onLine(line) {
          if (line.kind === 'item') {
            plan = line.entry;
          } else if (line.kind === 'playlist_head') {
            playlistImport = {
              title: line.title,
              entryCount: line.entry_count,
              entries: [],
              deselected: {}, // source_key -> true, for per-entry deselection
              done: false,
            };
          } else if (line.kind === 'entry') {
            playlistImport.entries.push(line);
          } else if (line.kind === 'playlist_done') {
            playlistImport.done = true;
          }
        },
      });
    } catch (err) {
      planError = err.message;
    } finally {
      resolving = false;
    }
  }

  async function confirmAdd() {
    const entry = plan;
    plan = null;
    try {
      const created = (await api.createItem(entry.source_key, $state.snapshot(profile))).items[0];
      const row = {
        id: created.item_id,
        source_key: entry.source_key,
        title: entry.title,
        uploader: entry.uploader,
        duration_s: entry.duration_s,
        added_at: new Date().toISOString(),
      };
      await db.put('items', row);
      items = [row, ...items.filter((i) => i.id !== row.id)];
      urlInput = '';
      startPolling();
    } catch (err) {
      planError = err.message;
    }
  }

  function toggleImportEntry(sourceKey) {
    if (playlistImport.deselected[sourceKey]) delete playlistImport.deselected[sourceKey];
    else playlistImport.deselected[sourceKey] = true;
  }

  async function confirmPlaylistImport() {
    const imp = playlistImport;
    playlistImport = null;
    const selected = imp.entries.filter((e) => !imp.deselected[e.source_key]);
    if (!selected.length) return;

    try {
      const playlistId = uuid7();
      await api.createPlaylist(playlistId, imp.title);

      const positions = generateNKeysBetween(null, null, selected.length);
      const entries = selected.map((e, i) => ({
        source_key: e.source_key,
        format_profile: $state.snapshot(profile),
        position: positions[i],
      }));
      const created = (await api.createItems(entries, playlistId)).items;

      const stamp = new Date().toISOString();
      const playlistRow = { id: playlistId, name: imp.title, created_at: stamp, updated_at: stamp };
      await db.put('playlists', playlistRow);
      playlists = [...playlists, playlistRow];

      for (let i = 0; i < created.length; i++) {
        const src = selected[i];
        const itemRow = {
          id: created[i].item_id,
          source_key: src.source_key,
          title: src.title,
          uploader: src.uploader,
          duration_s: src.duration_s,
          added_at: stamp,
        };
        await db.put('items', itemRow);
        items = [itemRow, ...items.filter((it) => it.id !== itemRow.id)];

        const piRow = {
          playlist_id: playlistId,
          item_id: created[i].item_id,
          position: positions[i],
          updated_at: stamp,
          deleted_at: null,
        };
        await db.put('playlist_items', piRow);
        playlistItems = [...playlistItems, piRow];
      }
      urlInput = '';
      openPlaylistId = playlistId;
      view = 'playlists';
      startPolling();
    } catch (err) {
      planError = err.message;
    }
  }

  // -------------------------------------------------------------- playlists
  //
  // Everything here writes to IndexedDB first and unconditionally — offline
  // mutation (02-offline-playback.md §4) means these must work with zero
  // network at all. `mutate()` then makes a best-effort server call and, if
  // that fails, queues the same mutation in the outbox for later — every
  // mutation kind it's used for is naturally safe to replay (see outbox.js).

  async function mutate(kind, payload, apply) {
    try {
      await apply();
      outbox.drainOnce(); // if that succeeded we're online; flush anything queued
    } catch {
      await outbox.enqueue(kind, payload);
    }
  }

  async function createPlaylistLocal() {
    const name = newPlaylistName.trim();
    if (!name) return;
    newPlaylistName = '';
    const id = uuid7();
    const stamp = new Date().toISOString();
    const row = { id, name, created_at: stamp, updated_at: stamp };
    await db.put('playlists', row);
    playlists = [...playlists, row];
    await mutate('playlist_create', { id, name }, () => api.createPlaylist(id, name));
  }

  async function renamePlaylistLocal(playlist, name) {
    name = name.trim();
    if (!name || name === playlist.name) return;
    const row = { ...playlist, name, updated_at: new Date().toISOString() };
    await db.put('playlists', row);
    playlists = playlists.map((p) => (p.id === row.id ? row : p));
    await mutate('playlist_rename', { id: row.id, name }, () => api.renamePlaylist(row.id, name));
  }

  async function deletePlaylistLocal(playlist) {
    const stamp = new Date().toISOString();
    const row = { ...playlist, deleted_at: stamp, updated_at: stamp };
    await db.put('playlists', row);
    playlists = playlists.map((p) => (p.id === row.id ? row : p));
    if (openPlaylistId === row.id) openPlaylistId = null;
    if (queuePlaylistId === row.id) queuePlaylistId = null;
    await mutate('playlist_delete', { id: row.id }, () => api.deletePlaylist(row.id));
  }

  async function addToPlaylist(playlist, item) {
    const last = orderedPlaylistRows(playlist.id).at(-1);
    const position = generateKeyBetween(last?.position ?? null, null);
    await upsertPlaylistItem(playlist.id, item.id, position);
  }

  async function moveInPlaylist(playlist, item, direction) {
    const ordered = orderedPlaylistRows(playlist.id);
    const idx = ordered.findIndex((pi) => pi.item_id === item.id);
    const j = idx + direction;
    if (idx < 0 || j < 0 || j >= ordered.length) return;
    const position =
      direction < 0
        ? generateKeyBetween(ordered[j - 1]?.position ?? null, ordered[j].position)
        : generateKeyBetween(ordered[j].position, ordered[j + 1]?.position ?? null);
    await upsertPlaylistItem(playlist.id, item.id, position);
  }

  async function upsertPlaylistItem(playlistId, itemId, position) {
    const stamp = new Date().toISOString();
    const row = { playlist_id: playlistId, item_id: itemId, position, updated_at: stamp, deleted_at: null };
    await db.put('playlist_items', row);
    playlistItems = [...playlistItems.filter((pi) => !(pi.playlist_id === playlistId && pi.item_id === itemId)), row];
    await mutate(
      'playlist_items_patch',
      { playlist_id: playlistId, upserts: [{ item_id: itemId, position }], removes: [] },
      () => api.patchPlaylistItems(playlistId, [{ item_id: itemId, position }], []),
    );
  }

  async function removeFromPlaylist(playlist, item) {
    const existing = playlistItems.find((pi) => pi.playlist_id === playlist.id && pi.item_id === item.id);
    if (!existing) return;
    const stamp = new Date().toISOString();
    const row = { ...existing, deleted_at: stamp, updated_at: stamp };
    await db.put('playlist_items', row);
    playlistItems = playlistItems.map((pi) =>
      pi.playlist_id === row.playlist_id && pi.item_id === row.item_id ? row : pi,
    );
    await mutate(
      'playlist_items_patch',
      { playlist_id: playlist.id, upserts: [], removes: [item.id] },
      () => api.patchPlaylistItems(playlist.id, [], [item.id]),
    );
  }

  // ------------------------------------------------------------------- jobs

  function startPolling() {
    if (poll) return;
    poll = api.openJobStream(applyJobs, () => {
      // Offline, or no server. Neither is an error — the library is local.
      stopPolling();
    });
  }

  function stopPolling() {
    poll?.close();
    poll = null;
  }

  function applyJobs(data) {
    lastSync = new Date().toISOString();
    db.setMeta('last_sync', lastSync);
    outbox.drainOnce(); // a live jobs stream is proof we're online right now

    let busy = false;
    for (const job of data.jobs) {
      jobs[job.item_id] = job;
      errors[job.item_id] = job.state === 'failed' ? job.error : null;
      // Only pull for items this device actually has in its catalogue.
      // /jobs returns everything the account has queued, including work started
      // elsewhere; downloading those would write media into OPFS that no row
      // points at, so the library never shows it and the sweep never checks it.
      // It just silently eats the storage the user is trying to budget.
      // Adopting other devices' items is what /sync is for, in v0.4.
      const known = items.some((i) => i.id === job.item_id);
      if (known && job.state === 'ready' && job.files.length && !inFlight.has(job.item_id)) {
        pull(job);
      }
      if (known && ACTIVE.includes(job.state)) busy = true;
    }
    // Idle means nothing to watch; holding the stream open would pin a server
    // thread for no reason. Any new action reopens it.
    if (!busy && inFlight.size === 0) stopPolling();
  }

  async function retry(item) {
    const job = jobs[item.id];
    if (!job) return;
    errors[item.id] = null;
    try {
      await api.retryJob(job.id);
      startPolling();
    } catch (err) {
      errors[item.id] = err.message;
    }
  }

  function pull(job) {
    inFlight.add(job.item_id);
    progress[job.item_id] = 0;
    errors[job.item_id] = null;
    worker.postMessage({
      type: 'download',
      itemId: job.item_id,
      // The hash the server computed with Python's hashlib. The worker checks
      // the bytes against it on the way to disk — FM-4 wants both length and
      // checksum, and length alone only catches truncation.
      files: job.files.map((f) => ({ name: f.name, url: API + f.path, sha256: f.sha256 })),
    });
  }

  // FM-6 recovery, and the highest-value button in the app: it tells someone
  // about to board a plane what is actually on their device, while there is
  // still network to fix it.
  function runSweep() {
    if (sweep) return;
    const rows = Object.values(media).filter((r) => r.state === 'present');
    if (!rows.length) return;
    sweep = { checked: 0, total: rows.length };
    try {
      worker.postMessage({
        type: 'verify',
        // $state.snapshot: these rows are reactive proxies, and structuredClone
        // cannot clone a Proxy — postMessage throws DataCloneError without this.
        rows: $state.snapshot(rows).map((r) => ({ item_id: r.item_id, files: r.files })),
      });
    } catch (err) {
      // Never leave the button stuck on "Checking…" with nothing running.
      sweep = null;
      errors.sweep = String(err);
    }
  }

  async function applySweep(missing) {
    const stamp = new Date().toISOString();
    for (const row of Object.values(media).filter((r) => r.state === 'present')) {
      const gone = missing.includes(row.item_id);
      const next = { ...row, state: gone ? 'missing' : 'present', verified_at: stamp };
      await db.put('local_media', next);
      media[row.item_id] = next;
      if (gone) {
        // Stays in the library, marked "not downloaded", one tap from being
        // restored. Local loss is an inconvenience, never data loss.
        for (const url of Object.values(urls[row.item_id] ?? {})) URL.revokeObjectURL(url);
        delete urls[row.item_id];
        if (playingId === row.item_id) stop();
      }
    }
    lastVerify = stamp;
    await db.setMeta('last_verify', stamp);
    sweep = null;
    refreshStorage();
  }

  const roleOf = (name) =>
    name.startsWith('audio') ? 'audio' : name.startsWith('art-sq') ? 'art' : 'art-full';

  async function onWorkerMessage({ data }) {
    if (data.type === 'progress') {
      progress[data.itemId] = data.done / data.total;
      return;
    }
    if (data.type === 'verify_progress') {
      sweep = { checked: data.checked, total: data.total };
      return;
    }
    if (data.type === 'verified') {
      await applySweep(data.missing);
      return;
    }
    if (data.type === 'purged') return;

    inFlight.delete(data.itemId);
    progress[data.itemId] = undefined;

    if (data.type === 'error') {
      errors[data.itemId] = data.error;
      return;
    }

    moveSupported = data.files.every((f) => f.moved);

    // FM-4: the row claiming the file exists is the LAST thing written, after
    // every byte is on disk and verified.
    const stamp = new Date().toISOString();
    const row = {
      item_id: data.itemId,
      state: 'present',
      files: data.files.map((f) => ({
        role: roleOf(f.name),
        name: f.name,
        bytes: f.bytes,
        sha256: f.sha256,
      })),
      downloaded_at: stamp,
      verified_at: stamp, // it was hashed on the way in
    };
    await db.put('local_media', row);
    media[data.itemId] = row;
    refreshStorage();

    // The collection acknowledgement. Best-effort: if it fails the TTL reaper
    // cleans up server-side, which is a backstop rather than the happy path.
    const job = jobs[data.itemId];
    if (job) api.acknowledge(job.id).catch(() => {});

    // FM-6: ask once there is something worth keeping, and show the real
    // answer — a silent false is how libraries disappear.
    navigator.storage.persist().then((v) => (persisted = v));
    resolveUrls(row);
  }

  async function resolveUrls(row) {
    // Per file, not all-or-nothing. An item whose audio was evicted but whose
    // artwork survived still shows its artwork — FM-6 wants it to stay visible
    // and recognisable in the library, not become a grey box.
    //
    // A file that is not there is an expected state, not an error: WebKit
    // evicts under storage pressure and that is precisely what the sweep and
    // the 'missing' badge exist to report. Surfacing a raw NotFoundError here
    // would put DOM exception text in front of someone who just needs to see
    // "not downloaded" and a button.
    const next = {};
    try {
      const root = await navigator.storage.getDirectory();
      const dir = await (await root.getDirectoryHandle('media')).getDirectoryHandle(row.item_id);
      for (const f of row.files) {
        try {
          next[f.role] = URL.createObjectURL(await (await dir.getFileHandle(f.name)).getFile());
        } catch {
          /* that one file is gone; the sweep is what records it */
        }
      }
    } catch {
      /* the whole directory is gone; same */
    }
    for (const url of Object.values(urls[row.item_id] ?? {})) URL.revokeObjectURL(url);
    urls[row.item_id] = next;
  }

  async function redownload(item) {
    errors[item.id] = null;
    try {
      await api.createItem(item.source_key, $state.snapshot(profile));
      startPolling();
    } catch (err) {
      errors[item.id] = err.message;
    }
  }

  async function forget(item) {
    for (const url of Object.values(urls[item.id] ?? {})) URL.revokeObjectURL(url);
    delete urls[item.id];
    if (playingId === item.id) stop();

    if (media[item.id]) worker.postMessage({ type: 'purge', itemId: item.id });
    await db.remove('local_media', item.id);
    await db.remove('items', item.id);
    delete media[item.id];
    items = items.filter((i) => i.id !== item.id);
    refreshStorage();
    await mutate('item_delete', { id: item.id }, () => api.deleteItem(item.id));
  }

  // ---------------------------------------------------------------- playback

  // `playlistId` is null for "play from the library" (cycles `downloaded`,
  // the v0.2 behaviour); otherwise next/previous cycle that playlist's live
  // order instead. Object URLs for every downloaded item are already resolved
  // at boot (see `resolveUrls` below), so the next track in either queue is
  // never waiting on OPFS when playback reaches it.
  function play(item, playlistId = null) {
    const url = urls[item.id]?.audio;
    if (!url) return;
    // Everything here is synchronous so the iOS gesture still counts.
    if (playingId !== item.id) {
      audio.src = url;
      playingId = item.id;
      queuePlaylistId = playlistId;
      setMetadata(item);
    }
    audio.play();
  }

  function stop() {
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
    playingId = null;
  }

  function step(delta) {
    const list = currentQueue();
    if (!list.length) return;
    const i = list.findIndex((c) => c.id === playingId);
    play(list[(i + delta + list.length) % list.length], queuePlaylistId);
  }

  function setMetadata(item) {
    if (!('mediaSession' in navigator)) return;
    const art = urls[item.id]?.art;
    navigator.mediaSession.metadata = new MediaMetadata({
      title: item.title ?? 'Unknown',
      artist: item.uploader ?? '',
      album: (queuePlaylistId && activePlaylists.find((p) => p.id === queuePlaylistId)?.name) || 'Library',
      // Must be a local object URL. A remote artwork URL blanks the lock screen
      // offline, which is the whole point of doing this at all.
      artwork: art ? [{ src: art, sizes: '512x512', type: 'image/jpeg' }] : [],
    });
  }

  function setupMediaSession() {
    if (!('mediaSession' in navigator)) return;
    const handlers = {
      play: () => audio.play(),
      pause: () => audio.pause(),
      previoustrack: () => step(-1),
      nexttrack: () => step(1),
      // Without seekto the lock-screen scrubber is decorative.
      seekto: (d) => {
        if (d.fastSeek && audio.fastSeek) audio.fastSeek(d.seekTime);
        else audio.currentTime = d.seekTime;
        pushPositionState();
      },
    };
    for (const [action, fn] of Object.entries(handlers)) {
      // Safari throws NotSupportedError for actions it does not implement.
      // Registering in a loop means one refusal cannot skip the rest.
      try {
        navigator.mediaSession.setActionHandler(action, fn);
        mediaActions[action] = true;
      } catch {
        mediaActions[action] = false;
      }
    }
  }

  // Left at the default 'none', the lock screen has to guess whether it is
  // playing, and iOS guesses by showing the wrong transport button.
  function setPlaybackState(state) {
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = state;
  }

  function pushPositionState() {
    if (!('mediaSession' in navigator) || !navigator.mediaSession.setPositionState) return;
    if (!Number.isFinite(audio.duration)) return;
    navigator.mediaSession.setPositionState({
      duration: audio.duration,
      playbackRate: audio.playbackRate,
      position: Math.min(audio.currentTime, audio.duration),
    });
  }

  let lastPush = 0;
  function onTimeUpdate() {
    at = audio.currentTime;
    if (performance.now() - lastPush > 1000) {
      lastPush = performance.now();
      pushPositionState();
    }
  }

  const clock = (s) =>
    Number.isFinite(s)
      ? `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`
      : '–:––';
  const mb = (b) => `${(b / 1e6).toFixed(1)} MB`;
  const gb = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : mb(b ?? 0));
  const when = (iso) => {
    const mins = Math.round((Date.now() - Date.parse(iso)) / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins} min ago`;
    const hrs = Math.round(mins / 60);
    return hrs < 24 ? `${hrs} h ago` : `${Math.round(hrs / 24)} d ago`;
  };
  const BUILD = __BUILD__;
</script>

<!-- The one long-lived audio element. Never recreated. -->
<audio
  bind:this={audio}
  preload="auto"
  playsinline
  onplay={() => {
    paused = false;
    setPlaybackState('playing');
  }}
  onpause={() => {
    paused = true;
    setPlaybackState('paused');
  }}
  ontimeupdate={onTimeUpdate}
  onloadedmetadata={() => {
    duration = audio.duration;
    pushPositionState();
  }}
  onended={() => step(1)}
></audio>

<main>
  <h1>PWA-YT <span class="ver">v0.3</span></h1>

  <form
    class="add"
    onsubmit={(e) => {
      e.preventDefault();
      doResolve();
    }}
  >
    <input
      type="url"
      bind:value={urlInput}
      placeholder="Paste a YouTube link"
      aria-label="Media URL"
    />
    <button type="submit" disabled={resolving || !urlInput.trim()}>
      {resolving ? 'Looking…' : 'Look up'}
    </button>
  </form>

  <details class="format">
    <summary class="dim">
      Format: {profile.audio_codec.toUpperCase()} {profile.audio_bitrate} kbps{profile.save_artwork
        ? ' · artwork'
        : ''}
    </summary>
    <div class="row">
      <label>
        Codec
        <select bind:value={profile.audio_codec}>
          <option value="aac">AAC</option>
          <option value="mp3">MP3</option>
        </select>
      </label>
      <label>
        Bitrate
        <select bind:value={profile.audio_bitrate}>
          <option value={128}>128</option>
          <option value={192}>192</option>
          <option value={256}>256</option>
        </select>
      </label>
      <label class="check">
        <input type="checkbox" bind:checked={profile.save_artwork} /> Artwork
      </label>
    </div>
    <p class="dim">
      Stored per item. Re-adding a track at a different setting re-pulls it at
      that setting. A bitrate above what the source offers is ignored — raising
      it cannot add back what the original encoder discarded.
    </p>
  </details>

  {#if planError}
    <p class="err">{planError}</p>
  {/if}

  {#if plan}
    <!-- Stage 3: confirm. The user sees what it is and what it will cost
         before anything is committed to their phone. -->
    <section class="plan">
      <strong>{plan.title}</strong>
      <span class="dim">{plan.uploader} · {clock(plan.duration_s)}</span>
      <span class="dim">~{mb(plan.estimated_bytes)} (estimate)</span>
      {#if plan.already_in_library}
        <span class="dim">Already in your library — this will re-download it.</span>
      {/if}
      <div class="actions">
        <button onclick={confirmAdd}>Download</button>
        <button class="ghost" onclick={() => (plan = null)}>Cancel</button>
      </div>
    </section>
  {/if}

  {#if playlistImport}
    <!-- Entries stream in as yt-dlp's flat enumeration produces them, so a
         400-entry playlist starts rendering well before it finishes. -->
    <section class="plan">
      <input
        class="playlist-title"
        bind:value={playlistImport.title}
        aria-label="Playlist name"
      />
      <span class="dim">
        {playlistImport.entries.length}{playlistImport.done
          ? ''
          : ` of ${playlistImport.entryCount}`} tracks found{playlistImport.done ? '' : ' · resolving…'}
      </span>
      <span class="dim">{importSelected.length} selected · ~{mb(importSelectedBytes)} (estimate)</span>
      <ul class="import-list">
        {#each playlistImport.entries as e (e.source_key)}
          <li>
            <label>
              <input
                type="checkbox"
                checked={!playlistImport.deselected[e.source_key]}
                onchange={() => toggleImportEntry(e.source_key)}
              />
              <span class="title">{e.title ?? e.source_key}</span>
              <span class="dim">
                {clock(e.duration_s)}{e.already_in_library ? ' · already in library' : ''}
              </span>
            </label>
          </li>
        {/each}
      </ul>
      <div class="actions">
        <button onclick={confirmPlaylistImport} disabled={!importSelected.length}>
          Import {importSelected.length} track{importSelected.length === 1 ? '' : 's'}
        </button>
        <button class="ghost" onclick={() => (playlistImport = null)}>Cancel</button>
      </div>
    </section>
  {/if}

  {#if !booted}
    <p class="dim">Reading local catalogue…</p>
  {:else}
    <div class="tabs">
      <button class:active={view === 'library'} onclick={() => (view = 'library')}>Library</button>
      <button class:active={view === 'playlists'} onclick={() => (view = 'playlists')}>
        Playlists{activePlaylists.length ? ` (${activePlaylists.length})` : ''}
      </button>
    </div>

    {#if view === 'library'}
      <ul class="library">
        {#each items as item (item.id)}
          {@const job = jobs[item.id]}
          {@const pct = progress[item.id]}
          <li class:active={playingId === item.id}>
            <div class="art">
              {#if urls[item.id]?.art}<img src={urls[item.id].art} alt="" />{/if}
            </div>
            <div class="meta">
              <strong>{item.title}</strong>
              <span class="dim">
                {item.uploader} · {clock(item.duration_s)}
                {#if media[item.id]?.state === 'missing'} · not downloaded{/if}
              </span>
              {#if errors[item.id]}<span class="err">{errors[item.id]}</span>{/if}
            </div>
            <div class="actions">
              {#if pct !== undefined}
                <span class="dim">{Math.round(pct * 100)}% to device</span>
              {:else if media[item.id]?.state === 'present'}
                <button onclick={() => play(item, null)}>Play</button>
                <button class="ghost" onclick={() => forget(item)}>Delete</button>
              {:else if job && ACTIVE.includes(job.state)}
                <span class="dim">
                  {STAGE[job.state]}{job.progress ? ` ${Math.round(job.progress * 100)}%` : ''}…
                </span>
              {:else if job?.state === 'failed'}
                <button onclick={() => retry(item)}>Retry</button>
                <button class="ghost" onclick={() => forget(item)}>Delete</button>
              {:else}
                <button onclick={() => redownload(item)}>Download</button>
                <button class="ghost" onclick={() => forget(item)}>Delete</button>
              {/if}
              {#if activePlaylists.length}
                <select
                  class="add-to-playlist"
                  aria-label="Add to playlist"
                  onchange={(e) => {
                    const pl = activePlaylists.find((p) => p.id === e.currentTarget.value);
                    e.currentTarget.value = '';
                    if (pl) addToPlaylist(pl, item);
                  }}
                >
                  <option value="" disabled selected>Add to…</option>
                  {#each activePlaylists as p (p.id)}<option value={p.id}>{p.name}</option>{/each}
                </select>
              {/if}
            </div>
          </li>
        {:else}
          <li class="empty dim">Nothing here yet. Paste a link above.</li>
        {/each}
      </ul>
    {:else if !openPlaylist}
      <form
        class="add"
        onsubmit={(e) => {
          e.preventDefault();
          createPlaylistLocal();
        }}
      >
        <input bind:value={newPlaylistName} placeholder="New playlist name" aria-label="Playlist name" />
        <button type="submit" disabled={!newPlaylistName.trim()}>Create</button>
      </form>
      <ul class="library">
        {#each activePlaylists as p (p.id)}
          <li>
            <div
              class="meta"
              onclick={() => (openPlaylistId = p.id)}
              onkeydown={(e) => e.key === 'Enter' && (openPlaylistId = p.id)}
              role="button"
              tabindex="0"
            >
              <strong>{p.name}</strong>
              <span class="dim">{orderedPlaylistItems(p.id).length} tracks</span>
            </div>
            <div class="actions">
              <button
                class="ghost"
                onclick={() => {
                  editingPlaylistId = p.id;
                  editingName = p.name;
                }}
              >
                Rename
              </button>
              <button class="ghost" onclick={() => deletePlaylistLocal(p)}>Delete</button>
            </div>
          </li>
          {#if editingPlaylistId === p.id}
            <li>
              <form
                class="add"
                onsubmit={(e) => {
                  e.preventDefault();
                  renamePlaylistLocal(p, editingName);
                  editingPlaylistId = null;
                }}
              >
                <input bind:value={editingName} aria-label="Rename playlist" />
                <button type="submit">Save</button>
                <button type="button" class="ghost" onclick={() => (editingPlaylistId = null)}>
                  Cancel
                </button>
              </form>
            </li>
          {/if}
        {:else}
          <li class="empty dim">No playlists yet.</li>
        {/each}
      </ul>
    {:else}
      <div class="row">
        <button class="ghost" onclick={() => (openPlaylistId = null)}>‹ Playlists</button>
        <strong>{openPlaylist.name}</strong>
        <button class="ghost" onclick={() => deletePlaylistLocal(openPlaylist)}>Delete</button>
      </div>
      <ul class="library">
        {#each openPlaylistTracks as item, i (item.id)}
          <li class:active={playingId === item.id}>
            <div class="art">
              {#if urls[item.id]?.art}<img src={urls[item.id].art} alt="" />{/if}
            </div>
            <div class="meta">
              <strong>{item.title}</strong>
              <span class="dim">
                {item.uploader} · {clock(item.duration_s)}
                {#if media[item.id]?.state !== 'present'} · not downloaded{/if}
              </span>
            </div>
            <div class="actions">
              <button onclick={() => moveInPlaylist(openPlaylist, item, -1)} disabled={i === 0}>↑</button>
              <button onclick={() => moveInPlaylist(openPlaylist, item, 1)} disabled={i === openPlaylistTracks.length - 1}>
                ↓
              </button>
              {#if media[item.id]?.state === 'present'}
                <button onclick={() => play(item, openPlaylist.id)}>Play</button>
              {/if}
              <button class="ghost" onclick={() => removeFromPlaylist(openPlaylist, item)}>Remove</button>
            </div>
          </li>
        {:else}
          <li class="empty dim">No tracks yet — add some from the Library tab.</li>
        {/each}
      </ul>
    {/if}

    {#if playing}
      <section class="player">
        <strong>{playing.title}</strong>
        <input
          type="range"
          min="0"
          max={duration || 0}
          value={at}
          step="0.1"
          oninput={(e) => {
            audio.currentTime = Number(e.currentTarget.value);
            pushPositionState();
          }}
          aria-label="Seek"
        />
        <div class="row">
          <span class="dim">{clock(at)} / {clock(duration)}</span>
          <span>
            <button onclick={() => step(-1)}>‹‹</button>
            <button onclick={() => (paused ? audio.play() : audio.pause())}>
              {paused ? '▶' : '❚❚'}
            </button>
            <button onclick={() => step(1)}>››</button>
          </span>
        </div>
      </section>
    {/if}

    <section class="readiness">
      <h2>Offline readiness</h2>
      <dl>
        <dt>App shell</dt>
        <dd class:bad={!shellCached}>
          {shellCached ? 'cached' : 'NOT CACHED — reload once'} · built {BUILD}
        </dd>
        <dt>Library</dt>
        <dd>
          {items.length} items · {downloaded.length} downloaded · {verified} verified
          {#if items.length - downloaded.length > 0}
            <span class="bad">· {items.length - downloaded.length} missing</span>
          {/if}
        </dd>
        <dt>Storage used</dt>
        <dd>
          {storage ? `${gb(storage.usage)} of ${gb(storage.quota)} available` : '…'}
        </dd>
        <dt>Persistent storage</dt>
        <dd class:bad={persisted === false}>
          {persisted === null ? '…' : persisted ? 'granted' : 'DENIED'}
        </dd>
        <dt>OPFS <code>move()</code></dt>
        <dd>
          {moveSupported === null
            ? 'unknown — download something'
            : moveSupported
              ? 'supported'
              : 'UNSUPPORTED — files kept as .part'}
        </dd>
        <dt>Lock-screen controls</dt>
        <dd class:bad={Object.values(mediaActions).some((v) => !v)}>
          {Object.entries(mediaActions)
            .filter(([, ok]) => !ok)
            .map(([a]) => a)
            .join(', ') || 'all registered'}
        </dd>
        <dt>Last sync</dt>
        <dd>{lastSync ? when(lastSync) : 'never'}</dd>
        <dt>Last checked</dt>
        <dd>{lastVerify ? when(lastVerify) : 'never'}</dd>
        <dt>Network calls</dt>
        <dd>{net.ok} ok / {net.fail} failed</dd>
      </dl>

      <button class="ghost wide" onclick={runSweep} disabled={!!sweep || !downloaded.length}>
        {sweep ? `Checking… ${sweep.checked}/${sweep.total}` : 'Check my library'}
      </button>

      <p class="dim">
        "Check my library" re-reads every downloaded file. Run it before you fly,
        while there is still network to re-download anything that has gone.
      </p>
      <p class="dim">
        Assertion 12: after a cold boot in airplane mode, with no downloads
        started, "ok" must read 0. Counts main-thread fetch only.
      </p>
    </section>
  {/if}
</main>

<style>
  /* ponytail: system font stack, so there is no font to self-host, subset,
     precache or forget to precache. The rule in docs is "no CDN fonts"; owning
     zero font files satisfies it more completely than owning the right ones. */
  :global(body) {
    margin: 0;
    background: #0b0b0c;
    color: #e8e8ea;
    font: 16px/1.5 system-ui, -apple-system, sans-serif;
    padding-top: env(safe-area-inset-top);
  }
  main {
    max-width: 34rem;
    margin: 0 auto;
    padding: 1rem 1rem 8rem;
  }
  h1 {
    font-size: 1.25rem;
    margin: 0 0 1rem;
  }
  h2 {
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #8b8b94;
    margin: 0 0 0.5rem;
  }
  .ver {
    color: #8b8b94;
    font-weight: 400;
  }
  .dim {
    color: #8b8b94;
  }
  .err {
    color: #ff8f8f;
    font-size: 0.85rem;
  }
  .bad {
    color: #ff8f8f;
  }
  .add {
    display: flex;
    gap: 0.5rem;
    margin-bottom: 1rem;
  }
  .add input {
    flex: 1;
    min-width: 0;
    font: inherit;
    padding: 0 0.75rem;
    min-height: 44px;
    border: 1px solid #33333a;
    border-radius: 6px;
    background: #16161a;
    color: inherit;
  }
  .plan {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    padding: 0.75rem;
    margin-bottom: 1rem;
    background: #16161a;
    border: 1px solid #33333a;
    border-radius: 8px;
  }
  .plan .actions {
    margin-top: 0.5rem;
  }
  .playlist-title {
    font: inherit;
    font-weight: 600;
    padding: 0.25rem 0.4rem;
    margin-bottom: 0.15rem;
    border: 1px solid transparent;
    border-radius: 4px;
    background: transparent;
    color: inherit;
  }
  .playlist-title:hover,
  .playlist-title:focus {
    border-color: #33333a;
    background: #0b0b0c;
  }
  .import-list {
    list-style: none;
    padding: 0;
    margin: 0.5rem 0;
    max-height: 40vh;
    overflow-y: auto;
  }
  .import-list li {
    padding: 0.3rem 0;
    border-bottom: 1px solid #232327;
  }
  .import-list label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .import-list .title {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .tabs {
    display: flex;
    gap: 0.4rem;
    margin-bottom: 1rem;
  }
  .tabs button {
    background: #16161a;
    color: #8b8b94;
  }
  .tabs button.active {
    background: #2f6feb;
    color: #fff;
  }
  .add-to-playlist {
    font: inherit;
    min-height: 44px;
    max-width: 8rem;
    background: #232327;
    color: #c8c8d0;
    border: 0;
    border-radius: 6px;
  }
  .library {
    list-style: none;
    padding: 0;
    margin: 0 0 2rem;
  }
  .library li {
    display: flex;
    gap: 0.75rem;
    align-items: center;
    padding: 0.6rem 0;
    border-bottom: 1px solid #232327;
  }
  .library li.empty {
    border: 0;
  }
  .library li.active strong {
    color: #7fd1ff;
  }
  .art {
    width: 48px;
    height: 48px;
    flex: none;
    background: #232327;
    border-radius: 4px;
    overflow: hidden;
  }
  .art img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
  .meta {
    display: flex;
    flex-direction: column;
    min-width: 0;
    flex: 1;
  }
  .meta strong {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .actions {
    display: flex;
    gap: 0.4rem;
    flex: none;
    align-items: center;
  }
  button {
    font: inherit;
    min-height: 44px;
    padding: 0 0.9rem;
    border: 0;
    border-radius: 6px;
    background: #2f6feb;
    color: #fff;
  }
  button:disabled {
    opacity: 0.5;
  }
  button.ghost {
    background: #232327;
    color: #c8c8d0;
  }
  button.wide {
    width: 100%;
    margin: 0.5rem 0;
  }
  .format {
    margin-bottom: 1rem;
  }
  .format summary {
    cursor: pointer;
    min-height: 32px;
  }
  .format .row {
    gap: 1rem;
    justify-content: flex-start;
    flex-wrap: wrap;
    margin: 0.5rem 0;
  }
  .format label {
    display: flex;
    gap: 0.4rem;
    align-items: center;
    color: #8b8b94;
  }
  .format select {
    font: inherit;
    min-height: 36px;
    background: #16161a;
    color: #e8e8ea;
    border: 1px solid #33333a;
    border-radius: 6px;
    padding: 0 0.4rem;
  }
  .format .check input {
    width: 20px;
    height: 20px;
  }
  .player {
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    background: #16161a;
    border-top: 1px solid #232327;
    padding: 0.75rem 1rem calc(0.75rem + env(safe-area-inset-bottom));
  }
  .player input[type='range'] {
    width: 100%;
    margin: 0.5rem 0;
  }
  .row {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  dl {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0.25rem 1rem;
    margin: 0 0 0.5rem;
  }
  dt {
    color: #8b8b94;
  }
  dd {
    margin: 0;
  }
</style>
