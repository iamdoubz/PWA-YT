"""Passkeys (WebAuthn), invite-only registration, and bearer sessions.

Runs in the main FastAPI process (not a pool worker) — everything here is
either a DB call through `db.py`'s single-writer discipline or pure crypto
via the `webauthn` package. No network calls.

Ceremony flow (both registration and login are two-step):
  1. `begin_*` generates a challenge, stashes it server-side keyed by a random
     `ceremony_id`, and returns that id plus the WebAuthn options for the
     browser to pass to `navigator.credentials.create()` / `.get()`.
  2. `finish_*` takes the ceremony id back plus the browser's response,
     verifies it against the stashed challenge, and never accepts the same
     ceremony id twice.

Registration and login are both usernameless (resident/discoverable
credentials) — there is no username field anywhere in this flow. The
authenticator's own passkey picker is the identity UI.
"""

import base64
import hashlib
import json
import os
import secrets
import smtplib
import threading
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import webauthn
from fastapi import Header, HTTPException
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

import db

RP_NAME = "PWA-YT"
# A passkey is bound to this hostname for its whole life. It must be the exact
# host the browser is served from — no scheme, no port. Defaults to localhost
# for dev; a real deployment (or a stable tunnel hostname) must set this.
RP_ID = os.environ.get("PWA_YT_RP_ID", "localhost")

# WebAuthn needs exact origins, unlike CORS's "*" default in main.py — reusing
# that env var here but rejecting the wildcard, since there is no such thing
# as a wildcard WebAuthn origin.
_origins_env = [o for o in os.environ.get("PWA_YT_ORIGINS", "").split(",") if o and o != "*"]
ORIGINS = _origins_env or ["http://localhost:4173"]

# 30 days: long enough that a home-screen PWA isn't re-prompting for a
# passkey every week. Expiry while offline degrades to read-only, it does not
# log out — see FM-2 and the client's session-handling code.
SESSION_TTL_S = 30 * 24 * 60 * 60
CEREMONY_TTL_S = 5 * 60

# Recovery/fallback path (D-008) — email round trips need longer than a
# WebAuthn ceremony's 5 minutes.
MAGIC_LINK_TTL_S = 15 * 60
MAGIC_LINK_COOLDOWN_S = 60           # minimum gap between sends to one email
MAGIC_LINK_MAX_PER_HOUR = 5          # "rate-limited hard", per 04-api.md

# Pending ceremonies live in memory, not the database: they are seconds-lived,
# single-process (FastAPI's threadpool, not the resolve/job process pools),
# and there is nothing here worth surviving a restart — a dropped ceremony is
# just "try signing in again".
_ceremonies: dict[str, dict] = {}
_ceremony_lock = threading.Lock()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _future(seconds: int) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _sweep_ceremonies() -> None:
    now = time.monotonic()
    with _ceremony_lock:
        for cid in [c for c, v in _ceremonies.items() if now - v["created"] > v["ttl"]]:
            _ceremonies.pop(cid, None)


def _new_ceremony(kind: str, challenge: bytes, ttl: int = CEREMONY_TTL_S, **extra) -> str:
    _sweep_ceremonies()
    ceremony_id = secrets.token_urlsafe(16)
    with _ceremony_lock:
        _ceremonies[ceremony_id] = {
            "kind": kind,
            "challenge": challenge,
            "created": time.monotonic(),
            "ttl": ttl,
            **extra,
        }
    return ceremony_id


def _take_ceremony(ceremony_id: str, kind: str) -> dict:
    """Ceremonies are single-use: popped here, not merely read."""
    with _ceremony_lock:
        entry = _ceremonies.pop(ceremony_id, None)
    if entry is None or entry["kind"] != kind:
        raise HTTPException(422, "That sign-in attempt expired or was already used. Try again.")
    if time.monotonic() - entry["created"] > entry["ttl"]:
        raise HTTPException(422, "That sign-in attempt expired. Try again.")
    return entry


# ------------------------------------------------------------------ sessions


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn, user_id: str, device_label: str | None = None) -> dict:
    token = secrets.token_urlsafe(32)
    expires_at = _future(SESSION_TTL_S)
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, device_label, created_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (_hash_token(token), user_id, device_label, db.now(), expires_at),
    )
    return {"token": token, "expires_at": expires_at}


def current_user(authorization: str | None = Header(default=None)) -> dict:
    """FastAPI dependency. Every endpoint that isn't `/auth/*` or `/health`
    takes this, and every query in it filters by the returned `id` — that is
    what makes two accounts unable to see or affect each other's rows."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Sign in required.")
    token = authorization.split(" ", 1)[1].strip()
    with db.reading() as conn:
        row = conn.execute(
            """SELECT u.id, u.email, u.display_name, u.daily_byte_budget,
                      u.max_concurrent, u.disabled_at, s.expires_at
                 FROM sessions s JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ?""",
            (_hash_token(token),),
        ).fetchone()
    if row is None:
        raise HTTPException(401, "Session not recognised. Sign in again.")
    if row["disabled_at"] is not None:
        raise HTTPException(403, "This account has been disabled.")
    if row["expires_at"] < db.now():
        raise HTTPException(401, "Session expired. Sign in again.")
    return dict(row)


def delete_session(authorization: str | None) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return
    token = authorization.split(" ", 1)[1].strip()
    with db.writing() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))


# ------------------------------------------------------------------- invites


def check_invite(conn, code: str) -> None:
    row = conn.execute("SELECT used_at FROM invites WHERE code = ?", (code,)).fetchone()
    if row is None:
        raise HTTPException(422, "That invite code doesn't exist.")
    if row["used_at"] is not None:
        raise HTTPException(422, "That invite code has already been used.")


# ------------------------------------------------------------- registration


def begin_registration(email: str, display_name: str, invite_code: str) -> tuple[str, str]:
    with db.reading() as conn:
        check_invite(conn, invite_code)
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise HTTPException(422, "That email is already registered.")

    # The user row doesn't exist yet — invite-only registration means an
    # abandoned ceremony must not leave a half-created account behind. The
    # candidate id is generated now because WebAuthn's `user.id` handle has to
    # be minted before the ceremony and is what a future usernameless login
    # gets back as `userHandle`.
    user_id = db.uuid7()
    options = webauthn.generate_registration_options(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_id=user_id.encode(),
        user_name=email,
        user_display_name=display_name or email,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    ceremony_id = _new_ceremony(
        "register",
        options.challenge,
        user_id=user_id,
        email=email,
        display_name=display_name,
        invite_code=invite_code,
    )
    return ceremony_id, webauthn.options_to_json(options)


def finish_registration(ceremony_id: str, credential: dict) -> dict:
    entry = _take_ceremony(ceremony_id, "register")
    try:
        verification = webauthn.verify_registration_response(
            credential=credential,
            expected_challenge=entry["challenge"],
            expected_rp_id=RP_ID,
            expected_origin=ORIGINS,
        )
    except WebAuthnException as err:
        raise HTTPException(422, f"That passkey could not be verified: {err}") from err

    with db.writing() as conn:
        # Re-checked here, inside the transaction, in case two ceremonies for
        # the same invite finished in the race between begin() and here.
        row = conn.execute(
            "SELECT used_at, created_by FROM invites WHERE code = ?", (entry["invite_code"],)
        ).fetchone()
        if row is None or row["used_at"] is not None:
            raise HTTPException(422, "That invite code has already been used.")

        conn.execute(
            "INSERT INTO users (id, email, display_name, invited_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (entry["user_id"], entry["email"], entry["display_name"] or entry["email"], row["created_by"], db.now()),
        )
        conn.execute(
            "UPDATE invites SET used_by = ?, used_at = ? WHERE code = ?",
            (entry["user_id"], db.now(), entry["invite_code"]),
        )
        conn.execute(
            "INSERT INTO credentials (id, user_id, public_key, sign_count, transports, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                _b64url(verification.credential_id),
                entry["user_id"],
                verification.credential_public_key,
                verification.sign_count,
                json.dumps(credential.get("response", {}).get("transports", [])),
                db.now(),
            ),
        )
        session = create_session(conn, entry["user_id"])

    return {
        "token": session["token"],
        "expires_at": session["expires_at"],
        "user": {"id": entry["user_id"], "email": entry["email"], "display_name": entry["display_name"]},
    }


# -------------------------------------------------------------------- login


def begin_login() -> tuple[str, str]:
    # No `allow_credentials`: the authenticator's own passkey picker shows
    # every discoverable credential for this RP, so there is no username
    # field on the client at all.
    options = webauthn.generate_authentication_options(
        rp_id=RP_ID,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    ceremony_id = _new_ceremony("login", options.challenge)
    return ceremony_id, webauthn.options_to_json(options)


def finish_login(ceremony_id: str, credential: dict) -> dict:
    entry = _take_ceremony(ceremony_id, "login")
    cred_id = credential.get("id")
    if not cred_id:
        raise HTTPException(422, "Malformed passkey response.")

    with db.reading() as conn:
        cred_row = conn.execute(
            "SELECT * FROM credentials WHERE id = ?", (cred_id,)
        ).fetchone()
    if cred_row is None:
        raise HTTPException(401, "That passkey isn't registered here.")

    try:
        verification = webauthn.verify_authentication_response(
            credential=credential,
            expected_challenge=entry["challenge"],
            expected_rp_id=RP_ID,
            expected_origin=ORIGINS,
            credential_public_key=cred_row["public_key"],
            credential_current_sign_count=cred_row["sign_count"],
        )
    except WebAuthnException as err:
        raise HTTPException(401, f"That passkey could not be verified: {err}") from err

    with db.reading() as conn:
        user_row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (cred_row["user_id"],)
        ).fetchone()
    if user_row is None or user_row["disabled_at"] is not None:
        raise HTTPException(403, "This account is disabled.")

    with db.writing() as conn:
        conn.execute(
            "UPDATE credentials SET sign_count = ?, last_used_at = ? WHERE id = ?",
            (verification.new_sign_count, db.now(), cred_id),
        )
        session = create_session(conn, user_row["id"])

    return {
        "token": session["token"],
        "expires_at": session["expires_at"],
        "user": {
            "id": user_row["id"],
            "email": user_row["email"],
            "display_name": user_row["display_name"],
        },
    }


# --------------------------------------------------------------- magic link
#
# Recovery/fallback for a passkey-only account (D-008): every device's
# passkey is bound to this RP_ID for life (see the CLAUDE.md/README traps),
# so losing every enrolled authenticator otherwise means losing the account.
# A magic link proves email ownership, which is enough to mint a session —
# the signed-in user then enrolls a *new* passkey from the account settings
# the normal registration ceremony already provides, this endpoint doesn't
# need to know anything about that.
#
# Reuses the ceremony store above rather than a new table: the token is
# exactly as random as a ceremony id, single-use the same way, and short-
# lived enough that surviving a server restart was never a requirement.

_magic_link_lock = threading.Lock()
_magic_link_sends: dict[str, list[float]] = {}  # email -> send timestamps (monotonic)


def _check_magic_link_rate_limit(email: str) -> None:
    now = time.monotonic()
    with _magic_link_lock:
        sends = [t for t in _magic_link_sends.get(email, []) if now - t < 3600]
        if sends and now - sends[-1] < MAGIC_LINK_COOLDOWN_S:
            raise HTTPException(429, "A sign-in link was just sent to that address. Wait a bit before retrying.")
        if len(sends) >= MAGIC_LINK_MAX_PER_HOUR:
            raise HTTPException(429, "Too many sign-in links requested for that address. Try again later.")
        sends.append(now)
        _magic_link_sends[email] = sends


def _send_email(to: str, subject: str, body: str) -> None:
    host = os.environ.get("PWA_YT_SMTP_HOST")
    if not host:
        # ponytail: no mail relay configured — print instead of failing, same
        # spirit as the auto-generated cookie key above. Fine for dev/self-
        # hosting; a real deployment sets PWA_YT_SMTP_HOST.
        print(f"[magic-link] PWA_YT_SMTP_HOST not set — would have emailed {to}:\n{body}", flush=True)
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("PWA_YT_SMTP_FROM", "noreply@pwa-yt.local")
    msg["To"] = to
    msg.set_content(body)

    port = int(os.environ.get("PWA_YT_SMTP_PORT", "587"))
    user = os.environ.get("PWA_YT_SMTP_USER")
    password = os.environ.get("PWA_YT_SMTP_PASS")
    with smtplib.SMTP(host, port, timeout=10) as smtp:
        smtp.starttls()
        if user:
            smtp.login(user, password or "")
        smtp.send_message(msg)


def request_magic_link(email: str) -> None:
    """Always succeeds from the caller's point of view — whether or not the
    email is registered is never observable in the response, only (loosely)
    in the rate limit, which applies regardless of registration status for
    exactly that reason.

    ponytail: sending an actual email (registered) vs. returning immediately
    (not registered) is a timing side channel this doesn't close — an
    invite-only app for "the owner and people they know" (R-12) rather than
    an adversarial public. Add a constant-time delay if that threat model
    ever changes.
    """
    _check_magic_link_rate_limit(email)

    with db.reading() as conn:
        user = conn.execute(
            "SELECT id, disabled_at FROM users WHERE email = ?", (email,)
        ).fetchone()
    if user is None or user["disabled_at"] is not None:
        return

    token = _new_ceremony("magic_link", b"", ttl=MAGIC_LINK_TTL_S, user_id=user["id"], email=email)
    origin = ORIGINS[0]
    link = f"{origin}/?magic_link={token}"
    _send_email(
        email,
        "Sign in to PWA-YT",
        f"Click to sign in: {link}\n\nThis link works once and expires in {MAGIC_LINK_TTL_S // 60} minutes. "
        "If you didn't request this, ignore it.",
    )


def verify_magic_link(token: str) -> dict:
    entry = _take_ceremony(token, "magic_link")

    with db.reading() as conn:
        user_row = conn.execute("SELECT * FROM users WHERE id = ?", (entry["user_id"],)).fetchone()
    if user_row is None or user_row["disabled_at"] is not None:
        raise HTTPException(403, "This account is disabled.")

    with db.writing() as conn:
        session = create_session(conn, user_row["id"])

    return {
        "token": session["token"],
        "expires_at": session["expires_at"],
        "user": {
            "id": user_row["id"],
            "email": user_row["email"],
            "display_name": user_row["display_name"],
        },
    }
