"""Generates steady LLM traffic so the dashboards fill up.

LLM_MOCK=true  -> simulated responses, no API keys needed (default in docker compose)
LLM_MOCK=false -> real calls; providers without an API key are skipped
"""

import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm_telemetry  # noqa: E402

MODELS = {
    "openai": (["gpt-4o", "gpt-4o-mini"], "OPENAI_API_KEY"),
    "anthropic": (["claude-sonnet-4-5", "claude-haiku-4-5"], "ANTHROPIC_API_KEY"),
    "xai": (["grok-4", "grok-3-mini"], "XAI_API_KEY"),
}
PROMPTS = [
    "Summarise the benefits of OpenTelemetry in two sentences.",
    "Write a haiku about dashboards.",
    "Explain p95 latency to a new engineer.",
    "List three ways to reduce LLM token cost.",
]


def main() -> None:
    llm_telemetry.setup()
    mock = os.getenv("LLM_MOCK", "false").lower() in ("1", "true", "yes", "on")
    providers = [p for p, (_, key) in MODELS.items() if mock or os.getenv(key)]
    if not providers:
        sys.exit("No API keys set. Use LLM_MOCK=true or set provider keys.")
    interval = float(os.getenv("LOAD_INTERVAL_SECONDS", "1" if mock else "10"))
    print(f"Sending traffic to {providers} (mock={mock}, prompt_logging={llm_telemetry.prompt_logging_enabled()})")

    try:
        while True:
            provider = random.choice(providers)
            model = random.choice(MODELS[provider][0])
            messages = [{"role": "user", "content": random.choice(PROMPTS)}]
            try:
                r = llm_telemetry.chat(provider, model, messages, max_tokens=256)
                print(f"ok    {provider:9} {model:18} in={r.input_tokens} out={r.output_tokens}")
            except Exception as e:  # errors are already recorded as telemetry
                print(f"error {provider:9} {model:18} {e}")
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        llm_telemetry.shutdown()


if __name__ == "__main__":
    main()
