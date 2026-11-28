# CLAUDE.md

Diagnosis-Locked Receivables Agent. Python 3.12, package `dlra`, SQLite via stdlib.

```
uv sync
uv run pytest -q
uvx ruff check dlra tests
uv run python -m dlra.cli case.json     # {"bundle": CaseBundle, "diagnosis": Diagnosis, "now": ISO+offset}
uv run python -m scripts.replay_demo    # six demo cases end to end (scripted diagnosis, no mail)
uv run python -m scripts.make_demo_data # rebuild data/demo/ from data/demo/world.yaml
```

## Lanes

- A (engine): `db.py schema.sql events.py engine.py rules.py policy.py gates.py reconcile.py promises.py
  scheduler.py outbox.py mcp_server.py` and their tests.
- B (intelligence, surfaces): `llm.py extract.py diagnose.py draft.py prompts/ eval/ web/` and integrations.
- C (data, content): `data/demo/`, holdout set, CSS polish, README prose, video. C never edits `rules.py`,
  `policy.py` or `prompts/`.

## Plugging in (lanes B and C)

- Diagnosis: pass `propose(bundle) -> Diagnosis` in `service.Deps`. Drafting: `draft(intent, bundle, facts) ->
  (subject, body, facts_text)`, replacing `engine.stub_draft`.
- Extraction and the buyer page write evidence through `service.record_input` and payments through
  `service.record_payment`, then call `service.dispatch`. The evidence field contract is at the top of
  `dlra/loader.py`. Never store an ungrounded extraction under those field names.
- Web: include `api.build_router(db_path, deps)`; start jobs with `scheduler.start(db_path, deps, mailer.send)`.
  Supplier decisions go through `service.decide_card`.
- `scripts/demo_world.py` is a stand-in for `diagnose.py` and for reading `data/demo/` (nothing parses those
  files yet). `tests/test_demo_data.py` keeps the two in agreement.
- Demo sources: edit `data/demo/world.yaml`, rerun the generator, commit the output. Never edit generated files.
- `eval/scenario_spec.yaml` is draft 1, for B to review. The holdout set is written by hand after the freeze,
  by a person who wrote neither the rules nor the prompts.

## Invariants

- `dlra/models.py` is the shared interface. Change it only with the team.
- Money is integer paise. Timestamps are stored in UTC.
- Every action goes through `policy.authorise` (pure). The LLM never picks an action or a date.
- Rule IDs in `rules.py` are numbered by position: append, never reorder.
- `events` is append-only (enforced by database triggers).
- All data is synthetic. The repo is public.
