// Incremental SHA-256 (FIPS 180-4).
//
// This exists because `crypto.subtle.digest` is one-shot: it takes a whole
// BufferSource and there is no streaming Web Crypto API. Hashing an 86 MB
// download with it would mean holding the entire file in memory as one buffer,
// which is exactly what docs/02-offline-playback.md FM-3 forbids. So the hash
// is computed chunk by chunk as the bytes go past on their way to disk.
//
// ponytail: ~70 lines instead of a dependency, but this is NOT the place to be
// clever — a wrong hash silently accepts a corrupt file, which is the failure
// mode FM-4 exists to prevent. Pinned by NIST vectors in sha256.test.js, and
// cross-checked in production against the server's Python hashlib digest on
// every single download. If this were wrong, nothing would ever verify.

const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
  0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
  0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
  0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
  0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
  0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

const rotr = (x, n) => (x >>> n) | (x << (32 - n));

export class Sha256 {
  constructor() {
    this.h = new Uint32Array([
      0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
      0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    ]);
    this.w = new Uint32Array(64);
    this.buf = new Uint8Array(64); // partial block carried between updates
    this.len = 0; // bytes currently in buf
    this.total = 0; // bytes seen overall
  }

  _block(b, off) {
    const w = this.w;
    for (let i = 0; i < 16; i++) {
      const j = off + i * 4;
      w[i] = (b[j] << 24) | (b[j + 1] << 16) | (b[j + 2] << 8) | b[j + 3];
    }
    for (let i = 16; i < 64; i++) {
      const x = w[i - 15];
      const y = w[i - 2];
      const s0 = rotr(x, 7) ^ rotr(x, 18) ^ (x >>> 3);
      const s1 = rotr(y, 17) ^ rotr(y, 19) ^ (y >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
    }

    let [a, b0, c, d, e, f, g, h] = this.h;
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (h + S1 + ch + K[i] + w[i]) | 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b0) ^ (a & c) ^ (b0 & c);
      const t2 = (S0 + maj) | 0;
      h = g;
      g = f;
      f = e;
      e = (d + t1) | 0;
      d = c;
      c = b0;
      b0 = a;
      a = (t1 + t2) | 0;
    }

    this.h[0] += a;
    this.h[1] += b0;
    this.h[2] += c;
    this.h[3] += d;
    this.h[4] += e;
    this.h[5] += f;
    this.h[6] += g;
    this.h[7] += h;
  }

  update(bytes) {
    this.total += bytes.length;
    let off = 0;

    if (this.len > 0) {
      const take = Math.min(64 - this.len, bytes.length);
      this.buf.set(bytes.subarray(0, take), this.len);
      this.len += take;
      off = take;
      if (this.len === 64) {
        this._block(this.buf, 0);
        this.len = 0;
      }
    }
    while (off + 64 <= bytes.length) {
      this._block(bytes, off);
      off += 64;
    }
    if (off < bytes.length) {
      this.buf.set(bytes.subarray(off), 0);
      this.len = bytes.length - off;
    }
    return this;
  }

  digest() {
    const bits = this.total * 8;
    // Split across 64 bits: JS numbers are exact to 2^53, so this is safe for
    // any file that could plausibly exist on a phone.
    const hi = Math.floor(this.total / 0x20000000);
    const lo = bits >>> 0;

    const pad = new Uint8Array(this.len < 56 ? 64 - this.len : 128 - this.len);
    pad[0] = 0x80;
    const n = pad.length;
    pad[n - 8] = (hi >>> 24) & 0xff;
    pad[n - 7] = (hi >>> 16) & 0xff;
    pad[n - 6] = (hi >>> 8) & 0xff;
    pad[n - 5] = hi & 0xff;
    pad[n - 4] = (lo >>> 24) & 0xff;
    pad[n - 3] = (lo >>> 16) & 0xff;
    pad[n - 2] = (lo >>> 8) & 0xff;
    pad[n - 1] = lo & 0xff;
    this.update(pad);

    let hex = '';
    for (let i = 0; i < 8; i++) hex += (this.h[i] >>> 0).toString(16).padStart(8, '0');
    return hex;
  }
}
