"""Pydantic contracts shared by every ignu pipeline stage.

These models are deliberately kept close to the frozen contracts in
``docs/00-SPEC.md``.  Pipeline stages should exchange these objects rather
than dictionaries so that evidence and source information cannot silently be
dropped between stages.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel


Skill: TypeAlias = Literal[
    "frontend",
    "backend",
    "ml_ai",
    "data",
    "mobile",
    "devops_cloud",
    "design_product",
    "pitch_comms",
]
EvidenceLevel: TypeAlias = Literal["none", "thin", "solid", "strong"]
Decision: TypeAlias = Literal["admit", "waitlist", "decline", "needs_human"]
Stage: TypeAlias = Literal[
    "ingest",
    "github",
    "profile",
    "graph",
    "rank",
    "team",
    "memory",
    "agent",
]


class Person(BaseModel):
    id: str
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
    alias_of: str | None
    source_file: str
    source_row: int


class Participation(BaseModel):
    person_id: str
    event: str
    registered: bool
    approved: bool | None
    checked_in: bool | None
    submitted: bool | None
    placed: int | None
    team_id: str | None
    at: date | None


class RepoEvidence(BaseModel):
    full_name: str
    html_url: str
    is_fork: bool
    stars: int
    author_commits_90d: int
    author_commits_total: int
    primary_language: str | None
    last_push: datetime | None
    description: str | None
    topics: list[str]
    readme_excerpt: str | None
    ai_relevance: float = 0.0


class Evidence(BaseModel):
    claim: str
    source_url: str
    kind: Literal[
        "repo",
        "commit",
        "readme",
        "registration",
        "participation",
        "note",
    ]
    confidence: float
    observed_at: date


class SkillScore(BaseModel):
    skill: Skill
    confidence: float


class Profile(BaseModel):
    person_id: str
    version: int
    built_at: datetime
    skills: list[SkillScore]
    evidence_level: EvidenceLevel
    original_work_score: float
    ai_relevance: float
    reliability: float | None
    summary: str
    evidence: list[Evidence]
    model_used: str
    input_tokens: int
    output_tokens: int


class Verdict(BaseModel):
    person_id: str
    decision: Decision
    score: float
    eligibility: Literal["pass", "fail", "unknown"]
    baseline_score: float
    baseline_rank: int
    ignu_rank: int
    reasons: list[str]
    evidence_ids: list[int]
    profile_version: int
    at: datetime


class Team(BaseModel):
    id: str
    event: str
    member_ids: list[str]
    coverage: dict[Skill, float]
    balance: float
    why: str


class Note(BaseModel):
    id: int | None
    person_id: str | None
    text: str
    author: str
    at: datetime
    kind: Literal["observation", "flag", "override", "praise"]
    source: Literal["chat", "slack", "import"]


class PipelineEvent(BaseModel):
    ts: datetime
    run_id: str
    stage: Stage
    person_id: str | None
    status: Literal["start", "ok", "skip", "error"]
    msg: str
    data: dict | None = None


__all__ = [
    "Decision",
    "Evidence",
    "EvidenceLevel",
    "Note",
    "Participation",
    "Person",
    "PipelineEvent",
    "Profile",
    "RepoEvidence",
    "Skill",
    "SkillScore",
    "Stage",
    "Team",
    "Verdict",
]
