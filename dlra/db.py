"""SQLite through the stdlib. Parameterised queries only."""
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Autocommit connection. Use `with transaction(conn)` for multi-statement writes.

    ponytail: one connection per thread; the web app and scheduler each open their own.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    return conn


class transaction:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, *_):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def utc_iso(when: datetime | None = None) -> str:
    return (when or datetime.now(UTC)).astimezone(UTC).isoformat()
