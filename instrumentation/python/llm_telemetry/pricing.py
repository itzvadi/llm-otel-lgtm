import os
from functools import lru_cache
from pathlib import Path

import yaml

_DEFAULT = Path(__file__).with_name("pricing.yaml")


@lru_cache(maxsize=1)
def _prices() -> dict:
    path = Path(os.getenv("LLM_PRICING_FILE", _DEFAULT))
    with path.open() as f:
        return yaml.safe_load(f) or {}


def pricing_version() -> str:
    """Version of the rate table used for cost estimates ("unversioned" if not set)."""
    return str(_prices().get("version") or "unversioned")


def cost_breakdown(provider: str, model: str, input_tokens: int, output_tokens: int,
                   cached_input_tokens: int = 0) -> dict[str, float]:
    """USD cost per token type. input_tokens excludes cached_input_tokens."""
    p = _prices().get(provider, {})
    p = p.get(model) if isinstance(p, dict) else None
    if not p:
        return {"input": 0.0, "cached_input": 0.0, "output": 0.0}
    return {
        "input": input_tokens * p["input"] / 1_000_000,
        "cached_input": cached_input_tokens * p.get("cached_input", p["input"]) / 1_000_000,
        "output": output_tokens * p["output"] / 1_000_000,
    }


def cost_usd(provider: str, model: str, input_tokens: int, output_tokens: int,
             cached_input_tokens: int = 0) -> float:
    return sum(cost_breakdown(provider, model, input_tokens, output_tokens,
                              cached_input_tokens).values())
