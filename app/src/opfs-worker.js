// The only thing in the app that writes to OPFS.
//
// Dedicated worker + createSyncAccessHandle(), NOT createWritable(). This is a
// compatibility requirement, not a preference — see docs/02-offline-playback.md
// FM-3. createSyncAccessHandle only exists in a dedicated worker, which is why
// this file exists at all.
//
// The fetch happens in here rather than on the main thread. The docs sketch
// fetch-on-main -> postMessage chunks -> worker; doing the fetch here is the
// same guarantee with one less hop and no chunk ping-pong. Still streamed, never
// buffered: a 200 MB file must not exist in memory as one blob.

const PROGRESS_EVERY = 2 * 1024 * 1024; // don't postMessage 1300 times per file

async function itemDir(itemId) {
  const root = await navigator.storage.getDirectory();
  const media = await root.getDirectoryHandle('media', { create: true });
  return media.getDirectoryHandle(itemId, { create: true });
}

async function downloadFile(dir, name, url, onProgress) {
  const res = await fetch(url, { signal: AbortSignal.timeout(120_000) });
  if (!res.ok) throw new Error(`${name}: HTTP ${res.status}`);

  const expected = Number(res.headers.get('content-length'));
  if (!expected) throw new Error(`${name}: no Content-Length, nothing to verify against`);

  // FM-4: write to .part, verify, and only then take the real name.
  const partName = `${name}.part`;
  const handle = await dir.getFileHandle(partName, { create: true });
  const sync = await handle.createSyncAccessHandle();
  let written = 0;
  try {
    sync.truncate(0);
    const reader = res.body.getReader();
    let lastPost = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      written += sync.write(value, { at: written });
      if (written - lastPost >= PROGRESS_EVERY) {
        lastPost = written;
        onProgress(written, expected);
      }
    }
    sync.flush();

    // ponytail: byte length only. SHA-256 would need an incremental digest —
    // crypto.subtle.digest is one-shot and buffering 86 MB to hash it is exactly
    // what FM-3 forbids. Add a streaming hash when the server emits one to check.
    const actual = sync.getSize();
    if (actual !== expected) throw new Error(`${name}: wrote ${actual} bytes, expected ${expected}`);
  } finally {
    sync.close();
  }

  // The commit point. Safari's support for OPFS move() is unverified — finding
  // out is one of the things v0.0 is for. If it throws, the file is complete and
  // verified regardless, so keep it under the .part name and report which name
  // actually won. The IndexedDB row is what decides "present", and it is written
  // by the caller only after this resolves.
  try {
    await handle.move(name);
    return { name, bytes: expected, moved: true };
  } catch (err) {
    return { name: partName, bytes: expected, moved: false, moveError: String(err) };
  }
}

self.onmessage = async ({ data }) => {
  const { itemId, files } = data;
  try {
    const dir = await itemDir(itemId);
    const written = [];
    for (const f of files) {
      written.push(
        await downloadFile(dir, f.name, f.url, (done, total) =>
          self.postMessage({ type: 'progress', itemId, file: f.name, done, total }),
        ),
      );
    }
    self.postMessage({ type: 'done', itemId, files: written });
  } catch (err) {
    self.postMessage({ type: 'error', itemId, error: String(err) });
  }
};
