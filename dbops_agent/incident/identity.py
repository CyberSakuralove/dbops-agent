"""Host-owned identity: independent of fault labels and stable across process restarts."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from pathlib import Path


def create_identity(conn: sqlite3.Connection) -> str:
    value = f"INC-{uuid.uuid4().hex}"
    conn.execute(
        "CREATE TABLE incident_identity "
        "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO incident_identity VALUES (1,?)", (value,))
    return value


def incident_id(db: Path) -> str:
    # Missing metadata is an error, never a fallback that embeds scenario.id.
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT id FROM incident_identity WHERE singleton=1").fetchone()
    if row is None:
        raise ValueError("事件身份缺失，不能启动运行")
    return row[0]
