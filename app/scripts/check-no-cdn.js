// docs/06-build-plan.md, cross-cutting: "No CDN references in the shell — add a
// build-time check that greps the bundle." A Google Fonts <link> is a white
// screen at 35,000 feet and it is invisible in development.
//
// Run after `npm run build`. Every absolute URL in the built shell is a finding
// until someone justifies it here.

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

const dist = new URL('../dist', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');

// Allowed because these appear only inside thrown error messages — text handed
// to a developer in a console, never passed to fetch, an <img src> or a <link>.
// Verified by reading the surrounding code; re-verify if a dependency changes.
const ALLOW = [
  /^https:\/\/svelte\.dev\/e\//, // svelte runtime error codes
  /^https:\/\/bit\.ly\/wb-precache$/, // workbox precache warning
  /^https?:\/\/www\.w3\.org\//, // xml namespaces
  // @simplewebauthn/browser's console.warn() for the pre-v9 calling
  // convention, which this app doesn't use (always calls with `{ optionsJSON }`).
  /^https:\/\/simplewebauthn\.dev\/docs\//,
  // Account sheet footer: a plain <a href> the user clicks, not a resource the
  // shell fetches — never touched by the offline boot or playback path.
  /^https:\/\/github\.com\/iamdoubz\/PWA-YT$/,
];

const walk = (dir) =>
  readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });

let findings = 0;
for (const file of walk(dist)) {
  if (!/\.(js|css|html|webmanifest|json)$/.test(file)) continue;
  const text = readFileSync(file, 'utf8');
  for (const [url] of text.matchAll(/https?:\/\/[^\s"'`)\\]+/g)) {
    if (ALLOW.some((re) => re.test(url))) continue;
    console.error(`${file.slice(dist.length + 1)}: ${url}`);
    findings += 1;
  }
}

if (findings) {
  console.error(`\n${findings} absolute URL(s) in the built shell. Self-host or remove.`);
  process.exit(1);
}
console.log('no CDN references in dist/');
