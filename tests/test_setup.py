import os, importlib

def _server():
    import server; return server

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
    (tmp_path / "chat.db").write_text("x")
    ok, info, err = s.validate_setup_folder(str(tmp_path))
    assert ok and info["chat_db"].endswith("chat.db") and info["contacts"] is None and err == ""

def test_validate_folder_picks_up_contacts(tmp_path):
    s = _server()
    (tmp_path / "chat.db").write_text("x"); (tmp_path / "contacts.vcf").write_text("y")
    ok, info, _ = s.validate_setup_folder(str(tmp_path))
    assert ok and info["contacts"].endswith("contacts.vcf")

def test_validate_folder_missing_dir(tmp_path):
    s = _server()
    ok, info, err = s.validate_setup_folder(str(tmp_path / "ghost"))
    assert ok is False and "not found" in err.lower()

def test_validate_folder_no_chatdb(tmp_path):
    s = _server()
    ok, info, err = s.validate_setup_folder(str(tmp_path))
    assert ok is False and "chat.db" in err
