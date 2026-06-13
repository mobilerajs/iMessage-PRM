import sqlite3, pytest

@pytest.fixture
def fake_chat_db(tmp_path):
    p = tmp_path / "chat.db"
    con = sqlite3.connect(p)
    con.executescript("""
        CREATE TABLE handle(ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message(ROWID INTEGER PRIMARY KEY, text TEXT,
            date INTEGER, handle_id INTEGER, is_from_me INTEGER DEFAULT 0);
        INSERT INTO handle(ROWID,id) VALUES (1,'+15551234567');
        INSERT INTO message(ROWID,text,date,handle_id) VALUES (1,'hi',700000000000000000,1);
    """)
    con.commit(); con.close()
    return p
