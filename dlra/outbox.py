"""The only path to sending. Every message is authorised again at send time."""
import sqlite3
from collections import Counter
from collections.abc import Callable
from datetime import datetime

from dlra import engine, events, loader
from dlra.db import transaction, utc_iso
from dlra.models import Diagnosis, Fallback, State, Verdict
from dlra.policy import Policy, ProposedAction, authorise

Send = Callable[[str, str, str], None]  # (to, subject, body); raises if it was not sent


def _recheck(conn, row, policy: Policy, now: datetime) -> tuple[Verdict, str]:
    case = conn.execute("SELECT state, diagnosis FROM cases WHERE id = ?", (row["case_id"],)).fetchone()
    if not row["action"] or not case or not case["state"] or not case["diagnosis"]:
        return Verdict(allowed=False, rule_id="G-DIAG", reason="No diagnosed case behind this message."), ""
    bundle = loader.load_bundle(conn, row["case_id"], now, exclude_message_id=row["id"])
    context = engine.context_for(
        bundle, Diagnosis.model_validate_json(case["diagnosis"]), State(case["state"]), policy, now,
        contact_id=row["contact_id"] or "",
    )
    proposed = ProposedAction(
        action=row["action"], text=f"{row['subject']}\n{row['body']}", facts_text=row["facts_text"] or ""
    )
    return authorise(context, proposed), context.contact.email if context.contact else ""


def flush(conn: sqlite3.Connection, policy: Policy, now: datetime, send: Send) -> Counter:
    """ponytail: one flusher at a time (scheduler max_instances=1). Claim rows first if there are ever two."""
    tally = Counter()
    for row in conn.execute("SELECT * FROM messages WHERE status = 'queued' ORDER BY rowid").fetchall():
        verdict, to = _recheck(conn, row, policy, now)
        if verdict.allowed:
            try:
                send(to, row["subject"], row["body"])
            except Exception:  # noqa: BLE001 - stays queued and is retried; one bad address must not stall the queue
                tally["failed"] += 1
                continue
            with transaction(conn):
                conn.execute(
                    "UPDATE messages SET status = 'sent', sent_at = ? WHERE id = ?", (utc_iso(now), row["id"])
                )
                events.append(conn, "message_sent", "outbox", {"message_id": row["id"]}, f"sent:{row['id']}",
                              row["case_id"], produced_by_agent=True, ts=now)
            tally["sent"] += 1
        elif verdict.fallback == Fallback.QUEUE:
            tally["queued"] += 1
        else:
            with transaction(conn):
                conn.execute(
                    "UPDATE messages SET status = 'blocked', rule_id = ? WHERE id = ?", (verdict.rule_id, row["id"])
                )
                engine._open_card(conn, row["case_id"], engine.Card(
                    "contact_blocked", f"Held back at send time: {verdict.reason}", verdict.rule_id,
                    verdict.evidence_ids, {"message_id": row["id"]},
                ))
            tally["blocked"] += 1
    return tally
