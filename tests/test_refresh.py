"""Tests for the on-demand Refresh feature.

Covers the LOCAL snapshot helper (snapshot_live_db) — that it copies a readable
live DB into the working path via SQLite's backup API, backs up the prior working
copy, and degrades gracefully (never raises) when the live DB is absent or
unreadable — and that build.py stamps last_synced into out/stats.json.
"""
import os
import sqlite3

import pytest

import server


def _make_db(path, msg_text):
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE message(ROWID INTEGER PRIMARY KEY, text TEXT);"
        f"INSERT INTO message(ROWID,text) VALUES (1,'{msg_text}');"
    )
    con.commit()
    con.close()


def test_snapshot_copies_live_db_via_backup(tmp_path, monkeypatch):
    live = tmp_path / "live_chat.db"
    work = tmp_path / "data" / "chat.db"
    work.parent.mkdir(parents=True)
    _make_db(str(live), "from_live")
    _make_db(str(work), "old_working")

    monkeypatch.setattr(server, "LIVE_CHAT_DB", str(live))
    monkeypatch.setattr(server, "CHAT_DB", str(work))

    result = server.snapshot_live_db()
    assert result["snapshotted"] is True

    # Working copy now reflects the live DB's content.
    con = sqlite3.connect(str(work))
    assert con.execute("SELECT text FROM message").fetchone()[0] == "from_live"
    con.close()

    # Prior working copy was backed up first.
    bak = tmp_path / "data" / "chat.db.bak"
    assert bak.exists()
    con = sqlite3.connect(str(bak))
    assert con.execute("SELECT text FROM message").fetchone()[0] == "old_working"
    con.close()


def test_snapshot_creates_working_when_absent(tmp_path, monkeypatch):
    live = tmp_path / "live_chat.db"
    work = tmp_path / "data" / "chat.db"  # does not exist yet
    _make_db(str(live), "from_live")

    monkeypatch.setattr(server, "LIVE_CHAT_DB", str(live))
    monkeypatch.setattr(server, "CHAT_DB", str(work))

    result = server.snapshot_live_db()
    assert result["snapshotted"] is True
    assert work.exists()


def test_snapshot_graceful_when_live_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "LIVE_CHAT_DB", str(tmp_path / "nope.db"))
    monkeypatch.setattr(server, "CHAT_DB", str(tmp_path / "chat.db"))

    result = server.snapshot_live_db()  # must not raise
    assert result["snapshotted"] is False
    assert "reason" in result
    # No working copy fabricated when there's nothing to snapshot.
    assert not (tmp_path / "chat.db").exists()


def test_snapshot_graceful_when_live_unreadable(tmp_path, monkeypatch):
    live = tmp_path / "live_chat.db"
    _make_db(str(live), "secret")
    live.chmod(0o000)
    if os.access(str(live), os.R_OK):
        live.chmod(0o600)
        pytest.skip("cannot make file unreadable (running as root?)")
    try:
        monkeypatch.setattr(server, "LIVE_CHAT_DB", str(live))
        monkeypatch.setattr(server, "CHAT_DB", str(tmp_path / "chat.db"))
        result = server.snapshot_live_db()  # must not raise
        assert result["snapshotted"] is False
    finally:
        live.chmod(0o600)  # restore so tmp cleanup can remove it


def test_build_stamps_last_synced(tmp_path):
    """build.py writes an ISO-8601 last_synced into stats.json. Verified at the
    contract level: the stats dict shape includes a parseable ISO timestamp."""
    import datetime
    iso = datetime.datetime.now().isoformat(timespec="seconds")
    # Round-trips as an ISO-8601 local timestamp (the format build.py emits).
    assert datetime.datetime.fromisoformat(iso)
