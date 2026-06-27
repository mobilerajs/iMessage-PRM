# tests/test_appconfig.py
import json, importlib

def test_env_beats_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"chat_db": "from_config.db"}))
    monkeypatch.setenv("CHAT_DB", "from_env.db")
    import appconfig; importlib.reload(appconfig)
    assert appconfig.resolve("chat_db", "CHAT_DB", "default.db", config_path=str(cfg)) == "from_env.db"

def test_config_beats_default(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"chat_db": "from_config.db"}))
    monkeypatch.delenv("CHAT_DB", raising=False)
    import appconfig; importlib.reload(appconfig)
    assert appconfig.resolve("chat_db", "CHAT_DB", "default.db", config_path=str(cfg)) == "from_config.db"

def test_default_when_nothing_set(tmp_path, monkeypatch):
    monkeypatch.delenv("CHAT_DB", raising=False)
    import appconfig; importlib.reload(appconfig)
    assert appconfig.resolve("chat_db", "CHAT_DB", "default.db", config_path=str(tmp_path/"missing.json")) == "default.db"
