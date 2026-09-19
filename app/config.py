"""Application settings and typed YAML configuration loaders."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class _YamlModel(BaseModel):
    """Permit forward-compatible keys in human-edited configuration files."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class ColumnSpec(_YamlModel):
    """A mapped source column, optionally with normalisation hints."""

    column: str = ""
    strip_prefixes: list[str] = Field(default_factory=list)
    truthy: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def from_string(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"column": value}
        if value is None:
            return {}
        return value


class MappingColumns(_YamlModel):
    name: str = "Full Name"
    email: str = "Email"
    github: ColumnSpec = Field(default_factory=ColumnSpec)
    linkedin: str | None = None
    org: str | None = None
    role: str | None = None
    student_flag: ColumnSpec = Field(default_factory=ColumnSpec)
    registered_at: str | None = None
    checked_in: ColumnSpec = Field(default_factory=ColumnSpec)
    answers: list[str] = Field(default_factory=list)

    @field_validator("answers", mode="before")
    @classmethod
    def normalise_answers(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return list(value)


class DedupeConfig(_YamlModel):
    keys: list[str] = Field(default_factory=lambda: ["email", "github"])


class Mapping(_YamlModel):
    source_name: str = ""
    sheet: int | str = 0
    columns: MappingColumns = Field(default_factory=MappingColumns)
    dedupe: DedupeConfig = Field(default_factory=DedupeConfig)


class ScoringWeights(_YamlModel):
    original_work: float = 0.45
    reliability: float = 0.25
    ai_relevance: float = 0.15
    trajectory: float = 0.15
    claim_consistency: float = 0.0


class EligibilityRule(_YamlModel):
    name: str = ""
    pass_if_any: list[str] = Field(default_factory=list)
    fail_if_all: list[str] = Field(default_factory=list)
    # Keep compatibility with the original scalar sample and the normalized
    # list form used by the ranker configuration.  Both forms are valid YAML
    # representations of the same predicate group.
    unknown_if: str | list[str] | None = None


class EligibilityConfig(_YamlModel):
    professional_titles: list[str] = Field(default_factory=list)
    intern_titles: list[str] = Field(default_factory=list)
    student_titles: list[str] = Field(default_factory=list)
    unknown_titles: list[str] = Field(default_factory=list)
    rules: list[EligibilityRule] = Field(default_factory=list)


class ScoringThresholds(_YamlModel):
    admit: float = 0.62
    waitlist: float = 0.45


class BaselineConfig(_YamlModel):
    keywords: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=lambda: ["name", "description", "topics"])
    count_forks: bool = True


class Scoring(_YamlModel):
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    eligibility: EligibilityConfig = Field(default_factory=EligibilityConfig)
    thresholds: ScoringThresholds = Field(default_factory=ScoringThresholds)
    needs_human_when: list[str] = Field(default_factory=list)
    baseline: BaselineConfig = Field(default_factory=BaselineConfig)
    disagreement_threshold: int = 100
    note_adjustments: dict[str, float | str] = Field(default_factory=dict)
    llm_gating: dict[str, int | float | str | list[str]] = Field(default_factory=dict)
    free_mail_domains: list[str] = Field(
        default_factory=lambda: [
            "gmail.com",
            "yahoo.com",
            "outlook.com",
            "hotmail.com",
            "icloud.com",
            "proton.me",
            "protonmail.com",
            "yahoo.in",
            "rediffmail.com",
        ],
        validation_alias="FREE_MAIL_DOMAINS",
        serialization_alias="FREE_MAIL_DOMAINS",
    )

    @property
    def FREE_MAIL_DOMAINS(self) -> list[str]:  # noqa: N802 - mirrors scoring.yaml
        """Compatibility view matching the key used in ``scoring.yaml``."""

        return self.free_mail_domains


class Settings(BaseSettings):
    """Environment-backed settings.

    Every key in ``.env.example`` is represented here.  Defaults intentionally
    keep an empty checkout bootable: optional integrations are disabled or
    simply log a warning until a human supplies credentials.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    anthropic_api_key: str = Field("", validation_alias="ANTHROPIC_API_KEY")
    llm_model_batch: str = Field("claude-haiku-4-5", validation_alias="LLM_MODEL_BATCH")
    llm_model_agent: str = Field("claude-sonnet-5", validation_alias="LLM_MODEL_AGENT")
    llm_budget_usd: float = Field(6.0, validation_alias="LLM_BUDGET_USD")
    llm_price_table: str = Field(
        "claude-haiku-4-5:1.0:5.0,claude-sonnet-5:3.0:15.0",
        validation_alias="LLM_PRICE_TABLE",
    )

    database_url: str = Field("sqlite:///data/ignu.db", validation_alias="DATABASE_URL")
    registrations_file: str = Field(
        "data/private/registrations.xlsx", validation_alias="REGISTRATIONS_FILE"
    )
    mapping_file: str = Field("mapping.yaml", validation_alias="MAPPING_FILE")
    scoring_file: str = Field("scoring.yaml", validation_alias="SCORING_FILE")
    cache_dir: str = Field("data/cache", validation_alias="CACHE_DIR")
    event_name: str = Field("ignite-with-delhi-2026", validation_alias="EVENT_NAME")
    prev_event_file: str = Field(
        "data/private/prev_event.csv", validation_alias="PREV_EVENT_FILE"
    )

    github_token: str = Field("", validation_alias="GITHUB_TOKEN")
    github_max_repos_per_person: int = Field(
        3, validation_alias="GITHUB_MAX_REPOS_PER_PERSON"
    )
    github_readme_max_chars: int = Field(4000, validation_alias="GITHUB_README_MAX_CHARS")

    graph_enabled: bool = Field(True, validation_alias="GRAPH_ENABLED")
    neo4j_uri: str = Field(
        "neo4j+s://xxxxxxxx.databases.neo4j.io", validation_alias="NEO4J_URI"
    )
    neo4j_username: str = Field("neo4j", validation_alias="NEO4J_USERNAME")
    neo4j_password: str = Field("", validation_alias="NEO4J_PASSWORD")
    neo4j_database: str = Field("neo4j", validation_alias="NEO4J_DATABASE")

    memory_backend: str = Field("sqlite", validation_alias="MEMORY_BACKEND")
    llm_provider: str = Field("anthropic", validation_alias="LLM_PROVIDER")
    llm_model: str = Field("anthropic/claude-haiku-4-5", validation_alias="LLM_MODEL")
    llm_api_key: str = Field("", validation_alias="LLM_API_KEY")
    embedding_provider: str = Field("fastembed", validation_alias="EMBEDDING_PROVIDER")
    graph_database_provider: str = Field("neo4j", validation_alias="GRAPH_DATABASE_PROVIDER")
    graph_database_url: str = Field("", validation_alias="GRAPH_DATABASE_URL")
    graph_database_username: str = Field("neo4j", validation_alias="GRAPH_DATABASE_USERNAME")
    graph_database_password: str = Field("", validation_alias="GRAPH_DATABASE_PASSWORD")
    enable_backend_access_control: bool = Field(
        False, validation_alias="ENABLE_BACKEND_ACCESS_CONTROL"
    )

    slack_signing_secret: str = Field("", validation_alias="SLACK_SIGNING_SECRET")
    slack_bot_token: str = Field("", validation_alias="SLACK_BOT_TOKEN")
    public_base_url: str = Field("", validation_alias="PUBLIC_BASE_URL")

    render_api_key: str = Field("", validation_alias="RENDER_API_KEY")
    render_workflow_slug: str = Field("ignu-workflows", validation_alias="RENDER_WORKFLOW_SLUG")
    workflow_runner: str = Field("local", validation_alias="WORKFLOW_RUNNER")

    def mapping(self, path: str | Path | None = None) -> Mapping:
        return load_mapping(path or self.mapping_file)

    def scoring(self, path: str | Path | None = None) -> Scoring:
        return load_scoring(path or self.scoring_file)


def _read_yaml(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        return {}
    with file_path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    return value if isinstance(value, dict) else {}


def load_mapping(path: str | Path) -> Mapping:
    """Load a mapping file, retaining safe defaults for absent optional keys."""

    return Mapping.model_validate(_read_yaml(path))


def load_scoring(path: str | Path) -> Scoring:
    """Load scoring constants without making startup depend on a file."""

    return Scoring.model_validate(_read_yaml(path))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


__all__ = [
    "BaselineConfig",
    "ColumnSpec",
    "DedupeConfig",
    "EligibilityConfig",
    "EligibilityRule",
    "Mapping",
    "MappingColumns",
    "Scoring",
    "ScoringThresholds",
    "ScoringWeights",
    "Settings",
    "get_settings",
    "load_mapping",
    "load_scoring",
]
