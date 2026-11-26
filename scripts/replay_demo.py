"""Feed the six demo cases through events -> engine -> outbox.

    uv run python -m scripts.replay_demo [--step] [--db demo.db]

Diagnoses are scripted until diagnose.propose exists. Nothing is emailed.
"""
import argparse
import sys
from pathlib import Path

from dlra import events, outbox, service
from dlra.db import connect
from dlra.engine import describe
from dlra.policy import load_policy
from scripts import demo_world as world


def replay(conn, deps: service.Deps, say=print, pause=lambda: None) -> list[tuple[str, str, str]]:
    """Returns what was sent as (to, subject, body)."""
    sent = []
    for label, kind, source, case_id, evidence in world.inputs():
        pause()
        if case_id == "K-1052":
            world.queue_old_reminder(conn)
        if case_id == "K-1060":  # the bank line lands first, then the buyer's report
            line = world.bank_line()
            service.record_payment(
                conn, line, events.bank_key(str(line.value_date), line.amount_paise, line.narration, line.id),
                now=world.NOW,
            )
        service.record_input(conn, kind, source, f"demo:{case_id}", case_id, evidence, now=world.NOW)
        runs = service.dispatch(conn, deps, world.NOW)
        tally = outbox.flush(conn, deps.policy, world.NOW, lambda *message: sent.append(message))
        say(f"== {label} ({case_id})")
        # the bank line and the report each wake the case; the last run is the current picture
        say(describe(runs[-1][1]))
        for row in conn.execute(
            "SELECT intent, rule_id FROM messages WHERE case_id = ? AND status = 'blocked'", (case_id,)
        ):
            say(f"  blocked  queued {row['intent']} [{row['rule_id']}]")
        say(f"  outbox   {dict(tally) or 'nothing to send'}\n")
    return sent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=":memory:", help="SQLite file; rebuilt from scratch")
    parser.add_argument("--step", action="store_true", help="wait for Enter before each case")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.db != ":memory:":
        path = Path(args.db)
        if path.exists() and path.suffix != ".db":
            parser.error("refusing to overwrite a file that is not a .db")
        path.unlink(missing_ok=True)
    conn = connect(args.db)
    world.seed(conn)
    deps = service.Deps(policy=load_policy(), propose=world.scripted_diagnosis)
    sent = replay(conn, deps, pause=(lambda: input("Enter to continue")) if args.step else (lambda: None))
    print(f"{len(sent)} messages handed to the (fake) mailer: {', '.join(to for to, _, _ in sent)}")


if __name__ == "__main__":
    main()
