import sqlite3, pytest
from imessage_db import open_readonly, backup_db

def test_open_readonly_can_read(fake_chat_db):
    con = open_readonly(fake_chat_db)
    assert con.execute("SELECT COUNT(*) FROM message").fetchone()[0] == 1

def test_open_readonly_blocks_writes(fake_chat_db):
    con = open_readonly(fake_chat_db)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM message")

def test_backup_creates_timestamped_copy(fake_chat_db, tmp_path):
    dest_dir = tmp_path / "backups"
    out = backup_db(fake_chat_db, dest_dir, stamp="20260610-120000")
    assert out.exists() and out.name == "chat-20260610-120000.db"
    assert out.read_bytes() == fake_chat_db.read_bytes()
