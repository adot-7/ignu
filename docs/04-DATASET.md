# The organizers' dataset — what it actually contains (read before writing ingest/rank code)

File: `Dataset for PS-3 (Ignite Room).csv`, handed to every PS-3 team. Put it at
`data/private/registrations.csv` — it is **gitignored** because it holds 656 real names and
real LinkedIn URLs. Do not commit it, do not paste rows into issues, blur names in screenshots.

## 1. Shape (measured, not assumed)

656 rows, 6 columns:

| Column | Non-empty | Notes |
|---|---|---|
| `first_name` | 652 (99%) | 4 rows blank |
| `last_name` | 608 (93%) | |
| `What is your LinkedIn profile?` | 650 (99%) | 649 are `linkedin.com/in/...` |
| `What is your GitHub username?` | 647 (99%) | **all 647 are full `https://github.com/<login>` URLs**, never bare handles |
| `What company do you work for? (if student then write your college name)` | 656 (100%) | free text; college for students |
| `What is your job title?` | 656 (100%) | free text; `Student` x268 + `student` x19 + `STUDENT` x5 |

**Absent: email, registration answers, registered_at, checked_in, approval status, prior events,
teams, placements, any timestamp.**

## 2. Consequences for the contracts

1. **`Person.id` cannot be `sha1(email)`** — there is no email. Use
   `sha1(github_login.lower())[:12]` when a valid login exists, else `sha1(lower(name)|lower(org))[:12]`.
   `Person.email` / `email_domain` become `""` for this source; nothing may assume them non-empty.
2. **Eligibility cannot use email domain.** It comes from `role` + `org` text — see `scoring.yaml`.
   ~339 of 656 (52%) read as students; 310 non-students have a GitHub link.
3. **`reliability` has no source data** and **profile-version `trajectory` has no history** on a
   first run. Both were 40% of the old weights. See `scoring.yaml` for what replaced them.
4. **`answers` is empty**, so the profile synthesiser has *only* GitHub + self-declared role/org
   to work from. The no-GitHub path produces `evidence_level: none` with a one-line summary and
   **must not** call the LLM.

## 3. Real data-quality defects to detect (these are demo moments, not edge cases)

| Defect | Rows | Why it matters |
|---|---|---|
| `https://github.com/in` submitted as a GitHub username | 389 (Hemang Dutt Mishra), 607 (Krish Gaur) | Someone pasted a LinkedIn path. **A naive dedupe-by-login merges these two different people into one.** Treat `in` and other reserved paths as invalid → `evidence_level: none`, `needs_human`, never merge. |
| Same login, two rows | `somyagupta1122` (116/118), `pranavbatra10` (200/648), `sid0000007` (304/494) | True duplicate registrations. `pranavbatra10` declares **"Student" in one row and "Software developer" in the other** — the same person's self-description changed. That is the cheapest true "evolves over time" example in the file. |
| Same person, different spelling | `Siddharth Gupta` @ "Times of india" vs `Siddharth` @ "Times of India" | Case/whitespace-only differences; dedupe must normalise. |
| Duplicate full names | 11 | e.g. `Aditya Sharma`, `Deepak Kumar` — **different logins**, so they are different people. Never merge on name alone; flag only. |
| No GitHub at all | 9 rows incl. `SHOURYA PRADHAN` (Microsoft, Technical Intern), `Sonia Jain` (co-founder) | Senior/professional people with zero GitHub evidence. The keyword script scores them **0 and drops them**; we must route them to `needs_human`, not `decline`. This is a headline disagreement row. |
| Blank `first_name` | 141, 153, 272, 656 | Row 153 has org "EY", role "Consultant", no name. Handle without crashing. |
| Trailing whitespace everywhere | many | Strip on every field before comparing. |

## 4. What we can honestly claim about "over time"

Real temporal signal available **today**, from GitHub only:
- commits per quarter over the last ~8 quarters → rising / flat / dormant trajectory,
- repo `created_at` vs `pushed_at` → abandoned vs maintained,
- fork-vs-original mix changing over time.

Everything else that evolves (attendance, mentor opinion, overrides) enters through the **notes /
memory layer** after t0. Say exactly this if a judge asks how the layer "continuously builds":
the first pass is evidence, and every organizer note after it changes future verdicts.

## 5. Budget arithmetic (656 rows, $7.50 total)

Haiku at ~$1/MTok in, ~$5/MTok out; ~3k in + ~400 out per profile ≈ **$0.005/person**
→ 656 profiles ≈ **$3.3**, over half the budget, for people most of whom will never be near a
decision boundary. Therefore `scoring.yaml: llm_gating`: deterministic scoring for **all 656**
(free), LLM synthesis only for the top ~220 by deterministic score plus anyone within 0.08 of a
threshold, capped at 300 → ≈ **$1.5**. Everyone else keeps a deterministic profile and, if a judge
asks about them, the answer is "no LLM was spent there because it could not change the outcome".

## 6. Generalizability evidence

`mapping.yaml` (this dataset: split name, URL-form GitHub, derived student flag, no email) and
`mapping.sample.yaml` (single name column, bare handles, explicit email + answers + check-in) are
the same contract over two genuinely different files. `data/sample_registrations.csv` exercises the
second shape; `data/sample_ignite_shaped.csv` exercises this one. That is the architecture claim,
demonstrated rather than asserted.
