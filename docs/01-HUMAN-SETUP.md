# Human setup checklist (do these while agents build)

Owner column: A = Akash, P = partner. Tick as done; write answers inline — agents read this file.

## Before 13:30 (unblocks issues #3 #4 #6 #9)
- [ ] **A** Anthropic key in `.env`; run `curl https://api.anthropic.com/v1/models -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01"` → paste the exact Haiku and Sonnet ids here: `LLM_MODEL_BATCH=______` `LLM_MODEL_AGENT=______`
- [ ] **P** Neo4j Aura Free instance created; URI/user/pass in `.env`; confirm `neo4j+s://` connects from laptop; open Explore (Bloom) once.
- [ ] **A** GitHub classic PAT (`public_repo`) in `.env`.
- [ ] **P** Organizers' PS-3 sample dataset → `data/private/registrations.<ext>`; write `mapping.yaml` from its real headers (copy `mapping.sample.yaml`). Paste the header list here: `______`
- [ ] **P** Does the sample include check-in / previous-event / team data? `______` If yes → `data/private/prev_event.csv` and note the columns.

## Ask the organizer (before lunch ends) — write answers verbatim
- [ ] Exact logic of the GitHub script: keywords? name only or description/topics? forks counted? any commit threshold? → update `scoring.yaml: baseline`. Answer: `______`
- [ ] How many registrations last time, and roughly what % had GitHub handles? Answer: `______`
- [ ] Do they keep check-in lists from previous events? Answer: `______`

## 13:30–14:15
- [ ] **A** Cognee spike in parallel with issue #9: `pip install cognee fastembed`; set env (see `.env.example`) **before** `import cognee`; `remember("Rohan no-showed at Bangalore")`, `recall("who no-showed?")`; record timings here: remember `__s`, recall `__s`, works with fastembed? `__`. If >15s or broken → `MEMORY_BACKEND=sqlite` and Cognee becomes a slide.
- [ ] **P** Render: upgrade workspace to Hobby (credits cover it), `pip install render`, deploy Render's tutorial workflow once. If not deployed by 14:30 → #13 is a slide.
- [ ] **A** `cloudflared tunnel --url http://localhost:7777` → paste URL into `PUBLIC_BASE_URL`; create Slack slash command `/ignu` → `${PUBLIC_BASE_URL}/slack/command` (only if #12 is alive at 14:30).

## 14:15–15:00
- [ ] Run `scripts/prerun.py` on the full sample; check `llm_usage` spend (target < $3).
- [ ] Slides (5) for mentoring, mapped to criteria: 1 Problem (organizer's manual screening + script), 2 Design decisions (sources table incl. LinkedIn skip; Neo4j/Cognee honest position), 3 the disagreement table (screenshot), 4 Scalability (Workflows fan-out, cache, budget guard, mapping.yaml generality), 5 Scope cuts made today.

## 15:00–15:30
- [ ] `scripts/reset_demo.py`; rehearse the 4-minute loop twice; blur names in any screenshot.

## After mentoring (if shortlisted)
- [ ] Deploy web app to Oracle VM (Caddy + systemd + venv, runbook from commits-dont-lie) with `MEMORY_BACKEND=sqlite`, `GRAPH_ENABLED=true`; this is the "production link" for judging.
