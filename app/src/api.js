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

// Set once at boot from the locally-stored session (never awaited before
// first paint — the token is read from IndexedDB by App.svelte, not fetched)
// and again on login/logout. Every request attaches it if present.
let authToken = null;
export const setAuthToken = (token) => (authToken = token);

// FM-2: an expired or rejected session degrades to read-only, it does not log
// out. This is the one place that has to notice a 401 no matter which call
// site triggered it, so it's a single subscribable callback rather than every
// call site checking `err.status === 401` itself.
let onUnauthorized = () => {};
export const setUnauthorizedHandler = (fn) => (onUnauthorized = fn);

function authHeaders(extra) {
  const headers = { 'content-type': 'application/json', ...extra };
  if (authToken) headers.Authorization = `Bearer ${authToken}`;
  return headers;
}

async function request(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    ...options,
    headers: authHeaders(options.headers),
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  const body = res.status === 204 ? null : await res.json().catch(() => null);
  if (res.status === 401) onUnauthorized();
  if (!res.ok) throw new ApiError(body, res.status);
  return body;
}

// ----------------------------------------------------------------------- auth

export const registerBegin = (invite_code, email, display_name) =>
  request('/auth/register/begin', {
    method: 'POST',
    body: JSON.stringify({ invite_code, email, display_name }),
  });

export const registerFinish = (ceremony_id, credential) =>
  request('/auth/register/finish', {
    method: 'POST',
    body: JSON.stringify({ ceremony_id, credential }),
  });

export const loginBegin = () => request('/auth/login/begin', { method: 'POST' });

export const loginFinish = (ceremony_id, credential) =>
  request('/auth/login/finish', {
    method: 'POST',
    body: JSON.stringify({ ceremony_id, credential }),
  });

export const magicLinkRequest = (email) =>
  request('/auth/magic-link/request', { method: 'POST', body: JSON.stringify({ email }) });

export const magicLinkVerify = (token) =>
  request('/auth/magic-link/verify', { method: 'POST', body: JSON.stringify({ token }) });

// Best-effort: server-side session invalidation only. The caller owns
// clearing the local session — and must never clear items/local_media/OPFS
// alongside it. See FM-2.
export const logout = () => request('/auth/logout', { method: 'POST' }).catch(() => {});

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
    headers: authHeaders(),
    body: JSON.stringify({ url, format_profile }),
    signal: signal ?? AbortSignal.timeout(120_000),
  });
  if (res.status === 401) onUnauthorized();
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

// ----------------------------------------------------------------------- sync

// `cursor` is opaque — whatever the server returned last time, or '' for a
// first-ever sync. The push half of multi-device convergence needs no
// dedicated endpoint: every offline mutation already replays through the
// idempotent REST calls above (see outbox.js, D-018). This is just the pull.
export const sync = (cursor) => request(`/sync?since=${encodeURIComponent(cursor ?? '')}`);

// ------------------------------------------------------------------- account

export const me = () => request('/me');
export const meUsage = () => request('/me/usage');
// One jar per integration, not one per user. The status call returns every
// supported source (configured or not), so the client renders the whole grid
// from one request and keeps no source list of its own.
export const cookiesStatus = () => request('/me/cookies');
export const putCookies = (source, cookies) =>
  request(`/me/cookies/${source}`, { method: 'PUT', body: JSON.stringify({ cookies }) });
export const deleteCookies = (source) =>
  request(`/me/cookies/${source}`, { method: 'DELETE' });

// Unauthenticated — the Account sheet's version footer needs it whether or
// not a session is still valid.
export const health = () => request('/health');

export const listJobs = () => request('/jobs');

export const retryJob = (id) => request(`/jobs/${id}/retry`, { method: 'POST' });

/**
 * One SSE connection for all in-flight jobs, hand-parsed rather than
 * `EventSource` — `EventSource` cannot set an `Authorization` header, and a
 * bearer token in the URL as a query param would end up in server logs and
 * any proxy in front of it. A manual reader can attach the same header every
 * other request uses.
 *
 * Auto-reconnects only through the server's own connection cap (an
 * `event: reconnect` line just before it closes the response) — a real
 * failure closes the stream and tells the caller instead, since FM-2 is
 * explicit that a library sitting in storage must not depend on the network.
 */
export function openJobStream(onJobs, onLost) {
  const controller = new AbortController();
  let closed = false;

  async function connect() {
    let sawReconnect = false;
    try {
      const res = await fetch(`${API}/jobs/stream`, {
        headers: authHeaders(),
        signal: controller.signal,
      });
      if (res.status === 401) {
        onUnauthorized();
        return;
      }
      if (!res.ok) throw new Error(`jobs stream failed (${res.status})`);

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let sep;
        while ((sep = buf.indexOf('\n\n')) >= 0) {
          const chunk = buf.slice(0, sep);
          buf = buf.slice(sep + 2);
          if (chunk.startsWith('event: reconnect')) {
            sawReconnect = true;
            continue;
          }
          const dataLine = chunk.split('\n').find((l) => l.startsWith('data: '));
          if (dataLine) onJobs(JSON.parse(dataLine.slice(6)));
        }
      }
    } catch {
      if (!closed) onLost?.();
      return;
    }
    if (closed) return;
    if (sawReconnect) connect();
    else onLost?.();
  }

  connect();
  return {
    close() {
      closed = true;
      controller.abort();
    },
  };
}

export const deleteItem = (id) => request(`/items/${id}`, { method: 'DELETE' });

// The collection acknowledgement. Not optional cleanup — it is the contract
// that keeps the server stateless.
export const acknowledge = (jobId) =>
  request(`/jobs/${jobId}/artifact`, { method: 'DELETE' });
