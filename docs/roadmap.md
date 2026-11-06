# Roadmap

## Status (v0.1.0)

- [x] Local stack: grafana/otel-lgtm + OpenTelemetry Collector + Grafana (docker compose)
- [x] Python `llm_telemetry` library for OpenAI, Anthropic Claude, xAI Grok
- [x] Prompt logging switch (`PROMPT_LOGGING=off|on`), enforced in the collector
- [x] Optional LiteLLM gateway, mock demo traffic
- [x] Five pre-built dashboards
- [x] Repository description
- [ ] Repository topics (not showing yet; re-check)
- [ ] Release `v0.1.0` (not published yet)

## Next session

1. Run the stack on a machine with Docker; confirm all dashboards render.
2. Real API test with OpenAI (ChatGPT) and Anthropic (Claude) keys, `LLM_MOCK=false`.
3. Update `pricing.yaml` with current provider prices.
4. Capture dashboard screenshots (Overview, Provider Comparison, Cost) into `docs/images/`
   and add them to the README.
5. LinkedIn post: screenshots, one-command quick start, link to the repo.

## Later

- Streaming support (real time-to-first-token)
- Grafana alert rules provisioned by default
- Helm values for Mimir / Loki / Tempo (enterprise)
- More providers (Gemini, Mistral), Node.js library
- CI: validate dashboards and run a compose smoke test
