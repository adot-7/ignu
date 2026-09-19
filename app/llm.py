"""Small, budget-guarded Anthropic wrapper used by profile and agent lanes."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypeVar

import anthropic
import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select

from .config import get_settings
from .db import LLMUsageRow, get_session, init_db

logger = logging.getLogger(__name__)

Tier = Literal["batch", "agent"]
T = TypeVar("T", bound=BaseModel)


class BudgetExceeded(RuntimeError):
    """Raised before/after an LLM call when the configured daily budget is met."""


class StructuredOutputError(RuntimeError):
    """Raised when Anthropic did not return a valid forced-tool payload."""


_client: Any | None = None
_model_overrides: dict[Tier, str] = {}


def _get_client() -> Any:
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


def _model_for_tier(tier: Tier) -> str:
    if tier not in ("batch", "agent"):
        raise ValueError(f"unsupported LLM tier: {tier}")
    if tier in _model_overrides:
        return _model_overrides[tier]
    settings = get_settings()
    return settings.llm_model_batch if tier == "batch" else settings.llm_model_agent


def _price_table() -> dict[str, tuple[float, float]]:
    table: dict[str, tuple[float, float]] = {}
    for entry in get_settings().llm_price_table.split(","):
        parts = [part.strip() for part in entry.split(":")]
        if len(parts) != 3:
            continue
        try:
            table[parts[0]] = (float(parts[1]), float(parts[2]))
        except ValueError:
            logger.warning("ignoring malformed LLM_PRICE_TABLE entry %r", entry)
    return table


def _cost(model: str, input_tokens: int, output_tokens: int) -> float:
    input_price, output_price = _price_table().get(model, (0.0, 0.0))
    return (input_tokens / 1_000_000 * input_price) + (
        output_tokens / 1_000_000 * output_price
    )


def spend_usd() -> float:
    """Return recorded spend, or zero when the database is not yet initialised."""

    try:
        init_db()
        with get_session() as session:
            value = session.scalar(select(func.sum(LLMUsageRow.cost_usd)))
        return float(value or 0.0)
    except Exception:  # pragma: no cover - startup/degraded path
        logger.exception("could not read LLM spend")
        return 0.0


def _guard_budget() -> None:
    current = spend_usd()
    budget = get_settings().llm_budget_usd
    if current >= budget:
        raise BudgetExceeded(
            f"LLM budget exhausted: ${current:.6f} >= configured ${budget:.6f}"
        )


def _usage_value(usage: Any, name: str) -> int:
    if usage is None:
        return 0
    if isinstance(usage, dict):
        value = usage.get(name, 0)
    else:
        value = getattr(usage, name, 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _record_usage(
    response: Any,
    *,
    model: str,
    tier: Tier,
    cache_key: str | None = None,
) -> tuple[int, int]:
    usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    input_tokens = _usage_value(usage, "input_tokens")
    output_tokens = _usage_value(usage, "output_tokens")
    cost = _cost(model, input_tokens, output_tokens)
    init_db()
    with get_session() as session:
        session.add(
            LLMUsageRow(
                ts=datetime.now(timezone.utc),
                model=model,
                tier=tier,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                cache_key=cache_key,
            )
        )
    return input_tokens, output_tokens


def _content_value(block: Any, name: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _tool_input(response: Any) -> Any:
    content = response.get("content", []) if isinstance(response, dict) else getattr(response, "content", [])
    for block in content or []:
        if _content_value(block, "type") == "tool_use":
            value = _content_value(block, "input")
            if value is not None:
                return value
    raise StructuredOutputError("Anthropic response did not contain a structured tool call")


def _cache_path(cache_key: str) -> Path:
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    return Path(get_settings().cache_dir) / "llm" / f"{digest}.json"


def _load_cache(cache_key: str, schema: type[T]) -> T | None:
    path = _cache_path(cache_key)
    if not path.exists():
        return None
    try:
        return schema.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as exc:
        logger.warning("ignoring invalid LLM cache %s: %s", path, type(exc).__name__)
        return None


def _write_cache(cache_key: str, value: BaseModel) -> None:
    path = _cache_path(cache_key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value.model_dump(mode="json"), sort_keys=True), encoding="utf-8"
        )
    except OSError:  # pragma: no cover - cache is an optimisation
        logger.warning("could not write LLM cache %s", path)


def _create_message(
    *,
    prompt: str,
    tier: Tier,
    tools: list[dict[str, Any]],
    tool_choice: dict[str, str] | None = None,
    max_tokens: int = 2048,
    cache_key: str | None = None,
) -> Any:
    _guard_budget()
    model = _model_for_tier(tier)
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "tools": tools,
    }
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice
    response = _get_client().messages.create(**kwargs)
    _record_usage(response, model=model, tier=tier, cache_key=cache_key)
    if spend_usd() >= get_settings().llm_budget_usd:
        # Do not discard a paid response; the pre-call guard refuses next time.
        logger.warning("LLM budget reached after this response; further calls will refuse")
    return response


def structured(
    prompt: str,
    schema: type[T],
    *,
    tier: Tier,
    cache_key: str | None,
) -> T:
    """Call Anthropic once with a forced tool, retrying one invalid payload."""

    if cache_key:
        cached = _load_cache(cache_key, schema)
        if cached is not None:
            return cached

    tool_name = "structured_output"
    tools = [
        {
            "name": tool_name,
            "description": "Return the requested structured result.",
            "input_schema": schema.model_json_schema(),
        }
    ]
    last_error: ValidationError | StructuredOutputError | None = None
    for attempt in range(2):
        attempt_prompt = prompt
        if last_error is not None:
            attempt_prompt = (
                f"{prompt}\n\nThe previous structured result was invalid. "
                f"Return valid JSON matching the schema. Validation error: {last_error}"
            )
        response = _create_message(
            prompt=attempt_prompt,
            tier=tier,
            tools=tools,
            tool_choice={"type": "tool", "name": tool_name},
            max_tokens=2048,
            cache_key=cache_key,
        )
        try:
            value = schema.model_validate(_tool_input(response))
        except (ValidationError, StructuredOutputError) as exc:
            last_error = exc
            if attempt == 0:
                continue
            raise StructuredOutputError(str(exc)) from exc
        if cache_key:
            _write_cache(cache_key, value)
        return value
    raise StructuredOutputError("structured output failed")  # pragma: no cover


def chat_with_tools(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    tier: Tier = "agent",
    system: str | None = None,
    max_tokens: int = 600,
) -> Any:
    """Run one ordinary Anthropic tool-capable message call.

    ``system`` is passed through as the Anthropic system prompt (the agent lane
    needs it for the tool-only rules).  The budget is checked *before* the
    call; a response that pushes spend over budget is still returned so paid
    tokens are never discarded — the next call will refuse.
    """

    _guard_budget()
    model = _model_for_tier(tier)
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
        "tools": tools,
    }
    if system:
        kwargs["system"] = system
    response = _get_client().messages.create(**kwargs)
    _record_usage(response, model=model, tier=tier)
    if spend_usd() >= get_settings().llm_budget_usd:
        logger.warning("LLM budget reached after this response; further calls will refuse")
    return response


def verify_models() -> dict[str, str]:
    """Best-effort startup check for configured model ids.

    No request is made without an API key, which keeps local development and
    offline tests deterministic.  If Anthropic reports that the configured
    batch id is absent, the first available Haiku id is used for this process.
    """

    settings = get_settings()
    if not settings.anthropic_api_key:
        logger.warning("ANTHROPIC_API_KEY is empty; skipping startup model verification")
        return {}
    try:
        response = httpx.get(
            "https://api.anthropic.com/v1/models",
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
            },
            timeout=5.0,
        )
        response.raise_for_status()
        payload = response.json()
        ids = [
            str(item.get("id"))
            for item in payload.get("data", [])
            if isinstance(item, dict) and item.get("id")
        ]
        result: dict[str, str] = {}
        if settings.llm_model_batch not in ids:
            haiku_ids = sorted(model_id for model_id in ids if "haiku" in model_id.lower())
            if haiku_ids:
                _model_overrides["batch"] = haiku_ids[0]
                result["batch"] = haiku_ids[0]
                logger.warning(
                    "configured batch model %s unavailable; using %s",
                    settings.llm_model_batch,
                    haiku_ids[0],
                )
        if settings.llm_model_agent not in ids:
            logger.warning("configured agent model %s was not listed by Anthropic", settings.llm_model_agent)
        return result
    except Exception as exc:  # pragma: no cover - depends on external service
        logger.warning("startup Anthropic model verification failed: %s", type(exc).__name__)
        return {}


def reset_client() -> None:
    """Clear the lazy client and model overrides for tests or a new config."""

    global _client
    _client = None
    _model_overrides.clear()


__all__ = [
    "BudgetExceeded",
    "StructuredOutputError",
    "chat_with_tools",
    "reset_client",
    "spend_usd",
    "structured",
    "verify_models",
]
