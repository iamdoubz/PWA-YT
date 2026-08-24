<script>
  // One shared SVG wrapper so every icon in the app has the same stroke width,
  // viewBox and size by construction — see icon-style-consistent /
  // stroke-consistency in the design brief. Hand-authored path data (no icon
  // library dependency, no CDN): simple geometric UI glyphs aren't the kind
  // of thing that needs one.
  let { name, size = 24, rotate = 0 } = $props();

  const ICONS = {
    play: { shapes: [{ d: 'M7 5v14l12-7z', fill: true }] },
    pause: {
      shapes: [
        { d: 'M6 5h4v14H6z', fill: true },
        { d: 'M14 5h4v14h-4z', fill: true },
      ],
    },
    'skip-back': {
      shapes: [
        { d: 'M12 6l-6 6 6 6' },
        { d: 'M19 6l-6 6 6 6' },
      ],
    },
    'skip-forward': {
      shapes: [
        { d: 'M5 6l6 6-6 6' },
        { d: 'M12 6l6 6-6 6' },
      ],
    },
    plus: { shapes: [{ d: 'M12 5v14M5 12h14' }] },
    library: {
      // two eighth-notes — a music/library glyph
      shapes: [
        { tag: 'circle', cx: 6, cy: 18, r: 2.8 },
        { tag: 'circle', cx: 17, cy: 16, r: 2.8 },
        { d: 'M8.8 18V5.5L19.8 3.5V16' },
      ],
    },
    playlists: { shapes: [{ d: 'M4 6h16M4 12h16M4 18h10' }] },
    // lines of decreasing length, distinct from playlists' equal-width bars
    lyrics: { shapes: [{ d: 'M4 6h16M4 12h12M4 18h8' }] },
    account: {
      shapes: [
        { tag: 'circle', cx: 12, cy: 8, r: 3.6 },
        { d: 'M4.5 20c0-4 3.5-6 7.5-6s7.5 2 7.5 6' },
      ],
    },
    close: { shapes: [{ d: 'M6 6l12 12M18 6L6 18' }] },
    'chevron-down': { shapes: [{ d: 'M6 9l6 6 6-6' }] },
    'chevron-right': { shapes: [{ d: 'M9 6l6 6-6 6' }] },
    'chevron-left': { shapes: [{ d: 'M15 6l-6 6 6 6' }] },
    'more-vertical': {
      shapes: [
        { tag: 'circle', cx: 12, cy: 5, r: 1.6, fill: true },
        { tag: 'circle', cx: 12, cy: 12, r: 1.6, fill: true },
        { tag: 'circle', cx: 12, cy: 19, r: 1.6, fill: true },
      ],
    },
    'check-circle': {
      shapes: [
        { tag: 'circle', cx: 12, cy: 12, r: 8.5 },
        { d: 'M8.2 12.3l2.6 2.6 5-5.4' },
      ],
    },
    download: { shapes: [{ d: 'M12 4v11m0 0l-4-4m4 4l4-4M5 18h14' }] },
    trash: {
      shapes: [
        { d: 'M5 7h14' },
        { d: 'M9.5 7V5.2a1 1 0 011-1h3a1 1 0 011 1V7' },
        { d: 'M7.2 7l1 12.2a1 1 0 001 1h5.6a1 1 0 001-1L16.8 7' },
      ],
    },
    'alert-triangle': {
      shapes: [
        { d: 'M12 3.6L21.5 20H2.5L12 3.6z' },
        { d: 'M12 10v4' },
        { tag: 'circle', cx: 12, cy: 17, r: 0.9, fill: true },
      ],
    },
    refresh: { shapes: [{ d: 'M4.5 12a7.5 7.5 0 0112.8-5.3M19.5 12a7.5 7.5 0 01-12.8 5.3M17.3 4.5v4.2h-4.2M6.7 19.5v-4.2h4.2' }] },
    link: {
      shapes: [
        { tag: 'rect', x: 2.5, y: 9.2, width: 9, height: 5.6, rx: 2.8, transform: 'rotate(-45 7 12)' },
        { tag: 'rect', x: 12.5, y: 9.2, width: 9, height: 5.6, rx: 2.8, transform: 'rotate(-45 17 12)' },
      ],
    },
    pencil: {
      shapes: [
        { d: 'M4 20l0.7-3.6L15.3 5.8l2.9 2.9L7.6 19.3 4 20z' },
        { d: 'M13.6 7.4l2.9 2.9' },
      ],
    },
    'drag-handle': {
      shapes: [
        { tag: 'circle', cx: 9, cy: 6, r: 1.3, fill: true },
        { tag: 'circle', cx: 9, cy: 12, r: 1.3, fill: true },
        { tag: 'circle', cx: 9, cy: 18, r: 1.3, fill: true },
        { tag: 'circle', cx: 15, cy: 6, r: 1.3, fill: true },
        { tag: 'circle', cx: 15, cy: 12, r: 1.3, fill: true },
        { tag: 'circle', cx: 15, cy: 18, r: 1.3, fill: true },
      ],
    },
  };

  const icon = $derived(ICONS[name] ?? { shapes: [] });
</script>

<svg
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="1.75"
  stroke-linecap="round"
  stroke-linejoin="round"
  style={rotate ? `transform: rotate(${rotate}deg)` : undefined}
  aria-hidden="true"
  focusable="false"
>
  {#each icon.shapes as s (s.d ?? `${s.cx}-${s.cy}-${s.x}`)}
    {#if s.tag === 'circle'}
      <circle cx={s.cx} cy={s.cy} r={s.r} fill={s.fill ? 'currentColor' : 'none'} stroke={s.fill ? 'none' : 'currentColor'} />
    {:else if s.tag === 'rect'}
      <rect x={s.x} y={s.y} width={s.width} height={s.height} rx={s.rx} transform={s.transform} fill="none" />
    {:else}
      <path d={s.d} fill={s.fill ? 'currentColor' : 'none'} />
    {/if}
  {/each}
</svg>
