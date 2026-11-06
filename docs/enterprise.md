# Enterprise deployment

The local stack uses the all-in-one `grafana/otel-lgtm` image. For production, keep the same
collector config, dashboards and Python library, and swap the backends for scalable ones.

## Components

| Local | Production |
|---|---|
| `grafana/otel-lgtm` Prometheus | **Mimir** (`grafana/mimir-distributed` Helm chart) on object storage |
| `grafana/otel-lgtm` Loki | **Loki** (`grafana/loki` chart, simple-scalable mode) on object storage |
| `grafana/otel-lgtm` Tempo | **Tempo** (`grafana/tempo-distributed` chart) on object storage |
| Grafana container | **Grafana** (`grafana/grafana` chart) or Grafana Cloud |
| Collector container | Collector as a Deployment behind a load balancer (`open-telemetry/opentelemetry-collector` chart) |

Object storage can be S3, GCS or Azure Blob.

## Checklist

1. **Collector exporters:** replace `otlp/lgtm` with `otlphttp` exporters to Mimir
   (`/otlp`), Loki (`/otlp`) and Tempo (OTLP gRPC).
2. **Datasource UIDs:** provision Grafana datasources with the UIDs `prometheus`, `loki` and `tempo`
   so the dashboards work unchanged. Copy `grafana/provisioning` into the Grafana chart values.
3. **Multi-tenancy:** send `X-Scope-OrgID: <team>` from the collector (one pipeline per tenant,
   or the `headers_setter` extension) and create one Grafana datasource per tenant.
4. **Security:** TLS or mTLS on the collector's OTLP receivers, and the `bearertokenauth` extension for
   apps. Keep API keys in a secret manager, never in config files.
5. **Prompt logging:** keep `PROMPT_LOGGING=off` and the `prompts-off` collector config unless your
   data policy allows storing prompts. Since the filter runs in the collector, it applies even to
   misconfigured apps.
6. **SSO and access:** configure Grafana OAuth/SAML, and use folder permissions per team.
7. **Retention:** set retention per backend, for example metrics 13 months, traces 14 days, logs 30 days.
8. **Sampling:** add the `tail_sampling` processor to keep all errors and slow traces and sample the rest.
9. **Alerts:** start with error rate > 5%, p95 latency > 15s, projected daily spend over budget, and a spike in
   `error_type=~".*429.*"`.
