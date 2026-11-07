# Connect your LLM

> **Shortcut:** `python3 setup_wizard.py` in the repo root sets up Claude Code and Codex for you, and
> starts and checks the stack. This page explains each step, and how to connect anything else.

Everything sends OpenTelemetry (OTLP) to one collector:

| Endpoint | Protocol |
|---|---|
| `http://localhost:4317` | OTLP gRPC |
| `http://localhost:4318` | OTLP HTTP |

On a shared or production stack, replace `localhost` with your collector's host name.

Pick the route that matches what you run:

| You use | Route | Dashboard |
|---|---|---|
| Claude Code (Pro, Max, Team, Enterprise or API key) | [1. Claude Code](#1-claude-code) | AI Coding Agents → Claude Code |
| OpenAI Codex CLI (ChatGPT plan or API key) | [2. Codex CLI](#2-codex-cli) | AI Coding Agents → Codex |
| Your own Python app calling OpenAI, Claude, Grok, Gemini, Mistral, Groq, DeepSeek, Ollama, vLLM… | [3. Python library](#3-python-library) | LLM Observability |
| An app in any language, without code changes | [4. Gateway](#4-litellm-gateway-no-code-changes) | LLM Observability → Traces & Logs |
| Another tool that already speaks OpenTelemetry | [5. Any OTel-capable tool](#5-any-tool-that-speaks-opentelemetry) | Grafana Explore |

No API key is needed for Claude Code or Codex on a subscription plan. They send their own telemetry.

---

## 1. Claude Code

Add this to `~/.claude/settings.json`. Telemetry settings in a project's `.claude/settings.json` are ignored.

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
    "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "cumulative",
    "OTEL_METRIC_EXPORT_INTERVAL": "10000"
  }
}
```

Restart Claude Code. Data appears within a few seconds.

| Optional variable | Effect |
|---|---|
| `OTEL_LOG_USER_PROMPTS=1` | Sends prompt text. The default collector config (`PROMPT_LOGGING=off`) still removes it. |
| `OTEL_RESOURCE_ATTRIBUTES=team.id=platform,cost_center=eng-1` | Adds labels for team-level cost views. Use no spaces. |
| `OTEL_METRICS_INCLUDE_SESSION_ID=false` | One metric series per user instead of per session. Use it on large fleets. |
| `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` + `OTEL_TRACES_EXPORTER=otlp` | Traces (beta). |

**Fleet rollout:** put the same `env` block in Claude Code's managed settings file and deploy it with MDM.

What you get: cost, tokens (including cache reads and writes), time to first token, API latency, errors,
sessions, prompts, tool calls with success rate and duration, permission decisions, lines of code, commits
and PRs. All of it can be filtered by user and model.

## 2. Codex CLI

Add this to `~/.codex/config.toml`. A project's `.codex/config.toml` ignores `[otel]`.

```toml
[otel]
environment = "local"
log_user_prompt = false
exporter         = { otlp-grpc = { endpoint = "http://localhost:4317" } }  # logs
trace_exporter   = { otlp-grpc = { endpoint = "http://localhost:4317" } }  # traces
metrics_exporter = { otlp-grpc = { endpoint = "http://localhost:4317" } }  # metrics
```

Start a new Codex session. Codex shows up as `service_name=codex_exec` or `codex_cli_rs`, depending on
how it was started.

What you get: prompts, model responses, input, cached, output and reasoning tokens, time to first token
(per turn and per response), API and websocket latency, HTTP status codes, failures, tool calls and
decisions, and conversations by model, reasoning effort and sandbox policy.

> Codex does not report cost. On a ChatGPT plan, usage isn't billed per token.

## 3. Python library

The library in `instrumentation/python` records a trace span (OTel GenAI conventions), metrics and a log
event for every call.

```bash
pip install "git+https://github.com/itzvadi/llm-otel-lgtm#subdirectory=instrumentation/python"
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_SERVICE_NAME=my-app        # becomes the "Service" filter in Grafana
```

### 3a. Built-in providers

```python
import llm_telemetry

llm_telemetry.setup()
msgs = [{"role": "user", "content": "Hello!"}]

r = llm_telemetry.chat("openai",    "gpt-4o-mini",      msgs)  # OPENAI_API_KEY
r = llm_telemetry.chat("anthropic", "claude-haiku-4-5", msgs)  # ANTHROPIC_API_KEY
r = llm_telemetry.chat("xai",       "grok-3-mini",      msgs)  # XAI_API_KEY
print(r.text, r.input_tokens, r.output_tokens, r.ttft, r.cost_usd)

llm_telemetry.shutdown()   # flush before the process exits
```

`chat()` streams by default, so time to first token is measured. Pass `stream=False` for a provider that
can't stream token usage.

### 3b. Add any OpenAI-compatible LLM

Most providers and local servers expose an OpenAI-compatible API. Register it once, then call
`chat()` as usual:

```python
llm_telemetry.register_provider("gemini",   base_url="https://generativelanguage.googleapis.com/v1beta/openai/", api_key_env="GEMINI_API_KEY")
llm_telemetry.register_provider("mistral",  base_url="https://api.mistral.ai/v1",       api_key_env="MISTRAL_API_KEY")
llm_telemetry.register_provider("groq",     base_url="https://api.groq.com/openai/v1",  api_key_env="GROQ_API_KEY")
llm_telemetry.register_provider("deepseek", base_url="https://api.deepseek.com",        api_key_env="DEEPSEEK_API_KEY")
llm_telemetry.register_provider("ollama",   base_url="http://localhost:11434/v1")       # no key
llm_telemetry.register_provider("vllm",     base_url="http://my-vllm:8000/v1")

llm_telemetry.chat("ollama", "llama3.2", msgs)
```

Use `kind="anthropic"` for an Anthropic-compatible endpoint.

### 3c. Any other SDK or HTTP call: `track()`

If you already call an LLM your own way, wrap the call:

```python
with llm_telemetry.track("bedrock", "my-model", messages=msgs) as call:
    for chunk in my_client.stream(...):
        call.first_token()                    # optional: time to first token
        ...
    call.set_usage(input_tokens=resp.in_tok, output_tokens=resp.out_tok)
    call.set_response(text=full_text, finish_reason="stop")   # optional
```

If the block raises, the call is recorded as an error with a normalized `error_type` and the exception
is re-raised.

### Prices

Cost is calculated from `llm_telemetry/pricing.yaml` in USD per 1M tokens. Add your models there, or
point `LLM_PRICING_FILE` at your own copy:

```yaml
gemini:
  gemini-2.5-flash: { input: 0.30, output: 2.50 }
ollama:
  llama3.2: { input: 0, output: 0 }
```

Models without a price are recorded with cost 0. You can also pass `call.set_usage(..., cost_usd=…)`.

### Error types

Errors from every SDK are mapped to the same labels, so the dashboards work for all providers:
`rate_limit_429`, `server_error_5xx`, `auth_401` / `auth_403`, `client_error_4xx`, `timeout`,
`connection_error`.

## 4. LiteLLM gateway (no code changes)

Start the stack with `--profile gateway`, add your models to `gateway/litellm/config.yaml`, and point any
OpenAI-compatible client at `http://localhost:4000`:

```yaml
model_list:
  - model_name: gemini-flash
    litellm_params: { model: gemini/gemini-2.5-flash, api_key: os.environ/GEMINI_API_KEY }
```

Gateway calls show up as traces (`service_name=llm-gateway`) on **LLM Observability → Traces & Logs**.
The metric panels need the Python library.

## 5. Any tool that speaks OpenTelemetry

Point the tool's OTLP exporter at the collector, for example with the standard variables:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
export OTEL_SERVICE_NAME=my-tool
```

Delta metrics are converted in the collector, so you don't need to change temporality. The data is in
Grafana **Explore** right away. To give the tool its own tab, add it to `scripts/generate_dashboards.py`.

---

## Check that data arrives

```bash
# metrics
curl -s 'localhost:3000/api/datasources/proxy/uid/prometheus/api/v1/label/__name__/values' | grep -oE '"(llm|claude_code|codex)_[a-z_]+' | sort -u
# log streams by service
curl -s 'localhost:3000/api/datasources/proxy/uid/loki/loki/api/v1/label/service_name/values'
```

If nothing shows up:

| Symptom | Fix |
|---|---|
| No data at all | `docker compose ps`. The collector must be up, with ports 4317 and 4318 published. |
| Claude Code logs arrive but no metrics | Restart Claude Code after editing settings. Check `OTEL_METRICS_EXPORTER=otlp`. |
| Changes to `settings.json` or `config.toml` ignored | Settings are read at startup. Open a new session. |
| Python script shows nothing | Call `llm_telemetry.shutdown()` before exit so buffers are flushed. |
