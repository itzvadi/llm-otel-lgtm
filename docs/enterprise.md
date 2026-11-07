# Enterprise deployment

The local stack uses the all-in-one `grafana/otel-lgtm` image. For production, keep the same
collector config, dashboards and Python library, and swap the backends for scalable ones.

## Components

| Local | Production |
|---|---|
| `grafana/otel-lgtm` Prometheus | **Mimir** (`grafana/mimir-distributed` Helm chart) on object storage |
| `grafana/otel-lgtm` Loki | **Loki** (`grafana/loki` chart, simple-scalable mode) on object storage |
| `grafana/otel-lgtm` Tempo | **Tempo** (`grafana/tempo-distributed` chart) on object storage |
| Grafana container | **Grafana 12 or later** (`grafana/grafana` chart) or Grafana Cloud. The dashboards use the v2 schema with tabs. |
| Collector container | Collector as a Deployment behind a load balancer (`open-telemetry/opentelemetry-collector` chart) |

Object storage can be S3, GCS or Azure Blob.

## Checklist

1. **Collector exporters:** replace `otlp_grpc/lgtm` with `otlphttp` exporters to Mimir
   (`/otlp`), Loki (`/otlp`) and Tempo (OTLP gRPC). Keep the `delta_to_cumulative` processor in the
   metrics pipeline, because Claude Code sends delta metrics by default.
2. **Datasource UIDs:** provision Grafana datasources with the UIDs `prometheus`, `loki` and `tempo`
   so the dashboards work unchanged. Copy `grafana/provisioning` into the Grafana chart values.
3. **Counter start times:** enable created-timestamp (start-time) ingestion in Mimir or Prometheus
   (`--enable-feature=created-timestamp-zero-ingestion` on Prometheus), so counters from short-lived CLI
   processes are counted from their first sample.
4. **Multi-tenancy:** send `X-Scope-OrgID: <team>` from the collector (one pipeline per tenant,
   or the `headers_setter` extension) and create one Grafana datasource per tenant.
5. **Security:** TLS or mTLS on the collector's OTLP receivers, and the `bearertokenauth` extension for
   apps. Claude Code sends extra headers through `OTEL_EXPORTER_OTLP_HEADERS`, Codex through
   `headers = { … }` in its exporter. Keep API keys in a secret manager, never in config files.
6. **Prompt logging:** keep `PROMPT_LOGGING=off` and the `prompts-off` collector config unless your
   data policy allows storing prompts. The filter runs in the collector, so it applies even to
   misconfigured clients, and it covers Claude Code and Codex content attributes.
7. **Personal data:** Claude Code and Codex always attach `user.email`. To remove it, uncomment the
   `user.email` line in `collector/otel-collector.prompts-off.yaml`. Per-user views then fall back to
   account IDs.
8. **SSO and access:** configure Grafana OAuth/SAML, and use folder permissions per team. Turn off
   anonymous access (`GF_AUTH_ANONYMOUS_ENABLED=false`).
9. **Retention:** set retention per backend, for example metrics 13 months, traces 14 days, logs 30 days.
   Logs hold the exact per-request cost for the coding-agent dashboards, so keep them as long as you
   need cost reporting.
10. **Sampling:** add the `tail_sampling` processor to keep all errors and slow traces and sample the rest.
11. **Alerts:** start with error rate > 5%, p95 latency > 15s, p95 time to first token > 5s, projected
    daily spend over budget, and a spike in `error_type="rate_limit_429"`.

## Rolling out Claude Code and Codex telemetry

**Claude Code:** put the telemetry `env` block from [add-your-llm.md](add-your-llm.md#1-claude-code) in
Claude Code's managed settings file and deploy it with MDM. Point `OTEL_EXPORTER_OTLP_ENDPOINT` at the
shared collector, and add `OTEL_RESOURCE_ATTRIBUTES=team.id=…,cost_center=…` for team views. On large
fleets, set `OTEL_METRICS_INCLUDE_SESSION_ID=false` to keep the number of metric series down.

**Codex:** deploy the `[otel]` section from [add-your-llm.md](add-your-llm.md#2-codex-cli) in each user's
`~/.codex/config.toml`, using your configuration management.
