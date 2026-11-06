"""Builds the Grafana dashboards in grafana/dashboards/. Run: python scripts/generate_dashboards.py"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "grafana" / "dashboards"
PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
TEMPO = {"type": "tempo", "uid": "tempo"}
F = '{job=~"$service", provider=~"$provider", model=~"$model"}'
FS = 'job=~"$service", provider=~"$provider", model=~"$model"'
RI = "$__rate_interval"

_id = 0


def _next_id():
    global _id
    _id += 1
    return _id


def target(expr, legend="", instant=False):
    t = {"datasource": PROM, "expr": expr, "legendFormat": legend, "refId": "A"}
    if instant:
        t.update(instant=True, range=False, format="table")
    return t


def panel(kind, title, targets, x, y, w, h, unit="short", desc="", **opts):
    for i, t in enumerate(targets):
        t["refId"] = chr(65 + i)
    p = {
        "id": _next_id(), "type": kind, "title": title, "description": desc,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "datasource": targets[0]["datasource"], "targets": targets,
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": opts.get("options", {}),
    }
    if kind == "timeseries":
        p["fieldConfig"]["defaults"]["custom"] = {"fillOpacity": 10, "lineWidth": 2, "showPoints": "never"}
        p["options"] = {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["mean", "max"]},
                        "tooltip": {"mode": "multi"}}
    if kind == "stat":
        p["options"] = {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "area"}
    if "thresholds" in opts:
        p["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": opts["thresholds"]}
    if "transformations" in opts:
        p["transformations"] = opts["transformations"]
    return p


def variables():
    def q(name, label, query):
        return {"name": name, "label": label, "type": "query", "datasource": PROM,
                "query": {"query": query, "refId": name}, "definition": query,
                "refresh": 2, "includeAll": True, "multi": True, "allValue": ".*",
                "current": {"text": "All", "value": "$__all"}, "sort": 1}
    return {"list": [
        q("service", "Service", "label_values(llm_requests_total, job)"),
        q("provider", "Provider", 'label_values(llm_requests_total{job=~"$service"}, provider)'),
        q("model", "Model", 'label_values(llm_requests_total{job=~"$service", provider=~"$provider"}, model)'),
    ]}


def dashboard(uid, title, panels, desc):
    return {
        "uid": uid, "title": title, "description": desc, "tags": ["llm", "opentelemetry"],
        "timezone": "browser", "schemaVersion": 39, "version": 1, "editable": True,
        "refresh": "30s", "time": {"from": "now-1h", "to": "now"},
        "templating": variables(), "panels": panels,
        "links": [{"title": "LLM dashboards", "type": "dashboards", "tags": ["llm"], "asDropdown": True}],
    }


GOOD_BAD = [{"color": "green", "value": None}, {"color": "orange", "value": 0.02}, {"color": "red", "value": 0.05}]
LAT = [{"color": "green", "value": None}, {"color": "orange", "value": 5}, {"color": "red", "value": 15}]


def overview():
    return dashboard("llm-overview", "LLM Overview", [
        panel("stat", "Requests", [target(f"sum(increase(llm_requests_total{F}[$__range]))")], 0, 0, 4, 4),
        panel("stat", "Tokens", [target(f"sum(increase(llm_tokens_total{F}[$__range]))")], 4, 0, 4, 4),
        panel("stat", "Cost", [target(f"sum(increase(llm_cost_usd_total{F}[$__range]))")], 8, 0, 4, 4, "currencyUSD"),
        panel("stat", "Error rate", [target(
            f'sum(rate(llm_requests_total{{{FS}, status="error"}}[$__range])) / sum(rate(llm_requests_total{F}[$__range]))')],
            12, 0, 4, 4, "percentunit", thresholds=GOOD_BAD),
        panel("stat", "p95 latency", [target(
            f"histogram_quantile(0.95, sum by (le) (rate(llm_request_duration_seconds_bucket{F}[$__range])))")],
            16, 0, 4, 4, "s", thresholds=LAT),
        panel("stat", "Active models", [target(f"count(count by (provider, model) (llm_requests_total{F}))")], 20, 0, 4, 4),
        panel("timeseries", "Requests / s by provider", [target(
            f"sum by (provider) (rate(llm_requests_total{F}[{RI}]))", "{{provider}}")], 0, 4, 12, 8, "reqps"),
        panel("timeseries", "Tokens / s by provider", [target(
            f"sum by (provider, token_type) (rate(llm_tokens_total{F}[{RI}]))", "{{provider}} {{token_type}}")], 12, 4, 12, 8),
        panel("timeseries", "Cost per hour by provider", [target(
            f"sum by (provider) (rate(llm_cost_usd_total{F}[{RI}])) * 3600", "{{provider}}")], 0, 12, 12, 8, "currencyUSD"),
        panel("timeseries", "p95 latency by provider", [target(
            f"histogram_quantile(0.95, sum by (le, provider) (rate(llm_request_duration_seconds_bucket{F}[{RI}])))",
            "{{provider}}")], 12, 12, 12, 8, "s"),
    ], "Health, usage and cost across all LLM providers at a glance.")


def comparison():
    sel = "sum by (provider, model)"
    t = [
        target(f"{sel} (increase(llm_requests_total{F}[$__range]))", instant=True),
        target(f"histogram_quantile(0.5, {sel[:-1]}, le) (rate(llm_request_duration_seconds_bucket{F}[$__range])))", instant=True),
        target(f"histogram_quantile(0.95, {sel[:-1]}, le) (rate(llm_request_duration_seconds_bucket{F}[$__range])))", instant=True),
        target(f'{sel} (rate(llm_requests_total{{{FS}, status="error"}}[$__range])) / {sel} (rate(llm_requests_total{F}[$__range]))', instant=True),
        target(f'{sel} (increase(llm_tokens_total{{{FS}, token_type="output"}}[$__range])) / {sel} (increase(llm_requests_total{{{FS}, status="ok"}}[$__range]))', instant=True),
        target(f"{sel} (increase(llm_cost_usd_total{F}[$__range]))", instant=True),
        target(f'{sel} (increase(llm_cost_usd_total{F}[$__range])) / {sel} (increase(llm_requests_total{{{FS}, status="ok"}}[$__range]))', instant=True),
    ]
    names = ["Requests", "p50 latency (s)", "p95 latency (s)", "Error rate", "Avg output tokens", "Total cost ($)", "Cost / request ($)"]
    rename = {f"Value #{chr(65 + i)}": n for i, n in enumerate(names)}
    table = panel("table", "Provider & model scorecard", t, 0, 0, 24, 10,
                  desc="Side-by-side comparison for the selected time range.",
                  transformations=[{"id": "merge", "options": {}},
                                   {"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": rename}}])
    table["options"] = {"showHeader": True, "sortBy": [{"displayName": "Requests", "desc": True}]}
    return dashboard("llm-provider-comparison", "LLM Provider Comparison", [
        table,
        panel("barchart", "p95 latency by model", [target(
            f"histogram_quantile(0.95, {sel[:-1]}, le) (rate(llm_request_duration_seconds_bucket{F}[$__range])))",
            "{{provider}}/{{model}}", instant=True)], 0, 10, 12, 9, "s",
            transformations=[{"id": "reduce", "options": {"reducers": ["lastNotNull"]}}]),
        panel("barchart", "Cost per 1k requests by model", [target(
            f'1000 * {sel} (increase(llm_cost_usd_total{F}[$__range])) / {sel} (increase(llm_requests_total{{{FS}, status="ok"}}[$__range]))',
            "{{provider}}/{{model}}", instant=True)], 12, 10, 12, 9, "currencyUSD",
            transformations=[{"id": "reduce", "options": {"reducers": ["lastNotNull"]}}]),
    ], "Compare OpenAI, Claude and Grok on latency, reliability and cost.")


def cost():
    return dashboard("llm-cost", "LLM Cost & Tokens", [
        panel("stat", "Spend (range)", [target(f"sum(increase(llm_cost_usd_total{F}[$__range]))")], 0, 0, 6, 4, "currencyUSD"),
        panel("stat", "Projected spend / day", [target(f"sum(rate(llm_cost_usd_total{F}[1h])) * 86400")], 6, 0, 6, 4, "currencyUSD"),
        panel("stat", "Input tokens", [target(f'sum(increase(llm_tokens_total{{{FS}, token_type="input"}}[$__range]))')], 12, 0, 6, 4),
        panel("stat", "Output tokens", [target(f'sum(increase(llm_tokens_total{{{FS}, token_type="output"}}[$__range]))')], 18, 0, 6, 4),
        panel("piechart", "Spend share by provider", [target(
            f"sum by (provider) (increase(llm_cost_usd_total{F}[$__range]))", "{{provider}}", instant=True)], 0, 4, 8, 9, "currencyUSD"),
        panel("timeseries", "Cumulative spend by model", [target(
            f"sum by (provider, model) (increase(llm_cost_usd_total{F}[$__range]))", "{{provider}}/{{model}}")], 8, 4, 16, 9, "currencyUSD"),
        panel("timeseries", "Tokens / min by model", [target(
            f"sum by (model, token_type) (rate(llm_tokens_total{F}[{RI}])) * 60", "{{model}} {{token_type}}")], 0, 13, 24, 8),
    ], "Spend, projected spend and token usage. Prices come from pricing.yaml.")


def reliability():
    return dashboard("llm-latency-errors", "LLM Latency & Errors", [
        panel("timeseries", "Latency percentiles", [
            target(f"histogram_quantile(0.5, sum by (le) (rate(llm_request_duration_seconds_bucket{F}[{RI}])))", "p50"),
            target(f"histogram_quantile(0.95, sum by (le) (rate(llm_request_duration_seconds_bucket{F}[{RI}])))", "p95"),
            target(f"histogram_quantile(0.99, sum by (le) (rate(llm_request_duration_seconds_bucket{F}[{RI}])))", "p99"),
        ], 0, 0, 12, 8, "s"),
        panel("timeseries", "Time to first token p95", [target(
            f"histogram_quantile(0.95, sum by (le, provider) (rate(llm_time_to_first_token_seconds_bucket{F}[{RI}])))",
            "{{provider}}")], 12, 0, 12, 8, "s", desc="Recorded for streaming/mock calls."),
        panel("timeseries", "Error rate by provider", [target(
            f'sum by (provider) (rate(llm_requests_total{{{FS}, status="error"}}[{RI}])) / sum by (provider) (rate(llm_requests_total{F}[{RI}]))',
            "{{provider}}")], 0, 8, 12, 8, "percentunit"),
        panel("timeseries", "Errors / min by type", [target(
            f'sum by (provider, error_type) (rate(llm_requests_total{{{FS}, status="error"}}[{RI}])) * 60',
            "{{provider}} {{error_type}}")], 12, 8, 12, 8),
    ], "Latency percentiles, time to first token, error rates and rate limits.")


def traces_logs():
    tempo = {"datasource": TEMPO, "queryType": "traceql", "limit": 50, "tableType": "traces",
             "query": '{ span.gen_ai.system =~ "${provider:regex}" && span.gen_ai.request.model =~ "${model:regex}" }'}
    slow = dict(tempo, query='{ span.gen_ai.system =~ "${provider:regex}" && duration > 3s }')
    errs = dict(tempo, query='{ span.gen_ai.system =~ "${provider:regex}" && status = error }')
    logs = {"datasource": LOKI, "expr": '{service_name=~"$service"} | provider=~"$provider" | model=~"$model"'}
    content = {"datasource": LOKI,
               "expr": '{service_name=~"$service"} | event_name="gen_ai.content" | provider=~"$provider"'}
    p_logs = panel("logs", "LLM request events", [logs], 0, 20, 24, 9)
    p_logs["options"] = {"showTime": True, "wrapLogMessage": True, "enableLogDetails": True, "sortOrder": "Descending"}
    p_content = panel("logs", "Prompts & responses (only when PROMPT_LOGGING=on)", [content], 0, 29, 24, 10)
    p_content["options"] = p_logs["options"]
    return dashboard("llm-traces-logs", "LLM Traces & Logs", [
        panel("table", "Recent LLM traces", [tempo], 0, 0, 24, 8, desc="Click a trace ID to open it in Tempo."),
        panel("table", "Slow requests (> 3s)", [slow], 0, 8, 12, 12),
        panel("table", "Failed requests", [errs], 12, 8, 12, 12),
        p_logs, p_content,
    ], "Drill from traces to request logs and (optionally) prompt content.")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in [("01-llm-overview", overview), ("02-llm-provider-comparison", comparison),
                     ("03-llm-cost", cost), ("04-llm-latency-errors", reliability),
                     ("05-llm-traces-logs", traces_logs)]:
        _id = 0
        (OUT / f"{name}.json").write_text(json.dumps(fn(), indent=2) + "\n")
        print("wrote", name)
