import shutil
import sqlite3
from pathlib import Path

def open_readonly(path):
    """Open chat.db strictly read-only + immutable. Physically cannot write."""
    uri = f"file:{Path(path).resolve()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)

def backup_db(src, dest_dir, stamp):
    """Copy chat.db to dest_dir/chat-<stamp>.db. stamp is injected by the caller."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"chat-{stamp}.db"
    shutil.copy2(src, out)
    return out
