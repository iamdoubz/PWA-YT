// `npm run test:sha`. Plain asserts, no framework.
//
// A wrong SHA-256 accepts corrupt downloads silently, so this checks the NIST
// vectors, the block-boundary cases where a hand-written implementation
// actually breaks, and agreement with node's own crypto over random data.

import assert from 'node:assert';
import { createHash, randomBytes } from 'node:crypto';
import { Sha256 } from '../src/sha256.js';

const enc = new TextEncoder();
const hash = (s) => new Sha256().update(enc.encode(s)).digest();

// FIPS 180-4 / NIST published vectors
assert.equal(
  hash(''),
  'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
  'empty string',
);
assert.equal(
  hash('abc'),
  'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
  'abc',
);
assert.equal(
  hash('abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq'),
  '248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1',
  '448-bit message',
);
assert.equal(
  hash('a'.repeat(1_000_000)),
  'cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0',
  'one million a',
);

// Lengths either side of every padding branch: 55/56 is where the extra block
// appears, 63/64/65 is the buffer-carry boundary in update().
for (const n of [1, 54, 55, 56, 57, 63, 64, 65, 119, 120, 127, 128, 129]) {
  const data = randomBytes(n);
  assert.equal(
    new Sha256().update(data).digest(),
    createHash('sha256').update(data).digest('hex'),
    `length ${n}`,
  );
}

// Chunked exactly as the worker feeds it: many updates, arbitrary split points.
for (let trial = 0; trial < 50; trial++) {
  const data = randomBytes(1 + Math.floor(Math.random() * 5000));
  const h = new Sha256();
  let off = 0;
  while (off < data.length) {
    const take = 1 + Math.floor(Math.random() * 300);
    h.update(data.subarray(off, off + take));
    off += take;
  }
  assert.equal(h.digest(), createHash('sha256').update(data).digest('hex'), 'chunked');
}

console.log('sha256: all vectors and 63 randomised cases pass');
