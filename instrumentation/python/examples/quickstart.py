"""Smallest possible example: one real request per provider you have a key for.

    export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
    export ANTHROPIC_API_KEY=...        # and/or OPENAI_API_KEY, XAI_API_KEY
    python examples/quickstart.py

Then open http://localhost:3000 -> LLM Observability.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import llm_telemetry  # noqa: E402

llm_telemetry.setup(service_name="quickstart")

# Optional: any OpenAI-compatible LLM, e.g. a local Ollama.
# llm_telemetry.register_provider("ollama", base_url="http://localhost:11434/v1")

calls = [
    ("openai", "gpt-4o-mini", "OPENAI_API_KEY"),
    ("anthropic", "claude-haiku-4-5", "ANTHROPIC_API_KEY"),
    ("xai", "grok-3-mini", "XAI_API_KEY"),
]
ran = 0
for provider, model, key in calls:
    if not os.getenv(key):
        print(f"skip {provider}: {key} not set")
        continue
    try:
        r = llm_telemetry.chat(provider, model, [{"role": "user", "content": "Say hello in five words."}])
        print(f"{provider}: {r.text!r} ({r.input_tokens}+{r.output_tokens} tokens, "
              f"first token after {r.ttft:.2f}s, ${r.cost_usd:.6f})")
    except Exception as e:  # recorded as an error in the dashboards too
        print(f"{provider}: failed with {llm_telemetry.error_type(e)}")
    ran += 1

llm_telemetry.shutdown()  # flush telemetry before exit
if not ran:
    sys.exit("No API keys set. Set at least one of OPENAI_API_KEY, ANTHROPIC_API_KEY, XAI_API_KEY.")
