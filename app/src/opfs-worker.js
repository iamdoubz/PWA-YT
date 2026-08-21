// The only thing in the app that touches OPFS.
//
// Dedicated worker + createSyncAccessHandle(), NOT createWritable(). This is a
// compatibility requirement, not a preference — see docs/02-offline-playback.md
// FM-3. createSyncAccessHandle only exists in a dedicated worker, which is why
// this file exists at all.
//
// Two jobs: pull artifacts in (download), and answer "is what the catalogue
// claims actually on disk?" (verify).

import { Sha256 } from './sha256.js';

const PROGRESS_EVERY = 2 * 1024 * 1024; // don't postMessage 1300 times per file
const VERIFY_BATCH = 50;

async function mediaRoot(create = false) {
  const root = await navigator.storage.getDirectory();
  return root.getDirectoryHandle('media', { create });
}

async function itemDir(itemId, create = false) {
  return (await mediaRoot(create)).getDirectoryHandle(itemId, { create });
}

async function downloadFile(dir, spec, onProgress) {
  const { name, url, sha256: expectedSha } = spec;
  const res = await fetch(url, { signal: AbortSignal.timeout(120_000) });
  if (!res.ok) throw new Error(`${name}: HTTP ${res.status}`);

  const expected = Number(res.headers.get('content-length'));
  if (!expected) throw new Error(`${name}: no Content-Length, nothing to verify against`);

  // FM-4: write to .part, verify, and only then take the real name.
  const partName = `${name}.part`;
  const handle = await dir.getFileHandle(partName, { create: true });
  const sync = await handle.createSyncAccessHandle();
  const hasher = new Sha256();
  let written = 0;
  let digest = null; // digest() absorbs padding, so it is computed exactly once
  try {
    sync.truncate(0);
    const reader = res.body.getReader();
    let lastPost = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      written += sync.write(value, { at: written });
      // Hashed on the way past, so the bytes are never held in memory twice.
      hasher.update(value);
      if (written - lastPost >= PROGRESS_EVERY) {
        lastPost = written;
        onProgress(written, expected);
      }
    }
    sync.flush();

    const actual = sync.getSize();
    if (actual !== expected) throw new Error(`${name}: wrote ${actual} bytes, expected ${expected}`);

    // Length catches truncation, which is the common failure. The hash catches
    // corruption, which is rarer and worse because it plays for a while first.
    digest = hasher.digest();
    if (expectedSha && digest !== expectedSha) {
      throw new Error(`${name}: checksum mismatch — got ${digest.slice(0, 12)}…`);
    }
  } catch (err) {
    // A failed write must never be left where a later sweep could mistake it
    // for a good file.
    sync.close();
    await dir.removeEntry(partName).catch(() => {});
    throw err;
  }
  sync.close();

  // The commit point. Safari's support for OPFS move() is still unverified on
  // hardware. If it throws, the file is complete and verified regardless, so
  // keep it under the .part name and report which name actually won.
  try {
    await handle.move(name);
    return { name, bytes: expected, sha256: digest, moved: true };
  } catch (err) {
    return { name: partName, bytes: expected, sha256: digest, moved: false,
             moveError: String(err) };
  }
}

async function download(itemId, files) {
  const dir = await itemDir(itemId, true);
  const written = [];
  for (const spec of files) {
    written.push(
      await downloadFile(dir, spec, (done, total) =>
        self.postMessage({ type: 'progress', itemId, file: spec.name, done, total }),
      ),
    );
  }

  // Re-downloading at a different profile writes audio.mp3 next to the old
  // audio.m4a, and nothing references the old one again — it just sits there
  // consuming the storage the user is trying to budget. Anything not in the
  // manifest we just wrote is stale, including abandoned .part files.
  const keep = new Set(written.map((f) => f.name));
  for await (const name of dir.keys()) {
    if (!keep.has(name)) await dir.removeEntry(name).catch(() => {});
  }
  return written;
}

/**
 * FM-6: WebKit evicts storage under pressure and after prolonged non-use. The
 * catalogue's claimed state is what the library renders from, so first paint is
 * never blocked on this — it runs afterwards, in batches, and downgrades any row
 * whose bytes are not actually there.
 *
 * Stats only. Re-hashing every file on every boot would cost minutes and catch
 * a failure mode that does not happen to files sitting still on a disk.
 */
async function verify(rows) {
  const missing = [];
  let checked = 0;
  for (let i = 0; i < rows.length; i += VERIFY_BATCH) {
    for (const row of rows.slice(i, i + VERIFY_BATCH)) {
      let ok = true;
      try {
        const dir = await itemDir(row.item_id);
        for (const f of row.files) {
          const file = await (await dir.getFileHandle(f.name)).getFile();
          if (file.size !== f.bytes) ok = false;
        }
      } catch {
        ok = false; // directory or file is gone
      }
      if (!ok) missing.push(row.item_id);
      checked += 1;
    }
    self.postMessage({ type: 'verify_progress', checked, total: rows.length });
  }
  return { missing, checked };
}

async function purge(itemId) {
  const root = await mediaRoot();
  await root.removeEntry(itemId, { recursive: true }).catch(() => {});
}

self.onmessage = async ({ data }) => {
  try {
    if (data.type === 'verify') {
      self.postMessage({ type: 'verified', ...(await verify(data.rows)) });
    } else if (data.type === 'purge') {
      await purge(data.itemId);
      self.postMessage({ type: 'purged', itemId: data.itemId });
    } else {
      self.postMessage({ type: 'done', itemId: data.itemId, files: await download(data.itemId, data.files) });
    }
  } catch (err) {
    self.postMessage({ type: 'error', itemId: data.itemId, error: String(err) });
  }
};
