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

export const resolveUrl = (url) =>
  request('/resolve', { method: 'POST', body: JSON.stringify({ url }) });

export const createItem = (source_key) =>
  request('/items', { method: 'POST', body: JSON.stringify({ entries: [{ source_key }] }) });

export const listJobs = () => request('/jobs');

export const deleteItem = (id) => request(`/items/${id}`, { method: 'DELETE' });

// The collection acknowledgement. Not optional cleanup — it is the contract
// that keeps the server stateless.
export const acknowledge = (jobId) =>
  request(`/jobs/${jobId}/artifact`, { method: 'DELETE' });
