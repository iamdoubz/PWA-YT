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
