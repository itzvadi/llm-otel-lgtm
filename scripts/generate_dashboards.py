"""Builds the Grafana dashboards in grafana/dashboards/. Run: python scripts/generate_dashboards.py

Dashboards use the Grafana 12+/13 v2 schema (dashboard.grafana.app/v2) so they
can have tabs. They are provisioned with allowUiUpdates, so you can also edit
them in the Grafana UI; export the JSON afterwards to keep the change in git.

  llm-observability.json  - apps instrumented with llm_telemetry (any provider)
  ai-coding-agents.json   - Claude Code and Codex CLI native OpenTelemetry
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "grafana" / "dashboards"
RI = "$__rate_interval"

# ---------- v2 schema helpers ----------


def _q(group, spec):
    return {"kind": "DataQuery", "group": group, "version": "v0",
            "datasource": {"name": group}, "spec": spec}


def prom(expr, legend="", instant=False, table=True):
    """instant + table: one row per series (tables). instant + table=False: one value per
    series named by legend (pie charts, bar gauges)."""
    spec = {"expr": expr, "legendFormat": legend}
    if instant:
        spec.update(instant=True, range=False, format="table" if table else "time_series")
    return _q("prometheus", spec)


def loki(expr, legend="", instant=False):
    return _q("loki", {"expr": expr, "legendFormat": legend,
                       "queryType": "instant" if instant else "range"})


def tempo(traceql):
    return _q("tempo", {"queryType": "traceql", "query": traceql, "limit": 50, "tableType": "traces"})


class Board:
    """Collects panels (elements) and lays them out in tabs."""

    def __init__(self):
        self.elements, self.tabs, self._items, self._n = {}, [], None, 0

    def tab(self, title):
        self._items = []
        self.tabs.append({"kind": "TabsLayoutTab", "spec": {
            "title": title, "layout": {"kind": "GridLayout", "spec": {"items": self._items}}}})

    def add(self, viz, title, queries, x, y, w, h, unit="short", desc="", options=None,
            custom=None, thresholds=None, transforms=None, decimals=None):
        self._n += 1
        name = f"panel-{self._n}"
        defaults = {"unit": unit}
        if custom:
            defaults["custom"] = custom
        if thresholds:
            defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
            defaults["color"] = {"mode": "thresholds"}
        if decimals is not None:
            defaults["decimals"] = decimals
        self.elements[name] = {"kind": "Panel", "spec": {
            "id": self._n, "title": title, "description": desc, "links": [],
            "data": {"kind": "QueryGroup", "spec": {
                "queries": [{"kind": "PanelQuery", "spec": {"query": q, "refId": chr(65 + i), "hidden": False}}
                            for i, q in enumerate(queries)],
                "transformations": [{"kind": "Transformation", "group": t, "spec": {"options": o}}
                                    for t, o in (transforms or [])],
                "queryOptions": {}}},
            "vizConfig": {"kind": "VizConfig", "group": viz, "version": "",
                          "spec": {"options": options or {}, "fieldConfig": {"defaults": defaults, "overrides": []}}},
        }}
        self._items.append({"kind": "GridLayoutItem", "spec": {
            "x": x, "y": y, "width": w, "height": h, "element": {"kind": "ElementReference", "name": name}}})

    # common panel shapes
    def stat(self, title, q, x, y, w=4, h=4, unit="short", **kw):
        # No thresholds given -> one neutral colour (Grafana's default turns values > 80 red).
        kw.setdefault("thresholds", [{"color": "text", "value": None}])
        self.add("stat", title, [q], x, y, w, h, unit, options={
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value", "graphMode": "none", "textMode": "auto", "justifyMode": "auto"}, **kw)

    def series(self, title, qs, x, y, w=12, h=8, unit="short", bars=False, **kw):
        custom = {"fillOpacity": 60 if bars else 10, "lineWidth": 1 if bars else 2, "showPoints": "never",
                  "drawStyle": "bars" if bars else "line", "stacking": {"mode": "normal" if bars else "none"}}
        self.add("timeseries", title, qs, x, y, w, h, unit, custom=custom, options={
            "legend": {"displayMode": "table", "placement": "bottom", "showLegend": True,
                       "calcs": ["sum"] if bars else ["mean", "max"]},
            "tooltip": {"mode": "multi", "sort": "desc"}}, **kw)

    def table(self, title, qs, names, x, y, w=24, h=9, sort=None, **kw):
        rename = {f"Value #{chr(65 + i)}": n for i, n in enumerate(names)}
        if len(qs) == 1:
            rename["Value"] = names[0]
        self.add("table", title, qs, x, y, w, h, options={
            "showHeader": True, "cellHeight": "sm",
            "sortBy": [{"displayName": sort or names[0], "desc": True}]},
            transforms=[("merge", {}), ("organize", {"excludeByName": {"Time": True}, "renameByName": rename})], **kw)

    def logs(self, title, q, x, y, w=24, h=10, desc=""):
        self.add("logs", title, [q], x, y, w, h, desc=desc, options={
            "showTime": True, "wrapLogMessage": True, "enableLogDetails": True,
            "sortOrder": "Descending", "prettifyLogMessage": False, "dedupStrategy": "none"})


def variable(name, label, query, all_value=".*"):
    return {"kind": "QueryVariable", "spec": {
        "name": name, "label": label, "hide": "dontHide", "refresh": "onTimeRangeChanged",
        "skipUrlSync": False, "current": {"text": "All", "value": "$__all"},
        "query": _q("prometheus", {"query": query, "refId": name}), "definition": query,
        "regex": "", "sort": "alphabeticalAsc", "options": [], "multi": True,
        "includeAll": True, "allValue": all_value, "allowCustomValue": True}}


LINKS = [
    {"title": "LLM Observability", "type": "link", "url": "/d/llm-observability", "icon": "dashboard",
     "tooltip": "Apps instrumented with llm_telemetry", "tags": [], "asDropdown": False,
     "targetBlank": False, "includeVars": False, "keepTime": True},
    {"title": "AI Coding Agents", "type": "link", "url": "/d/ai-coding-agents", "icon": "dashboard",
     "tooltip": "Claude Code and Codex CLI", "tags": [], "asDropdown": False,
     "targetBlank": False, "includeVars": False, "keepTime": True},
]


def dashboard(uid, title, desc, board, variables):
    return {
        "apiVersion": "dashboard.grafana.app/v2",
        "kind": "Dashboard",
        "metadata": {"name": uid},
        "spec": {
            "title": title, "description": desc, "tags": ["llm", "opentelemetry"],
            "editable": True, "cursorSync": "Crosshair", "liveNow": False, "preload": False,
            "links": LINKS,
            "annotations": [{"kind": "AnnotationQuery", "spec": {
                "query": {"kind": "DataQuery", "group": "grafana", "version": "v0",
                          "datasource": {"name": "-- Grafana --"}, "spec": {}},
                "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                "name": "Annotations & Alerts", "builtIn": True,
                "legacyOptions": {"type": "dashboard"}}}],
            "timeSettings": {"timezone": "browser", "from": "now-1h", "to": "now", "autoRefresh": "30s",
                             "autoRefreshIntervals": ["10s", "30s", "1m", "5m", "15m", "30m", "1h"],
                             "hideTimepicker": False, "fiscalYearStartMonth": 0},
            "variables": variables,
            "elements": board.elements,
            "layout": {"kind": "TabsLayout", "spec": {"tabs": board.tabs}},
        },
    }


GOOD_BAD = [{"color": "green", "value": None}, {"color": "orange", "value": 0.02}, {"color": "red", "value": 0.05}]
LAT = [{"color": "green", "value": None}, {"color": "orange", "value": 5}, {"color": "red", "value": 15}]

# ---------- LLM Observability (llm_telemetry metrics) ----------

F = '{job=~"$service", provider=~"$provider", model=~"$model"}'
FS = 'job=~"$service", provider=~"$provider", model=~"$model"'


def llm_observability():
    b = Board()

    b.tab("Overview")
    b.stat("Requests", prom(f"sum(increase(llm_requests_total{F}[$__range]))"), 0, 0, 3, decimals=0)
    b.stat("Tokens", prom(f"sum(increase(llm_tokens_total{F}[$__range]))"), 3, 0, 3, decimals=0)
    b.stat("Cost", prom(f"sum(increase(llm_cost_usd_total{F}[$__range]))"), 6, 0, 3, unit="currencyUSD")
    b.stat("Error rate", prom(
        f'sum(rate(llm_requests_total{{{FS}, status="error"}}[$__range])) / sum(rate(llm_requests_total{F}[$__range]))'),
        9, 0, 3, unit="percentunit", thresholds=GOOD_BAD)
    b.stat("Rate limited (429)", prom(
        f'sum(increase(llm_requests_total{{{FS}, error_type="rate_limit_429"}}[$__range])) or vector(0)'),
        12, 0, 3, decimals=0, thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}])
    b.stat("p95 time to first token", prom(
        f"histogram_quantile(0.95, sum by (le) (rate(llm_time_to_first_token_seconds_bucket{F}[$__range])))"),
        15, 0, 3, unit="s", thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 2},
                                        {"color": "red", "value": 5}])
    b.stat("p95 latency", prom(
        f"histogram_quantile(0.95, sum by (le) (rate(llm_request_duration_seconds_bucket{F}[$__range])))"),
        18, 0, 3, unit="s", thresholds=LAT)
    b.stat("Active models", prom(f"count(count by (provider, model) (llm_requests_total{F}))"), 21, 0, 3)
    b.series("Requests / s by provider", [prom(f"sum by (provider) (rate(llm_requests_total{F}[{RI}]))", "{{provider}}")],
             0, 4, unit="reqps")
    b.series("Tokens / s by provider", [prom(
        f"sum by (provider, token_type) (rate(llm_tokens_total{F}[{RI}]))", "{{provider}} {{token_type}}")], 12, 4)
    b.series("Cost per hour by provider", [prom(
        f"sum by (provider) (rate(llm_cost_usd_total{F}[{RI}])) * 3600", "{{provider}}")], 0, 12, unit="currencyUSD")
    b.series("p95 latency by provider", [prom(
        f"histogram_quantile(0.95, sum by (le, provider) (rate(llm_request_duration_seconds_bucket{F}[{RI}])))",
        "{{provider}}")], 12, 12, unit="s")
    b.series("p95 time to first token by provider", [prom(
        f"histogram_quantile(0.95, sum by (le, provider) (rate(llm_time_to_first_token_seconds_bucket{F}[{RI}])))",
        "{{provider}}")], 0, 20, unit="s", desc="How long users wait before text starts appearing (streaming calls).")
    b.series("Error rate by provider", [prom(
        f'sum by (provider) (rate(llm_requests_total{{{FS}, status="error"}}[{RI}])) / sum by (provider) (rate(llm_requests_total{F}[{RI}]))',
        "{{provider}}")], 12, 20, unit="percentunit")

    b.tab("Providers")
    sel = "sum by (provider, model)"
    by = "sum by (provider, model, le)"
    b.table("Provider & model scorecard", [
        prom(f"round({sel} (increase(llm_requests_total{F}[$__range])))", instant=True),
        prom(f"histogram_quantile(0.5, {by} (rate(llm_request_duration_seconds_bucket{F}[$__range])))", instant=True),
        prom(f"histogram_quantile(0.95, {by} (rate(llm_request_duration_seconds_bucket{F}[$__range])))", instant=True),
        prom(f'{sel} (rate(llm_requests_total{{{FS}, status="error"}}[$__range])) / {sel} (rate(llm_requests_total{F}[$__range]))', instant=True),
        prom(f'{sel} (increase(llm_tokens_total{{{FS}, token_type="output"}}[$__range])) / {sel} (increase(llm_requests_total{{{FS}, status="ok"}}[$__range]))', instant=True),
        prom(f"{sel} (increase(llm_cost_usd_total{F}[$__range]))", instant=True),
        prom(f'{sel} (increase(llm_cost_usd_total{F}[$__range])) / {sel} (increase(llm_requests_total{{{FS}, status="ok"}}[$__range]))', instant=True),
    ], ["Requests", "p50 latency (s)", "p95 latency (s)", "Error rate", "Avg output tokens", "Total cost ($)",
        "Cost / request ($)"], 0, 0, h=10, desc="Side-by-side comparison for the selected time range.")
    bars = {"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
            "valueMode": "color", "namePlacement": "left", "sizing": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}
    b.add("bargauge", "p95 latency by model", [prom(
        f"histogram_quantile(0.95, {by} (rate(llm_request_duration_seconds_bucket{F}[$__range])))",
        "{{provider}}/{{model}}", instant=True, table=False)], 0, 10, 12, 9, "s", options=bars, thresholds=LAT)
    b.add("bargauge", "Cost per 1k requests by model", [prom(
        f'1000 * {sel} (increase(llm_cost_usd_total{F}[$__range])) / {sel} (increase(llm_requests_total{{{FS}, status="ok"}}[$__range]))',
        "{{provider}}/{{model}}", instant=True, table=False)], 12, 10, 12, 9, "currencyUSD", options=bars,
        thresholds=[{"color": "blue", "value": None}])

    b.tab("Cost & Tokens")
    b.stat("Spend (range)", prom(f"sum(increase(llm_cost_usd_total{F}[$__range]))"), 0, 0, 6, unit="currencyUSD")
    b.stat("Projected spend / day", prom(f"sum(rate(llm_cost_usd_total{F}[1h])) * 86400"), 6, 0, 6, unit="currencyUSD")
    b.stat("Input tokens", prom(f'sum(increase(llm_tokens_total{{{FS}, token_type="input"}}[$__range]))'), 12, 0, 6, decimals=0)
    b.stat("Output tokens", prom(f'sum(increase(llm_tokens_total{{{FS}, token_type="output"}}[$__range]))'), 18, 0, 6, decimals=0)
    b.add("piechart", "Spend share by provider", [prom(
        f"sum by (provider) (increase(llm_cost_usd_total{F}[$__range]))", "{{provider}}", instant=True, table=False)],
        0, 4, 8, 9, "currencyUSD", options={"pieType": "donut", "legend": {"displayMode": "table",
                                                                          "placement": "right", "values": ["value", "percent"]},
                                            "reduceOptions": {"calcs": ["lastNotNull"]}})
    b.series("Spend by model", [prom(
        f"sum by (provider, model) (increase(llm_cost_usd_total{F}[$__interval]))", "{{provider}}/{{model}}")],
        8, 4, 16, 9, unit="currencyUSD", bars=True)
    b.series("Tokens / min by model", [prom(
        f"sum by (model, token_type) (rate(llm_tokens_total{F}[{RI}])) * 60", "{{model}} {{token_type}}")], 0, 13, 24, 8)

    b.tab("Latency & Errors")

    def pct(metric, by=""):
        g = f"le{', ' + by if by else ''}"
        return [prom(f"histogram_quantile({p}, sum by ({g}) (rate({metric}{F}[{RI}])))",
                     f"{n} {{{{{by}}}}}" if by else n) for p, n in (("0.5", "p50"), ("0.95", "p95"), ("0.99", "p99"))]

    b.series("Latency percentiles", pct("llm_request_duration_seconds_bucket"), 0, 0, unit="s",
             desc="Full request duration, from send to last token.")
    b.series("Time to first token percentiles", pct("llm_time_to_first_token_seconds_bucket"), 12, 0, unit="s",
             desc="Recorded for streaming calls (chat() streams by default; track() via call.first_token()).")
    b.series("p95 latency by model", [prom(
        f"histogram_quantile(0.95, sum by (le, provider, model) (rate(llm_request_duration_seconds_bucket{F}[{RI}])))",
        "{{provider}}/{{model}}")], 0, 8, unit="s")
    b.series("p95 time to first token by model", [prom(
        f"histogram_quantile(0.95, sum by (le, provider, model) (rate(llm_time_to_first_token_seconds_bucket{F}[{RI}])))",
        "{{provider}}/{{model}}")], 12, 8, unit="s")
    b.series("Errors / min by type", [prom(
        f'sum by (provider, error_type) (rate(llm_requests_total{{{FS}, status="error"}}[{RI}])) * 60',
        "{{provider}} {{error_type}}")], 0, 16,
        desc="rate_limit_429, server_error_5xx, auth_401/403, client_error_4xx, timeout, connection_error.")
    b.series("Rate limits (429) / min by model", [prom(
        f'sum by (provider, model) (rate(llm_requests_total{{{FS}, error_type="rate_limit_429"}}[{RI}])) * 60',
        "{{provider}}/{{model}}")], 12, 16)

    b.tab("Traces & Logs")
    sys_f = 'span.gen_ai.system =~ "${provider:regex}"'
    b.add("table", "Recent LLM traces", [tempo(
        f'{{ {sys_f} && span.gen_ai.request.model =~ "${{model:regex}}" }}')], 0, 0, 24, 8,
        desc="Click a trace ID to open it in Tempo.")
    b.add("table", "Slow requests (> 3s)", [tempo(f"{{ {sys_f} && duration > 3s }}")], 0, 8, 12, 10)
    b.add("table", "Failed requests", [tempo(f"{{ {sys_f} && status = error }}")], 12, 8, 12, 10)
    b.logs("LLM request events", loki(
        '{service_name=~"$service"} | event_name="llm.request" | provider=~"$provider" | model=~"$model"'), 0, 18)
    b.logs("Prompts & responses (only when PROMPT_LOGGING=on)", loki(
        '{service_name=~"$service"} | event_name="gen_ai.content" | provider=~"$provider" | model=~"$model"'), 0, 28)

    return dashboard("llm-observability", "LLM Observability",
                     "Requests, tokens, cost, latency and errors for apps instrumented with llm_telemetry.", b, [
        # ".+" (not ".*") for All: Loki rejects selectors that can match an empty value.
        variable("service", "Service", "label_values(llm_requests_total, job)", all_value=".+"),
        variable("provider", "Provider", 'label_values(llm_requests_total{job=~"$service"}, provider)'),
        variable("model", "Model", 'label_values(llm_requests_total{job=~"$service", provider=~"$provider"}, model)'),
    ])


# ---------- AI Coding Agents (Claude Code + Codex native OTel) ----------
# Totals come from per-request log events (exact); Prometheus counters from short-lived
# CLI processes are approximate under increase(), so they are only used for trends.

U = 'user_email=~"$user" | model=~"$model"'
CC = '{service_name="claude-code"}'
CC_REQ = f'{CC} | event_name="api_request" | {U}'
CX = '{service_name=~"codex.*"}'
CX_RESP = f'{CX} | event_name="codex.sse_event" | event_kind="response.completed" | {U}'


def ssum(sel, field, rng="$__range"):
    return f"sum(sum_over_time({sel} | unwrap {field} [{rng}]))"


def scount(sel, rng="$__range"):
    return f"sum(count_over_time({sel} [{rng}]))"


def ai_coding_agents():
    b = Board()
    ok_bad = [{"color": "green", "value": None}, {"color": "red", "value": 1}]
    ttft_th = [{"color": "green", "value": None}, {"color": "orange", "value": 3000}, {"color": "red", "value": 10000}]

    # ----- Claude Code -----
    b.tab("Claude Code")
    b.stat("Cost", loki(ssum(CC_REQ, "cost_usd"), instant=True), 0, 0, 3, unit="currencyUSD",
           desc="Sum of cost_usd on every api_request event. On a Pro/Max plan this is the API-equivalent value.")
    b.stat("Prompts", loki(
        f'{scount(CC + " | event_name=\"user_prompt\" | user_email=~\"$user\"")} or vector(0)', instant=True),
        3, 0, 3, decimals=0)
    b.stat("API requests", loki(scount(CC_REQ), instant=True), 6, 0, 3, decimals=0)
    b.stat("Sessions", loki(
        f'count(sum by (session_id) (count_over_time({CC} | user_email=~"$user" [$__range])))', instant=True),
        9, 0, 3, decimals=0)
    b.stat("Cache hit rate", loki(
        f"{ssum(CC_REQ, 'cache_read_tokens')} / ({ssum(CC_REQ, 'input_tokens')} + "
        f"{ssum(CC_REQ, 'cache_read_tokens')} + {ssum(CC_REQ, 'cache_creation_tokens')})", instant=True),
        12, 0, 3, unit="percentunit", desc="Share of input tokens served from the prompt cache.")
    b.stat("p95 time to first token", loki(
        f"quantile_over_time(0.95, {CC_REQ} | unwrap ttft_ms [$__range]) by ()", instant=True),
        15, 0, 3, unit="ms", thresholds=ttft_th)
    b.stat("p95 API latency", loki(
        f"quantile_over_time(0.95, {CC_REQ} | unwrap duration_ms [$__range]) by ()", instant=True), 18, 0, 3, unit="ms")
    b.stat("API errors", loki(
        f'{scount(CC + " | event_name=\"api_error\" | " + U)} or vector(0)', instant=True), 21, 0, 3, decimals=0,
        thresholds=ok_bad)

    b.series("Spend by model", [loki(
        f"sum by (model) (sum_over_time({CC_REQ} | unwrap cost_usd [$__auto]))", "{{model}}")],
        0, 4, unit="currencyUSD", bars=True)
    b.series("Tokens by type", [
        loki(f"sum(sum_over_time({CC_REQ} | unwrap {f} [$__auto]))", n) for f, n in (
            ("input_tokens", "input"), ("output_tokens", "output"),
            ("cache_read_tokens", "cache read"), ("cache_creation_tokens", "cache write"))], 12, 4, bars=True)
    b.series("Time to first token by model", [
        loki(f"quantile_over_time({p}, {CC_REQ} | unwrap ttft_ms [$__auto]) by (model)", f"{n} {{{{model}}}}")
        for p, n in (("0.5", "p50"), ("0.95", "p95"))], 0, 12, unit="ms",
        desc="Time until the first token of each API response.")
    b.series("API latency by model", [
        loki(f"quantile_over_time({p}, {CC_REQ} | unwrap duration_ms [$__auto]) by (model)", f"{n} {{{{model}}}}")
        for p, n in (("0.5", "p50"), ("0.95", "p95"))], 12, 12, unit="ms")
    b.series("Cost by source", [loki(
        f"sum by (query_source) (sum_over_time({CC_REQ} | unwrap cost_usd [$__auto]))", "{{query_source}}")],
        0, 20, unit="currencyUSD", bars=True, desc="main = your conversation; others = subagents, compaction, etc.")
    b.series("API errors by status", [loki(
        f'sum by (status_code) (count_over_time({CC} | event_name="api_error" | {U} [$__auto]))',
        "{{status_code}}")], 12, 20, bars=True)

    T = f'{CC} | event_name="tool_result" | user_email=~"$user"'
    b.series("Tool calls by tool", [loki(
        f"sum by (tool_name) (count_over_time({T} [$__auto]))", "{{tool_name}}")], 0, 28, bars=True)
    b.series("Tool duration p95 by tool", [loki(
        f"quantile_over_time(0.95, {T} | unwrap duration_ms [$__auto]) by (tool_name)", "{{tool_name}}")],
        12, 28, unit="ms")
    b.table("Tools", [
        loki(f"sum by (tool_name) (count_over_time({T} [$__range]))", instant=True),
        loki(f'sum by (tool_name) (count_over_time({T} | success="true" [$__range])) / '
             f"sum by (tool_name) (count_over_time({T} [$__range]))", instant=True),
        loki(f"quantile_over_time(0.95, {T} | unwrap duration_ms [$__range]) by (tool_name)", instant=True),
    ], ["Calls", "Success rate", "p95 duration (ms)"], 0, 36, 12, 9)
    b.table("Permission decisions", [loki(
        f'sum by (tool_name, decision, source) (count_over_time({CC} | event_name="tool_decision" '
        f'| user_email=~"$user" [$__range]))', instant=True)], ["Count"], 12, 36, 12, 9,
        desc="accept / reject per tool, and whether it came from config, a hook, the user or auto mode.")

    PU = 'user_email=~"$user"'
    b.stat("Lines added", prom(
        f'sum(increase(claude_code_lines_of_code_count_total{{type="added", {PU}}}[$__range]))'), 0, 45, 4, decimals=0)
    b.stat("Lines removed", prom(
        f'sum(increase(claude_code_lines_of_code_count_total{{type="removed", {PU}}}[$__range]))'), 4, 45, 4, decimals=0)
    b.stat("Active time", prom(
        f"sum(increase(claude_code_active_time_seconds_total{{{PU}}}[$__range]))"), 8, 45, 4, unit="s",
        desc="Time actively using Claude Code (user typing + CLI working).")
    b.stat("Commits", prom(
        f"sum(increase(claude_code_commit_count_total{{{PU}}}[$__range])) or vector(0)"), 12, 45, 4, decimals=0)
    b.stat("Pull requests", prom(
        f"sum(increase(claude_code_pull_request_count_total{{{PU}}}[$__range])) or vector(0)"), 16, 45, 4, decimals=0)
    b.stat("Edits accepted", prom(
        f'sum(increase(claude_code_code_edit_tool_decision_total{{decision="accept", {PU}}}[$__range])) / '
        f"sum(increase(claude_code_code_edit_tool_decision_total{{{PU}}}[$__range]))"), 20, 45, 4,
        unit="percentunit", desc="Share of Edit/Write/NotebookEdit proposals accepted.")
    b.table("Usage by user & model", [
        loki(f"sum by (user_email, model) (sum_over_time({CC_REQ} | unwrap cost_usd [$__range]))", instant=True),
        loki(f"sum by (user_email, model) (count_over_time({CC_REQ} [$__range]))", instant=True),
        loki(f"sum by (user_email, model) (sum_over_time({CC_REQ} | unwrap input_tokens [$__range]))", instant=True),
        loki(f"sum by (user_email, model) (sum_over_time({CC_REQ} | unwrap output_tokens [$__range]))", instant=True),
        loki(f"sum by (user_email, model) (sum_over_time({CC_REQ} | unwrap cache_read_tokens [$__range]))", instant=True),
        loki(f"quantile_over_time(0.95, {CC_REQ} | unwrap ttft_ms [$__range]) by (user_email, model)", instant=True),
    ], ["Cost ($)", "Requests", "Input tokens", "Output tokens", "Cache read tokens", "p95 TTFT (ms)"], 0, 49)
    b.logs("API errors & refusals", loki(f'{CC} | event_name=~"api_error|api_refusal" | {U}'), 0, 58, h=8)

    # ----- Codex -----
    b.tab("Codex")
    API = f'{CX} | event_name=~"codex.api_request|codex.websocket_request" | user_email=~"$user"'
    b.stat("Prompts", loki(
        f'{scount(CX + " | event_name=\"codex.user_prompt\" | " + U)} or vector(0)', instant=True),
        0, 0, 3, decimals=0)
    b.stat("Model responses", loki(scount(CX_RESP), instant=True), 3, 0, 3, decimals=0)
    b.stat("Input tokens", loki(ssum(CX_RESP, "input_token_count"), instant=True), 6, 0, 3, decimals=0)
    b.stat("Cached input", loki(
        f"{ssum(CX_RESP, 'cached_token_count')} / {ssum(CX_RESP, 'input_token_count')}", instant=True),
        9, 0, 3, unit="percentunit", desc="Share of input tokens served from the prompt cache.")
    b.stat("Output tokens", loki(ssum(CX_RESP, "output_token_count"), instant=True), 12, 0, 3, decimals=0)
    b.stat("Reasoning tokens", loki(ssum(CX_RESP, "reasoning_token_count"), instant=True), 15, 0, 3, decimals=0)
    b.stat("p95 time to first token", loki(
        f'quantile_over_time(0.95, {CX} | event_name="codex.turn_ttft" | {U} | unwrap duration_ms [$__range]) by ()',
        instant=True), 18, 0, 3, unit="ms", thresholds=ttft_th, desc="From prompt submitted to first token of the turn.")
    b.stat("Failed API requests", loki(
        f'{scount(API + " | success=\"false\"")} or vector(0)', instant=True), 21, 0, 3, decimals=0, thresholds=ok_bad)

    b.series("Tokens by type", [
        loki(f"sum(sum_over_time({CX_RESP} | unwrap {f} [$__auto]))", n) for f, n in (
            ("input_token_count", "input"), ("cached_token_count", "cached input"),
            ("output_token_count", "output"), ("reasoning_token_count", "reasoning"))], 0, 4, bars=True)
    b.series("Tokens by model", [loki(
        f"sum by (model) (sum_over_time({CX_RESP} | unwrap input_token_count [$__auto]))", "{{model}} input"),
        loki(f"sum by (model) (sum_over_time({CX_RESP} | unwrap output_token_count [$__auto]))", "{{model}} output")],
        12, 4, bars=True)
    b.series("Time to first token by model", [
        loki(f'quantile_over_time({p}, {CX} | event_name="codex.turn_ttft" | {U} | unwrap duration_ms [$__auto]) by (model)',
             f"{n} turn {{{{model}}}}") for p, n in (("0.5", "p50"), ("0.95", "p95"))] + [
        loki(f"quantile_over_time(0.95, {CX_RESP} | unwrap ttft_ms [$__auto]) by (model)", "p95 response {{model}}")],
        0, 12, unit="ms", desc="Turn = from prompt to first token; response = per model response.")
    b.series("Turn duration p95 by model", [prom(
        'histogram_quantile(0.95, sum by (le, model) (rate(codex_turn_e2e_duration_ms_milliseconds_bucket'
        f'{{model=~"$model"}}[{RI}])))', "{{model}}")], 12, 12, unit="ms",
        desc="End-to-end turn time from Codex metrics (not filtered by user).")
    b.series("API request latency p95", [loki(
        f"quantile_over_time(0.95, {API} | unwrap duration_ms [$__auto]) by (event_name)", "{{event_name}}")],
        0, 20, unit="ms")
    b.series("API requests by status", [loki(
        f'sum by (http_response_status_code, success) (count_over_time({API} [$__auto]))',
        "HTTP {{http_response_status_code}} success={{success}}")], 12, 20, bars=True)
    TR = f'{CX} | event_name="codex.tool_result" | user_email=~"$user"'
    b.series("Tool calls by tool", [loki(
        f"sum by (tool_name) (count_over_time({TR} [$__auto]))", "{{tool_name}}")], 0, 28, bars=True)
    b.table("Conversations by model & settings", [loki(
        f'sum by (model, reasoning_effort, approval_policy, sandbox_policy) (count_over_time({CX} '
        f'| event_name="codex.conversation_starts" | {U} [$__range]))', instant=True)],
        ["Conversations"], 12, 28, 12, 8)
    b.table("Tool decisions", [loki(
        f'sum by (tool_name, decision, source) (count_over_time({CX} | event_name="codex.tool_decision" '
        f'| user_email=~"$user" [$__range]))', instant=True)], ["Count"], 0, 36, 12, 8)
    b.table("Tool results", [
        loki(f"sum by (tool_name) (count_over_time({TR} [$__range]))", instant=True),
        loki(f'sum by (tool_name) (count_over_time({TR} | success="true" [$__range])) / '
             f"sum by (tool_name) (count_over_time({TR} [$__range]))", instant=True),
        loki(f"quantile_over_time(0.95, {TR} | unwrap duration_ms [$__range]) by (tool_name)", instant=True),
    ], ["Calls", "Success rate", "p95 duration (ms)"], 12, 36, 12, 8)
    b.table("Usage by user & model", [
        loki(f"sum by (user_email, model) (count_over_time({CX_RESP} [$__range]))", instant=True),
        loki(f"sum by (user_email, model) (sum_over_time({CX_RESP} | unwrap input_token_count [$__range]))", instant=True),
        loki(f"sum by (user_email, model) (sum_over_time({CX_RESP} | unwrap cached_token_count [$__range]))", instant=True),
        loki(f"sum by (user_email, model) (sum_over_time({CX_RESP} | unwrap output_token_count [$__range]))", instant=True),
        loki(f"sum by (user_email, model) (sum_over_time({CX_RESP} | unwrap reasoning_token_count [$__range]))", instant=True),
    ], ["Responses", "Input tokens", "Cached input tokens", "Output tokens", "Reasoning tokens"], 0, 44)
    b.logs("Failed requests, warnings & errors", loki(
        f'{CX} | user_email=~"$user" | success="false" or detected_level=~"warn|error"'), 0, 53, h=8)

    agents = "claude_code_token_usage_tokens_total|codex_turn_token_usage_count"
    return dashboard("ai-coding-agents", "AI Coding Agents",
                     "Claude Code and Codex CLI usage from their built-in OpenTelemetry export.", b, [
        variable("user", "User", "label_values(claude_code_session_count_total, user_email)"),
        variable("model", "Model", f'label_values({{__name__=~"{agents}"}}, model)'),
    ])


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.json"):
        old.unlink()
    for name, fn in [("llm-observability", llm_observability), ("ai-coding-agents", ai_coding_agents)]:
        (OUT / f"{name}.json").write_text(json.dumps(fn(), indent=2) + "\n")
        print("wrote", name)
