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


def cost_usd(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    p = _prices().get(provider, {}).get(model)
    if not p:
        return 0.0
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000
