import os, importlib

def _server():
    import server; return server

# A minimal but real SQLite file (valid 16-byte magic header) for folder tests.
_SQLITE_HEADER = b"SQLite format 3\x00"
def _mk_sqlite(path):
    path.write_bytes(_SQLITE_HEADER + b"\x00" * 100)

def test_needs_setup_true_when_people_missing(tmp_path, monkeypatch):
    s = _server()
    monkeypatch.setattr(s, "PEOPLE_OUT", str(tmp_path / "nope.json"))
    assert s.needs_setup() is True

def test_needs_setup_false_when_people_present(tmp_path, monkeypatch):
    s = _server()
    p = tmp_path / "people.json"; p.write_text("[]")
    monkeypatch.setattr(s, "PEOPLE_OUT", str(p))
    assert s.needs_setup() is False

def test_classify_snapshot_proceeds_when_snapshotted():
    s = _server()
    d = s.classify_snapshot_for_setup({"snapshotted": True, "reason": "ok"}, chat_db_exists=False)
    assert d["proceed"] is True and d["fda_needed"] is False

def test_classify_snapshot_proceeds_on_existing_copy_when_not_snapshotted():
    s = _server()
    d = s.classify_snapshot_for_setup({"snapshotted": False, "reason": "x"}, chat_db_exists=True)
    assert d["proceed"] is True and d["fda_needed"] is False

def test_classify_snapshot_fda_needed_when_unreadable_and_no_copy():
    s = _server()
    d = s.classify_snapshot_for_setup(
        {"snapshotted": False, "reason": "live Messages DB not readable (grant Full Disk Access)"},
        chat_db_exists=False)
    assert d["proceed"] is False and d["fda_needed"] is True

def test_classify_snapshot_not_found_is_blocked_but_not_fda():
    s = _server()
    d = s.classify_snapshot_for_setup(
        {"snapshotted": False, "reason": "live Messages DB not found"}, chat_db_exists=False)
    assert d["proceed"] is False and d["fda_needed"] is False  # use the folder path instead

def test_validate_folder_ok(tmp_path):
    s = _server()
    _mk_sqlite(tmp_path / "chat.db")
    ok, info, err = s.validate_setup_folder(str(tmp_path))
    assert ok and info["chat_db"].endswith("chat.db") and info["contacts"] is None and err == ""

def test_validate_folder_picks_up_contacts(tmp_path):
    s = _server()
    _mk_sqlite(tmp_path / "chat.db"); (tmp_path / "contacts.vcf").write_text("y")
    ok, info, _ = s.validate_setup_folder(str(tmp_path))
    assert ok and info["contacts"].endswith("contacts.vcf")

def test_validate_folder_rejects_non_sqlite(tmp_path):
    s = _server()
    (tmp_path / "chat.db").write_text("not a database")
    ok, info, err = s.validate_setup_folder(str(tmp_path))
    assert ok is False and "sqlite" in err.lower()

def test_validate_folder_rejects_symlinked_chatdb(tmp_path):
    s = _server()
    real = tmp_path / "real.db"; _mk_sqlite(real)
    link = tmp_path / "chat.db"; os.symlink(real, link)
    ok, info, err = s.validate_setup_folder(str(tmp_path))
    assert ok is False and "symlink" in err.lower()

# ---- HIGH: never write the working DB onto the live Messages DB ----
def test_is_safe_working_db_allows_normal_path(tmp_path):
    s = _server()
    assert s.is_safe_working_db(str(tmp_path / "chat.db")) is True

def test_is_safe_working_db_blocks_live_db():
    s = _server()
    assert s.is_safe_working_db("~/Library/Messages/chat.db") is False
    assert s.is_safe_working_db("~/Library/Messages/chat.db-wal") is False

# ---- MEDIUM: SQLite header sniff ----
def test_looks_like_sqlite(tmp_path):
    s = _server()
    good = tmp_path / "g.db"; _mk_sqlite(good)
    bad = tmp_path / "b.db"; bad.write_text("nope")
    assert s.looks_like_sqlite(str(good)) is True
    assert s.looks_like_sqlite(str(bad)) is False
    assert s.looks_like_sqlite(str(tmp_path / "missing.db")) is False

# ---- MEDIUM: birthday validation before any Contacts mutation ----
def test_valid_birthday():
    s = _server()
    assert s.valid_birthday(2, 29) is True
    assert s.valid_birthday(12, 31) is True
    assert s.valid_birthday(13, 1) is False
    assert s.valid_birthday(1, 32) is False
    assert s.valid_birthday("x", "y") is False
    assert s.valid_birthday(None, None) is False

def test_validate_folder_missing_dir(tmp_path):
    s = _server()
    ok, info, err = s.validate_setup_folder(str(tmp_path / "ghost"))
    assert ok is False and "not found" in err.lower()

def test_validate_folder_no_chatdb(tmp_path):
    s = _server()
    ok, info, err = s.validate_setup_folder(str(tmp_path))
    assert ok is False and "chat.db" in err

def test_setup_status_endpoint(tmp_path, monkeypatch):
    s = _server()
    monkeypatch.setattr(s, "PEOPLE_OUT", str(tmp_path / "nope.json"))
    client = s.app.test_client()
    r = client.get("/api/setup/status")
    body = r.get_json()
    assert r.status_code == 200
    assert body["needs_setup"] is True
    assert "fda_ok" in body and "chat_db_present" in body

def test_setup_from_folder_rejects_bad_folder(tmp_path):
    s = _server()
    client = s.app.test_client()
    r = client.post("/api/setup/from-folder", json={"folder": str(tmp_path / "ghost")})
    assert r.status_code == 400
    assert "not found" in r.get_json()["error"].lower()

def test_setup_from_folder_rejects_missing_chatdb(tmp_path):
    s = _server()
    client = s.app.test_client()
    r = client.post("/api/setup/from-folder", json={"folder": str(tmp_path)})
    assert r.status_code == 400 and "chat.db" in r.get_json()["error"]
