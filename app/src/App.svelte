<script>
  import { onMount } from 'svelte';
  import * as db from './db.js';
  import { net } from './net.svelte.js';

  // v0.0 has no server, so the catalogue is a constant. In v0.1 this comes from
  // IndexedDB, populated by /items. What matters for this phase is that boot
  // reads only local state — see the onMount below.
  const CATALOGUE = [
    {
      id: 'chirp-3m',
      title: 'Rising chirp (3 min)',
      artist: 'Test fixture',
      files: [
        { role: 'audio', name: 'audio.m4a', url: '/media/chirp-3m.m4a' },
        { role: 'art', name: 'art-sq.jpg', url: '/media/art-sq.jpg' },
      ],
    },
    {
      id: 'chirp-60m',
      title: 'Rising chirp (60 min)',
      artist: 'Test fixture',
      files: [
        { role: 'audio', name: 'audio.m4a', url: '/media/chirp-60m.m4a' },
        { role: 'art', name: 'art-sq.jpg', url: '/media/art-sq.jpg' },
      ],
    },
  ];

  let media = $state({}); // item_id -> local_media row
  let urls = $state({}); // item_id -> { audio, art } object URLs
  let progress = $state({}); // item_id -> 0..1 while downloading
  let errors = $state({});
  let persisted = $state(null);
  let moveSupported = $state(null);
  let playingId = $state(null);
  let paused = $state(true);
  let at = $state(0);
  let duration = $state(0);
  let booted = $state(false);

  let audio; // ONE element for the app's lifetime. FM-5: a fresh element per
  // track loses the iOS gesture unlock and playback dies once backgrounded.
  let worker;

  const playing = $derived(CATALOGUE.find((c) => c.id === playingId) ?? null);

  onMount(async () => {
    // The boot path. Local reads only — no fetch, no await on anything that
    // could hang. FM-2 is the most commonly missed failure mode and it is
    // missed exactly here.
    const rows = await db.all();
    for (const row of rows) media[row.item_id] = row;
    booted = true;

    navigator.storage.persisted().then((v) => (persisted = v));

    // Object URLs are built for every downloaded item up front, not on click.
    // Reading OPFS is async, and awaiting it inside a click handler breaks the
    // iOS user-gesture chain, so play() would be rejected. FM-5 calls this
    // prefetching; with two items it is just "all of them".
    for (const row of rows) resolveUrls(row);

    worker = new Worker(new URL('./opfs-worker.js', import.meta.url), { type: 'module' });
    worker.onmessage = onWorkerMessage;

    setupMediaSession();
  });

  async function resolveUrls(row) {
    try {
      const root = await navigator.storage.getDirectory();
      const dir = await (await root.getDirectoryHandle('media')).getDirectoryHandle(row.item_id);
      const next = {};
      for (const f of row.files) {
        const file = await (await dir.getFileHandle(f.name)).getFile();
        next[f.role] = URL.createObjectURL(file);
      }
      for (const url of Object.values(urls[row.item_id] ?? {})) URL.revokeObjectURL(url);
      urls[row.item_id] = next;
    } catch (err) {
      // FM-6: the row claims the file exists and it does not. v0.2 downgrades
      // the item to state 'missing' with a one-tap re-download; v0.0 just says so.
      errors[row.item_id] = `not on disk: ${err}`;
    }
  }

  function download(item) {
    errors[item.id] = null;
    progress[item.id] = 0;
    worker.postMessage({
      itemId: item.id,
      files: item.files.map(({ name, url }) => ({ name, url: new URL(url, location.origin).href })),
    });
  }

  async function onWorkerMessage({ data }) {
    const item = CATALOGUE.find((c) => c.id === data.itemId);
    if (data.type === 'progress') {
      progress[data.itemId] = data.done / data.total;
      return;
    }
    if (data.type === 'error') {
      progress[data.itemId] = undefined;
      errors[data.itemId] = data.error;
      return;
    }

    progress[data.itemId] = undefined;
    moveSupported = data.files.every((f) => f.moved);

    // FM-4: the row claiming the file exists is the LAST thing written, after
    // every byte is on disk and verified.
    const row = {
      item_id: data.itemId,
      state: 'present',
      files: data.files.map((f, i) => ({ role: item.files[i].role, name: f.name, bytes: f.bytes })),
      downloaded_at: new Date().toISOString(),
    };
    await db.put(row);
    media[data.itemId] = row;

    // FM-6: ask once we actually have something worth keeping, and show the
    // real answer — a silent false is how libraries disappear.
    navigator.storage.persist().then((v) => (persisted = v));
    resolveUrls(row);
  }

  async function forget(item) {
    for (const url of Object.values(urls[item.id] ?? {})) URL.revokeObjectURL(url);
    delete urls[item.id];
    if (playingId === item.id) stop();
    const root = await navigator.storage.getDirectory();
    const dir = await root.getDirectoryHandle('media');
    await dir.removeEntry(item.id, { recursive: true });
    await db.remove(item.id);
    delete media[item.id];
  }

  function play(item) {
    const url = urls[item.id]?.audio;
    if (!url) return;
    // Everything here is synchronous so the iOS gesture still counts.
    if (playingId !== item.id) {
      audio.src = url;
      playingId = item.id;
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
    const downloaded = CATALOGUE.filter((c) => media[c.id]);
    if (!downloaded.length) return;
    const i = downloaded.findIndex((c) => c.id === playingId);
    play(downloaded[(i + delta + downloaded.length) % downloaded.length]);
  }

  function setMetadata(item) {
    if (!('mediaSession' in navigator)) return;
    const art = urls[item.id]?.art;
    navigator.mediaSession.metadata = new MediaMetadata({
      title: item.title,
      artist: item.artist,
      album: 'Library',
      // Must be a local object URL. A remote artwork URL blanks the lock screen
      // offline, which is the whole point of doing this at all.
      artwork: art ? [{ src: art, sizes: '512x512', type: 'image/jpeg' }] : [],
    });
  }

  function setupMediaSession() {
    if (!('mediaSession' in navigator)) return;
    const ms = navigator.mediaSession;
    ms.setActionHandler('play', () => audio.play());
    ms.setActionHandler('pause', () => audio.pause());
    ms.setActionHandler('previoustrack', () => step(-1));
    ms.setActionHandler('nexttrack', () => step(1));
    // Without seekto the lock-screen scrubber is decorative.
    ms.setActionHandler('seekto', (d) => {
      if (d.fastSeek && audio.fastSeek) audio.fastSeek(d.seekTime);
      else audio.currentTime = d.seekTime;
      pushPositionState();
    });
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
    // timeupdate fires ~4x/s; the scrubber only needs ~1.
    if (performance.now() - lastPush > 1000) {
      lastPush = performance.now();
      pushPositionState();
    }
  }

  function seek(e) {
    audio.currentTime = Number(e.currentTarget.value);
    pushPositionState();
  }

  const clock = (s) =>
    Number.isFinite(s)
      ? `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`
      : '–:––';
</script>

<!-- The one long-lived audio element. Never recreated. -->
<audio
  bind:this={audio}
  preload="auto"
  playsinline
  onplay={() => (paused = false)}
  onpause={() => (paused = true)}
  ontimeupdate={onTimeUpdate}
  onloadedmetadata={() => {
    duration = audio.duration;
    pushPositionState();
  }}
  onended={() => step(1)}
></audio>

<main>
  <h1>Tarmac <span class="ver">v0.0</span></h1>

  {#if !booted}
    <p class="dim">Reading local catalogue…</p>
  {:else}
    <ul class="library">
      {#each CATALOGUE as item (item.id)}
        {@const row = media[item.id]}
        {@const pct = progress[item.id]}
        <li class:active={playingId === item.id}>
          <div class="art">
            {#if urls[item.id]?.art}
              <img src={urls[item.id].art} alt="" />
            {/if}
          </div>
          <div class="meta">
            <strong>{item.title}</strong>
            <span class="dim">{item.artist}</span>
            {#if errors[item.id]}<span class="err">{errors[item.id]}</span>{/if}
          </div>
          <div class="actions">
            {#if pct !== undefined}
              <span class="dim">{Math.round(pct * 100)}%</span>
            {:else if row}
              <button onclick={() => play(item)}>Play</button>
              <button class="ghost" onclick={() => forget(item)}>Delete</button>
            {:else}
              <button onclick={() => download(item)}>Download</button>
            {/if}
          </div>
        </li>
      {/each}
    </ul>

    {#if playing}
      <section class="player">
        <strong>{playing.title}</strong>
        <input
          type="range"
          min="0"
          max={duration || 0}
          value={at}
          step="0.1"
          oninput={seek}
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

    <!-- The probe results. This is what v0.0 exists to report. -->
    <section class="readiness">
      <h2>Offline readiness</h2>
      <dl>
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
        <dt>Network calls</dt>
        <dd class:bad={net.ok > 0}>{net.ok} ok / {net.fail} failed</dd>
      </dl>
      <p class="dim">
        Assertion 12: after a cold boot in airplane mode, "ok" must read 0.
        Counts main-thread fetch only.
      </p>
    </section>
  {/if}
</main>

<style>
  /* ponytail: system font stack, so there is no font to self-host, subset,
     precache or forget to precache. The rule in docs is "no CDN fonts"; owning
     zero font files satisfies it more completely than owning the right ones.
     Revisit at v0.2 when the design actually asks for a typeface. */
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
    padding: 1rem 1rem 6rem;
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
  .actions {
    display: flex;
    gap: 0.4rem;
    flex: none;
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
  button.ghost {
    background: #232327;
    color: #c8c8d0;
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
