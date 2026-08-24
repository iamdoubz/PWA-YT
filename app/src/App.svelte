<script>
  import { onMount } from 'svelte';
  import { fly, fade } from 'svelte/transition';
  import { cubicOut, cubicIn } from 'svelte/easing';
  import { startRegistration, startAuthentication } from '@simplewebauthn/browser';
  import { generateKeyBetween, generateNKeysBetween } from 'fractional-indexing';
  import * as db from './db.svelte.js';
  import * as api from './api.js';
  import { API } from './api.js';
  import { net } from './net.svelte.js';
  import * as outbox from './outbox.js';
  import * as sync from './sync.js';
  import { uuid7 } from './id.js';
  import Icon from './Icon.svelte';

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

  // Read from IndexedDB at boot, never fetched to render — FM-2. `null` means
  // no local session at all, which is the only case that blocks the library
  // view; an *expired* session still shows the library, read-only. See FM-2
  // and D-019's note on this device's local catalogue not being namespaced
  // per user (fine for one person/one device, a real gap on a shared one).
  let session = $state(null); // { token, expires_at, user } | null
  let readOnly = $state(false);
  let authView = $state('login'); // 'login' | 'register'
  let authBusy = $state(false);
  let authError = $state(null);
  let inviteCode = $state('');
  let authEmail = $state('');
  let authDisplayName = $state('');
  let magicLinkView = $state(false); // login sub-view: request an email link
  let magicLinkEmail = $state('');
  let magicLinkMessage = $state(null);

  // Account settings — loaded lazily after first paint, same as everything
  // else that needs a network round trip. FM-2.
  let usage = $state(null); // { bytes_used_today, daily_byte_budget, remaining_bytes, active_jobs } | null
  let cookiesInfo = $state(null); // { configured, updated_at } | null
  let health = $state(null); // { version, yt_dlp_version } | null — server /health, shown in the Account footer
  let cookiesText = $state('');
  let cookiesBusy = $state(false);
  let cookiesMessage = $state(null);

  // ---- UI-only state (this redesign pass). None of this touches the
  // sync/outbox/OPFS machinery — it only decides what's on screen. (Theme is
  // the one exception that persists, to localStorage — see setTheme below.)
  let sheet = $state(null); // null | 'add' | 'account'
  let playerExpanded = $state(false); // mini-player vs. full player sheet
  let openMenuItemId = $state(null); // which row's kebab menu is open (item or playlist id)

  // Dark is the default (D-029). A light choice is read back from the
  // `data-theme` attribute an inline script in index.html already set
  // before first paint (from the same localStorage key setTheme writes),
  // so this never fights that pre-paint value or causes its own flash.
  let theme = $state(
    typeof document !== 'undefined' && document.documentElement.dataset.theme === 'light' ? 'light' : 'dark',
  );
  function setTheme(next) {
    theme = next;
    if (next === 'light') document.documentElement.dataset.theme = 'light';
    else delete document.documentElement.dataset.theme;
    const meta = document.getElementById('theme-color-meta');
    if (meta) meta.content = next === 'light' ? '#fafaf9' : '#0b0b0c';
    try {
      localStorage.setItem('pwa-yt-theme', next);
    } catch {
      /* private browsing or storage disabled — theme just won't survive a reload */
    }
  }

  const prefersReducedMotion =
    typeof matchMedia !== 'undefined' && matchMedia('(prefers-reduced-motion: reduce)').matches;
  const sheetEnter = { y: 24, duration: prefersReducedMotion ? 0 : 220, easing: cubicOut };
  const sheetExit = { y: 24, duration: prefersReducedMotion ? 0 : 160, easing: cubicIn };
  const backdropEnter = { duration: prefersReducedMotion ? 0 : 200 };
  const backdropExit = { duration: prefersReducedMotion ? 0 : 150 };

  function closeAddSheet() {
    sheet = null;
    plan = null;
    planError = null;
    playlistImport = null;
  }

  let audio; // ONE element for the app's lifetime. FM-5: a fresh element per
  // track loses the iOS gesture unlock and playback dies once backgrounded.
  let worker;
  let poll = null;
  const inFlight = new Set();

  const playing = $derived(items.find((i) => i.id === playingId) ?? null);
  // "downloaded" means the bytes are here, not that a row once claimed they were.
  const downloaded = $derived(items.filter((i) => media[i.id]?.state === 'present'));
  const verified = $derived(downloaded.filter((i) => media[i.id]?.verified_at).length);
  // One glance at the account avatar badge: persisted, shell cached, nothing
  // known-missing. A rollup for the badge only — the account sheet's own
  // diagnostics list is what tells the full, honest story per D-002/FM-6.
  const readinessOk = $derived(persisted === true && shellCached && items.length - downloaded.length <= 0);

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

    session = await db.getMeta('session');
    if (session) {
      api.setAuthToken(session.token);
      // Expired offline degrades to read-only, it does not log out — FM-2.
      // A later successful request (once back online) clears this the same
      // way any other 401 would, by prompting a fresh sign-in.
      if (session.expires_at < new Date().toISOString()) readOnly = true;
    }
    api.setUnauthorizedHandler(() => {
      readOnly = true;
    });

    booted = true;

    // persist(), not just persisted() — iOS Safari has been observed revoking
    // a prior grant on its own (storage pressure from another app appears
    // sufficient), and the app previously only ever re-asked right after a
    // download. Asking on every boot is the only way to notice and try to
    // re-earn it before the next download, rather than silently staying
    // unprotected until one happens.
    navigator.storage.persist().then((v) => (persisted = v));
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
      reconcile();
      refreshAccountInfo();
    });

    // The other trigger for reconciling — reconnect can happen with the app
    // already open and idle, not just at boot.
    window.addEventListener('online', reconcile);

    // A magic-link click lands here with ?magic_link=<token> on first load.
    // Only present when it's actually a magic-link visit, so this never adds
    // a network call to the ordinary boot path (FM-2).
    const magicToken = new URL(location.href).searchParams.get('magic_link');
    if (magicToken) {
      history.replaceState(null, '', location.pathname);
      queueMicrotask(() => finishMagicLink(magicToken));
    }
  });

  async function refreshStorage() {
    if (!navigator.storage?.estimate) return;
    const { usage, quota } = await navigator.storage.estimate();
    storage = { usage, quota };
  }

  // ------------------------------------------------------------------- sync
  //
  // Pull half of multi-device convergence (sync.js). A remote tombstone for
  // an item only removes IndexedDB rows there — purging the actual OPFS
  // bytes and any in-memory object URLs is a side effect only this component
  // can do, so it's passed in as a callback rather than living in sync.js.

  async function purgeLocalMedia(itemId) {
    for (const url of Object.values(urls[itemId] ?? {})) URL.revokeObjectURL(url);
    delete urls[itemId];
    if (playingId === itemId) stop();
    if (media[itemId]) worker.postMessage({ type: 'purge', itemId });
    await db.remove('local_media', itemId);
    delete media[itemId];
  }

  async function refreshCatalogueFromLocal() {
    const [catalogue, pls, plItems] = await Promise.all([
      db.all('items'),
      db.all('playlists'),
      db.all('playlist_items'),
    ]);
    items = catalogue.sort((a, b) => b.added_at.localeCompare(a.added_at));
    playlists = pls;
    playlistItems = plItems;
  }

  // The one place both halves of reconnect run together: push this device's
  // queued mutations, then pull whatever changed on another device signed
  // into the same account, then reflect both in the reactive state that
  // sync.js and outbox.js — plain modules, no Svelte runes — can't touch
  // directly.
  async function reconcile() {
    await outbox.drainOnce();
    await sync.pullOnce(purgeLocalMedia);
    await refreshCatalogueFromLocal();
  }

  // ------------------------------------------------------------------- auth
  //
  // Usernameless throughout: no username/email field at sign-in, just a
  // passkey prompt. `startRegistration`/`startAuthentication` are
  // @simplewebauthn/browser — they turn the server's JSON options into the
  // ArrayBuffers `navigator.credentials.create()/.get()` need and back again,
  // so nothing here touches base64url by hand. See D-019.

  async function applySession(result) {
    session = { token: result.token, expires_at: result.expires_at, user: result.user };
    await db.setMeta('session', session);
    api.setAuthToken(session.token);
    readOnly = false;
    authError = null;
    startPolling();
    refreshAccountInfo();
    // The boot-time reconcile() at onMount ran before this session existed
    // (or before any session existed, if local storage was wiped and this is
    // a fresh login) and so pulled nothing. Without this, a device that lost
    // its local catalogue re-authenticates successfully and then just sits
    // at "0 items" until something else happens to trigger a sync.
    reconcile();
  }

  async function refreshAccountInfo() {
    if (!session) return;
    // Both best-effort and independent — one failing (e.g. offline) must not
    // hide the other.
    try {
      usage = await api.meUsage();
    } catch {
      /* shown as '…' below; not worth a banner over */
    }
    try {
      cookiesInfo = await api.cookiesStatus();
    } catch {
      /* same */
    }
    try {
      health = await api.health();
    } catch {
      /* same */
    }
  }

  async function saveCookies() {
    if (!cookiesText.trim()) return;
    cookiesBusy = true;
    cookiesMessage = null;
    try {
      await api.putCookies(cookiesText);
      cookiesText = '';
      cookiesInfo = await api.cookiesStatus();
      cookiesMessage = 'Saved.';
    } catch (err) {
      cookiesMessage = err.message;
    } finally {
      cookiesBusy = false;
    }
  }

  async function clearCookies() {
    cookiesBusy = true;
    cookiesMessage = null;
    try {
      await api.deleteCookies();
      cookiesInfo = await api.cookiesStatus();
    } catch (err) {
      cookiesMessage = err.message;
    } finally {
      cookiesBusy = false;
    }
  }

  async function doRegister() {
    authError = null;
    authBusy = true;
    try {
      const begin = await api.registerBegin(inviteCode.trim(), authEmail.trim(), authDisplayName.trim());
      const credential = await startRegistration({ optionsJSON: begin.options });
      const result = await api.registerFinish(begin.ceremony_id, credential);
      await applySession(result);
    } catch (err) {
      authError = err.message;
    } finally {
      authBusy = false;
    }
  }

  async function doLogin() {
    authError = null;
    authBusy = true;
    try {
      const begin = await api.loginBegin();
      const credential = await startAuthentication({ optionsJSON: begin.options });
      const result = await api.loginFinish(begin.ceremony_id, credential);
      await applySession(result);
    } catch (err) {
      authError = err.message;
    } finally {
      authBusy = false;
    }
  }

  async function doMagicLinkRequest() {
    if (!magicLinkEmail.trim()) return;
    authError = null;
    magicLinkMessage = null;
    authBusy = true;
    try {
      const result = await api.magicLinkRequest(magicLinkEmail.trim());
      magicLinkMessage = result.message;
    } catch (err) {
      authError = err.message;
    } finally {
      authBusy = false;
    }
  }

  // Runs unprompted at boot when the URL carries ?magic_link=<token> (a click
  // from the emailed link) — not part of the normal sign-in button flow.
  async function finishMagicLink(token) {
    authBusy = true;
    try {
      const result = await api.magicLinkVerify(token);
      await applySession(result);
    } catch (err) {
      authError = err.message;
    } finally {
      authBusy = false;
    }
  }

  async function doLogout() {
    await api.logout();
    // Clears only the session — items, local_media, artwork, and OPFS are
    // untouched. Logging out must never look like data loss. See FM-2.
    await db.remove('meta', 'session');
    session = null;
    readOnly = false;
    api.setAuthToken(null);
    stopPolling();
    sheet = null;
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
      sheet = null;
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
      sheet = null;
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
    // Not the full reconcile() here — this fires up to once a second while
    // any job is active, and a /sync round trip that often is chatter for no
    // benefit. drainOnce() alone is cheap (no network call at all when the
    // outbox is empty, the common case) and boot + reconnect already cover
    // pulling in whatever changed on another device.
    outbox.drainOnce();

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
      // The worker is a separate context with no access to api.js's
      // module-scoped authToken — get_artifact requires a session on top of
      // the signed per-file token, so it has to be passed in explicitly.
      token: session?.token,
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

  // Matched against whatever precedes a possible ".part" suffix, since a
  // device where OPFS move() is unsupported never drops it — see resolveUrls.
  const mimeFor = (name) => (name.includes('.mp3') ? 'audio/mpeg' : 'audio/mp4');

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
          const file = await (await dir.getFileHandle(f.name)).getFile();
          // The File's own .type is whatever the platform infers from the
          // filename — and when OPFS move() is unsupported (confirmed on at
          // least one real iOS Safari), the file stays under its .part name
          // forever, so that inference comes back empty. Safari's <audio>,
          // unlike Chrome, trusts the blob's declared type rather than
          // sniffing content, and a blob URL with no type reliably fails as
          // MEDIA_ERR_SRC_NOT_SUPPORTED. Re-wrapping with an explicit type
          // does not copy the bytes — Blob-from-Blob is a lazy reference.
          const blob = f.role === 'audio' ? new Blob([file], { type: mimeFor(f.name) }) : file;
          next[f.role] = URL.createObjectURL(blob);
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
    if (jobs[item.id] && ACTIVE.includes(jobs[item.id].state)) return;
    errors[item.id] = null;
    // Optimistic: server-side jobs are never deduped against an already-active
    // one for this item (see D-028), so nothing but this stops a second click
    // landing before the real "queued" job comes back over the stream and
    // creating its own duplicate download.
    jobs[item.id] = { state: 'queued' };
    try {
      await api.createItem(item.source_key, $state.snapshot(profile));
      startPolling();
    } catch (err) {
      delete jobs[item.id];
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

    // Cascades locally too, mirroring what the server does in the same
    // transaction when this replays — otherwise the local playlist_items row
    // just sits there forever with nothing to ever clean it up. See D-018.
    const stamp = new Date().toISOString();
    const next = [];
    for (const pi of playlistItems) {
      if (pi.item_id === item.id && !pi.deleted_at) {
        const row = { ...pi, deleted_at: stamp, updated_at: stamp };
        await db.put('playlist_items', row);
        next.push(row);
      } else {
        next.push(pi);
      }
    }
    playlistItems = next;

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
    if (!url) {
      errors[item.id] = 'No local copy to play — try "Check my library" or re-download.';
      return;
    }
    errors[item.id] = null;
    // Everything here is synchronous so the iOS gesture still counts. The
    // .catch below is fire-and-forget on purpose — attaching it doesn't
    // un-synchronise the play() call itself, it just stops a rejection
    // (autoplay policy, an unsupported source) from vanishing as a silent,
    // unhandled promise rejection with nothing shown on screen.
    if (playingId !== item.id) {
      audio.src = url;
      playingId = item.id;
      queuePlaylistId = playlistId;
      setMetadata(item);
    }
    audio.play().catch((err) => {
      errors[item.id] = `Playback failed: ${err.name} — ${err.message}`;
    });
  }

  function stop() {
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
    playingId = null;
    playerExpanded = false;
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
  onerror={() => {
    // Some failures (an unsupported codec/container, in particular) surface
    // here rather than as a play() rejection — play() can have already
    // resolved by the time decoding actually fails.
    const codes = { 1: 'ABORTED', 2: 'NETWORK', 3: 'DECODE', 4: 'SRC_NOT_SUPPORTED' };
    if (playingId && audio.error) {
      errors[playingId] = `Playback failed: ${codes[audio.error.code] ?? audio.error.code}`;
    }
  }}
></audio>

<!-- svelte:window must live at the template's top level, unconditionally, so
     it can't sit inside the signed-in {#if} branch below. -->
<svelte:window
  onclick={(e) => {
    if (!e.target.closest('.item-menu')) openMenuItemId = null;
  }}
  onkeydown={(e) => {
    if (e.key !== 'Escape') return;
    if (openMenuItemId) openMenuItemId = null;
    else if (playerExpanded) playerExpanded = false;
    else if (sheet) sheet = null;
  }}
/>

{#if !booted}
  <div class="boot-screen">
    <span class="spinner lg"></span>
    <p class="dim">Reading local catalogue…</p>
  </div>
{:else if !session}
  <!-- Usernameless: no username/email field here at all. The authenticator's
       own passkey picker is the entire sign-in UI. -->
  <div class="auth-screen">
    <div class="auth-card">
      <div class="brand">
        <span class="brand-mark"><Icon name="library" size={22} /></span>
        <span class="brand-name">PWA-YT</span>
      </div>
      {#if authError}<p class="row-err center">{authError}</p>{/if}
      {#if authView === 'register'}
        <h2>Create your account</h2>
        <input placeholder="Invite code" bind:value={inviteCode} aria-label="Invite code" />
        <input type="email" placeholder="Email" bind:value={authEmail} aria-label="Email" />
        <input
          placeholder="Display name (optional)"
          bind:value={authDisplayName}
          aria-label="Display name"
        />
        <button class="btn accent wide" onclick={doRegister} disabled={authBusy || !inviteCode.trim() || !authEmail.trim()}>
          {authBusy ? 'Creating…' : 'Create account with a passkey'}
        </button>
        <button class="link-btn" onclick={() => (authView = 'login')}>
          Already have an account? Sign in
        </button>
      {:else if magicLinkView}
        <h2>Email me a sign-in link</h2>
        {#if magicLinkMessage}
          <p class="dim">{magicLinkMessage}</p>
        {:else}
          <input type="email" placeholder="Email" bind:value={magicLinkEmail} aria-label="Email" />
          <button class="btn accent wide" onclick={doMagicLinkRequest} disabled={authBusy || !magicLinkEmail.trim()}>
            {authBusy ? 'Sending…' : 'Send sign-in link'}
          </button>
        {/if}
        <button
          class="link-btn"
          onclick={() => {
            magicLinkView = false;
            magicLinkMessage = null;
          }}
        >
          Back to passkey sign-in
        </button>
      {:else}
        <h2>Sign in</h2>
        <button class="btn accent wide" onclick={doLogin} disabled={authBusy}>
          {authBusy ? 'Signing in…' : 'Sign in with a passkey'}
        </button>
        <button class="link-btn" onclick={() => (authView = 'register')}>
          Have an invite code? Register
        </button>
        <button class="link-btn" onclick={() => (magicLinkView = true)}>
          Lost your passkey? Email me a sign-in link
        </button>
      {/if}
    </div>
  </div>
{:else}
  <div class="app-shell">
    <header class="topbar">
      <span class="wordmark">
        <Icon name="library" size={18} />
        PWA-YT
      </span>
      <button
        class="avatar-btn"
        onclick={() => (sheet = sheet === 'account' ? null : 'account')}
        aria-label={`Account. Offline readiness ${readinessOk ? 'ready' : 'needs attention'}.`}
      >
        <Icon name="account" size={20} />
        <span class="status-badge" class:ok={readinessOk} class:warn={!readinessOk}></span>
      </button>
    </header>

    <main class="content">
      {#if readOnly}
        <div class="banner warn">
          <Icon name="alert-triangle" size={18} />
          <span>
            Session expired — sign in again once you're back online. Your library
            is still here either way; nothing local was touched.
          </span>
        </div>
      {/if}

      {#if view === 'library'}
        <ul class="track-list">
          {#each items as item (item.id)}
            {@const job = jobs[item.id]}
            {@const pct = progress[item.id]}
            <li class="track" class:active={playingId === item.id}>
              <button
                class="track-tap"
                onclick={() => media[item.id]?.state === 'present' && play(item, null)}
                aria-label={`Play ${item.title}`}
              >
                <span class="art">
                  {#if urls[item.id]?.art}<img src={urls[item.id].art} alt="" />{/if}
                </span>
                <span class="meta">
                  <span class="title">{item.title}</span>
                  <span class="artist">
                    {item.uploader} · {clock(item.duration_s)}
                    {#if media[item.id]?.state === 'missing'} · not downloaded{/if}
                  </span>
                  {#if errors[item.id]}<span class="row-err">{errors[item.id]}</span>{/if}
                </span>
              </button>

              <span class="track-status">
                {#if pct !== undefined}
                  <span class="status-progress" aria-label={`${Math.round(pct * 100)}% downloaded to device`}>
                    {Math.round(pct * 100)}%
                  </span>
                {:else if media[item.id]?.state === 'present'}
                  <span class="status-icon good" title="Downloaded"><Icon name="check-circle" size={20} /></span>
                {:else if job && ACTIVE.includes(job.state)}
                  <span class="spinner" role="status" aria-label={STAGE[job.state]}></span>
                {:else if job?.state === 'failed'}
                  <button class="status-icon danger" onclick={() => retry(item)} aria-label="Retry download">
                    <Icon name="refresh" size={20} />
                  </button>
                {:else}
                  <button class="status-icon warn" onclick={() => redownload(item)} aria-label="Download">
                    <Icon name="alert-triangle" size={20} />
                  </button>
                {/if}

                <span class="item-menu">
                  <button
                    class="icon-btn kebab-btn"
                    aria-label={`More actions for ${item.title}`}
                    onclick={() => (openMenuItemId = openMenuItemId === item.id ? null : item.id)}
                  >
                    <Icon name="more-vertical" size={20} />
                  </button>
                  {#if openMenuItemId === item.id}
                    <div class="menu-pop" role="menu">
                      {#each activePlaylists as p (p.id)}
                        <button
                          role="menuitem"
                          onclick={() => {
                            addToPlaylist(p, item);
                            openMenuItemId = null;
                          }}
                        >
                          <Icon name="plus" size={16} /> Add to {p.name}
                        </button>
                      {:else}
                        <span class="menu-empty dim">No playlists yet</span>
                      {/each}
                      <button
                        role="menuitem"
                        class="danger"
                        onclick={() => {
                          forget(item);
                          openMenuItemId = null;
                        }}
                      >
                        <Icon name="trash" size={16} /> Delete
                      </button>
                    </div>
                  {/if}
                </span>
              </span>
            </li>
          {:else}
            <li class="empty-state">
              <p>Nothing downloaded yet.</p>
              <p class="dim">Tap the + below to add a track from YouTube or SoundCloud.</p>
            </li>
          {/each}
        </ul>
      {:else if !openPlaylist}
        <form
          class="url-form compact"
          onsubmit={(e) => {
            e.preventDefault();
            createPlaylistLocal();
          }}
        >
          <input bind:value={newPlaylistName} placeholder="New playlist name" aria-label="Playlist name" />
          <button type="submit" class="btn accent" disabled={!newPlaylistName.trim()}>Create</button>
        </form>
        <ul class="playlist-grid">
          {#each activePlaylists as p (p.id)}
            <li class="playlist-card">
              {#if editingPlaylistId === p.id}
                <form
                  class="url-form compact"
                  onsubmit={(e) => {
                    e.preventDefault();
                    renamePlaylistLocal(p, editingName);
                    editingPlaylistId = null;
                  }}
                >
                  <input bind:value={editingName} aria-label="Rename playlist" />
                  <button type="submit" class="btn accent">Save</button>
                  <button type="button" class="btn ghost" onclick={() => (editingPlaylistId = null)}>Cancel</button>
                </form>
              {:else}
                <button class="playlist-tap" onclick={() => (openPlaylistId = p.id)}>
                  <span class="playlist-cover"><Icon name="playlists" size={22} /></span>
                  <span class="meta">
                    <strong>{p.name}</strong>
                    <span class="dim">{orderedPlaylistItems(p.id).length} tracks</span>
                  </span>
                </button>
                <span class="item-menu">
                  <button
                    class="icon-btn kebab-btn"
                    aria-label={`More actions for ${p.name}`}
                    onclick={() => (openMenuItemId = openMenuItemId === p.id ? null : p.id)}
                  >
                    <Icon name="more-vertical" size={20} />
                  </button>
                  {#if openMenuItemId === p.id}
                    <div class="menu-pop" role="menu">
                      <button
                        role="menuitem"
                        onclick={() => {
                          editingPlaylistId = p.id;
                          editingName = p.name;
                          openMenuItemId = null;
                        }}
                      >
                        <Icon name="pencil" size={16} /> Rename
                      </button>
                      <button
                        role="menuitem"
                        class="danger"
                        onclick={() => {
                          deletePlaylistLocal(p);
                          openMenuItemId = null;
                        }}
                      >
                        <Icon name="trash" size={16} /> Delete
                      </button>
                    </div>
                  {/if}
                </span>
              {/if}
            </li>
          {:else}
            <li class="empty-state">
              <p>No playlists yet.</p>
              <p class="dim">Create one above, or import a whole playlist by pasting its URL.</p>
            </li>
          {/each}
        </ul>
      {:else}
        <div class="detail-header">
          <button class="icon-btn" onclick={() => (openPlaylistId = null)} aria-label="Back to playlists">
            <Icon name="chevron-left" />
          </button>
          <strong class="detail-title">{openPlaylist.name}</strong>
          <button class="icon-btn danger" onclick={() => deletePlaylistLocal(openPlaylist)} aria-label="Delete playlist">
            <Icon name="trash" size={20} />
          </button>
        </div>
        <ul class="track-list">
          {#each openPlaylistTracks as item, i (item.id)}
            <li class="track" class:active={playingId === item.id}>
              <button
                class="track-tap"
                onclick={() => media[item.id]?.state === 'present' && play(item, openPlaylist.id)}
                aria-label={`Play ${item.title}`}
              >
                <span class="art">
                  {#if urls[item.id]?.art}<img src={urls[item.id].art} alt="" />{/if}
                </span>
                <span class="meta">
                  <span class="title">{item.title}</span>
                  <span class="artist">
                    {item.uploader} · {clock(item.duration_s)}
                    {#if media[item.id]?.state !== 'present'} · not downloaded{/if}
                  </span>
                </span>
              </button>
              <span class="track-status">
                <button class="icon-btn sm" onclick={() => moveInPlaylist(openPlaylist, item, -1)} disabled={i === 0} aria-label="Move up">
                  <Icon name="chevron-down" size={18} rotate={180} />
                </button>
                <button
                  class="icon-btn sm"
                  onclick={() => moveInPlaylist(openPlaylist, item, 1)}
                  disabled={i === openPlaylistTracks.length - 1}
                  aria-label="Move down"
                >
                  <Icon name="chevron-down" size={18} />
                </button>
                {#if media[item.id]?.state === 'present'}
                  <span class="status-icon good"><Icon name="check-circle" size={18} /></span>
                {/if}
                <button class="icon-btn danger" onclick={() => removeFromPlaylist(openPlaylist, item)} aria-label="Remove from playlist">
                  <Icon name="close" size={18} />
                </button>
              </span>
            </li>
          {:else}
            <li class="empty-state">
              <p>No tracks yet.</p>
              <p class="dim">Add some from the Library tab.</p>
            </li>
          {/each}
        </ul>
      {/if}
    </main>

    <div class="dock">
      {#if playing && !playerExpanded}
        <div class="miniplayer" style={`--pct: ${duration ? (at / duration) * 100 : 0}%`}>
          <button class="miniplayer-tap" onclick={() => (playerExpanded = true)} aria-label="Expand player">
            <span class="art small">
              {#if urls[playing.id]?.art}<img src={urls[playing.id].art} alt="" />{/if}
            </span>
            <span class="meta">
              <span class="title">{playing.title}</span>
              <span class="artist">{playing.uploader}</span>
            </span>
          </button>
          <button
            class="icon-btn accent"
            onclick={() => (paused ? audio.play() : audio.pause())}
            aria-label={paused ? 'Play' : 'Pause'}
          >
            <Icon name={paused ? 'play' : 'pause'} size={20} />
          </button>
        </div>
      {/if}

      <nav class="bottomnav" aria-label="Primary">
        <button class="nav-item" class:active={view === 'library'} onclick={() => (view = 'library')}>
          <Icon name="library" size={22} />
          <span>Library</span>
        </button>
        <button class="nav-fab" onclick={() => (sheet = sheet === 'add' ? null : 'add')} aria-label="Add by URL">
          <Icon name="plus" size={26} />
        </button>
        <button class="nav-item" class:active={view === 'playlists'} onclick={() => (view = 'playlists')}>
          <Icon name="playlists" size={22} />
          <span>Playlists{activePlaylists.length ? ` (${activePlaylists.length})` : ''}</span>
        </button>
      </nav>
    </div>
  </div>

  {#if sheet === 'add'}
    <button class="sheet-backdrop" onclick={closeAddSheet} aria-label="Close" in:fade={backdropEnter} out:fade={backdropExit}></button>
    <div class="sheet" role="dialog" aria-label="Add music" in:fly={sheetEnter} out:fly={sheetExit}>
      <div class="sheet-handle-row">
        <span class="sheet-kicker">Add</span>
        <button class="icon-btn" onclick={closeAddSheet} aria-label="Close"><Icon name="close" /></button>
      </div>
      <div class="sheet-body">
        <form
          class="url-form"
          onsubmit={(e) => {
            e.preventDefault();
            doResolve();
          }}
        >
          <span class="url-input-wrap">
            <Icon name="link" size={18} />
            <input type="url" bind:value={urlInput} placeholder="Paste a YouTube or SoundCloud link" aria-label="Media URL" />
          </span>
          <button type="submit" class="btn accent" disabled={resolving || !urlInput.trim()}>
            {resolving ? 'Looking…' : 'Look up'}
          </button>
        </form>

        <div class="format-chips">
          <span class="chip-label dim">Format</span>
          <div class="chip-group">
            <button type="button" class="chip" class:selected={profile.audio_codec === 'aac'} onclick={() => (profile.audio_codec = 'aac')}>
              AAC
            </button>
            <button type="button" class="chip" class:selected={profile.audio_codec === 'mp3'} onclick={() => (profile.audio_codec = 'mp3')}>
              MP3
            </button>
          </div>
          <div class="chip-group">
            {#each [128, 192, 256] as br (br)}
              <button
                type="button"
                class="chip"
                class:selected={profile.audio_bitrate === br}
                onclick={() => (profile.audio_bitrate = br)}
              >
                {br} kbps
              </button>
            {/each}
          </div>
          <label class="chip-check">
            <input type="checkbox" bind:checked={profile.save_artwork} /> Artwork
          </label>
          <p class="dim tiny">
            Stored per item. Re-adding a track at a different setting re-pulls it at that setting.
          </p>
        </div>

        {#if planError}<p class="row-err">{planError}</p>{/if}

        {#if plan}
          <div class="preview-card">
            <strong>{plan.title}</strong>
            <span class="dim">{plan.uploader} · {clock(plan.duration_s)}</span>
            <span class="dim">~{mb(plan.estimated_bytes)} (estimate)</span>
            {#if plan.already_in_library}
              <span class="dim">Already in your library — this will re-download it.</span>
            {/if}
            <div class="sheet-actions">
              <button class="btn accent" onclick={confirmAdd}>Download</button>
              <button class="btn ghost" onclick={() => (plan = null)}>Cancel</button>
            </div>
          </div>
        {/if}

        {#if playlistImport}
          <div class="preview-card">
            <input class="playlist-title-input" bind:value={playlistImport.title} aria-label="Playlist name" />
            <span class="dim">
              {playlistImport.entries.length}{playlistImport.done ? '' : ` of ${playlistImport.entryCount}`} tracks found{playlistImport.done
                ? ''
                : ' · resolving…'}
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
            <div class="sheet-actions">
              <button class="btn accent" onclick={confirmPlaylistImport} disabled={!importSelected.length}>
                Import {importSelected.length} track{importSelected.length === 1 ? '' : 's'}
              </button>
              <button class="btn ghost" onclick={() => (playlistImport = null)}>Cancel</button>
            </div>
          </div>
        {/if}
      </div>
    </div>
  {/if}

  {#if sheet === 'account'}
    <button class="sheet-backdrop" onclick={() => (sheet = null)} aria-label="Close" in:fade={backdropEnter} out:fade={backdropExit}></button>
    <div class="sheet tall" role="dialog" aria-label="Account" in:fly={sheetEnter} out:fly={sheetExit}>
      <div class="sheet-handle-row">
        <span class="sheet-kicker">Account</span>
        <button class="icon-btn" onclick={() => (sheet = null)} aria-label="Close"><Icon name="close" /></button>
      </div>
      <div class="sheet-body">
        <div class="identity-row">
          <span class="avatar-lg"><Icon name="account" size={22} /></span>
          <span class="meta">
            <strong>{session.user.display_name || session.user.email}</strong>
            <span class="dim">{session.user.email}</span>
          </span>
        </div>
        <button class="btn danger-outline wide" onclick={doLogout}>Sign out</button>

        <h3 class="section-title">Appearance</h3>
        <div class="chip-group">
          <button type="button" class="chip" class:selected={theme === 'dark'} onclick={() => setTheme('dark')}>
            Dark
          </button>
          <button type="button" class="chip" class:selected={theme === 'light'} onclick={() => setTheme('light')}>
            Light
          </button>
        </div>

        <h3 class="section-title">Usage today</h3>
        {#if usage}
          <div class="meter">
            <div
              class="meter-fill"
              style={`width: ${Math.min(100, (usage.bytes_used_today / Math.max(usage.daily_byte_budget, 1)) * 100)}%`}
            ></div>
          </div>
          <p class="dim tiny">
            {gb(usage.bytes_used_today)} of {gb(usage.daily_byte_budget)} ·
            {usage.active_jobs} active job{usage.active_jobs === 1 ? '' : 's'} ·
            {gb(usage.remaining_bytes)} left
          </p>
        {:else}
          <p class="dim tiny">Loading…</p>
        {/if}

        <h3 class="section-title">Cookies</h3>
        <p class="dim tiny">
          For private or age-restricted content
          {#if cookiesInfo?.configured}
            — configured {when(cookiesInfo.updated_at)}
          {:else if cookiesInfo}
            — not configured
          {/if}
          . Exported from a browser extension, Netscape format. Stored encrypted; never shown back once saved.
        </p>
        <textarea rows="3" placeholder="Paste a cookies.txt export here" bind:value={cookiesText} aria-label="Cookies"></textarea>
        <div class="sheet-actions">
          <button class="btn" onclick={saveCookies} disabled={cookiesBusy || !cookiesText.trim()}>
            {cookiesBusy ? 'Saving…' : 'Save cookies'}
          </button>
          {#if cookiesInfo?.configured}
            <button class="btn ghost" onclick={clearCookies} disabled={cookiesBusy}>Clear</button>
          {/if}
        </div>
        {#if cookiesMessage}<p class="dim tiny">{cookiesMessage}</p>{/if}

        <h3 class="section-title">Offline readiness</h3>
        <dl class="diag">
          <dt>App shell</dt>
          <dd class:bad={!shellCached}>{shellCached ? 'cached' : 'NOT CACHED — reload once'} · built {BUILD}</dd>
          <dt>Library</dt>
          <dd>
            {items.length} items · {downloaded.length} downloaded · {verified} verified
            {#if items.length - downloaded.length > 0}
              <span class="bad">· {items.length - downloaded.length} missing</span>
            {/if}
          </dd>
          <dt>Storage used</dt>
          <dd>{storage ? `${gb(storage.usage)} of ${gb(storage.quota)} available` : '…'}</dd>
          <dt>Persistent storage</dt>
          <dd class:bad={persisted === false}>{persisted === null ? '…' : persisted ? 'granted' : 'DENIED'}</dd>
          <dt>OPFS <code>move()</code></dt>
          <dd>
            {moveSupported === null ? 'unknown — download something' : moveSupported ? 'supported' : 'UNSUPPORTED — files kept as .part'}
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

        <button class="btn ghost wide" onclick={runSweep} disabled={!!sweep || !downloaded.length}>
          {sweep ? `Checking… ${sweep.checked}/${sweep.total}` : 'Check my library'}
        </button>
        <p class="dim tiny">
          "Check my library" re-reads every downloaded file. Run it before you fly, while there is still
          network to re-download anything that has gone.
        </p>
        <p class="dim tiny">
          Assertion 12: after a cold boot in airplane mode, with no downloads started, "ok" must read 0.
          Counts main-thread fetch only.
        </p>

        <p class="dim tiny" style="margin-top: 16px;">
          PWA-YT {health?.version ?? '…'}
          · <a href="https://github.com/iamdoubz/PWA-YT" target="_blank" rel="noopener">source</a>
          · yt-dlp {health?.yt_dlp_version ?? '…'}
        </p>
      </div>
    </div>
  {/if}

  {#if playerExpanded && playing}
    <button class="sheet-backdrop" onclick={() => (playerExpanded = false)} aria-label="Close" in:fade={backdropEnter} out:fade={backdropExit}></button>
    <div class="sheet player-sheet" role="dialog" aria-label="Now playing" in:fly={sheetEnter} out:fly={sheetExit}>
      <div class="sheet-handle-row">
        <button class="icon-btn" onclick={() => (playerExpanded = false)} aria-label="Collapse player">
          <Icon name="chevron-down" />
        </button>
        <span class="sheet-kicker">
          {(queuePlaylistId && activePlaylists.find((p) => p.id === queuePlaylistId)?.name) || 'Library'}
        </span>
        <span class="spacer-44"></span>
      </div>
      <div class="player-art">
        {#if urls[playing.id]?.art}
          <img src={urls[playing.id].art} alt="" />
        {:else}
          <span class="art-fallback"><Icon name="library" size={48} /></span>
        {/if}
      </div>
      <div class="player-meta">
        <strong class="player-title">{playing.title}</strong>
        <span class="player-artist dim">{playing.uploader}</span>
      </div>
      <input
        type="range"
        class="scrubber"
        min="0"
        max={duration || 0}
        value={at}
        step="0.1"
        style={`--pct: ${duration ? (at / duration) * 100 : 0}%`}
        oninput={(e) => {
          audio.currentTime = Number(e.currentTarget.value);
          pushPositionState();
        }}
        aria-label="Seek"
      />
      <div class="player-times dim tiny">
        <span>{clock(at)}</span>
        <span>{clock(duration)}</span>
      </div>
      <div class="player-transport">
        <button class="icon-btn lg" onclick={() => step(-1)} aria-label="Previous track">
          <Icon name="skip-back" size={26} />
        </button>
        <button class="icon-btn accent xl" onclick={() => (paused ? audio.play() : audio.pause())} aria-label={paused ? 'Play' : 'Pause'}>
          <Icon name={paused ? 'play' : 'pause'} size={30} />
        </button>
        <button class="icon-btn lg" onclick={() => step(1)} aria-label="Next track">
          <Icon name="skip-forward" size={26} />
        </button>
      </div>
      {#if errors[playing.id]}<p class="row-err center">{errors[playing.id]}</p>{/if}
    </div>
  {/if}
{/if}

<style>
  /* ---- tokens ------------------------------------------------------------
     ponytail: system font stack throughout, so there is no font to
     self-host, subset, precache or forget to precache — the visual quality
     here comes from color/spacing/radius/motion instead. See D-011.

     Dark is the default (bare :root, no attribute needed) and light is an
     explicit opt-in via [data-theme='light'] on <html> — set synchronously
     by an inline script in index.html before first paint (reading the same
     localStorage key setTheme() below writes to), so there's no flash of
     the wrong theme. Light isn't dark-inverted: accent/good/warn/danger are
     all deepened shades of the same hues, not the bright dark-mode values,
     because the bright versions fail 4.5:1 text contrast on a light
     background (verified by hand — see D-029 in 08-decisions.md). */
  :global(:root) {
    --bg: #0b0b0d;
    --surface: #17171b;
    --surface-2: #1f1f25;
    --border: #2a2a31;
    --text: #f2f2f4;
    --text-dim: #a1a1aa;
    --text-faint: #6e6e78;
    --accent: #ff6b3d;
    --accent-ink: #180d06;
    --accent-soft: rgba(255, 107, 61, 0.14);
    --accent-glow: rgba(255, 107, 61, 0.35);
    --good: #35d0a0;
    --warn: #f4b740;
    --danger: #fb5b6e;
    --danger-ink: #1a0508;
    --glass-bg: rgba(11, 11, 13, 0.85);
    --warn-soft-bg: rgba(244, 183, 64, 0.12);
    --warn-soft-text: #f4c869;
    --warn-soft-border: rgba(244, 183, 64, 0.35);
    --radius-sm: 8px;
    --radius-md: 12px;
    --radius-lg: 16px;
    --topbar-h: 52px;
    --nav-h: 60px;
    --miniplayer-h: 64px;
  }
  :global([data-theme='light']) {
    --bg: #fafaf9;
    --surface: #ffffff;
    --surface-2: #f1f0ee;
    --border: #e4e2df;
    --text: #17171b;
    --text-dim: #6b6b76;
    --text-faint: #9a9aa5;
    --accent: #c2410c;
    --accent-ink: #fff8f5;
    --accent-soft: rgba(194, 65, 12, 0.12);
    --accent-glow: rgba(194, 65, 12, 0.35);
    --good: #0f766e;
    --warn: #b45309;
    --danger: #dc2626;
    --danger-ink: #fff8f5;
    --glass-bg: rgba(250, 250, 249, 0.85);
    --warn-soft-bg: rgba(180, 83, 9, 0.1);
    --warn-soft-text: #92400e;
    --warn-soft-border: rgba(180, 83, 9, 0.35);
  }

  :global(body) {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  }

  @media (prefers-reduced-motion: reduce) {
    :global(*) {
      transition-duration: 0.01ms !important;
      animation-duration: 0.01ms !important;
    }
  }

  .dim {
    color: var(--text-dim);
  }
  .tiny {
    font-size: 12px;
  }
  .center {
    text-align: center;
  }
  .row-err {
    color: var(--danger);
    font-size: 13px;
  }
  .bad {
    color: var(--danger);
  }
  .spacer-44 {
    width: 44px;
    display: inline-block;
  }

  /* ---- boot / auth --------------------------------------------------- */
  .boot-screen,
  .auth-screen {
    min-height: 100dvh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 24px;
    box-sizing: border-box;
    gap: 12px;
  }
  .auth-card {
    width: 100%;
    max-width: 22rem;
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 28px 22px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
    color: var(--accent);
    font-weight: 700;
    font-size: 18px;
  }
  .brand-mark {
    display: inline-flex;
  }
  .auth-card h2 {
    margin: 0 0 2px;
    font-size: 20px;
  }
  .auth-card input {
    font: inherit;
    min-height: 44px;
    padding: 0 14px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--bg);
    color: inherit;
  }
  .link-btn {
    background: none;
    border: 0;
    color: var(--text-dim);
    font: inherit;
    font-size: 13px;
    min-height: 36px;
    text-align: left;
    text-decoration: underline;
    text-underline-offset: 2px;
  }

  /* ---- app shell -------------------------------------------------------- */
  .topbar {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    height: calc(var(--topbar-h) + env(safe-area-inset-top));
    padding: env(safe-area-inset-top) 16px 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: var(--glass-bg);
    backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--border);
    z-index: 20;
    box-sizing: border-box;
  }
  .wordmark {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-weight: 700;
    font-size: 15px;
    color: var(--text);
  }
  .avatar-btn {
    position: relative;
    width: 40px;
    height: 40px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: var(--surface);
    border: 0;
    border-radius: 999px;
    color: var(--text);
  }
  .status-badge {
    position: absolute;
    top: 4px;
    right: 4px;
    width: 9px;
    height: 9px;
    border-radius: 50%;
    border: 2px solid var(--bg);
  }
  .status-badge.ok {
    background: var(--good);
  }
  .status-badge.warn {
    background: var(--warn);
  }

  .content {
    max-width: 34rem;
    margin: 0 auto;
    padding: calc(var(--topbar-h) + env(safe-area-inset-top) + 16px) 16px
      calc(var(--nav-h) + var(--miniplayer-h) + env(safe-area-inset-bottom) + 24px);
    box-sizing: border-box;
  }

  .banner {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 10px 12px;
    margin-bottom: 16px;
    border-radius: var(--radius-md);
    font-size: 13px;
  }
  .banner.warn {
    background: var(--warn-soft-bg);
    color: var(--warn-soft-text);
    border: 1px solid var(--warn-soft-border);
  }
  .banner :global(svg) {
    flex: none;
    margin-top: 1px;
  }

  /* ---- forms / inputs shared across sheets ------------------------------ */
  .url-form {
    display: flex;
    gap: 8px;
    margin-bottom: 16px;
  }
  .url-form.compact {
    margin-bottom: 16px;
  }
  .url-input-wrap {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 0 12px;
    min-height: 44px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--surface);
    color: var(--text-dim);
  }
  .url-input-wrap input {
    flex: 1;
    min-width: 0;
    border: 0;
    background: none;
    color: var(--text);
    font: inherit;
    min-height: 42px;
  }
  .url-input-wrap input:focus {
    outline: none;
  }
  .url-form input:not(.url-input-wrap input) {
    flex: 1;
    min-width: 0;
    font: inherit;
    padding: 0 14px;
    min-height: 44px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--surface);
    color: inherit;
  }
  textarea {
    width: 100%;
    box-sizing: border-box;
    font: inherit;
    padding: 10px 12px;
    margin: 8px 0;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    background: var(--surface);
    color: inherit;
    resize: vertical;
  }

  /* ---- buttons ------------------------------------------------------- */
  .btn {
    min-height: 44px;
    padding: 0 18px;
    border-radius: 999px;
    border: 0;
    font: inherit;
    font-weight: 600;
    font-size: 15px;
    background: var(--surface-2);
    color: var(--text);
    transition: transform 100ms ease-out;
  }
  .btn.accent {
    background: var(--accent);
    color: var(--accent-ink);
  }
  .btn.ghost {
    background: transparent;
    color: var(--text-dim);
    border: 1px solid var(--border);
  }
  .btn.danger-outline {
    background: transparent;
    color: var(--danger);
    border: 1px solid var(--danger);
  }
  .btn.wide {
    width: 100%;
  }
  .btn:disabled {
    opacity: 0.45;
  }
  .btn:active:not(:disabled) {
    transform: scale(0.97);
  }
  .btn:focus-visible,
  .link-btn:focus-visible,
  input:focus-visible,
  textarea:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  .icon-btn {
    width: 44px;
    height: 44px;
    flex: none;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: transparent;
    border: 0;
    border-radius: 999px;
    color: var(--text);
    transition: background-color 100ms ease-out, transform 100ms ease-out;
  }
  .icon-btn.sm {
    width: 36px;
    height: 36px;
  }
  .icon-btn.lg {
    width: 56px;
    height: 56px;
  }
  .icon-btn.xl {
    width: 72px;
    height: 72px;
  }
  .icon-btn:hover:not(:disabled) {
    background: var(--surface);
  }
  .icon-btn:active:not(:disabled) {
    transform: scale(0.94);
  }
  .icon-btn.accent {
    background: var(--accent);
    color: var(--accent-ink);
  }
  .icon-btn.danger {
    color: var(--danger);
  }
  .icon-btn:disabled {
    opacity: 0.35;
  }
  .icon-btn:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  /* ---- chips ----------------------------------------------------------- */
  .format-chips {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-bottom: 16px;
    padding: 12px;
    background: var(--surface);
    border-radius: var(--radius-md);
  }
  .chip-label {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .chip-group {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .chip {
    min-height: 36px;
    padding: 0 14px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--surface-2);
    color: var(--text-dim);
    font: inherit;
    font-size: 13px;
    font-weight: 600;
  }
  .chip.selected {
    background: var(--accent-soft);
    border-color: var(--accent);
    color: var(--accent);
  }
  .chip-check {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--text-dim);
    font-size: 13px;
  }
  .chip-check input {
    width: 20px;
    height: 20px;
  }

  /* ---- track / playlist lists ------------------------------------------ */
  .track-list,
  .playlist-grid {
    list-style: none;
    padding: 0;
    margin: 0 0 16px;
  }
  .track {
    position: relative;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 0;
    border-bottom: 1px solid var(--border);
  }
  .track-tap {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 12px;
    background: none;
    border: 0;
    padding: 6px 0;
    text-align: left;
    color: inherit;
    border-radius: var(--radius-sm);
    transition: transform 100ms ease-out;
  }
  .track-tap:active {
    transform: scale(0.98);
  }
  .track.active .title {
    color: var(--accent);
  }
  .art,
  .playlist-cover {
    width: 46px;
    height: 46px;
    flex: none;
    background: var(--surface);
    border-radius: var(--radius-sm);
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-faint);
  }
  .art.small {
    width: 38px;
    height: 38px;
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
    gap: 1px;
  }
  .title {
    font-weight: 600;
    font-size: 15px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .artist {
    font-size: 13px;
    color: var(--text-dim);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .track-status {
    display: flex;
    align-items: center;
    gap: 4px;
    flex: none;
  }
  .status-icon {
    width: 36px;
    height: 36px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: 0;
    border-radius: 999px;
  }
  .status-icon.good {
    color: var(--good);
  }
  .status-icon.warn {
    color: var(--warn);
  }
  .status-icon.danger {
    color: var(--danger);
  }
  .status-progress {
    font-size: 12px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    color: var(--accent);
    min-width: 34px;
    text-align: right;
  }
  .spinner {
    width: 18px;
    height: 18px;
    border-radius: 50%;
    border: 2px solid var(--border);
    border-top-color: var(--accent);
    animation: spin 0.8s linear infinite;
    display: inline-block;
  }
  .spinner.lg {
    width: 32px;
    height: 32px;
    border-width: 3px;
  }
  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  .item-menu {
    position: relative;
  }
  .menu-pop {
    position: absolute;
    right: 0;
    top: calc(100% + 4px);
    min-width: 190px;
    max-width: 70vw;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: 4px;
    box-shadow: 0 12px 28px rgba(0, 0, 0, 0.45);
    z-index: 50;
    display: flex;
    flex-direction: column;
  }
  .menu-pop button {
    display: flex;
    align-items: center;
    gap: 8px;
    text-align: left;
    background: none;
    border: 0;
    color: var(--text);
    font: inherit;
    font-size: 14px;
    padding: 10px 12px;
    min-height: 40px;
    border-radius: var(--radius-sm);
  }
  .menu-pop button:hover,
  .menu-pop button:focus-visible {
    background: var(--surface);
  }
  .menu-pop button.danger {
    color: var(--danger);
  }
  .menu-empty {
    padding: 8px 12px;
    font-size: 13px;
  }

  .empty-state {
    padding: 48px 12px;
    text-align: center;
  }
  .empty-state p {
    margin: 4px 0;
  }

  /* ---- playlist grid / detail -------------------------------------- */
  .playlist-card {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 0;
    border-bottom: 1px solid var(--border);
  }
  .playlist-tap {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 12px;
    background: none;
    border: 0;
    padding: 6px 0;
    text-align: left;
    color: inherit;
  }
  .playlist-cover {
    color: var(--accent);
  }
  .detail-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-bottom: 8px;
  }
  .detail-title {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    text-align: center;
    font-size: 16px;
  }

  /* ---- dock: miniplayer + bottom nav -------------------------------- */
  .dock {
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 30;
    display: flex;
    flex-direction: column;
  }
  .miniplayer {
    position: relative;
    display: flex;
    align-items: center;
    gap: 8px;
    height: var(--miniplayer-h);
    padding: 0 8px 0 12px;
    background: var(--surface-2);
    border-top: 1px solid var(--border);
    overflow: hidden;
  }
  .miniplayer::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    height: 2px;
    width: var(--pct, 0%);
    background: var(--accent);
  }
  .miniplayer-tap {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 10px;
    background: none;
    border: 0;
    color: inherit;
    text-align: left;
    padding: 6px 0;
  }
  .bottomnav {
    display: flex;
    align-items: flex-start;
    justify-content: space-around;
    height: var(--nav-h);
    padding: 6px 8px calc(6px + env(safe-area-inset-bottom));
    background: var(--surface-2);
    border-top: 1px solid var(--border);
    box-sizing: content-box;
  }
  .nav-item {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    min-width: 64px;
    min-height: 44px;
    padding: 4px 10px;
    background: none;
    border: 0;
    border-radius: 999px;
    color: var(--text-dim);
    font-size: 11px;
    font-weight: 600;
  }
  .nav-item.active {
    color: var(--accent);
    background: var(--accent-soft);
  }
  .nav-fab {
    width: 52px;
    height: 52px;
    margin-top: -18px;
    flex: none;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: var(--accent);
    color: var(--accent-ink);
    border: 4px solid var(--bg);
    border-radius: 999px;
    box-shadow: 0 4px 14px var(--accent-glow);
    transition: transform 100ms ease-out;
  }
  .nav-fab:active {
    transform: scale(0.94);
  }
  .nav-fab:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 3px;
  }

  /* ---- sheets --------------------------------------------------------- */
  .sheet-backdrop {
    position: fixed;
    inset: 0;
    width: 100%;
    border: 0;
    padding: 0;
    background: rgba(0, 0, 0, 0.55);
    z-index: 90;
  }
  .sheet {
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    max-height: min(88dvh, 720px);
    display: flex;
    flex-direction: column;
    background: var(--surface-2);
    border-top: 1px solid var(--border);
    border-radius: var(--radius-lg) var(--radius-lg) 0 0;
    padding-bottom: env(safe-area-inset-bottom);
    z-index: 100;
    box-sizing: border-box;
  }
  .sheet.tall {
    max-height: min(92dvh, 820px);
  }
  .sheet-handle-row {
    flex: none;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 8px 4px 16px;
  }
  .sheet-kicker {
    font-weight: 700;
    font-size: 15px;
  }
  .sheet-body {
    overflow-y: auto;
    padding: 4px 16px 24px;
  }
  .sheet-actions {
    display: flex;
    gap: 8px;
    margin-top: 8px;
  }
  .section-title {
    margin: 20px 0 6px;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-dim);
  }

  .identity-row {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 12px;
  }
  .avatar-lg {
    width: 44px;
    height: 44px;
    flex: none;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: var(--surface);
    border-radius: 999px;
    color: var(--text);
  }
  .meter {
    height: 8px;
    border-radius: 999px;
    background: var(--surface);
    overflow: hidden;
  }
  .meter-fill {
    height: 100%;
    background: var(--accent);
    border-radius: 999px;
  }
  .diag {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 6px 12px;
    margin: 0 0 8px;
    font-size: 13px;
  }
  .diag dt {
    color: var(--text-dim);
  }
  .diag dd {
    margin: 0;
  }

  /* ---- preview cards (resolve / playlist import) ------------------- */
  .preview-card {
    display: flex;
    flex-direction: column;
    gap: 3px;
    padding: 14px;
    margin-bottom: 16px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
  }
  .playlist-title-input {
    font: inherit;
    font-weight: 700;
    font-size: 15px;
    padding: 4px 6px;
    margin-bottom: 2px;
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    background: transparent;
    color: inherit;
  }
  .playlist-title-input:hover,
  .playlist-title-input:focus {
    border-color: var(--border);
    background: var(--bg);
  }
  .import-list {
    list-style: none;
    padding: 0;
    margin: 8px 0;
    max-height: 40vh;
    overflow-y: auto;
  }
  .import-list li {
    padding: 5px 0;
    border-bottom: 1px solid var(--border);
  }
  .import-list label {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .import-list input[type='checkbox'] {
    width: 20px;
    height: 20px;
    flex: none;
  }
  .import-list .title {
    flex: 1;
    min-width: 0;
    font-weight: 500;
  }

  /* ---- full player sheet -------------------------------------------- */
  .player-sheet {
    align-items: center;
    padding-bottom: calc(24px + env(safe-area-inset-bottom));
  }
  .player-art {
    width: min(72vw, 320px);
    height: min(72vw, 320px);
    margin: 12px 0 20px;
    border-radius: var(--radius-lg);
    overflow: hidden;
    background: var(--surface);
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-faint);
  }
  .player-art img {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  .player-meta {
    width: 100%;
    max-width: 26rem;
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 0 24px;
    box-sizing: border-box;
    text-align: center;
  }
  .player-title {
    font-size: 19px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .player-artist {
    font-size: 14px;
  }
  .scrubber {
    width: 100%;
    max-width: 26rem;
    margin: 20px 0 4px;
    padding: 0 24px;
    box-sizing: border-box;
    appearance: none;
    -webkit-appearance: none;
    height: 6px;
    border-radius: 999px;
    background: linear-gradient(to right, var(--accent) var(--pct, 0%), var(--border) var(--pct, 0%));
  }
  .scrubber::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: var(--text);
    margin-top: 0;
  }
  .scrubber::-moz-range-thumb {
    width: 16px;
    height: 16px;
    border: 0;
    border-radius: 50%;
    background: var(--text);
  }
  .scrubber::-moz-range-track {
    height: 6px;
    border-radius: 999px;
    background: var(--border);
  }
  .scrubber:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 4px;
  }
  .player-times {
    width: 100%;
    max-width: 26rem;
    display: flex;
    justify-content: space-between;
    padding: 0 24px;
    box-sizing: border-box;
    font-variant-numeric: tabular-nums;
  }
  .player-transport {
    display: flex;
    align-items: center;
    gap: 20px;
    margin: 20px 0 8px;
  }
</style>
