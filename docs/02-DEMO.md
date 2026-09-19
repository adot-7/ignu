# Demo runbook (filled in by issue #15)

Target: 4 minutes, laptop, dashboard at http://localhost:7777, chat panel on the right.

1. `python scripts/reset_demo.py` (restores pre-note state; < 5 s)
2. `python scripts/demo_slice.py --n 15 --slow 0.6` — stage boxes light up; counters climb.
3. Point at the disagreement table; read two rows aloud (one fork-farmer demoted, one original builder promoted).
4. Chat: "Rohan Verma no-showed at HackArena Bangalore after being approved." → watch verdict change.
5. Chat: "Who else here was at Bangalore and how did they do?"
6. Chat: "Form teams of 3 from admitted solos." → teams with coverage bars.
7. If asked: open Neo4j Explore → the same people as a graph.

Issue #15 replaces this with exact wording, expected outputs, and fallbacks per step.
