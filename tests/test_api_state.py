from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app import db
from app.api_state import build_state


def test_build_state_is_empty_safe(tmp_db) -> None:
    state = build_state()

    assert state["counters"] == {
        "rows": 0,
        "with_github": 0,
        "with_github_pct": 0.0,
        "aliases": 0,
        "admitted": 0,
        "waitlist": 0,
        "decline": 0,
        "needs_human": 0,
    }
    assert state["spend"] == 0.0
    assert state["disagreements"] == []
    assert state["teams"] == []
    assert state["last_run"] is None
    assert state["scoring"]["thresholds"]["admit"] == 0.62
    json.dumps(state)


def test_build_state_reads_latest_verdicts_teams_spend_and_run(tmp_db) -> None:
    now = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    db.init_db()
    with db.get_session() as session:
        session.add_all(
            [
                db.PersonRow(
                    id="person-a",
                    name="Ada Example",
                    email="",
                    email_domain="",
                    github_login="ada-example",
                    linkedin_url=None,
                    org="Example",
                    role="Engineer",
                    is_student=False,
                    answers={},
                    registered_at=None,
                    alias_of=None,
                    source_file="sample.csv",
                    source_row=1,
                ),
                db.PersonRow(
                    id="person-b",
                    name="Bea Alias",
                    email="",
                    email_domain="",
                    github_login=None,
                    linkedin_url=None,
                    org="Example",
                    role="Student",
                    is_student=True,
                    answers={},
                    registered_at=None,
                    alias_of="person-a",
                    source_file="sample.csv",
                    source_row=2,
                ),
                db.VerdictRow(
                    person_id="person-a",
                    decision="waitlist",
                    score=0.5,
                    eligibility="pass",
                    baseline_score=0.2,
                    baseline_rank=3,
                    ignu_rank=5,
                    reasons=["older verdict"],
                    evidence_ids=[],
                    profile_version=1,
                    at=now - timedelta(minutes=1),
                ),
                db.VerdictRow(
                    person_id="person-a",
                    decision="admit",
                    score=0.8,
                    eligibility="pass",
                    baseline_score=0.2,
                    baseline_rank=150,
                    ignu_rank=1,
                    reasons=["original work signal"],
                    evidence_ids=[],
                    profile_version=2,
                    at=now,
                ),
                db.VerdictRow(
                    person_id="person-b",
                    decision="needs_human",
                    score=0.3,
                    eligibility="unknown",
                    baseline_score=0.0,
                    baseline_rank=2,
                    ignu_rank=4,
                    reasons=["no evidence"],
                    evidence_ids=[],
                    profile_version=1,
                    at=now,
                ),
                db.TeamRow(
                    id="team-01",
                    event="demo",
                    member_ids=["person-a", "person-b"],
                    coverage={"backend": 0.9},
                    balance=0.75,
                    why="Strong backend coverage.",
                ),
                db.LLMUsageRow(
                    ts=now,
                    model="test",
                    tier="batch",
                    input_tokens=10,
                    output_tokens=5,
                    cost_usd=0.125,
                    cache_key=None,
                ),
                db.EventRow(
                    ts=now,
                    run_id="run-demo",
                    stage="rank",
                    person_id="person-a",
                    status="ok",
                    msg="ranked cohort",
                    data=None,
                ),
            ]
        )

    state = build_state()

    assert state["counters"]["rows"] == 2
    assert state["counters"]["with_github"] == 1
    assert state["counters"]["with_github_pct"] == 50.0
    assert state["counters"]["aliases"] == 1
    assert state["counters"]["admitted"] == 1
    assert state["counters"]["needs_human"] == 1
    assert state["spend"] == 0.125
    assert state["disagreements"][0]["person_id"] == "person-a"
    assert state["teams"][0]["members"] == [
        {"id": "person-a", "name": "Ada Example"},
        {"id": "person-b", "name": "Bea Alias"},
    ]
    assert state["last_run"]["run_id"] == "run-demo"
