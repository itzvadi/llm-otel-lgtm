"""One `chat()` call for OpenAI, Anthropic Claude and xAI Grok, fully instrumented.

Every call produces:
  * a span following the OTel GenAI semantic conventions  -> Tempo
  * request / token / cost / latency metrics               -> Prometheus/Mimir
  * an `llm.request` log event (metadata only)             -> Loki
  * a `gen_ai.content` log event with prompt + response    -> Loki (only if PROMPT_LOGGING=on)
"""

import json
import logging
import os
import random
import time
from dataclasses import dataclass

from opentelemetry import metrics, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from .pricing import cost_usd
from .telemetry import prompt_logging_enabled

_tracer = trace.get_tracer("llm_telemetry")
_meter = metrics.get_meter("llm_telemetry")
_log = logging.getLogger("llm_telemetry.events")

_requests = _meter.create_counter("llm_requests", description="LLM requests")
_tokens = _meter.create_counter("llm_tokens", description="Tokens by type (input/output)")
_cost = _meter.create_counter("llm_cost_usd", description="Estimated cost in USD")
_duration = _meter.create_histogram("llm_request_duration_seconds", description="Request latency")
_ttft = _meter.create_histogram("llm_time_to_first_token_seconds", description="Time to first token")

XAI_BASE_URL = "https://api.x.ai/v1"
PROVIDERS = ("openai", "anthropic", "xai")


@dataclass
class ChatResult:
    text: str
    input_tokens: int
    output_tokens: int
    finish_reason: str
    response_model: str
    ttft: float | None = None


def _mock_enabled() -> bool:
    return os.getenv("LLM_MOCK", "false").lower() in ("1", "true", "yes", "on")


# ---------- provider adapters ----------

def _call_openai_compatible(provider, model, messages, max_tokens, **kw) -> ChatResult:
    from openai import OpenAI

    if provider == "xai":
        client = OpenAI(api_key=os.environ["XAI_API_KEY"], base_url=XAI_BASE_URL)
    else:
        client = OpenAI()
    r = client.chat.completions.create(model=model, messages=messages, max_tokens=max_tokens, **kw)
    c = r.choices[0]
    return ChatResult(
        text=c.message.content or "",
        input_tokens=r.usage.prompt_tokens,
        output_tokens=r.usage.completion_tokens,
        finish_reason=c.finish_reason or "",
        response_model=r.model,
    )


def _call_anthropic(model, messages, max_tokens, **kw) -> ChatResult:
    import anthropic

    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    msgs = [m for m in messages if m["role"] != "system"]
    extra = {"system": system} if system else {}
    r = anthropic.Anthropic().messages.create(
        model=model, messages=msgs, max_tokens=max_tokens, **extra, **kw
    )
    text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
    return ChatResult(
        text=text,
        input_tokens=r.usage.input_tokens,
        output_tokens=r.usage.output_tokens,
        finish_reason=r.stop_reason or "",
        response_model=r.model,
    )


class MockProviderError(Exception):
    pass


def _call_mock(provider, model, messages, max_tokens) -> ChatResult:
    """Simulated provider: realistic-ish latency, tokens and occasional errors."""
    profile = {"openai": (0.6, 0.03), "anthropic": (0.8, 0.02), "xai": (0.5, 0.05)}
    base, err_rate = profile.get(provider, (0.7, 0.03))
    ttft = random.uniform(0.1, 0.4) * base * 2
    time.sleep(ttft + random.uniform(0.1, 1.2) * base)
    if random.random() < err_rate:
        raise MockProviderError(random.choice(["rate_limit_429", "server_error_500", "timeout"]))
    prompt_len = sum(len(m["content"]) for m in messages)
    out = random.randint(20, max_tokens)
    return ChatResult(
        text=f"[mock {provider}/{model}] answer with {out} tokens",
        input_tokens=max(1, prompt_len // 4),
        output_tokens=out,
        finish_reason="stop",
        response_model=model,
        ttft=ttft,
    )


# ---------- public API ----------

def chat(provider: str, model: str, messages: list[dict], max_tokens: int = 512, **kwargs) -> ChatResult:
    """Send a chat request and record telemetry.

    provider: "openai" | "anthropic" | "xai"
    messages: [{"role": "system"|"user"|"assistant", "content": "..."}]
    """
    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {PROVIDERS}")

    labels = {"provider": provider, "model": model}
    start = time.perf_counter()
    with _tracer.start_as_current_span(f"chat {model}", kind=SpanKind.CLIENT) as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.system", provider)
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("gen_ai.request.max_tokens", max_tokens)
        try:
            if _mock_enabled():
                res = _call_mock(provider, model, messages, max_tokens)
            elif provider == "anthropic":
                res = _call_anthropic(model, messages, max_tokens, **kwargs)
            else:
                res = _call_openai_compatible(provider, model, messages, max_tokens, **kwargs)
        except Exception as e:
            elapsed = time.perf_counter() - start
            err = str(e) if isinstance(e, MockProviderError) else type(e).__name__
            span.set_status(Status(StatusCode.ERROR, err))
            span.set_attribute("error.type", err)
            _requests.add(1, {**labels, "status": "error", "error_type": err})
            _duration.record(elapsed, {**labels, "status": "error"})
            _log.warning("llm.request", extra={"event.name": "llm.request", **labels,
                                               "status": "error", "error_type": err,
                                               "duration_s": round(elapsed, 3)})
            raise

        elapsed = time.perf_counter() - start
        cost = cost_usd(provider, model, res.input_tokens, res.output_tokens)
        span.set_attribute("gen_ai.response.model", res.response_model)
        span.set_attribute("gen_ai.response.finish_reasons", [res.finish_reason])
        span.set_attribute("gen_ai.usage.input_tokens", res.input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", res.output_tokens)
        span.set_attribute("llm.cost_usd", cost)

        _requests.add(1, {**labels, "status": "ok", "error_type": ""})
        _tokens.add(res.input_tokens, {**labels, "token_type": "input"})
        _tokens.add(res.output_tokens, {**labels, "token_type": "output"})
        _cost.add(cost, labels)
        _duration.record(elapsed, {**labels, "status": "ok"})
        if res.ttft is not None:
            _ttft.record(res.ttft, labels)

        _log.info("llm.request", extra={"event.name": "llm.request", **labels, "status": "ok",
                                        "input_tokens": res.input_tokens,
                                        "output_tokens": res.output_tokens,
                                        "cost_usd": round(cost, 6),
                                        "duration_s": round(elapsed, 3)})
        if prompt_logging_enabled():
            _log.info(json.dumps({"prompt": messages, "response": res.text}),
                      extra={"event.name": "gen_ai.content", **labels})
        return res
