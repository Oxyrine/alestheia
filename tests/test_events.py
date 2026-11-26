import re
import sqlite3
from pathlib import Path

import pytest

from dlra import events
from dlra.db import connect


def add(conn, kind="email_received", key="email:<1@x.example>", **kw):
    return events.append(conn, kind, "test", {}, key, **kw)


def test_duplicate_is_counted_once_and_triggers_nothing():
    conn = connect()
    assert add(conn)["triggers_run"] == 1
    assert add(conn) is None
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


@pytest.mark.parametrize("kind", sorted(events.LOGGED_ONLY_KINDS))
def test_logged_only_kinds_never_trigger(kind):
    assert add(connect(), kind=kind)["triggers_run"] == 0


@pytest.mark.parametrize("kind", sorted(events.TRIGGERING_KINDS))
def test_loop_guard_agent_events_never_trigger(kind):
    assert add(connect(), kind=kind, produced_by_agent=True)["triggers_run"] == 0


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        add(connect(), kind="surprise")


@pytest.mark.parametrize("sql", ["UPDATE events SET kind = 'x'", "DELETE FROM events"])
def test_events_are_immutable(sql):
    conn = connect()
    add(conn)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(sql)


def test_bank_key_depends_on_every_part():
    base = ("2026-11-20", 100, "NEFT", "R1")
    keys = {events.bank_key(*base)} | {
        events.bank_key(*base[:i], "other" if i != 1 else 101, *base[i + 1:]) for i in range(4)
    }
    assert len(keys) == 5


def test_no_sql_is_built_with_fstrings():
    pattern = re.compile(r"""\bf["']{1,3}[^"']*\b(SELECT|INSERT INTO|UPDATE|DELETE FROM)\b""")
    for source in Path(__file__).resolve().parent.parent.joinpath("dlra").rglob("*.py"):
        assert not pattern.search(source.read_text(encoding="utf-8")), source.name
