<script>
  // Brand marks for the supported integrations. Separate component from
  // Icon.svelte on purpose: those are 1.75px stroked UI glyphs, these are
  // solid marks. Mixing the two inside one wrapper would mean a brand mark
  // rendered as an outline, which stops looking like the brand at all — the
  // icon-style-consistent rule is about one family per hierarchy level, and
  // these sit at a different level from the app's own controls.
  //
  // Monochrome (currentColor), not brand colours: SoundCloud's orange lands
  // almost exactly on this app's --accent, so a coloured tile would read as
  // "selected", and five saturated brands would each need their own contrast
  // check against the light theme. The shapes plus their labels carry the
  // recognition. Hand-authored, self-hosted, no icon dependency, no CDN.
  let { name, size = 24 } = $props();

  const MARKS = {
    // Rounded rect with the play triangle punched out (evenodd), so the
    // knockout shows whatever surface is behind it rather than a fixed colour.
    youtube: {
      shapes: [
        {
          d:
            'M5.5 5h13a4.5 4.5 0 014.5 4.5v5a4.5 4.5 0 01-4.5 4.5h-13A4.5 4.5 0 011 14.5v-5A4.5 4.5 0 015.5 5z' +
            'M10 8.8v6.4l5.4-3.2z',
          evenodd: true,
        },
      ],
    },
    // Equaliser bars beside a cloud — the bars are what tell it apart from
    // Mixcloud's at a glance.
    soundcloud: {
      shapes: [
        { tag: 'rect', x: 1.5, y: 11.5, width: 1.7, height: 5.5, rx: 0.85 },
        { tag: 'rect', x: 4.6, y: 9.8, width: 1.7, height: 7.2, rx: 0.85 },
        { tag: 'rect', x: 7.7, y: 8.2, width: 1.7, height: 8.8, rx: 0.85 },
        // Overlapping solids in one colour union into a single cloud.
        { tag: 'circle', cx: 14.6, cy: 12.4, r: 4.1 },
        { tag: 'circle', cx: 19.1, cy: 13.6, r: 3.4 },
        { tag: 'rect', x: 11.5, y: 13, width: 8, height: 4, rx: 0.6 },
      ],
    },
    // A centred, wider cloud with no bars.
    mixcloud: {
      shapes: [
        { tag: 'circle', cx: 8.6, cy: 12.6, r: 3.6 },
        { tag: 'circle', cx: 13, cy: 10.6, r: 4.6 },
        { tag: 'circle', cx: 17.6, cy: 12.8, r: 3.4 },
        { tag: 'rect', x: 5, y: 12.4, width: 16, height: 4.6, rx: 2.3 },
      ],
    },
    // Rounded square with a bold chevron knocked out.
    vimeo: {
      shapes: [
        {
          d:
            'M7 3h10a4 4 0 014 4v10a4 4 0 01-4 4H7a4 4 0 01-4-4V7a4 4 0 014-4z' +
            'M7.6 8.4h2.6L12 12.6l1.8-4.2h2.6L12 17.2z',
          evenodd: true,
        },
      ],
    },
    // Rounded square with Bandcamp's slanted band knocked out.
    bandcamp: {
      shapes: [
        {
          d:
            'M7 3h10a4 4 0 014 4v10a4 4 0 01-4 4H7a4 4 0 01-4-4V7a4 4 0 014-4z' +
            'M6.6 15.4h6.9l3.9-6.8H10.5z',
          evenodd: true,
        },
      ],
    },
  };

  const mark = $derived(MARKS[name] ?? { shapes: [] });
</script>

<svg
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="currentColor"
  aria-hidden="true"
  focusable="false"
>
  {#each mark.shapes as s (s.d ?? `${s.tag}-${s.cx ?? s.x}-${s.cy ?? s.y}`)}
    {#if s.tag === 'circle'}
      <circle cx={s.cx} cy={s.cy} r={s.r} />
    {:else if s.tag === 'rect'}
      <rect x={s.x} y={s.y} width={s.width} height={s.height} rx={s.rx} />
    {:else}
      <path d={s.d} fill-rule={s.evenodd ? 'evenodd' : undefined} />
    {/if}
  {/each}
</svg>
