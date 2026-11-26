"""Deadlines. The scheduler only appends events and wakes the engine or the outbox.

It never imports the mailer: whoever starts it hands the outbox its send function,
and the outbox re-authorises every message.
"""
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from dlra import events, loader, outbox, promises, service
from dlra.db import connect
from dlra.models import PromiseStatus
from dlra.policy import Policy


def scan_deadlines(conn: sqlite3.Connection, policy: Policy, now: datetime) -> int:
    """Append a deadline_reached event for everything that has fallen due. Dedup makes repeats free."""
    today = now.astimezone(ZoneInfo(policy.business_hours.tz)).date()
    added = 0

    def emit(case_id: str, kind: str, day: date, subject: str = "", **payload) -> None:
        nonlocal added
        key = events.deadline_key(case_id, f"{kind}{':' if subject else ''}{subject}", day.isoformat())
        added += bool(events.append(
            conn, "deadline_reached", "scheduler", {"deadline": kind, **payload}, key, case_id, ts=now
        ))

    for case in conn.execute("SELECT id, invoice_id, safe_pause_until FROM cases WHERE state IS NOT 'RESOLVED'").fetchall():
        case_id = case["id"]
        if case["safe_pause_until"] and datetime.fromisoformat(case["safe_pause_until"]) <= now:
            emit(case_id, "safe_pause", datetime.fromisoformat(case["safe_pause_until"]).date())

        promise, baseline = loader.load_promise(conn, case_id)
        if promise and promise.status == PromiseStatus.ACTIVE:
            for item in promise.installments:
                if item.due_date + timedelta(days=policy.promise_grace_days + 1) <= today:
                    emit(case_id, "promise", item.due_date)
            paid = loader.confirmed_totals(conn)[0].get(case["invoice_id"], 0) - baseline
            upcoming = promises.next_due(promise, paid)
            if upcoming and 0 <= (upcoming - today).days <= policy.reminder_window_days:
                emit(case_id, "reminder_window", upcoming)

        closed = None
        evidence = loader.case_evidence(conn, case_id)
        for item in evidence:
            if item.field == "dispute_closed" and item.source_type == "human":
                closed = item.ts
        for item in evidence:
            if item.status not in loader.LIVE:
                continue
            if item.field == "payment_report":
                fires = item.ts + timedelta(days=policy.payment_report_verify_days)
                if fires <= now:
                    emit(case_id, "payment_report", fires.date(), item.id, evidence_id=item.id)
            elif item.field == "dispute" and (closed is None or item.ts > closed):
                fires = item.ts + timedelta(days=policy.dispute_response_days)
                if fires <= now:
                    emit(case_id, "dispute_response", fires.date(), item.id)
    return added


def tick(conn: sqlite3.Connection, deps: service.Deps, now: datetime):
    """Scan, then wake the engine for anything pending (deadlines or any missed input)."""
    scan_deadlines(conn, deps.policy, now)
    return service.dispatch(conn, deps, now)


def release_stale_runs(db_path: str) -> None:
    """At startup nothing is running, so a flag left by a killed process would lock its case for good.

    ponytail: assumes one service process; a lease with a timestamp if there are ever two.
    """
    conn = connect(db_path)
    try:
        conn.execute("UPDATE cases SET running = 0, rerun_pending = 0")
    finally:
        conn.close()


def start(db_path: str, deps: service.Deps, send: outbox.Send, minutes: int = 5) -> BackgroundScheduler:
    tz = ZoneInfo(deps.policy.business_hours.tz)

    def job(work):
        def run():
            conn = connect(db_path)  # SQLite connections stay on the thread that made them
            try:
                work(conn, datetime.now(tz))
            finally:
                conn.close()

        return run

    release_stale_runs(db_path)
    scheduler = BackgroundScheduler(timezone=tz, job_defaults={"max_instances": 1, "coalesce": True})
    scheduler.add_job(job(lambda c, now: tick(c, deps, now)), "interval", minutes=minutes, id="deadlines")
    scheduler.add_job(
        job(lambda c, now: outbox.flush(c, deps.policy, now, send)), "interval", minutes=minutes, id="outbox"
    )
    scheduler.start()
    return scheduler

