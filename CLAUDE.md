# CLAUDE.md

Diagnosis-Locked Receivables Agent. Python 3.12, package `dlra`, SQLite via stdlib.

```
uv sync
uv run pytest -q
uvx ruff check dlra tests
uv run python -m dlra.cli case.json     # {"bundle": CaseBundle, "diagnosis": Diagnosis, "now": ISO+offset}
```

## Lanes

- A (engine): `db.py schema.sql events.py engine.py rules.py policy.py gates.py reconcile.py promises.py
  scheduler.py outbox.py mcp_server.py` and their tests.
- B (intelligence, surfaces): `llm.py extract.py diagnose.py draft.py prompts/ eval/ web/` and integrations.
- C (data, content): `data/demo/`, holdout set, CSS polish, README prose, video. C never edits `rules.py`,
  `policy.py` or `prompts/`.

## Invariants

- `dlra/models.py` is the shared interface. Change it only with the team.
- Money is integer paise. Timestamps are stored in UTC.
- Every action goes through `policy.authorise` (pure). The LLM never picks an action or a date.
- Rule IDs in `rules.py` are numbered by position: append, never reorder.
- `events` is append-only (enforced by database triggers).
- All data is synthetic. The repo is public.
