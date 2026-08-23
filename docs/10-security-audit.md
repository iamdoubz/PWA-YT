# Security audit log

Tracks an ad-hoc audit that started as "verify no one can reach someone
else's playlist/songs" and grew into five passes across one day
(2026-08-23). Each pass is a decision in `08-decisions.md` (D-021 through
D-027) with the full writeup — reproduction steps, the fix, and how it was
re-verified. This file is the checklist: what's been looked at, what hasn't,
so a future session (or a future audit pass) doesn't have to re-derive scope
from five conversations' worth of narrative in `09-status.md` §1b.

**Standard used throughout:** a finding isn't "done" until it's reproduced
against the *real running server* (not just read from the code), fixed,
re-verified against the same live reproduction, and pinned by a permanent
test in `test_server.py`. Several findings only exist because something was
actually attacked rather than reviewed — assume that's still true of
whatever hasn't been looked at yet.

---

## 1. Covered

| # | Category | Method | Outcome |
|---|---|---|---|
| 1 | Authentication completeness | Hit all 20 non-public endpoints with no `Authorization` header at all | Clean — every one 401s. `/health`, `/health/extractors`, `/auth/*` are the only intentionally public routes. |
| 2 | Authorization / IDOR on every mutating endpoint | Two real accounts, real sessions, real cross-account HTTP requests against each mutating endpoint | **2 bugs found and fixed** (D-021): `DELETE /items/{id}`'s playlist cascade and `PUT /playlists/{id}/items`'s upsert both trusted an id from the request without checking it belonged to the caller. |
| 3 | SSRF via server-side URL fetching | Pointed `/resolve` at cloud-metadata-shaped and loopback addresses | **1 bug found and fixed** (D-022): yt-dlp's generic extractor fetched any unrecognised URL. Restricted to `youtube.*`/`soundcloud.*` in both `extract.py` and `pipeline.py`. |
| 4 | Unbounded resource fields | Reviewed every request field for persistence + repeated-cost fields | **1 bug found and fixed** (D-023): cookie jar had no size cap; capped at 256 KiB. |
| 5 | Dependency CVEs | `npm audit` (client), `pip-audit` via a temporary `uv export` (server) | Clean — 0 known vulnerabilities in either tree, at time of audit. **Point-in-time; re-run periodically, not just once.** |
| 6 | Client-side XSS surface | Grepped for `@html`/`innerHTML`/`eval`/`document.write`; traced `thumb_url` and every `<img>` binding | Clean — no unsafe HTML sinks anywhere; `thumb_url` isn't even referenced client-side; every `<img>` binds only to a local blob URL. |
| 7 | SQL injection | Grepped every `.execute(` call site across the server for string-built queries | Clean — every query is parameterized, none built with f-strings or `.format()`. |
| 8 | CORS / WebAuthn origin handling | Re-read `auth.py`'s origin filtering and `main.py`'s CORS middleware config | Clean — `*` is stripped before it could reach `expected_origin`; `allow_credentials` is never set, so the separate CORS wildcard default never combines with credentialed requests. |
| 9 | 429 circuit breaker completeness | Traced which code paths reach the shared server IP | **1 bug found and fixed** (D-024): `/resolve` shared none of the job runner's breaker state. Now shares it in both directions. |
| 10 | Security response headers | Checked what headers were actually set | **1 gap found and fixed** (D-025): none were. Added `X-Content-Type-Options: nosniff`. |
| 11 | Crash-on-malformed-input | Fuzzed 6 endpoints with wrong-typed/malformed JSON bodies | **1 bug found and fixed** (D-026): `/sync`'s cursor crashed with a raw 500 on well-formed-but-wrong-shaped JSON — the one place client input is hand-decoded after Pydantic is done. Every other endpoint's typed Pydantic body caught garbage input cleanly (422). |
| 12 | Unauthenticated information disclosure | Asked what a visitor with *no login attempt at all* can see | **1 gap found and fixed** (D-027): `/docs`, `/redoc`, `/openapi.json` were public by default, handing out the entire API surface. Now off unless `PWA_YT_ENABLE_DOCS` is set. |
| 13 | Mass assignment | Grepped for `model_dump()`/`.dict()`/`**req.` spread into SQL calls | Clean — every write uses an explicit, hand-written column list; no request model is ever blanket-spread into an INSERT/UPDATE. |

**Running total: 8 real issues found and fixed, all live-verified, all
pinned by regression tests** (`test_server.py` is at 25 checks as of
D-027). Zero found in categories 5, 6, 7, 8, 13.

---

## 2. Not yet covered

Roughly in the order I'd actually do them — highest-value first, not
strictly by OWASP category:

1. **Debug-mode / stack-trace leakage, verified rather than assumed.** The
   `/sync` crash (D-026) confirmed the *client* only ever saw a generic
   Starlette 500 for one specific unhandled exception — that's good
   evidence, but it was incidental to that finding, not a deliberate check
   that `FastAPI(...)` never runs with `debug=True` and that uvicorn itself
   isn't started with a flag that would echo tracebacks. Worth a direct
   check, not an inference from one example.
2. **Excessive data exposure (field-level, not just object-level).** IDOR
   (can you reach a row you shouldn't) is covered. Not systematically
   checked: does any response return *more fields* than the client needs,
   in a way that could matter later — e.g., does anything ever leak
   `disabled_at`, `invited_by`, or a credential's public key where it
   doesn't need to? Spot-checked for `/me` and `/me/cookies` during the
   mass-assignment pass, not audited response-by-response.
3. **Untrusted third-party data (yt-dlp's output).** SSRF covers what URL
   the server is allowed to *reach*. Not checked: what yt-dlp's *response*
   for a legitimate-looking video can contain — title/uploader length caps
   before storing into `sources` (shared across all users), whether an
   extremely large or adversarially-crafted metadata field could cause
   problems downstream (storage bloat, `outtmpl` templating in
   `pipeline.py` using `%(id)s` — could a compromised or malicious
   extractor response ever put path-traversal characters into `id` and
   affect where `ffmpeg`/yt-dlp write files?). This is auditing what the
   app does with data coming *back* from a source it already trusts enough
   to query, which is a different question from SSRF.
4. **Resource exhaustion via legitimate-shaped requests.** Explicitly
   *not* pursued so far, with reasoning recorded (D-023's decision note):
   a global request-body-size limit, and rate-limiting on the
   unauthenticated `/auth/*` endpoints. That reasoning leaned on this app's
   invite-only threat model (R-12) — worth revisiting specifically if that
   assumption ever changes, rather than re-deciding from scratch. Also
   uncapped today: `/sync` and `/jobs` (documented, known gaps, D-020) —
   fine at personal scale, not stress-tested at any scale.
5. **Timing side-channels**, reasoned about but never measured: session
   token lookup (SHA-256 hash, then SQL equality), invite code lookup.
   Reasoned as impractical to exploit over a real network given jitter
   dominates, but that's an argument, not a measurement.
6. **Sync/LWW conflict manipulation.** Could a client push a mutation with
   a forged future `updated_at` to manipulate last-write-wins ordering?
   Everything `/sync`-related is scoped to the caller's own rows, so the
   blast radius is "mess with your own data across your own devices," not
   cross-tenant — likely low value, but not explicitly tried.
7. **Broken function-level authorization.** Not applicable yet — there is
   exactly one privilege tier (an authenticated user acting on their own
   data). Revisit this category specifically the moment any admin-only or
   cross-user function is added (user management, disabling an account,
   etc. — none of which exist today).
8. **Audit/forensic logging.** If an account *were* compromised, there's
   currently no log of sensitive actions (login, cookie jar changes,
   deletions) to reconstruct what happened. Not a vulnerability by itself,
   but worth a conscious decision either way rather than an oversight.
9. **Supply-chain hygiene beyond known-CVE scanning.** `npm audit`/
   `pip-audit` check against known vulnerability databases at a point in
   time. Not checked: lockfiles are committed and used (`uv.lock`,
   `package-lock.json` — worth confirming CI/deploy actually installs from
   the lockfile, not a fresh resolve), or dependency pinning strategy
   generally.
10. **Physical WebAuthn ceremony completion.** Not a code gap — a known,
    already-documented blocker (`09-status.md` §1a): needs a human at a
    real authenticator, same category as the device offline-test gate.
    Re-running the full cross-account criteria (D-021's fix, specifically)
    with two *real* passkey-registered accounts rather than seeded sessions
    would be the strongest possible confirmation once that's done.

---

## 3. How to continue

Pick up at §2 in order, or hand a specific item to a fresh audit pass. Each
new finding should get: a live reproduction against the running server (not
just a code read), a fix, live re-verification against the same
reproduction, a permanent regression test, and an entry in `08-decisions.md`
— then a line added to §1 of this file and the corresponding line removed
from §2.
