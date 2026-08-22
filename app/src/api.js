// Everything that touches the network. Nothing here may be called before first
// paint — see docs/02-offline-playback.md FM-2.
//
// Requests go to a same-origin /api prefix that Vite proxies to the server, so
// there is one origin, no CORS, and a cloudflared tunnel in front of it works
// without the client knowing anything about it.

export const API = '/api';

// Airplane mode usually fails fast, but captive portals and cabin wifi produce
// hangs, which are worse than failures because nothing ever rejects.
const TIMEOUT_MS = 20_000;

class ApiError extends Error {
  constructor(body, status) {
    super(body?.message || `Request failed (${status})`);
    this.code = body?.error ?? 'http_error';
    this.status = status;
  }
}

async function request(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    ...options,
    headers: { 'content-type': 'application/json', ...options.headers },
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  const body = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(body, res.status);
  return body;
}

export const resolveUrl = (url, format_profile) =>
  request('/resolve', { method: 'POST', body: JSON.stringify({ url, format_profile }) });

export const createItem = (source_key, format_profile) =>
  request('/items', {
    method: 'POST',
    body: JSON.stringify({ entries: [{ source_key, format_profile }] }),
  });

export const createItems = (entries, playlist_id) =>
  request('/items', { method: 'POST', body: JSON.stringify({ entries, playlist_id }) });

/**
 * `/resolve` is one JSON object for a single item, or NDJSON for a playlist —
 * one line per entry as yt-dlp's flat enumeration produces it, so a 400-entry
 * playlist can start rendering before the last entry arrives rather than after
 * a single multi-second wait. Read as a stream either way; a single item is
 * just a one-line stream.
 *
 * Resolving a large playlist is slower than the 20s default, so this bypasses
 * `request()` and sets its own timeout.
 */
export async function streamResolve(url, format_profile, { onLine, signal } = {}) {
  const res = await fetch(`${API}/resolve`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ url, format_profile }),
    signal: signal ?? AbortSignal.timeout(120_000),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new ApiError(body, res.status);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl);
      buf = buf.slice(nl + 1);
      if (line) onLine(JSON.parse(line));
    }
  }
  if (buf.trim()) onLine(JSON.parse(buf));
}

// ------------------------------------------------------------------ playlists

export const createPlaylist = (id, name) =>
  request('/playlists', { method: 'POST', body: JSON.stringify({ id, name }) });

export const listPlaylists = () => request('/playlists');

export const renamePlaylist = (id, name) =>
  request(`/playlists/${id}`, { method: 'PATCH', body: JSON.stringify({ name }) });

export const deletePlaylist = (id) => request(`/playlists/${id}`, { method: 'DELETE' });

export const patchPlaylistItems = (playlistId, upserts = [], removes = []) =>
  request(`/playlists/${playlistId}/items`, {
    method: 'PUT',
    body: JSON.stringify({ upserts, removes }),
  });

export const listJobs = () => request('/jobs');

export const retryJob = (id) => request(`/jobs/${id}/retry`, { method: 'POST' });

/**
 * One SSE connection for all in-flight jobs.
 *
 * EventSource reconnects on its own, which is usually the point — but offline
 * that means a network request every few seconds forever, and FM-2 is explicit
 * that a library sitting in storage must not depend on the network. So a real
 * failure closes the stream and tells the caller; only the server's own
 * connection cap (which arrives as a `reconnect` event) is reconnected through.
 */
export function openJobStream(onJobs, onLost) {
  let source = null;
  let expected = false;
  let closed = false;

  const connect = () => {
    source = new EventSource(`${API}/jobs/stream`);
    source.onmessage = (e) => onJobs(JSON.parse(e.data));
    source.addEventListener('reconnect', () => {
      expected = true;
    });
    source.onerror = () => {
      source.close();
      if (closed) return;
      if (expected) {
        expected = false;
        connect();
        return;
      }
      onLost?.();
    };
  };

  connect();
  return {
    close() {
      closed = true;
      source?.close();
    },
  };
}

export const deleteItem = (id) => request(`/items/${id}`, { method: 'DELETE' });

// The collection acknowledgement. Not optional cleanup — it is the contract
// that keeps the server stateless.
export const acknowledge = (jobId) =>
  request(`/jobs/${jobId}/artifact`, { method: 'DELETE' });
