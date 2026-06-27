# tests/test_userstate.py
import json, importlib

def test_rejects_non_dict_and_keeps_backup(tmp_path, monkeypatch):
    import server; importlib.reload(server)
    us = tmp_path / "userstate.json"
    us.write_text(json.dumps({"hidden": ["pX"]}))
    monkeypatch.setattr(server, "USERSTATE", str(us))
    # A bad payload must be rejected and the good prior state preserved.
    ok, err = server.validate_userstate([1, 2, 3])
    assert ok is False and err
    ok, err = server.validate_userstate({"hidden": ["pY"]})
    assert ok is True
