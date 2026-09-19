# ignu — an evolving context layer for hackathon organizers

**Ignite With Delhi · 19 Sept 2026 · PS-3 "Universal Context Layer" · Team: Akash Parashar + partner**

Organizers have a registrations spreadsheet and a script that counts GitHub repos matching "AI".
ignu is the layer above that: it reads the evidence (fork or original? commits by *them*? what does the
README actually describe?), builds a versioned profile per person, ranks the cohort with reasons,
forms balanced teams, and **remembers organizer feedback so the next event's verdicts change**.

The pitch in one line: *your script tells you how many AI repos someone has; ignu tells you whether they
built them, whether they'll show up, and who they should sit next to — and it remembers next time.*

- `docs/00-SPEC.md` — the design, contracts, scoring, cut order (read first)
- `docs/01-HUMAN-SETUP.md` — what the humans set up while agents build
- `docs/02-DEMO.md` — the 4-minute hero loop (issue #15 fills this in)
- `AGENTS.md` — rules for coding agents working in parallel on this repo

## Run

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env   # fill keys
pytest -q
uvicorn app.main:app --reload --port 7777      # dashboard at http://localhost:7777
python scripts/prerun.py data/private/registrations.xlsx --mapping mapping.yaml
python scripts/demo_slice.py --n 15 --slow 0.6
```

Sponsors used: **Neo4j Aura** (people/skills/events/teams graph, Bloom view), **Cognee** (organizer-note
memory, behind a fallback), **Render Workflows** (per-person profile build fan-out; local runner fallback).
