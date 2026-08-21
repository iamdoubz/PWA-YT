// Assertion 12 of the offline test protocol: "no successful network request".
// A counter in the app is weaker than a proxy log but it is the thing you can
// actually read while standing in an aeroplane, so it is on screen.
//
// Counts main-thread fetch only. The worker has its own global scope and the
// service worker has another; neither runs while offline in v0.0, so a non-zero
// `ok` here offline means the boot path is reaching for the network — which is
// FM-2, the failure mode this whole phase is watching for.

export const net = $state({ ok: 0, fail: 0 });

const original = globalThis.fetch.bind(globalThis);

globalThis.fetch = async (...args) => {
  try {
    const res = await original(...args);
    net.ok += 1;
    return res;
  } catch (err) {
    net.fail += 1;
    throw err;
  }
};
