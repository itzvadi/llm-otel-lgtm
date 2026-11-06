"""Smallest possible example: one real request per provider you have a key for."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm_telemetry  # noqa: E402

llm_telemetry.setup(service_name="quickstart")

calls = [
    ("openai", "gpt-4o-mini", "OPENAI_API_KEY"),
    ("anthropic", "claude-haiku-4-5", "ANTHROPIC_API_KEY"),
    ("xai", "grok-3-mini", "XAI_API_KEY"),
]
for provider, model, key in calls:
    if not os.getenv(key) and os.getenv("LLM_MOCK", "false") != "true":
        print(f"skip {provider}: {key} not set")
        continue
    r = llm_telemetry.chat(provider, model, [{"role": "user", "content": "Say hello in five words."}])
    print(f"{provider}: {r.text!r} ({r.input_tokens}+{r.output_tokens} tokens)")

llm_telemetry.shutdown()
