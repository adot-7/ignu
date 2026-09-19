# AGENTS.md — read this first

You are one of several coding agents working **in parallel** on this repo during a one-day hackathon
(Ignite With Delhi, 19 Sept 2026, PS-3 "Universal Context Layer"). Humans are setting up infra and
talking to organizers while you build. Deadline for a demo-able hero loop: **15:30 IST**.

## Ground rules

1. **Read `docs/00-SPEC.md` fully before writing code.** It defines the contracts (models, DB, events,
   config, memory interface). Contracts are frozen after issue #1 merges. If you believe a contract is
   wrong, leave a comment in your PR and implement the contract anyway.
2. **You own only the files listed in your issue.** Do not edit files owned by another issue. If you need
   something from another lane that doesn't exist yet, write a stub in *your* module and note it.
3. **Every pipeline stage emits `PipelineEvent`s** via `app.events.emit()`. The dashboard is only a consumer.
4. **Never call the LLM in a loop without going through `app.llm.structured()`.** It enforces the budget
   guard (`LLM_BUDGET_USD`) and the model split (Haiku for batch, Sonnet for the agent). Total Anthropic
   budget for the whole day is **$7.50**. Blowing it kills the demo.
5. **Never run the full dataset live.** `scripts/prerun.py` runs everything once with caching;
   `scripts/demo_slice.py` replays 15 people. GitHub responses and LLM outputs are cached on disk under
   `data/cache/` keyed by input hash.
6. **PII:** `data/private/` is gitignored and holds the organizers' real registrations. Tests and fixtures
   use `data/sample_registrations.csv` (fake). Never log full emails; log `email_domain` and a hash.
7. **Degrade, don't crash.** No GitHub handle → `evidence_level="none"`. GitHub 403/404 → same. LLM failure →
   retry once, then mark the person `needs_human` with reason. Neo4j unreachable → `GRAPH_ENABLED=false`
   path. Cognee unavailable → `MEMORY_BACKEND=sqlite`.
8. **Every claim about a person carries a source.** Profiles cite evidence; the agent answers only from
   tool results; use "signals that moved with" — never "caused".
9. **Tests:** `pytest -q` must stay green. Add at least one test per module you own, using fixtures under
   `tests/fixtures/` (recorded GitHub JSON, fake CSV rows). No network in tests.
10. **Small PRs, one issue each, branch name `issue-<n>-<slug>`.** Merge order is in the spec §9.

## Stack (fixed)

Python 3.11 · FastAPI · SQLAlchemy 2 (SQLite, `DATABASE_URL` swappable) · pydantic v2 · `anthropic` SDK ·
`httpx` · `rapidfuzz` · `neo4j` driver · `cognee` (behind `app/memory.py` interface, optional) ·
`render` SDK (optional lane) · plain HTML+JS dashboard with SSE (no build step).

## Commands

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # humans fill keys
pytest -q
uvicorn app.main:app --reload --port 7777
python scripts/prerun.py data/private/registrations.xlsx --mapping mapping.yaml
python scripts/demo_slice.py --n 15 --slow 0.6
```
