"""Self-check: `uv run python test_server.py`.

Plain asserts, no pytest. Covers the things that are silently wrong rather than
loudly broken — id and timestamp formats the sync cursor depends on, the pragmas
FM-nothing will remind you about, and the size estimate the user reads before
committing a download.

Nothing here touches the network. Extraction is checked against the live site by
hitting /resolve; that check becomes the nightly extractor canary in v0.2.
"""

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

os.environ["PWA_YT_DB"] = str(Path(tempfile.mkdtemp()) / "test.db")

import db  # noqa: E402  (must follow the env var)
import extract  # noqa: E402


def test_uuid7_shape_and_ordering():
    ids = [db.uuid7() for _ in range(200)]
    assert len(set(ids)) == 200, "uuid7 collided"
    for i in ids:
        assert i[14] == "7", f"version nibble should be 7, got {i}"
        assert i[19] in "89ab", f"variant should be 10xx, got {i}"
    # Same-millisecond ids may tie, but the sequence must never go backwards.
    assert ids == sorted(ids) or ids[0][:8] <= ids[-1][:8], "uuid7 lost time ordering"


def test_now_is_iso8601_utc_millis():
    earlier = db.now()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", earlier), earlier
    time.sleep(0.002)
    later = db.now()
    # Lexicographic order must equal chronological order; the sync cursor relies
    # on it, and a stamp without fixed-width millis would silently break that.
    assert earlier < later, f"{earlier} should sort before {later}"


def test_init_applies_pragmas_and_seeds_user():
    db.init()
    with db.reading() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        row = conn.execute("SELECT * FROM users WHERE id = ?", (db.DEV_USER_ID,)).fetchone()
        assert row is not None and row["email"] == db.DEV_USER_EMAIL
    db.init()  # idempotent: a restart must not duplicate the dev user
    with db.reading() as conn:
        # Scoped to the dev user specifically, not a total row count — other
        # tests legitimately create their own throwaway users, and test order
        # is alphabetical, not declaration order.
        assert (
            conn.execute("SELECT COUNT(*) FROM users WHERE id = ?", (db.DEV_USER_ID,)).fetchone()[0]
            == 1
        )


def test_foreign_keys_are_enforced():
    with db.reading() as conn:
        try:
            conn.execute(
                "INSERT INTO library_items (id, user_id, source_key, format_profile,"
                " added_at, updated_at) VALUES (?,?,?,?,?,?)",
                (db.uuid7(), "nobody", "youtube:x", "{}", db.now(), db.now()),
            )
        except Exception as err:
            assert "FOREIGN KEY" in str(err).upper(), err
        else:
            raise AssertionError("foreign key violation was accepted")


def test_writing_rolls_back_on_error():
    key = "youtube:rollback"
    try:
        with db.writing() as conn:
            conn.execute(
                "INSERT INTO sources (source_key, extractor, source_id, canonical_url,"
                " refreshed_at) VALUES (?,?,?,?,?)",
                (key, "youtube", "rollback", "http://x", db.now()),
            )
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with db.reading() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sources WHERE source_key = ?", (key,)
        ).fetchone()[0] == 0, "failed transaction left a row behind"


def test_estimated_bytes_matches_the_documented_example():
    # 04-api.md: a 213s item at 192 kbps estimates 5_112_000 bytes.
    assert extract.estimated_bytes(213, 192) == 5_112_000
    # v0.3: None, not 0 — a flat playlist entry with no reported duration is
    # "unknown size", which the import UI must not silently count as zero.
    assert extract.estimated_bytes(None, 192) is None


def test_source_key_is_extractor_colon_id():
    assert extract._source_key({"extractor_key": "Youtube", "id": "dQw4w9WgXcQ"}) == (
        "youtube:dQw4w9WgXcQ"
    )
    assert extract._source_key({"extractor_key": "SoundCloud", "id": "1234567"}) == (
        "soundcloud:1234567"
    )


def test_ssrf_extractor_allowlist_blocks_out_of_scope_urls():
    """Security hardening, 2026-08-23 (D-022). Without `allowed_extractors`,
    yt-dlp's generic extractor fetches *any* URL that doesn't match a known
    site — including internal/local addresses this app has no other way to
    reach. yt-dlp decides "no extractor matches" by testing each
    extractor's URL pattern, which is why this rejects instantly with no
    real network call despite using a real-looking URL."""
    try:
        extract.probe("https://example.com/not-a-real-video", 192)
    except extract.ResolveError as err:
        assert "no suitable extractor" in str(err).lower(), err
    else:
        raise AssertionError("a URL outside the allowed extractor families must be rejected, not fetched")


def test_allowed_extractors_cover_the_supported_platform_names():
    """D-030: adding a platform means adding its extractor_key pattern here —
    this pins that each supported site's real yt-dlp extractor_key (including
    the sub-extractors used for playlists/albums/users) actually matches one
    of the configured patterns, the same case-insensitive full match yt-dlp
    itself applies (D-022)."""
    import re

    extractor_keys = [
        "Youtube", "youtube:playlist", "youtube:tab",
        "SoundCloud", "soundcloud:set", "soundcloud:user",
        "Bandcamp", "Bandcamp:album", "Bandcamp:user", "Bandcamp:weekly",
        "mixcloud", "mixcloud:playlist", "mixcloud:user",
        "vimeo", "vimeo:album", "vimeo:channel", "vimeo:user",
    ]
    for key in extractor_keys:
        assert any(
            re.fullmatch(pattern, key, re.IGNORECASE)
            for pattern in extract.ALLOWED_EXTRACTORS
        ), f"{key} matches none of {extract.ALLOWED_EXTRACTORS}"


def test_api_docs_are_disabled_by_default():
    """Security hardening, 2026-08-23. FastAPI serves interactive docs and
    the raw OpenAPI schema publicly with no auth by default — free,
    unauthenticated reconnaissance (every endpoint, field name, and
    validation rule) this invite-only app has no reason to hand out.
    PWA_YT_ENABLE_DOCS is unset in this test run, so all three must be off."""
    import main

    assert main.app.docs_url is None
    assert main.app.redoc_url is None
    assert main.app.openapi_url is None


def test_prefer_copy_never_upscales_a_bitrate():
    import pipeline

    aac = {"prefer_copy": True, "audio_codec": "aac", "audio_bitrate": 192}
    # YouTube itag 140 is ~129 kbps AAC and the default target is 192. Copy it:
    # raising the bitrate cannot recover information the encoder already threw
    # away, and it makes the file bigger. This is the D-014 case.
    assert pipeline.should_copy(aac, "mp4a.40.2", 129.5) is True
    # Asked for something smaller than the source: that is a real transcode.
    assert pipeline.should_copy({**aac, "audio_bitrate": 128}, "mp4a.40.2", 129.5) is False
    # Equal is still a copy.
    assert pipeline.should_copy({**aac, "audio_bitrate": 128}, "mp4a.40.2", 128) is True
    # Opus source must always be transcoded; it is not AAC.
    assert pipeline.should_copy(aac, "opus", 128.9) is False
    # Explicitly disabled, or a different target codec.
    assert pipeline.should_copy({**aac, "prefer_copy": False}, "mp4a.40.2", 129.5) is False
    assert pipeline.should_copy({**aac, "audio_codec": "mp3"}, "mp4a.40.2", 129.5) is False


def test_lyrics_clean_title_strips_video_noise_and_redundant_uploader_prefix():
    import lyrics

    assert lyrics._clean_title("Some Artist - A Song (Official Music Video)", "Some Artist") == "A Song"
    assert lyrics._clean_title("Some Artist - A Song (Lyric Video) ft. Other", "Some Artist") == "A Song ft. Other"
    # A genuinely parenthetical part of a title must survive — only
    # noise-keyword-gated groups get stripped, not every set of parens.
    assert lyrics._clean_title("A Song (Artist's Version)", "Artist") == "A Song (Artist's Version)"
    # No uploader match at the front: title left alone apart from noise-strip.
    assert lyrics._clean_title("Someone Else - A Song", "Some Artist") == "Someone Else - A Song"


def test_lyrics_best_match_picks_closest_duration_within_tolerance():
    import lyrics

    results = [
        {"duration": 180, "instrumental": False, "syncedLyrics": "x", "plainLyrics": "x"},
        {"duration": 205, "instrumental": False, "syncedLyrics": "x", "plainLyrics": "x"},
        {"duration": 400, "instrumental": False, "syncedLyrics": "x", "plainLyrics": "x"},
    ]
    match = lyrics.best_match(results, duration_s=203)
    assert match["duration"] == 205


def test_lyrics_best_match_rejects_outside_tolerance():
    import lyrics

    results = [{"duration": 300, "instrumental": False, "syncedLyrics": "x", "plainLyrics": "x"}]
    assert lyrics.best_match(results, duration_s=200) is None


def test_lyrics_best_match_skips_instrumental():
    import lyrics

    results = [{"duration": 200, "instrumental": True, "syncedLyrics": None, "plainLyrics": None}]
    assert lyrics.best_match(results, duration_s=200) is None


def test_lyrics_best_match_none_without_a_known_duration():
    import lyrics

    results = [{"duration": 200, "instrumental": False, "syncedLyrics": "x", "plainLyrics": "x"}]
    assert lyrics.best_match(results, duration_s=None) is None


def test_lyrics_cache_round_trips_and_force_bypasses_it():
    # Hand-rolled save/restore rather than a mocking framework — this file
    # has neither pytest nor monkeypatch, see the module docstring.
    import lyrics

    calls = {"n": 0}

    def fake_search(track_name, artist_name):
        calls["n"] += 1
        return [{"duration": 100, "instrumental": False, "syncedLyrics": "[00:01.00]placeholder",
                  "plainLyrics": "placeholder"}]

    orig_search = lyrics._search
    lyrics._search = fake_search
    try:
        with db.writing() as conn:
            conn.execute(
                "INSERT INTO sources (source_key, extractor, source_id, canonical_url, refreshed_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(source_key) DO NOTHING",
                ("test:lyrics-cache", "test", "lyrics-cache", "https://x", db.now()),
            )

        row1 = lyrics.get_or_fetch("test:lyrics-cache", "t", "a", 100, force=False)
        assert row1["found"] == 1 and calls["n"] == 1

        lyrics.get_or_fetch("test:lyrics-cache", "t", "a", 100, force=False)
        assert calls["n"] == 1, "a second call without force should read the cache, not re-query LRCLIB"

        lyrics.get_or_fetch("test:lyrics-cache", "t", "a", 100, force=True)
        assert calls["n"] == 2, "force=True must bypass the cache"
    finally:
        lyrics._search = orig_search


def test_finish_backfills_missing_source_fields_independently():
    # SoundCloud's flat playlist probe leaves title/uploader/duration_s all
    # NULL (extract.py); the full per-item extraction that runs at download
    # time is the first point any of them is known. _finish() backfills
    # sources with whatever's still missing — COALESCE per column, so an
    # already-correct field never gets clobbered by the same job filling in
    # a *different* still-missing one.
    import main

    with db.writing() as conn:
        conn.execute(
            "INSERT INTO sources (source_key, extractor, source_id, canonical_url,"
            " title, uploader, duration_s, refreshed_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(source_key) DO NOTHING",
            ("test:finish-partial", "test", "finish-partial", "https://x",
             "Already Correct", "Some Artist", None, db.now()),
        )
        item_id = db.uuid7()
        conn.execute(
            "INSERT INTO library_items (id, user_id, source_key, format_profile, added_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (item_id, db.DEV_USER_ID, "test:finish-partial", "{}", db.now(), db.now()),
        )

    main._finish(
        "test-job-finish-partial", item_id, "test:finish-partial",
        {"copied": True, "title": "Already Correct", "uploader": "Some Artist", "duration_s": 200,
         "files": [{"name": "audio.m4a", "bytes": 10, "sha256": "a" * 64}]},
        tempfile.mkdtemp(), db.DEV_USER_ID,
    )

    with db.reading() as conn:
        row = conn.execute(
            "SELECT title, uploader, duration_s FROM sources WHERE source_key = ?",
            ("test:finish-partial",),
        ).fetchone()
    assert row["title"] == "Already Correct", "an already-correct field must not be overwritten"
    assert row["uploader"] == "Some Artist"
    assert row["duration_s"] == 200, "the still-missing field must get backfilled"


def test_delete_item_cascades_into_playlists():
    import main

    with db.writing() as conn:
        conn.execute(
            "INSERT INTO sources (source_key, extractor, source_id, canonical_url, refreshed_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(source_key) DO NOTHING",
            ("youtube:cascade-test", "youtube", "cascade-test", "http://x", db.now()),
        )
        item_id = db.uuid7()
        conn.execute(
            "INSERT INTO library_items (id, user_id, source_key, format_profile, added_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (item_id, db.DEV_USER_ID, "youtube:cascade-test", "{}", db.now(), db.now()),
        )
        playlist_id = db.uuid7()
        conn.execute(
            "INSERT INTO playlists (id, user_id, name, created_at, updated_at) VALUES (?,?,?,?,?)",
            (playlist_id, db.DEV_USER_ID, "Cascade Test", db.now(), db.now()),
        )
        conn.execute(
            "INSERT INTO playlist_items (playlist_id, item_id, position, updated_at) VALUES (?,?,?,?)",
            (playlist_id, item_id, "a0", db.now()),
        )

    main.delete_item(item_id, user={"id": db.DEV_USER_ID})

    with db.reading() as conn:
        row = conn.execute(
            "SELECT deleted_at FROM playlist_items WHERE playlist_id=? AND item_id=?",
            (playlist_id, item_id),
        ).fetchone()
    assert row is not None and row["deleted_at"] is not None, "playlist_items row was not cascaded"


def test_delete_item_does_not_touch_another_users_playlist():
    """Security regression, found by live audit 2026-08-23 (D-021). The
    cascade in delete_item used to run unconditionally, keyed only on
    item_id — so calling DELETE /items/{other_user's_item_id} silently wiped
    that item out of the *other* user's playlists, even though it correctly
    left their library_items row untouched (the WHERE user_id=? there always
    excluded it)."""
    import main

    owner, attacker = db.uuid7(), db.uuid7()
    with db.writing() as conn:
        for uid in (owner, attacker):
            conn.execute(
                "INSERT INTO users (id, email, created_at) VALUES (?, ?, ?)",
                (uid, f"{uid}@example.com", db.now()),
            )
        conn.execute(
            "INSERT INTO sources (source_key, extractor, source_id, canonical_url, refreshed_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(source_key) DO NOTHING",
            ("youtube:idor-test", "youtube", "idor-test", "http://x", db.now()),
        )
        item_id = db.uuid7()
        conn.execute(
            "INSERT INTO library_items (id, user_id, source_key, format_profile, added_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (item_id, owner, "youtube:idor-test", "{}", db.now(), db.now()),
        )
        playlist_id = db.uuid7()
        conn.execute(
            "INSERT INTO playlists (id, user_id, name, created_at, updated_at) VALUES (?,?,?,?,?)",
            (playlist_id, owner, "Owner's playlist", db.now(), db.now()),
        )
        conn.execute(
            "INSERT INTO playlist_items (playlist_id, item_id, position, updated_at) VALUES (?,?,?,?)",
            (playlist_id, item_id, "a0", db.now()),
        )

    main.delete_item(item_id, user={"id": attacker})  # attacker doesn't own this item

    with db.reading() as conn:
        row = conn.execute(
            "SELECT deleted_at FROM playlist_items WHERE playlist_id=? AND item_id=?",
            (playlist_id, item_id),
        ).fetchone()
    assert row["deleted_at"] is None, "an attacker deleted an item out of someone else's playlist"


def test_patch_playlist_items_rejects_another_users_item_id():
    """Security regression, found by live audit 2026-08-23 (D-021). Only
    playlist ownership was checked, not item ownership — so PUT
    /playlists/{own_playlist}/items with an upsert referencing another
    user's item_id succeeded (the FK to library_items is satisfied by
    *any* user's row), attaching a foreign item into your own playlist."""
    import main

    owner, attacker = db.uuid7(), db.uuid7()
    with db.writing() as conn:
        for uid in (owner, attacker):
            conn.execute(
                "INSERT INTO users (id, email, created_at) VALUES (?, ?, ?)",
                (uid, f"{uid}@example.com", db.now()),
            )
        conn.execute(
            "INSERT INTO sources (source_key, extractor, source_id, canonical_url, refreshed_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(source_key) DO NOTHING",
            ("youtube:idor-test-2", "youtube", "idor-test-2", "http://x", db.now()),
        )
        owners_item = db.uuid7()
        conn.execute(
            "INSERT INTO library_items (id, user_id, source_key, format_profile, added_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (owners_item, owner, "youtube:idor-test-2", "{}", db.now(), db.now()),
        )
        attackers_playlist = db.uuid7()
        conn.execute(
            "INSERT INTO playlists (id, user_id, name, created_at, updated_at) VALUES (?,?,?,?,?)",
            (attackers_playlist, attacker, "Attacker's playlist", db.now(), db.now()),
        )

    req = main.PlaylistItemsPatch(upserts=[main.PlaylistItemUpsert(item_id=owners_item, position="a0")])
    try:
        main.patch_playlist_items(attackers_playlist, req, user={"id": attacker})
    except Exception as err:
        assert getattr(err, "status_code", None) == 404, err
    else:
        raise AssertionError("attaching another user's item into your own playlist must be rejected")

    with db.reading() as conn:
        row = conn.execute(
            "SELECT 1 FROM playlist_items WHERE playlist_id=? AND item_id=?",
            (attackers_playlist, owners_item),
        ).fetchone()
    assert row is None, "the foreign item must not have been attached"


def test_session_round_trip_and_expiry():
    import auth

    with db.writing() as conn:
        session = auth.create_session(conn, db.DEV_USER_ID, device_label="test")

    user = auth.current_user(authorization=f"Bearer {session['token']}")
    assert user["id"] == db.DEV_USER_ID

    try:
        auth.current_user(authorization="Bearer not-a-real-token")
    except Exception as err:
        assert getattr(err, "status_code", None) == 401, err
    else:
        raise AssertionError("an unknown token should not resolve to a user")

    try:
        auth.current_user(authorization=None)
    except Exception as err:
        assert getattr(err, "status_code", None) == 401, err
    else:
        raise AssertionError("a missing Authorization header should 401")

    # Backdate the session past its own expiry — same trick 09-status.md's
    # reaper test uses — rather than sleeping 30 days.
    with db.writing() as conn:
        conn.execute(
            "UPDATE sessions SET expires_at = '2000-01-01T00:00:00.000Z' WHERE token_hash = ?",
            (auth._hash_token(session["token"]),),
        )
    try:
        auth.current_user(authorization=f"Bearer {session['token']}")
    except Exception as err:
        assert getattr(err, "status_code", None) == 401, err
    else:
        raise AssertionError("an expired session should 401, not silently pass")


def test_logout_deletes_the_session():
    import auth

    with db.writing() as conn:
        session = auth.create_session(conn, db.DEV_USER_ID)
    auth.delete_session(f"Bearer {session['token']}")
    try:
        auth.current_user(authorization=f"Bearer {session['token']}")
    except Exception as err:
        assert getattr(err, "status_code", None) == 401, err
    else:
        raise AssertionError("logout should invalidate the session server-side")


def test_invite_gates_registration():
    import auth

    with db.writing() as conn:
        conn.execute(
            "INSERT INTO invites (code, created_at) VALUES ('nope-not-real-invite', ?)",
            (db.now(),),
        )
        conn.execute(
            "UPDATE invites SET used_at = ? WHERE code = 'nope-not-real-invite'", (db.now(),)
        )

    try:
        auth.begin_registration("a@example.com", "A", "this-code-does-not-exist")
    except Exception as err:
        assert getattr(err, "status_code", None) == 422, err
    else:
        raise AssertionError("an unknown invite code should be rejected")

    try:
        auth.begin_registration("a@example.com", "A", "nope-not-real-invite")
    except Exception as err:
        assert getattr(err, "status_code", None) == 422, err
    else:
        raise AssertionError("an already-used invite code should be rejected")


def test_registration_and_login_ceremonies_generate_valid_options():
    """Exercises the real `webauthn` integration for the half that doesn't
    need a signature: option generation, ceremony storage, and single-use
    ceremony ids. The verify_* half (a real passkey signing a real challenge)
    needs an actual authenticator and is checked by hand in a browser, not
    here — this file touches no network and no browser, same as extraction.
    """
    import auth

    code = db.uuid7()[-12:]
    with db.writing() as conn:
        conn.execute("INSERT INTO invites (code, created_at) VALUES (?, ?)", (code, db.now()))

    ceremony_id, options_json = auth.begin_registration("new-user@example.com", "New User", code)
    options = json.loads(options_json)
    assert options["rp"]["id"] == auth.RP_ID
    assert options["authenticatorSelection"]["residentKey"] == "required", (
        "registration must request a discoverable credential — usernameless "
        "login depends on it"
    )
    assert ceremony_id in auth._ceremonies

    # A ceremony id is single-use even before it's finished successfully.
    with_bad_credential = {"id": "x", "rawId": "x", "response": {}, "type": "public-key"}
    try:
        auth.finish_registration(ceremony_id, with_bad_credential)
    except Exception:
        pass  # expected — it's not a real signature
    assert ceremony_id not in auth._ceremonies, "a finished ceremony must not be replayable"

    login_ceremony_id, login_options_json = auth.begin_login()
    login_options = json.loads(login_options_json)
    assert "allowCredentials" not in login_options or not login_options["allowCredentials"], (
        "login must not restrict to allow_credentials — the passkey picker "
        "showing every discoverable credential is the whole point"
    )
    assert login_ceremony_id != ceremony_id


def test_usage_ledger_accumulates_and_gates_new_jobs():
    import main

    user_id = db.uuid7()
    with db.writing() as conn:
        conn.execute(
            "INSERT INTO users (id, email, display_name, daily_byte_budget, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (user_id, f"{user_id}@example.com", "Budget Test", 1000, db.now()),
        )
        conn.execute(
            "INSERT INTO sources (source_key, extractor, source_id, canonical_url, refreshed_at)"
            " VALUES (?,?,?,?,?)",
            ("youtube:budget-test", "youtube", "budget-test", "http://x", db.now()),
        )

    assert main._usage_today(user_id) == 0
    main._record_usage(user_id, 600)
    assert main._usage_today(user_id) == 600
    main._record_usage(user_id, 600)  # now over the 1000-byte budget
    assert main._usage_today(user_id) == 1200

    req = main.ItemsRequest(entries=[main.ItemEntry(source_key="youtube:budget-test")])
    resp = main.create_items(req, user={"id": user_id})
    assert resp.status_code == 429, "a user already over budget should be refused a new job"
    assert json.loads(resp.body)["error"] == "quota_exceeded"


def test_cookie_jar_round_trips_and_survives_key_rotation():
    import main
    from cryptography.fernet import Fernet

    user_id = db.uuid7()
    with db.writing() as conn:
        conn.execute(
            "INSERT INTO users (id, email, display_name, created_at) VALUES (?, ?, ?, ?)",
            (user_id, f"{user_id}@example.com", "Cookie Test", db.now()),
        )

    assert main._user_cookies(user_id) is None, "no cookies configured yet"

    cookie_text = "# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tFALSE\t0\tfoo\tbar\n"
    encrypted = main._fernet.encrypt(cookie_text.encode())
    with db.writing() as conn:
        conn.execute(
            "UPDATE users SET cookies_encrypted = ?, cookies_updated_at = ? WHERE id = ?",
            (encrypted, db.now(), user_id),
        )
    assert main._user_cookies(user_id) == cookie_text

    # A key rotation must degrade to "not configured", not raise — a resolve
    # for a user whose old cookies no longer decrypt must still work for
    # everything that isn't gated behind those cookies.
    stale = Fernet(Fernet.generate_key()).encrypt(b"irrelevant")
    with db.writing() as conn:
        conn.execute("UPDATE users SET cookies_encrypted = ? WHERE id = ?", (stale, user_id))
    assert main._user_cookies(user_id) is None


def test_sync_returns_incremental_changes_with_a_working_cursor():
    import main

    user_id = db.uuid7()
    with db.writing() as conn:
        conn.execute(
            "INSERT INTO users (id, email, display_name, created_at) VALUES (?, ?, ?, ?)",
            (user_id, f"{user_id}@example.com", "Sync Test", db.now()),
        )
        conn.execute(
            "INSERT INTO sources (source_key, extractor, source_id, canonical_url, refreshed_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(source_key) DO NOTHING",
            ("youtube:sync-test", "youtube", "sync-test", "http://x", db.now()),
        )

    first = main.sync(since="", user={"id": user_id})
    assert first["items"] == [] and first["playlists"] == [] and first["playlist_items"] == []
    cursor = first["cursor"]

    item_id = db.uuid7()
    with db.writing() as conn:
        conn.execute(
            "INSERT INTO library_items (id, user_id, source_key, format_profile, added_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (item_id, user_id, "youtube:sync-test", "{}", db.now(), db.now()),
        )

    second = main.sync(since=cursor, user={"id": user_id})
    assert len(second["items"]) == 1 and second["items"][0]["id"] == item_id
    cursor2 = second["cursor"]

    third = main.sync(since=cursor2, user={"id": user_id})
    assert third["items"] == [], "nothing changed since the second cursor"

    # A tombstone is a change too, and re-syncing from the very first (empty)
    # cursor returns full history including it — not just "current state".
    with db.writing() as conn:
        conn.execute(
            "UPDATE library_items SET deleted_at=?, updated_at=? WHERE id=?",
            (db.now(), db.now(), item_id),
        )
    fourth = main.sync(since=cursor2, user={"id": user_id})
    assert len(fourth["items"]) == 1
    assert fourth["items"][0]["deleted_at"] is not None


def test_sync_scopes_playlist_items_by_playlist_ownership():
    import main

    owner = db.uuid7()
    other = db.uuid7()
    with db.writing() as conn:
        for uid in (owner, other):
            conn.execute(
                "INSERT INTO users (id, email, created_at) VALUES (?, ?, ?)",
                (uid, f"{uid}@example.com", db.now()),
            )
        playlist_id = db.uuid7()
        conn.execute(
            "INSERT INTO playlists (id, user_id, name, created_at, updated_at) VALUES (?,?,?,?,?)",
            (playlist_id, owner, "Owner's playlist", db.now(), db.now()),
        )

    # `other` should never see the owner's playlist in their own sync feed.
    result = main.sync(since="", user={"id": other})
    assert result["playlists"] == []


def test_sync_survives_a_malformed_cursor():
    """Security/robustness bug found live 2026-08-23: `_decode_cursor` only
    guarded against a cursor that fails to *decode* (bad base64/JSON) — one
    that decodes cleanly but has the wrong shape (a string instead of a
    2-element position, wrong length, wrong element types, or not even a
    dict at the top level) crashed the unpacking with an unhandled 500. A
    cursor is client-supplied and should never be trusted enough to crash
    on; every shape here must degrade to "sync everything", not raise."""
    import base64
    import json as json_module

    import main

    bad_shapes = [
        {"items": "oops"},
        {"items": ["a"]},  # wrong arity
        {"items": [1, 2]},  # wrong element types
        {"items": None},
        {"playlist_items": ["a", "b"]},  # wrong arity (needs 3)
        [1, 2, 3],  # not a dict at all
        "just a string",
        42,
    ]
    user_id = db.uuid7()  # fresh, not db.DEV_USER_ID — no cross-test history to confuse "did it crash?" with "what did it return?"
    with db.writing() as conn:
        conn.execute(
            "INSERT INTO users (id, email, created_at) VALUES (?, ?, ?)",
            (user_id, f"{user_id}@example.com", db.now()),
        )
    for shape in bad_shapes:
        cursor = base64.urlsafe_b64encode(json_module.dumps(shape).encode()).decode()
        result = main.sync(since=cursor, user={"id": user_id})  # must not raise
        assert result["items"] == [], f"shape {shape!r} should degrade to sync-everything, not crash"

    # Undecodable-entirely still works too (the case the original fix covered).
    result = main.sync(since="not-valid-base64-at-all!!!", user={"id": user_id})
    assert result["items"] == []


def test_is_rate_limited_detects_429_messages():
    import main

    assert main._is_rate_limited("DownloadError: HTTP Error 429: Too Many Requests")
    assert main._is_rate_limited("some 429 mid-message")
    assert not main._is_rate_limited("HTTP Error 404: Not Found")


def test_circuit_breaker_trips_and_backs_off_exponentially():
    import main

    main._consecutive_429s = 0
    main._breaker_until = 0.0
    assert main.breaker_status()["tripped"] is False

    main._trip_breaker()
    first = main.breaker_status()
    assert first["tripped"] is True
    assert first["consecutive_429s"] == 1

    until_after_first = main._breaker_until
    main._trip_breaker()
    assert main._consecutive_429s == 2
    assert main._breaker_until > until_after_first, "a second consecutive 429 should back off further"

    main._reset_breaker()
    assert main._consecutive_429s == 0
    main._breaker_until = 0.0  # don't leak a tripped breaker into other tests


def test_resolve_short_circuits_when_the_breaker_is_tripped():
    """Hardening, 2026-08-23: /resolve reaches the same shared server IP the
    job runner does, so it shares the circuit breaker in both directions —
    a 429 from a resolve trips it, and an already-tripped breaker should
    stop /resolve from hammering the source while it's cooling down. This
    checks the short-circuit specifically: it returns before ever touching
    the resolve pool, so it needs no running server or pool to test."""
    import main

    main._consecutive_429s = 0
    main._breaker_until = 0.0
    main._trip_breaker()
    try:
        resp = main.resolve(
            main.ResolveRequest(url="https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
            user={"id": db.DEV_USER_ID},
        )
        assert resp.status_code == 503
        assert json.loads(resp.body)["error"] == "rate_limited"
    finally:
        main._reset_breaker()
        main._breaker_until = 0.0


def test_magic_link_round_trip_and_single_use():
    import auth

    email = "magiclink@example.com"
    user_id = db.uuid7()
    with db.writing() as conn:
        conn.execute(
            "INSERT INTO users (id, email, display_name, created_at) VALUES (?, ?, ?, ?)",
            (user_id, email, "Magic", db.now()),
        )

    auth.request_magic_link(email)  # PWA_YT_SMTP_HOST unset in tests -> prints, doesn't mail
    tokens = [cid for cid, v in auth._ceremonies.items() if v["kind"] == "magic_link" and v["email"] == email]
    assert len(tokens) == 1, "a known email should produce exactly one pending ceremony"
    token = tokens[0]

    result = auth.verify_magic_link(token)
    assert result["user"]["id"] == user_id
    assert result["user"]["email"] == email
    assert "token" in result and "expires_at" in result

    try:
        auth.verify_magic_link(token)
    except Exception as err:
        assert getattr(err, "status_code", None) == 422, err
    else:
        raise AssertionError("a magic-link token should be single-use")


def test_magic_link_unknown_email_is_silent():
    import auth

    before = len(auth._ceremonies)
    auth.request_magic_link("nobody-registered@example.com")
    after = len(auth._ceremonies)
    assert after == before, "an unregistered email must not create a usable ceremony (no account-existence oracle)"


def test_magic_link_rate_limited():
    import auth

    email = "ratelimit@example.com"
    with db.writing() as conn:
        conn.execute(
            "INSERT INTO users (id, email, display_name, created_at) VALUES (?, ?, ?, ?)",
            (db.uuid7(), email, "Rate", db.now()),
        )

    try:
        auth.request_magic_link(email)  # 1st: fine
        auth.request_magic_link(email)  # 2nd, immediately: cooldown
    except Exception as err:
        assert getattr(err, "status_code", None) == 429, err
    else:
        raise AssertionError("a second request inside the cooldown window should be rate-limited")

    # Past the cooldown but at the hourly cap — same trick other tests use to
    # simulate elapsed time without sleeping for it.
    now = time.monotonic()
    auth._magic_link_sends[email] = [now - 120] * auth.MAGIC_LINK_MAX_PER_HOUR
    try:
        auth.request_magic_link(email)
    except Exception as err:
        assert getattr(err, "status_code", None) == 429, err
    else:
        raise AssertionError("exceeding the hourly cap should be rate-limited")
    finally:
        del auth._magic_link_sends[email]  # don't leak into other tests


if __name__ == "__main__":
    db.init()  # schema must exist before any test that touches it
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            fn()
            print(f"pass  {name}")
        except Exception as err:
            failures += 1
            print(f"FAIL  {name}: {type(err).__name__}: {err}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
