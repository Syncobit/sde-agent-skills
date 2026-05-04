# Non-Cloud Backends — Datadog, Splunk, New Relic, Grafana, Honeycomb, Dynatrace, Elastic

`gcp-pipeline.md` and `aws-pipeline.md` cover the cloud-native paths (Cloud Trace, X-Ray, CloudWatch). In practice, most enterprise teams ship OTel data to a third-party observability backend instead — for multi-cloud portability, better UX, or because of an existing vendor commitment. All major vendors now accept OTLP natively or via a short bridge.

This file is the working exporter config and the per-vendor gotchas. The pattern is the same everywhere: in your apps, export OTLP to your gateway Collector; in the gateway, configure a vendor-specific exporter and route signals to it.

## The general shape

```
[ App OTLP ] ──→ [ Gateway Collector ] ──→ [ Vendor backend ]
                       │
                       ├─ memory_limiter
                       ├─ redaction
                       ├─ tail_sampling
                       ├─ batch
                       └─ exporter (vendor-specific)
```

The app side is unchanged from `instrumentation-polyglot.md` — vendor-neutral OTLP. Switching vendors becomes a Collector config change, not a code deploy.

## Datadog

Datadog accepts OTLP natively and is the most common third-party destination in enterprise. Two paths:

### Path A — OTLP exporter (recommended for OTel-first shops)

```yaml
exporters:
  otlphttp/datadog:
    endpoint: https://trace.agent.datadoghq.com    # US site; see regions below
    headers:
      dd-api-key: "${env:DD_API_KEY}"
    sending_queue: { enabled: true, queue_size: 10000, storage: file_storage }
    retry_on_failure: { enabled: true, max_elapsed_time: 5m }

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, tail_sampling, batch]
      exporters: [otlphttp/datadog]
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [otlphttp/datadog]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [otlphttp/datadog]
```

Datadog regional endpoints (substitute in `endpoint`):

| Region | Endpoint |
|--------|----------|
| US1 | `https://trace.agent.datadoghq.com` |
| US3 | `https://trace.agent.us3.datadoghq.com` |
| US5 | `https://trace.agent.us5.datadoghq.com` |
| EU | `https://trace.agent.datadoghq.eu` |
| AP1 (Tokyo) | `https://trace.agent.ap1.datadoghq.com` |
| Gov | `https://trace.agent.ddog-gov.com` |

For metrics use `https://api.datadoghq.com/api/intake/otlp/v1/metrics`, for logs use `https://http-intake.logs.datadoghq.com/api/v2/logs` (path differs per signal — consult Datadog docs for the current paths).

### Path B — Datadog exporter (Datadog-curated)

```yaml
exporters:
  datadog:
    api:
      site: datadoghq.eu
      key: "${env:DD_API_KEY}"
    traces:
      compute_stats_by_span_kind: true              # generates APM stats
      peer_tags_aggregation: true                   # better service map
    metrics:
      resource_attributes_as_tags: true             # propagate resource attrs as tags
    logs:
      use_compression: true
```

Path B uses the Datadog-maintained exporter (lives in opentelemetry-collector-contrib), which produces APM-style trace stats Datadog's UI expects. **For richest Datadog UX use Path B; for vendor portability use Path A.**

### Gotchas

- **`service.name` becomes Datadog's `service` tag**; `deployment.environment.name` becomes `env`. Without these, Datadog APM views are empty.
- **APM stats vs raw traces**: Datadog computes top-N stats from sampled traces server-side. If you sample at 1% before Datadog, your "request rate" view is wrong by 100x. Use the Datadog exporter's `compute_stats_by_span_kind: true` to compute stats *before* sampling — Datadog ingests the stats, then samples the raw traces.
- **Container tag mapping**: Datadog wants `container_id`, `kube_*` tags. The k8sattributes processor produces these, but Datadog's expected names differ from OTel's — Path B handles the mapping; Path A doesn't.
- **API key in env, not config file** — never commit. Use K8s Secret + envFrom.

## Splunk Observability Cloud (formerly SignalFx)

Splunk Observability Cloud accepts OTLP natively via the Splunk Distribution of OpenTelemetry Collector or upstream + OTLP exporter.

```yaml
exporters:
  otlphttp/splunk_o11y:
    endpoint: "https://ingest.${env:SPLUNK_REALM}.signalfx.com/v2/trace"
    headers:
      x-sf-token: "${env:SPLUNK_ACCESS_TOKEN}"
    sending_queue: { enabled: true, storage: file_storage }
    retry_on_failure: { enabled: true }
```

For metrics:
```yaml
  signalfx:
    access_token: "${env:SPLUNK_ACCESS_TOKEN}"
    realm: "${env:SPLUNK_REALM}"                    # us0, us1, eu0, etc.
```

For logs (Splunk Enterprise / Cloud, not Observability Cloud — those go through the HEC):
```yaml
  splunk_hec:
    token: "${env:SPLUNK_HEC_TOKEN}"
    endpoint: "https://hec.${env:SPLUNK_DOMAIN}:8088/services/collector"
    source: "otel"
    index: "main"
    tls: { insecure_skip_verify: false }
```

### Gotchas

- **Realm in URL** — wrong realm = silent 404. Verify with `curl https://ingest.${REALM}.signalfx.com/v2/datapoint`.
- **Two different products**: Splunk Observability Cloud (OTLP-native, formerly SignalFx) and Splunk Enterprise (logs via HEC). Don't mix the tokens.
- **Metric type defaults**: SignalFx infers metric types differently than OTel. Use the `signalfx` exporter (not generic OTLP) for metrics or types may be wrong (gauge vs counter).

## New Relic

New Relic has been OTLP-native since 2022. Single endpoint per region:

```yaml
exporters:
  otlphttp/newrelic:
    endpoint: https://otlp.nr-data.net                # US
    # endpoint: https://otlp.eu01.nr-data.net         # EU
    headers:
      api-key: "${env:NEW_RELIC_LICENSE_KEY}"
    sending_queue: { enabled: true, storage: file_storage }
    retry_on_failure: { enabled: true }
    compression: gzip
```

### Gotchas

- **1MB payload limit per OTLP request** (compressed). Tune the `batch` processor: `send_batch_max_size: 1024` for traces, less for very-attribute-heavy spans. Hitting the limit = silent partial drop.
- **License key vs ingest key** — older accounts use license keys; new ones use ingest keys with a different header (`Api-Key`). Both work as the `api-key` header in Collector config.
- **Resource attributes become entity attributes**. `service.name` is required for entity correlation in New Relic UI. Without it traces appear under a generic "Unknown" entity.
- **Not all OTel signals at parity**: traces are mature, metrics good, logs supported but UI features lag.

## Grafana Cloud (Tempo / Mimir / Loki)

Grafana Cloud is the OSS-native enterprise stack: traces in Tempo, metrics in Mimir, logs in Loki. Each accepts OTLP. Authentication is HTTP Basic with the instance ID as username and an API token as password.

```yaml
exporters:
  otlphttp/grafana_traces:
    endpoint: "https://tempo-prod-04-prod-eu-west-0.grafana.net/tempo"
    auth: { authenticator: basicauth/grafana }
    sending_queue: { enabled: true, storage: file_storage }

  otlphttp/grafana_metrics:
    endpoint: "https://prometheus-prod-13-prod-eu-west-0.grafana.net/api/prom/push"
    auth: { authenticator: basicauth/grafana }

  otlphttp/grafana_logs:
    endpoint: "https://logs-prod-eu-west-0.grafana.net/loki/api/v1/push"
    auth: { authenticator: basicauth/grafana }

extensions:
  basicauth/grafana:
    client_auth:
      username: "${env:GRAFANA_INSTANCE_ID}"
      password: "${env:GRAFANA_API_TOKEN}"

service:
  extensions: [basicauth/grafana, file_storage]
  pipelines:
    traces:  { exporters: [otlphttp/grafana_traces] }
    metrics: { exporters: [otlphttp/grafana_metrics] }
    logs:    { exporters: [otlphttp/grafana_logs] }
```

### Gotchas

- **Three different endpoints, three different "instance IDs"**. Tempo, Mimir, and Loki each have their own — don't reuse one for all three.
- **Per-region endpoints** — use the URL Grafana Cloud's UI shows for your stack, not a generic one.
- **Loki labels vs OTel attributes**: Loki has aggressive cardinality limits on labels. Configure the Collector's `attributes` processor to demote high-cardinality OTel attributes to log body fields, not labels.
- **Mimir cardinality**: same concern as Prometheus generally — drop high-cardinality attributes before export.

## Honeycomb

Honeycomb is OTLP-native and uses dataset routing via the `x-honeycomb-dataset` header.

```yaml
exporters:
  otlphttp/honeycomb_traces:
    endpoint: https://api.honeycomb.io
    headers:
      x-honeycomb-team: "${env:HONEYCOMB_API_KEY}"
      x-honeycomb-dataset: "vqms-prod"               # required for traces; auto-created
    compression: gzip
    sending_queue: { enabled: true, storage: file_storage }

  otlphttp/honeycomb_metrics:
    endpoint: https://api.honeycomb.io
    headers:
      x-honeycomb-team: "${env:HONEYCOMB_API_KEY}"
      x-honeycomb-dataset: "vqms-prod-metrics"
    compression: gzip
```

EU region: `https://api.eu1.honeycomb.io`.

### Gotchas

- **One dataset per environment** is the recommended pattern (`vqms-prod`, `vqms-staging`). Don't put dev and prod in the same dataset.
- **Dataset auto-creation**: first event creates the dataset. Typo in the name → orphaned dataset. Validate via Honeycomb UI after first deploy.
- **High-cardinality is a feature**: unlike Prometheus-style backends, Honeycomb is built for high-cardinality. Less reason to drop user.id, request.id, etc. — but still redact PII.
- **Sampling**: Honeycomb supports its own dynamic sampling (Refinery). Decide between Collector tail-sampling and Refinery — running both wastes work.

## Dynatrace

Dynatrace accepts OTLP via the OneAgent (auto-instrumentation) or directly to its API.

```yaml
exporters:
  otlphttp/dynatrace:
    endpoint: "${env:DT_TENANT_URL}/api/v2/otlp"
    headers:
      authorization: "Api-Token ${env:DT_API_TOKEN}"
    sending_queue: { enabled: true, storage: file_storage }
```

`DT_TENANT_URL` looks like `https://abc12345.live.dynatrace.com` for SaaS or `https://dynatrace.example.com/e/UUID` for Managed.

API token needs scopes: `openTelemetryTrace.ingest`, `metrics.ingest`, `logs.ingest`.

### Gotchas

- **OneAgent vs OTLP**: if OneAgent is also installed on your hosts, it auto-instruments; combining with OTel produces duplicate spans. Pick one path. For containerized workloads, OTLP is usually cleaner.
- **Token scopes per signal** — a token without `metrics.ingest` silently 401s on metrics. Check token permissions in Dynatrace UI when one signal works and another doesn't.

## Elastic Observability

Elastic accepts OTLP at `apm-server` directly (Elastic Cloud or self-hosted).

```yaml
exporters:
  otlp/elastic:
    endpoint: "${env:ELASTIC_APM_ENDPOINT}:443"      # apm-server
    headers:
      authorization: "Bearer ${env:ELASTIC_APM_SECRET_TOKEN}"
    tls: { insecure: false }
    sending_queue: { enabled: true, storage: file_storage }
```

Elastic Cloud's APM endpoints look like `https://abc123.apm.us-central1.gcp.cloud.es.io`.

### Gotchas

- **Elastic version matters**: APM Server ≥ 8.0 is required for OTLP; older versions accept only Elastic's own protocol.
- **Index lifecycle**: Elastic indexes traces in `traces-apm-*` data streams with ILM rolling. Configure ILM for retention you want; default is 30 days.

## Multi-vendor fan-out

Sending to two backends during a vendor migration or for HA:

```yaml
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, redaction, batch]
      exporters: [otlphttp/datadog, otlphttp/honeycomb_traces]   # both!
```

The Collector duplicates each export. Cost: ~2x egress and per-event ingestion cost on the vendor side. Useful during cutover; not a long-term posture.

## Cost-aware routing — "two tiers, one set of apps"

Send error traces and slow traces to a high-cost vendor (Honeycomb, Datadog) for rich debugging; send the bulk to a low-cost backend (Tempo / self-hosted Jaeger) for retention:

```yaml
processors:
  routing/traces:
    from_attribute: trace.tier
    table:
      - value: hot
        exporters: [otlphttp/honeycomb_traces]
      - value: cold
        exporters: [otlp/tempo]
    default_exporters: [otlp/tempo]

  # Tag traces based on properties
  transform/tag_tier:
    trace_statements:
      - context: span
        statements:
          - set(attributes["trace.tier"], "hot") where status.code == STATUS_CODE_ERROR
          - set(attributes["trace.tier"], "hot") where duration_unix_nano > 1000000000   # >1s
          - set(attributes["trace.tier"], "cold") where attributes["trace.tier"] == nil

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, transform/tag_tier, routing/traces, batch]
      exporters: [otlphttp/honeycomb_traces, otlp/tempo]
```

Per-trace tier decision saves significant cost: 95% of traces go to the cheap backend, 5% (errors + slow) go to the expensive one with rich UI.

## What every backend exporter needs

Regardless of vendor, configure these on every exporter:

```yaml
sending_queue:
  enabled: true
  num_consumers: 4
  queue_size: 5000
  storage: file_storage         # persistent — see collector-production.md

retry_on_failure:
  enabled: true
  initial_interval: 5s
  max_interval: 30s
  max_elapsed_time: 300s

timeout: 30s                    # per-call timeout
compression: gzip               # always
```

Without these, a brief vendor outage drops data. With them, the queue absorbs the outage and drains when the backend recovers.

## Common pitfalls

**Pasting the wrong region's endpoint.** Datadog `datadoghq.com` (US1) vs `datadoghq.eu` (EU) — silent failure if your account is in the other region. Always verify with a curl test from the Collector pod.

**API key in ConfigMap or git.** Use Secrets, mount via `envFrom`. Rotate quarterly.

**Sending all signals to the same vendor without checking pricing.** Logs are usually 5-50x more expensive than traces or metrics. A vendor that's cheap for traces may be wildly expensive for logs at the same volume. Price each signal separately before committing.

**Vendor-specific resource attributes missing.** Datadog wants `env`, `service`, `version` (auto-mapped from `deployment.environment.name`, `service.name`, `service.version`). New Relic wants `service.name` for entity grouping. Honeycomb wants nothing specific but benefits from `service.name`. Always set the trio.

**Cardinality at the vendor.** Even high-cardinality-friendly vendors charge by event count. A `user.id` attribute on a million-user app is fine cardinality-wise but multiplies ingestion cost if you're not sampling. Combine vendor choice with appropriate sampling (`sampling-and-cost.md`).

**Skipping the OTel Collector for "simplicity".** Going SDK → vendor exporter directly works but locks every app to that vendor. The first time you need to migrate or dual-export during a migration, every service needs a redeploy. The Collector is one config change.

**Authentication by query parameter.** Some vendors historically supported `?api_key=...` in the URL. Don't — query strings end up in proxy logs. Use headers.

## Quick checklist

```markdown
## Non-cloud backend review

- [ ] Endpoint matches your account's region
- [ ] API key from Secret, not ConfigMap; never in git
- [ ] sending_queue with file_storage on every vendor exporter
- [ ] retry_on_failure with explicit max_elapsed_time
- [ ] Compression enabled (gzip minimum)
- [ ] service.name, service.version, deployment.environment.name set on all telemetry
- [ ] Vendor-specific attributes mapped (Datadog env/service/version, etc.)
- [ ] Logs vs traces vs metrics priced separately and budgeted
- [ ] Sampling configured before export (vendor cost = event count × rate)
- [ ] Cutover/HA plan if dual-exporting (cost implications documented)
- [ ] Auth by header, never by query parameter
```

## Sources

- Datadog OTLP ingestion — docs.datadoghq.com/opentelemetry/otlp_ingest_in_the_agent/
- Splunk Observability Cloud OTel — docs.splunk.com/Observability/gdi/opentelemetry/opentelemetry.html
- New Relic OTLP — docs.newrelic.com/docs/opentelemetry/best-practices/opentelemetry-otlp/
- Grafana Cloud OTLP — grafana.com/docs/grafana-cloud/send-data/otlp/
- Honeycomb OTLP — docs.honeycomb.io/getting-data-in/opentelemetry-overview/
- Dynatrace OTLP — docs.dynatrace.com/docs/extend-dynatrace/opentelemetry
- Elastic OTLP — elastic.co/guide/en/observability/current/apm-open-telemetry.html