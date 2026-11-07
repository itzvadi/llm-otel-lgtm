"""Instrument any LLM call with OpenTelemetry.

Two ways to use it:

1. `chat()` - one call for OpenAI, Anthropic Claude, xAI Grok and any
   OpenAI-compatible provider you register (Gemini, Mistral, Groq, DeepSeek,
   Ollama, vLLM, ...). Streams by default so time-to-first-token is measured.

2. `track()` - wrap a call you already make with your own SDK or HTTP client:

       with llm_telemetry.track("my-provider", "my-model") as call:
           resp = my_client.generate(...)
           call.set_usage(resp.input_tokens, resp.output_tokens)

Every call produces:
  * a span following the OTel GenAI semantic conventions  -> Tempo
  * request / token / cost / latency metrics               -> Prometheus/Mimir
  * an `llm.request` log event (metadata only)             -> Loki
  * a `gen_ai.content` log event with prompt + response    -> Loki (only if PROMPT_LOGGING=on)
"""

import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

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


# ---------- provider registry ----------

@dataclass
class Provider:
    name: str
    kind: str = "openai"            # "openai" (OpenAI-compatible API) or "anthropic"
    base_url: str | None = None     # None = the SDK default
    api_key_env: str | None = None  # env var holding the key; None = no key (e.g. Ollama)
    extra: dict = field(default_factory=dict)  # passed to the SDK client constructor


_PROVIDERS: dict[str, Provider] = {
    "openai": Provider("openai", api_key_env="OPENAI_API_KEY"),
    "anthropic": Provider("anthropic", kind="anthropic", api_key_env="ANTHROPIC_API_KEY"),
    "xai": Provider("xai", base_url="https://api.x.ai/v1", api_key_env="XAI_API_KEY"),
}


def register_provider(name: str, base_url: str | None = None, api_key_env: str | None = None,
                      kind: str = "openai", **client_kwargs) -> None:
    """Add your own LLM. Any OpenAI-compatible endpoint works with kind="openai".

    llm_telemetry.register_provider("gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_env="GEMINI_API_KEY")
    llm_telemetry.register_provider("ollama", base_url="http://localhost:11434/v1")
    """
    if kind not in ("openai", "anthropic"):
        raise ValueError('kind must be "openai" or "anthropic"')
    _PROVIDERS[name] = Provider(name, kind, base_url, api_key_env, client_kwargs)


def providers() -> list[str]:
    return sorted(_PROVIDERS)


# ---------- error normalisation ----------

def error_type(exc: BaseException) -> str:
    """Map any SDK/HTTP exception to a small, stable set of labels.

    rate_limit_429, server_error_5xx, auth_401/403, client_error_4xx, timeout,
    connection_error, or the exception class name as a last resort.
    """
    name = type(exc).__name__
    if isinstance(exc, TimeoutError) or "Timeout" in name:
        return "timeout"
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        if status == 429:
            return "rate_limit_429"
        if status >= 500:
            return f"server_error_{status}"
        if status in (401, 403):
            return f"auth_{status}"
        return f"client_error_{status}"
    if isinstance(exc, ConnectionError) or "Connection" in name:
        return "connection_error"
    return name


# ---------- generic tracker ----------

@dataclass
class ChatResult:
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = ""
    response_model: str = ""
    ttft: float | None = None
    cost_usd: float = 0.0


class LLMCall:
    """Handle yielded by `track()`. Fill in what you know; everything is optional."""

    def __init__(self, provider: str, model: str, messages=None):
        self.provider, self.model, self.messages = provider, model, messages
        self.result = ChatResult(response_model=model)
        self._cost: float | None = None
        self._start = time.perf_counter()

    def first_token(self) -> None:
        """Call when the first streamed token arrives (records time-to-first-token)."""
        if self.result.ttft is None:
            self.result.ttft = time.perf_counter() - self._start

    def set_usage(self, input_tokens: int | None = None, output_tokens: int | None = None,
                  cost_usd: float | None = None) -> None:
        """Token counts from the response; cost_usd overrides the pricing.yaml lookup."""
        if input_tokens is not None:
            self.result.input_tokens = int(input_tokens)
        if output_tokens is not None:
            self.result.output_tokens = int(output_tokens)
        if cost_usd is not None:
            self._cost = float(cost_usd)

    def set_response(self, text: str | None = None, model: str | None = None,
                     finish_reason: str | None = None) -> None:
        if text is not None:
            self.result.text = text
        if model:
            self.result.response_model = model
        if finish_reason:
            self.result.finish_reason = finish_reason


@contextmanager
def track(provider: str, model: str, messages=None, max_tokens: int | None = None,
          operation: str = "chat"):
    """Record telemetry for one LLM call made by your own code. Re-raises any error."""
    labels = {"provider": provider, "model": model}
    call = LLMCall(provider, model, messages)
    with _tracer.start_as_current_span(f"{operation} {model}", kind=SpanKind.CLIENT) as span:
        span.set_attribute("gen_ai.operation.name", operation)
        span.set_attribute("gen_ai.system", provider)
        span.set_attribute("gen_ai.provider.name", provider)
        span.set_attribute("gen_ai.request.model", model)
        if max_tokens:
            span.set_attribute("gen_ai.request.max_tokens", max_tokens)
        try:
            yield call
        except Exception as e:
            elapsed = time.perf_counter() - call._start
            err = error_type(e)
            span.set_status(Status(StatusCode.ERROR, err))
            span.set_attribute("error.type", err)
            _requests.add(1, {**labels, "status": "error", "error_type": err})
            _duration.record(elapsed, {**labels, "status": "error"})
            _log.warning("llm.request", extra={"event.name": "llm.request", **labels,
                                               "status": "error", "error_type": err,
                                               "duration_s": round(elapsed, 3)})
            raise

        res = call.result
        elapsed = time.perf_counter() - call._start
        res.cost_usd = call._cost if call._cost is not None else cost_usd(
            provider, model, res.input_tokens, res.output_tokens)
        span.set_attribute("gen_ai.response.model", res.response_model)
        if res.finish_reason:
            span.set_attribute("gen_ai.response.finish_reasons", [res.finish_reason])
        span.set_attribute("gen_ai.usage.input_tokens", res.input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", res.output_tokens)
        span.set_attribute("llm.cost_usd", res.cost_usd)
        if res.ttft is not None:
            span.set_attribute("llm.time_to_first_token_s", res.ttft)

        _requests.add(1, {**labels, "status": "ok", "error_type": ""})
        _tokens.add(res.input_tokens, {**labels, "token_type": "input"})
        _tokens.add(res.output_tokens, {**labels, "token_type": "output"})
        _cost.add(res.cost_usd, labels)
        _duration.record(elapsed, {**labels, "status": "ok"})
        if res.ttft is not None:
            _ttft.record(res.ttft, labels)

        _log.info("llm.request", extra={"event.name": "llm.request", **labels, "status": "ok",
                                        "input_tokens": res.input_tokens,
                                        "output_tokens": res.output_tokens,
                                        "cost_usd": round(res.cost_usd, 6),
                                        "duration_s": round(elapsed, 3)})
        if prompt_logging_enabled():
            _log.info(json.dumps({"prompt": messages, "response": res.text}),
                      extra={"event.name": "gen_ai.content", **labels})


# ---------- built-in adapters ----------

def _api_key(p: Provider) -> str | None:
    if not p.api_key_env:
        return "not-needed"  # local servers such as Ollama ignore the key
    key = os.getenv(p.api_key_env)
    if not key:
        raise RuntimeError(f"{p.api_key_env} is not set (needed for provider '{p.name}')")
    return key


def _call_openai_compatible(call: LLMCall, p: Provider, messages, max_tokens, stream, **kw) -> None:
    from openai import OpenAI

    client = OpenAI(api_key=_api_key(p), base_url=p.base_url, **p.extra)
    # OpenAI's newer models only accept max_completion_tokens; other providers use max_tokens.
    limit = {"max_completion_tokens": max_tokens} if p.name == "openai" else {"max_tokens": max_tokens}
    if not stream:
        r = client.chat.completions.create(model=call.model, messages=messages, **limit, **kw)
        c = r.choices[0]
        call.set_response(c.message.content or "", r.model, c.finish_reason)
        if r.usage:
            call.set_usage(r.usage.prompt_tokens, r.usage.completion_tokens)
        return

    parts, finish = [], None
    s = client.chat.completions.create(model=call.model, messages=messages, stream=True,
                                       stream_options={"include_usage": True}, **limit, **kw)
    for chunk in s:
        if chunk.choices:
            d = chunk.choices[0]
            if d.delta and d.delta.content:
                call.first_token()
                parts.append(d.delta.content)
            finish = d.finish_reason or finish
        if getattr(chunk, "usage", None):
            call.set_usage(chunk.usage.prompt_tokens, chunk.usage.completion_tokens)
        if chunk.model:
            call.set_response(model=chunk.model)
    call.set_response("".join(parts), finish_reason=finish)


def _call_anthropic(call: LLMCall, p: Provider, messages, max_tokens, stream, **kw) -> None:
    import anthropic

    client = anthropic.Anthropic(api_key=_api_key(p), base_url=p.base_url, **p.extra)
    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    msgs = [m for m in messages if m["role"] != "system"]
    args = dict(model=call.model, messages=msgs, max_tokens=max_tokens, **kw)
    if system:
        args["system"] = system
    if stream:
        with client.messages.stream(**args) as s:
            for _ in s.text_stream:
                call.first_token()
            r = s.get_final_message()
    else:
        r = client.messages.create(**args)
    text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
    call.set_response(text, r.model, r.stop_reason)
    call.set_usage(r.usage.input_tokens, r.usage.output_tokens)


# ---------- public API ----------

def chat(provider: str, model: str, messages: list[dict], max_tokens: int = 512,
         stream: bool = True, **kwargs) -> ChatResult:
    """Send a chat request to a built-in or registered provider and record telemetry.

    messages: [{"role": "system"|"user"|"assistant", "content": "..."}]
    stream:   True measures time-to-first-token; set False for providers that
              don't support streaming usage (tokens then come from the final response).
    """
    p = _PROVIDERS.get(provider)
    if p is None:
        raise ValueError(f"unknown provider '{provider}'. Known: {providers()}. "
                         "Add yours with llm_telemetry.register_provider().")
    with track(provider, model, messages=messages, max_tokens=max_tokens) as call:
        if p.kind == "anthropic":
            _call_anthropic(call, p, messages, max_tokens, stream, **kwargs)
        else:
            _call_openai_compatible(call, p, messages, max_tokens, stream, **kwargs)
    return call.result
