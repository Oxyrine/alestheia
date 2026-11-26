"""The immutable event log: deduplication and the loop guard."""
import hashlib
import json
import sqlite3
from datetime import datetime

from dlra.db import new_id, utc_iso

TRIGGERING_KINDS = frozenset({
    "email_received", "buyer_submission", "payment_received", "bank_line_added", "human_decision",
    "evidence_changed", "deadline_reached",
})
LOGGED_ONLY_KINDS = frozenset({"message_sent", "run_completed", "card_created", "decision_recorded"})


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def email_key(message_id: str) -> str:
    return f"email:{message_id}"


def buyer_key(idempotency_uuid: str) -> str:
    return f"buyer:{idempotency_uuid}"


def razorpay_key(event_id: str) -> str:
    return f"rzp:{event_id}"


def bank_key(value_date: str, amount_paise: int, narration: str, ref: str) -> str:
    return "bank:" + _sha(json.dumps([value_date, amount_paise, narration, ref]))


def card_key(card_id: str, decision: str, decided_at: str) -> str:
    return f"card:{card_id}:{decision}:{decided_at}"


def deadline_key(case_id: str, kind: str, day: str) -> str:
    return f"deadline:{case_id}:{kind}:{day}"


def file_key(content: bytes) -> str:
    return "file:" + hashlib.sha256(content).hexdigest()


def append(
    conn: sqlite3.Connection,
    kind: str,
    source: str,
    payload: dict,
    dedup_key: str,
    case_id: str | None = None,
    produced_by_agent: bool = False,
    ts: datetime | None = None,
) -> sqlite3.Row | None:
    """Returns the new event row, or None for a duplicate: counted once, triggers nothing."""
    if kind not in TRIGGERING_KINDS | LOGGED_ONLY_KINDS:
        raise ValueError(f"unknown event kind: {kind}")
    triggers = kind in TRIGGERING_KINDS and not produced_by_agent
    return conn.execute(
        "INSERT INTO events (id, ts, source, kind, dedup_key, case_id, payload, produced_by_agent, triggers_run) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (dedup_key) DO NOTHING RETURNING *",
        (new_id("EVT"), utc_iso(ts), source, kind, dedup_key, case_id, json.dumps(payload),
         int(produced_by_agent), int(triggers)),
    ).fetchone()
