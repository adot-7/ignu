# ignu — Specification (v1, 19 Sept 2026 13:05 IST)

## 0. Situation

- PS-3 "Universal Context Layer": build a layer above a platform's raw DB that *continuously* builds a
  structured understanding of each user; expose via a conversational agent; deterministic existence checks;
  generalizable design. Bonus: matchmaking / downstream app. Judged on use-case clarity, context quality,
  agent quality, architecture, bonus execution.
- Mentoring (eliminatory) 15:30: 5-min pitch + 2-min Q&A on Problem Clarity, Design Decisions, Scalability,
  Technical Implementation, Scope & Prioritisation. Final 17:00: 1-min pitch, 2-min demo, 2-min Q&A on
  Production standards, Technical Understanding, System Architecture, Completeness, Reliability.
- The customer is in the room: Ignite Room screens registrations by hand plus a script that counts GitHub
  repos matching AI keywords. Organizers sent a sample registrations dataset to all PS-3 teams.
- Constraints: 2 humans + parallel coding agents; ~2.5h to a demo-able loop; **$7.50 total Anthropic
  budget**; Neo4j Aura Free; Cognee (OSS, self-hosted); Render credits; Oracle E2.1.Micro VM (1 GB) known-good.

## 1. Use case and what "context" means here

**Platform:** a hackathon organizer's registration + participation history (Ignite Room).
**Users:** registrants. **Askers:** organizers.

| Source | Signals | Justification |
|---|---|---|
| Registration file (platform-native) | name, email → domain, org/role, student flag, free-text answers, registered_at | eligibility + intent; it is what the organizer already has |
| Previous events (platform-native) | registered, approved, checked_in / no_show, submitted, placed, team membership | behaviour over time; reliability |
| GitHub (external, deterministic by handle) | repos: fork?, author commits (90d/total), language, last push, README | evidence of what they actually build; only external source with timestamps |
| Organizer / mentor notes (platform-native, unstructured) | "team left after lunch", "override: she is a professional" | the feedback that must change future verdicts — the "evolves" requirement |
| LinkedIn | **skipped deliberately** | no API, scraping violates ToS, unbuildable reliably in a day; URL stored as a link for the human |
| Web search on name+company | **skipped** | recall lottery; absence proves nothing; cannot drive a verdict |

## 2. Why this is a step deeper than the organizer's script (the pitch)

| Script | ignu | Tool that makes it real |
|---|---|---|
| counts repos whose name matches keywords | reads evidence: fork vs original, commits *by the author*, recency, README meaning → typed `Evidence` with confidence | LLM structured synthesis (Anthropic Haiku, batch) |
| scores individuals | cohort decisions: alias/duplicate collapse, returning participants, team skill coverage, who-worked-with-whom | **Neo4j** — relationship queries and a legible graph |
| forgets between events | organizer notes + overrides persist and change verdicts; trajectory across profile versions | **Cognee** (memory adapter) + versioned profiles |
| outputs a number | verdict + reasons + sources + `needs_human` bucket | agent design |

**Visible proof, not assertion:** the *disagreement table* — baseline rank vs ignu rank, side by side,
with the reason strings, on the same data every other team received.

### Honest position on Neo4j and Cognee (say this if asked)
- The hero loop runs on SQLite without either. Neo4j is kept as the system of record for
  relationships because alias detection, returning-participant history and team coverage are graph queries
  and the graph is what makes a 500-person cohort legible. Cognee is kept **only** for unstructured
  feedback memory, behind an interface with a SQLite fallback, because that is the one input it is shaped
  for. If Cognee's spike (issue #9) fails, the fallback ships and the slide says "memory adapter".

## 3. Hero workflow (mentoring demo, ~4 min)

1. Drop the registrations file → ingest counters: rows, with-GitHub %, aliases collapsed.
2. Live slice of 15 people; stage boxes light up per person (GitHub → evidence → profile → graph → rank).
   The other N are pre-run and cached.
3. Disagreement table: baseline vs ignu, top rows with reasons.
4. Chat: *"Rohan Verma no-showed at HackArena Bangalore after being approved."* → memory write →
   re-rank → Rohan moves to `needs_human`, note cited. *"Who else here was at Bangalore, how did they do?"*
5. Chat: *"Form teams of 3 from admitted solos."* → teams with coverage bars and a one-line why.

## 4. Architecture

```
registrations.xlsx/csv ──mapping.yaml──► ingest ──► persons (SQLite)
prev_event.csv ─────────────────────────► participations
                                              │
                        ┌─────────────────────┴─────────────────────┐
                        ▼                                           ▼
             github_evidence (httpx, cache)                 baseline (keyword count)
                        ▼
             profile (Haiku structured call, versioned)
                        ▼
             graph (Neo4j MERGE)  ◄── memory notes attach to Person
                        ▼
             rank (scoring.yaml) ──► verdicts + disagreements
                        ▼
             teams (greedy coverage)
                        ▼
   events bus (PipelineEvent) ──► SSE /events ──► dashboard (index.html)
                                                  └► chat panel ──► POST /ask ──► agent (Sonnet, tools only)
   slack.py (/ignu) ─────────────────────────────────────────────────┘
   workflows.py: build_all → fan-out build_person (Render Workflows | local thread pool)
```

Runs on the laptop for the demo (dashboard at localhost:7777, screenshot-style UI). Public URL for Slack via
cloudflared. Judging "production link": deploy the web app to the Oracle VM after mentoring
(Caddy + systemd + venv runbook from the previous project); Cognee stays off on the 1 GB VM
(`MEMORY_BACKEND=sqlite` there) — say so.

## 5. Contracts (frozen after issue #1)

### 5.1 Models — `app/models.py` (pydantic v2)

```python
Skill = Literal["frontend","backend","ml_ai","data","mobile","devops_cloud","design_product","pitch_comms"]
EvidenceLevel = Literal["none","thin","solid","strong"]
Decision = Literal["admit","waitlist","decline","needs_human"]
Stage = Literal["ingest","github","profile","graph","rank","team","memory","agent"]

class Person(BaseModel):
    id: str                      # sha1(email.lower()) [:12]
    name: str
    email: str
    email_domain: str
    github_login: str | None
    linkedin_url: str | None
    org: str | None
    role: str | None
    is_student: bool | None
    answers: dict[str, str]
    registered_at: datetime | None
    alias_of: str | None         # set when merged into another person
    source_file: str
    source_row: int

class Participation(BaseModel):
    person_id: str; event: str; registered: bool; approved: bool | None
    checked_in: bool | None; submitted: bool | None; placed: int | None; team_id: str | None; at: date | None

class RepoEvidence(BaseModel):
    full_name: str; html_url: str; is_fork: bool; stars: int
    author_commits_90d: int; author_commits_total: int
    primary_language: str | None; last_push: datetime | None
    description: str | None; topics: list[str]; readme_excerpt: str | None
    ai_relevance: float = 0.0    # filled by profile step

class Evidence(BaseModel):
    claim: str; source_url: str
    kind: Literal["repo","commit","readme","registration","participation","note"]
    confidence: float; observed_at: date

class SkillScore(BaseModel):
    skill: Skill; confidence: float

class Profile(BaseModel):
    person_id: str; version: int; built_at: datetime
    skills: list[SkillScore]
    evidence_level: EvidenceLevel
    original_work_score: float   # 0..1, deterministic from RepoEvidence (see §6.1)
    ai_relevance: float          # 0..1, LLM-graded
    reliability: float | None    # from participations; None if unknown
    summary: str                 # <= 80 words; each sentence traceable to evidence
    evidence: list[Evidence]
    model_used: str; input_tokens: int; output_tokens: int

class Verdict(BaseModel):
    person_id: str; decision: Decision; score: float
    eligibility: Literal["pass","fail","unknown"]
    baseline_score: float; baseline_rank: int; ignu_rank: int
    reasons: list[str]; evidence_ids: list[int]; profile_version: int; at: datetime

class Team(BaseModel):
    id: str; event: str; member_ids: list[str]
    coverage: dict[Skill, float]; balance: float; why: str

class Note(BaseModel):
    id: int | None; person_id: str | None; text: str; author: str; at: datetime
    kind: Literal["observation","flag","override","praise"]
    source: Literal["chat","slack","import"]

class PipelineEvent(BaseModel):
    ts: datetime; run_id: str; stage: Stage; person_id: str | None
    status: Literal["start","ok","skip","error"]; msg: str; data: dict | None = None
```

### 5.2 DB — `app/db.py`
SQLAlchemy 2, tables mirror the models: `persons, participations, repos, profiles (versioned, never
updated), verdicts (append-only, latest wins), teams, notes, events, llm_usage`. `DATABASE_URL` from env.
`get_session()` context manager. Alembic not used — `init_db()` creates tables.

### 5.3 Events bus — `app/events.py`
`emit(event: PipelineEvent)` → persists to `events` and fans out to async subscribers.
`subscribe() -> AsyncIterator[PipelineEvent]`. `GET /events` (SSE) streams JSON lines; heartbeat every 10s.

### 5.4 Config — `app/config.py`
`pydantic-settings` `Settings` reading `.env` (§ .env.example). Loads `mapping.yaml` and `scoring.yaml`
into typed models `Mapping` and `Scoring`.

### 5.5 LLM — `app/llm.py`
```python
def structured(prompt: str, schema: type[T], *, tier: Literal["batch","agent"], cache_key: str | None) -> T
def chat_with_tools(messages, tools, *, tier="agent") -> AnthropicResponse
```
- Anthropic SDK. `tier="batch"` → `LLM_MODEL_BATCH`; `tier="agent"` → `LLM_MODEL_AGENT`.
- Structured output via a single forced tool call whose input schema is `schema.model_json_schema()`;
  validate with pydantic; on validation error retry once with the error appended.
- **Budget guard:** every call records tokens to `llm_usage`; cost computed from `LLM_PRICE_TABLE`;
  if cumulative ≥ `LLM_BUDGET_USD` raise `BudgetExceeded` (callers mark `needs_human`, never crash).
- **Cache:** if `cache_key` given, hit `data/cache/llm/<sha>.json` first. Prerun sets cache keys =
  sha(person_id + evidence hash + prompt version).
- Startup: `GET /v1/models` to verify both ids exist; log the cheapest available Haiku if configured id is
  missing and fall back to it.

### 5.6 Memory interface — `app/memory.py`
```python
class Memory(Protocol):
    def add_note(self, note: Note) -> None
    def recall(self, question: str, person_id: str | None = None, k: int = 5) -> list[MemoryHit]
class MemoryHit(BaseModel): text: str; source: str; score: float; person_id: str | None; at: datetime | None
def get_memory() -> Memory   # by MEMORY_BACKEND
```
`add_note` must: persist to `notes`, attach to Neo4j `(:Note)` if graph enabled, emit `memory` event,
and call `rank.rerank_person(person_id)`.

### 5.7 Graph — `app/graph.py`
Labels/edges (all MERGE, idempotent):
`(:Person {id,name,email_domain})`, `(:GitHubAccount {login})`, `(:Skill {name})`, `(:Event {name})`,
`(:Team {id})`, `(:Note {id,kind,at})`, `(:Org {domain})`.
`(p)-[:HAS_GITHUB]->(g)`, `(p)-[:HAS_SKILL {confidence}]->(s)`, `(p)-[:PARTICIPATED {checked_in,placed,at}]->(e)`,
`(p)-[:MEMBER_OF]->(t)-[:AT]->(e)`, `(p)-[:NOTED]->(n)`, `(p)-[:ALIAS_OF]->(p2)`, `(p)-[:FROM]->(o)`.
Queries: `aliases()`, `returning(event) -> [(person_id, history)]`, `skill_coverage(member_ids)`,
`prior_teammates(person_id)`, `cohort_skill_histogram()`.

### 5.8 Mapping — `mapping.yaml` (see `mapping.sample.yaml`). Loader: `app/mapping.py::load(path) -> Mapping`,
`apply(row: dict, mapping) -> Person`.

### 5.9 HTTP API — `app/main.py`
`GET /` dashboard · `GET /events` SSE · `GET /api/state` (counters, disagreements, teams, last run) ·
`POST /api/run {file?, n?, slow?}` starts a pipeline run · `POST /ask {question, channel}` → agent ·
`POST /api/notes` · `GET /api/person/{id}` · `POST /slack/command`.

## 6. Scoring (deterministic; all constants in `scoring.yaml`)

### 6.1 original_work_score (0..1), per person, from RepoEvidence of top-3 repos by author commits
```
repo_score = (0 if is_fork and author_commits_total < 5 else 1)
           * min(1, log1p(author_commits_total)/log1p(50)) * 0.6
           + min(1, author_commits_90d/20) * 0.3
           + (0.1 if last_push within 180d else 0)
original_work_score = clamp(mean(top3 repo_score) * (0.7 + 0.1*min(3, n_repos_non_fork)), 0, 1)
```
evidence_level: none (no GitHub/403/404) · thin (only forks or <5 commits) · solid (≥1 original repo
with ≥10 author commits) · strong (≥2 such repos or ≥50 commits in 90d).

### 6.2 reliability: from participations: `checked_in/approved` ratio with Laplace smoothing; None if no history.
### 6.3 trajectory: `(original_work_score_v_latest - v_prev)` scaled to 0..1 around 0.5; 0.5 if single version.
### 6.4 score = Σ weights × components (None → 0.5 neutral). Eligibility gate first (see scoring.yaml).
### 6.5 baseline: count of repos where any keyword ∈ name/description/topics (forks counted) → rank desc.
### 6.6 disagreements: rows with |baseline_rank − ignu_rank| ≥ threshold, ordered by gap; each with ignu reasons.

## 7. Matchmaking (issue #8)
Input: admitted persons not already in a team. Team size k (default 3).
Greedy: seed = highest original_work_score unassigned; repeatedly add the candidate maximising
`Δcoverage − 0.3·|experience_gap| − 1.0·bad_pair_penalty + 0.2·prior_good_teammate`, where coverage =
Σ_skill max(confidence) over members, bad_pair from notes with kind=flag mentioning both, prior_good from
graph `prior_teammates` with placed ≤ 3. `why` generated by a Haiku call from the coverage facts
(≤ 30 words), cached.

## 8. Agent (issue #11)
Sonnet tool loop, max 6 tool calls. Tools: `exists(name) -> candidates[{person_id,name,score}]` (rapidfuzz
token_set_ratio ≥ 80 over name+email local-part; deterministic), `verdict(person_id)`, `profile(person_id)`,
`disagreements(limit)`, `returning(event)`, `form_teams(k)`, `add_note(person_name_or_id, text, kind)`,
`recall(question, person_id?)`. System prompt rules: answer only from tool results; every person-fact
sentence ends with a source tag `[repo|registration|participation|note]`; existence questions must call
`exists` first and report confidence; use "signals that moved with", never "caused"; if `exists` has no
candidate ≥ 80, say not found and list the top 2 near names.

## 9. Lanes, merge order, cut order
- **#1 Foundation** (blocking) → parallel: #2 ingest, #3 github, #4 profile, #5 baseline, #6 graph, #9 memory,
  #10 dashboard → #7 rank, #13 workflows, #14 prerun/demo → #8 teams, #11 agent → #12 slack, #15 demo doc,
  #16 hardening.
- **Cut order if behind at 14:15:** #12 Slack → #13 Render → #8 teams → Cognee backend (keep sqlite).
- Critical path: 1 → 3 → 4 → 7 → 11 → 15.

## 10. Reliability rules for the demo (what makes us "the reliable one")
1. Zero live network dependency in the hero loop except the note write and the chat: GitHub and LLM
   outputs cached by prerun; `demo_slice` replays with pacing.
2. `scripts/reset_demo.py` restores pre-note state in <5s; rehearse twice.
3. Budget guard prevents a runaway; dashboard shows spend so far.
4. Every stage has a `skip`/`error` path that keeps the run going and shows up as a coloured cell.
5. Agent cannot invent people: `exists` is deterministic and the prompt forbids untooled claims.
6. PII: private data gitignored; logs carry hashes; screenshots blurred.
7. Feature flags: `GRAPH_ENABLED`, `MEMORY_BACKEND`, `WORKFLOW_RUNNER` — every sponsor lane is
   removable without touching the loop.

## 11. Open questions for the humans (answer in docs/01-HUMAN-SETUP.md as you learn)
- Exact logic of the organizer's script (keywords, thresholds) → `scoring.yaml: baseline`.
- Headers of the organizers' sample dataset → `mapping.yaml`. Does it include check-in or prior-event data?
- Exact Anthropic model ids available on the key (`GET /v1/models`).
- Cognee: does `EMBEDDING_PROVIDER=fastembed` work with the installed version? Timing of `remember`/`recall`?
