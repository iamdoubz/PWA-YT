// UUIDv7: 48-bit big-endian unix-ms prefix, so ids sort by creation time. Only
// needed client-side for rows the client creates itself offline (playlists) —
// item ids still come from the server. Mirrors server/db.py's uuid7(); ~10
// lines, not worth a dependency.
export function uuid7() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  let ms = BigInt(Date.now());
  for (let i = 5; i >= 0; i--) {
    bytes[i] = Number(ms & 0xffn);
    ms >>= 8n;
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x70; // version
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
