// Generates the local media v0.0 pretends it downloaded. No network, no yt-dlp,
// no server — the storage question does not care where the bytes came from.
//
// The audio is a rising chirp so you can hear roughly where you are in the file.
// That is what makes assertions 4 and 7 (seek to 80%, play to the end without
// truncation) checkable by ear on a phone with no debugger attached.

import { spawnSync } from 'node:child_process';
import { mkdirSync, existsSync } from 'node:fs';

const out = new URL('../public/', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
const media = `${out}media`;
mkdirSync(media, { recursive: true });

const chirp = (seconds) =>
  `aevalsrc=0.3*sin(2*PI*(200+600*t/${seconds})*t):d=${seconds}:s=44100`;

const jobs = [
  ['chirp-3m.m4a', ['-f', 'lavfi', '-i', chirp(180),
    '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', `${media}/chirp-3m.m4a`]],
  ['chirp-60m.m4a', ['-f', 'lavfi', '-i', chirp(3600),
    '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', `${media}/chirp-60m.m4a`]],
  ['art-sq.jpg', ['-f', 'lavfi', '-i', 'testsrc2=s=512x512',
    '-frames:v', '1', '-q:v', '3', `${media}/art-sq.jpg`]],
  ['icon-512.png', ['-f', 'lavfi', '-i', 'color=c=0x2f6feb:s=512x512',
    '-frames:v', '1', `${out}icon-512.png`]],
  ['icon-192.png', ['-f', 'lavfi', '-i', 'color=c=0x2f6feb:s=192x192',
    '-frames:v', '1', `${out}icon-192.png`]],
];

for (const [name, args] of jobs) {
  const dest = args[args.length - 1];
  if (existsSync(dest)) {
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
