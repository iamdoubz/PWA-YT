// Generates the PWA icons. Deliberately plain — v0.1 is meant to be ugly, and
// a solid square is findable on a home screen, which is all the icon has to do.
//
// This used to generate chirp audio fixtures too, for the v0.0 probe that ran
// without a server. The server exists now and supplies real media, so that half
// was dead weight and went.

import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';

const out = new URL('../public/', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');

const jobs = [
  ['icon-512.png', ['-f', 'lavfi', '-i', 'color=c=0x2f6feb:s=512x512',
    '-frames:v', '1', `${out}icon-512.png`]],
  ['icon-192.png', ['-f', 'lavfi', '-i', 'color=c=0x2f6feb:s=192x192',
    '-frames:v', '1', `${out}icon-192.png`]],
];

for (const [name, args] of jobs) {
  if (existsSync(args[args.length - 1])) {
    console.log(`skip  ${name} (exists)`);
    continue;
  }
  process.stdout.write(`build ${name} … `);
  const r = spawnSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-y', ...args]);
  if (r.status !== 0) {
    console.error(`\nffmpeg failed for ${name}:\n${r.stderr}`);
    process.exit(1);
  }
  console.log('ok');
}
