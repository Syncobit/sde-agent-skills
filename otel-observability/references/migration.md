# Migrating to OpenTelemetry — From X-Ray, Jaeger, Zipkin, StatsD, Prometheus, Datadog APM

Most enterprises don't start observability from scratch. They have years of investment in X-Ray segments, Datadog APM agents, Prometheus scrape configs, statsd dashboards, Jaeger UIs. A migration to OTel is rarely a big-bang cutover; it's a series of coexistence steps that each reduce risk.

This file covers the practical patterns: how to run old and new in parallel, how to preserve trace correlation across the boundary, how to validate equivalence before switching, and the gotchas in each source-system migration.

## Migration principles

1. **Run in parallel.** Both old and new emit telemetry for at least 2 weeks. Compare. Cut over when confident.
2. **Don't re-instrument all at once.** Service-by-service. Edge service first (low blast radius), critical-path service last.
3. **Preserve trace ID compatibility.** A trace started in an old SDK and continued in OTel must remain joinable. Use compatible ID formats and propagators during the bridge period.
4. **Decommission only after verification.** Keep the old pipeline writing to its old backend until you have N weeks of confidence in the new one. The cost of running both is small compared to the cost of a regression caught after switch-off.
5. **Migrate signal-by-signal where useful.** Traces first (highest debugging value), then logs (operational), then metrics (longest tail of dashboards/alerts to update).

## Migration order — recommended

```
Phase 0: Readiness                          (1-2 weeks)
  ├─ Deploy gateway Collector
  ├─ Validate end-to-end with one test service
  └─ Set up dual-export (write to old AND new backend)

Phase 1: New service onboarding              (ongoing)
  └─ All NEW services use OTel from day one

Phase 2: Migrate existing services           (per service: 1-2 weeks)
  ├─ Add OTel SDK alongside existing instrumentation
  ├─ Run in parallel; verify span/metric/log equivalence
  ├─ Remove old SDK
  └─ Update dashboards/alerts/runbooks

Phase 3: Decommission old pipeline           (after all services migrated)
  ├─ Stop writing to old backend
  ├─ Migrate or archive historical data
  └─ Cancel old vendor contract (if applicable)
```

Don't start Phase 2 until Phase 0 is rock-solid. A flaky gateway is a trust problem that taints the whole migration.

## From AWS X-Ray SDK

X-Ray SDKs (`aws-xray-sdk-python`, `aws-xray-sdk-node`, `aws-xray-sdk-go`, `aws-xray-sdk-java`) are AWS's pre-OTel tracing libraries. Migration:

### Coexistence — keep X-Ray ID format during the bridge period

X-Ray uses a non-W3C trace ID format with a timestamp prefix. If you have AWS-managed services in the trace path (API Gateway, ALB, Lambda), they emit X-Ray segments with X-Ray IDs. To keep these joinable with OTel spans, configure the OTel SDK with the X-Ray ID generator and propagator:

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
from opentelemetry.propagators.aws import AwsXRayPropagator
from opentelemetry import propagate

propagate.set_global_textmap(AwsXRayPropagator())
provider = TracerProvider(id_generator=AwsXRayIdGenerator())
```

This is not OTel's preferred state — eventually you want W3C IDs everywhere. But during migration, X-Ray-format IDs let new OTel spans show up as continuation of existing X-Ray traces in the X-Ray UI *and* in the OTel backend.

### Step-by-step replacement

```python
# Before — X-Ray SDK
from aws_xray_sdk.core import xray_recorder, patch_all
patch_all()                                        # auto-instruments boto3, requests, etc.

with xray_recorder.in_segment("process_order") as segment:
    segment.put_annotation("order.id", order_id)
    segment.put_metadata("payload", small_payload)
    process(order)

# After — OTel SDK with X-Ray compatibility
from opentelemetry import trace
tracer = trace.get_tracer(__name__)

with tracer.start_as_current_span("process_order") as span:
    span.set_attribute("order.id", order_id)        # annotations → attributes
    span.set_attribute("order.payload_size", len(small_payload))   # metadata is gone; pick what to keep
    process(order)
```

X-Ray's "annotations" map to OTel attributes. X-Ray's "metadata" doesn't map directly — OTel attributes are flat key-value, not nested. Decide what's worth keeping; typically large metadata blobs become span events or stay in logs.

### Validation

Run both SDKs simultaneously for 1-2 weeks. The OTel side ships to your gateway, the X-Ray SDK ships to X-Ray. Compare:
- Span count per minute (should be identical or OTel slightly higher due to better library coverage)
- p50/p99 latency per operation (should match within 5%)
- Error rate (should match exactly)

If counts diverge significantly, the OTel SDK is missing instrumentation the X-Ray SDK provides — usually a library that needs an additional `opentelemetry-instrumentation-*` package.

### Gotchas

- **Lambda integration**: AWS_XRAY_TRACING_NAME and X-Ray's auto-segment in Lambda still apply. Use ADOT layer (see `aws-pipeline.md`) which integrates the OTel SDK with Lambda's X-Ray auto-segment.
- **Service map**: X-Ray's service map is computed server-side from segments. Mid-migration, services that mix X-Ray and OTel-via-X-Ray-IDs map correctly. Pure-OTel-with-W3C-IDs fall off the X-Ray service map; that's the cue you've fully migrated.
- **Sampling rules**: X-Ray's centralized sampling rules are read by the X-Ray SDK from the X-Ray API. OTel SDKs don't read these — sampling moves to OTel SDK config (`OTEL_TRACES_SAMPLER`) or Collector tail sampling.

## From Jaeger

Jaeger client libraries (`jaeger-client-python`, `jaeger-client-node`, `jaeger-client-go`) are deprecated — Jaeger officially recommends migrating to OTel SDKs (announced 2022, accelerated 2023+).

### The good news

Jaeger Collector and Jaeger Query Backend already accept OTLP. You can keep the Jaeger UI and storage during migration:

```yaml
# Apps export OTLP (instead of Jaeger Thrift) to your OTel Collector
# OTel Collector exports OTLP to Jaeger Collector

exporters:
  otlp/jaeger:
    endpoint: jaeger-collector.observability:4317
    tls: { insecure: true }                           # internal cluster
```

Result: you migrate the SDKs first (low-risk per-service work) and keep the UI you already know. Migrate the backend (Tempo, Datadog, Honeycomb) as a separate phase.

### Migration

```python
# Before — jaeger-client
from jaeger_client import Config
config = Config(config={'sampler': {'type': 'const', 'param': 1}}, service_name='vqms-api')
tracer = config.initialize_tracer()

with tracer.start_span('process_order') as span:
    span.set_tag('order.id', order_id)

# After — OTel SDK
from opentelemetry import trace
tracer = trace.get_tracer('vqms-api')

with tracer.start_as_current_span('process_order') as span:
    span.set_attribute('order.id', order_id)
```

Jaeger "tags" map directly to OTel "attributes". Jaeger "logs" (timestamped events on a span) map to OTel "events". The mental model is the same; just the names changed.

### Gotchas

- **Propagation format**: Jaeger SDKs default to the Jaeger format (`uber-trace-id` header). OTel defaults to W3C (`traceparent`). During the bridge, register both propagators on the OTel side:
  ```python
  from opentelemetry.propagate import set_global_textmap
  from opentelemetry.propagators.composite import CompositePropagator
  from opentelemetry.propagators.jaeger import JaegerPropagator
  from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
  set_global_textmap(CompositePropagator([TraceContextTextMapPropagator(), JaegerPropagator()]))
  ```
- **Sampler config**: Jaeger's adaptive sampler (per-operation rate) doesn't have a direct OTel equivalent. Move to head-based ratio sampling + Collector tail sampling.

## From Zipkin

Same shape as Jaeger — Zipkin clients are largely deprecated; Zipkin servers accept OTLP via the Zipkin v2 receiver in the OTel Collector.

```yaml
exporters:
  zipkin/legacy:
    endpoint: http://zipkin.observability:9411/api/v2/spans
```

Apps move to OTel SDK; Collector emits in Zipkin format to keep the existing Zipkin UI. Same coexistence story.

Zipkin's B3 propagation (`b3` or `X-B3-*` headers) is standard at companies using Zipkin or older Spring Cloud Sleuth. Register the B3 propagator alongside W3C during migration:

```python
from opentelemetry.propagators.b3 import B3MultiFormat
set_global_textmap(CompositePropagator([TraceContextTextMapPropagator(), B3MultiFormat()]))
```

Spring Boot 3+ (with Micrometer Tracing) ships with both W3C and B3 by default — modern Java services migrate cleanly without code changes.

## From StatsD

StatsD has been the dominant lightweight metrics protocol for a decade. Migration to OTel metrics:

### Coexistence — Collector receives StatsD, exports OTLP

```yaml
receivers:
  statsd:
    endpoint: 0.0.0.0:8125
    aggregation_interval: 60s
    enable_metric_type: true
    is_monotonic_counter: false
    timer_histogram_mapping:
      - statsd_type: histogram
        observer_type: histogram

processors:
  memory_limiter: { ... }
  batch: { ... }

exporters:
  otlphttp/backend: { ... }

service:
  pipelines:
    metrics:
      receivers: [statsd]
      processors: [memory_limiter, batch]
      exporters: [otlphttp/backend]
```

Apps continue to emit StatsD via UDP; the Collector converts to OTLP. Zero application code change. Useful when migrating tens or hundreds of services that all use the same StatsD client.

### Step-by-step replacement (when ready)

```python
# Before — datadog statsd client (works the same for any StatsD)
from datadog import statsd
statsd.increment('orders.processed', tags=['tenant:acme', 'status:ok'])
statsd.histogram('orders.latency_ms', latency, tags=['tenant:acme'])

# After — OTel metrics
from opentelemetry import metrics
meter = metrics.get_meter('vqms-api')
order_counter = meter.create_counter('orders.processed')
order_latency = meter.create_histogram('orders.latency_ms', unit='ms')

order_counter.add(1, {'tenant': 'acme', 'status': 'ok'})
order_latency.record(latency, {'tenant': 'acme'})
```

The model is the same — counter, histogram, gauge. The differences:
- OTel attributes (key=value) replace StatsD tags (`key:value`)
- OTel histograms emit explicit bucket boundaries; StatsD aggregated server-side
- OTel exemplars (sample trace IDs attached to histogram buckets) are an upgrade — not available in StatsD

### Gotchas

- **High cardinality**: StatsD aggregation tolerated tags with high cardinality at the cost of memory at the StatsD server. OTel histograms with high-cardinality attributes blow up at the SDK and the backend. Re-evaluate cardinality during migration; drop or bucket aggressively.
- **Non-aggregable counters**: Some StatsD usage emits a counter increment per operation that's later summed. OTel counters work the same way; no change needed.
- **Timing units**: StatsD timers were milliseconds by convention but unit-less in protocol. OTel histograms have an explicit `unit` field — set it (`"ms"`, `"s"`) for backend correctness.

## From Prometheus client libraries

Prometheus and OTel are largely interoperable — both can scrape and ingest each other's formats. Migration is more about *which side scrapes* than rewriting code.

### Path A — keep Prometheus scrape, ship Prometheus → OTel via Collector

```yaml
receivers:
  prometheus:
    config:
      scrape_configs:
        - job_name: vqms-api
          scrape_interval: 30s
          kubernetes_sd_configs:
            - role: pod
          relabel_configs:
            - source_labels: [__meta_kubernetes_pod_label_app]
              target_label: service.name
```

Apps continue to expose `/metrics` in Prometheus format. Collector scrapes and converts to OTLP. Zero application code change.

### Path B — replace Prometheus client with OTel metrics

```python
# Before — prometheus_client
from prometheus_client import Counter, Histogram
order_counter = Counter('orders_processed', 'Orders processed', ['tenant', 'status'])
order_latency = Histogram('orders_latency_seconds', 'Order processing latency')

order_counter.labels(tenant='acme', status='ok').inc()
order_latency.observe(latency)

# After — OTel metrics
from opentelemetry import metrics
meter = metrics.get_meter('vqms-api')
order_counter = meter.create_counter('orders_processed')
order_latency = meter.create_histogram('orders_latency_seconds', unit='s')

order_counter.add(1, {'tenant': 'acme', 'status': 'ok'})
order_latency.record(latency)
```

OTel's metrics API is similar but not identical:

| Prometheus | OTel |
|------------|------|
| Counter | Counter (monotonic) |
| Gauge | Gauge / UpDownCounter |
| Histogram | Histogram |
| Summary | (no direct equivalent — use Histogram with quantile views) |
| labels(...) | attributes parameter on add()/record() |
| `_total` suffix on counters | OTel doesn't add suffix; consult exporter docs |

### Gotchas

- **Counter naming**: Prometheus convention is `_total` suffix on counters; OTel doesn't add it. The Collector's `prometheus` exporter (when shipping back to a Prometheus backend) handles this — but be consistent or queries break.
- **Histogram bucket boundaries**: Prometheus client libraries let you specify buckets at instrument creation. OTel uses Views to configure buckets, applied at SDK config time. Migrating exact bucket boundaries requires View setup.
- **Push gateway**: Prometheus push gateway has no OTel direct equivalent. For batch jobs that need to push, use OTLP HTTP exporter from the job to your gateway Collector — same effect.

## From Datadog APM agent

Datadog's APM agent (the `dd-trace-*` libraries) auto-instruments many libraries. Migration paths:

### Path A — keep Datadog agent, just enable OTLP receive

The Datadog agent has accepted OTLP since 2022. Configure your OTel SDK to ship OTLP to the local Datadog agent (which runs as a daemon):

```yaml
# datadog.yaml on the agent
otlp_config:
  receiver:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318
```

Apps emit OTLP to localhost:4317; Datadog agent ingests as APM. Zero code rewrite, zero new infrastructure.

### Path B — full migration

Replace `dd-trace-*` with the OTel SDK; ship OTLP to a gateway Collector that exports to Datadog (see `non-cloud-backends.md`). Removes the Datadog agent dependency.

```python
# Before — dd-trace-py
from ddtrace import tracer, patch_all
patch_all()

with tracer.trace('process_order') as span:
    span.set_tag('order.id', order_id)
    span.set_tag('tenant', 'acme')

# After — OTel SDK
from opentelemetry import trace
tracer = trace.get_tracer('vqms-api')

with tracer.start_as_current_span('process_order') as span:
    span.set_attribute('order.id', order_id)
    span.set_attribute('tenant.id', 'acme')
```

### Gotchas

- **`dd.service`, `dd.env`, `dd.version` tags**: Datadog UI relies on these. With OTel, they're derived from `service.name`, `deployment.environment.name`, `service.version`. The Datadog OTLP exporter handles the mapping — verify in Datadog APM that services appear as expected after migration.
- **Continuous Profiler**: Datadog's profiler is currently outside the OTel spec (Profiles signal is RC). Keep the Datadog profiler agent during the OTel migration if you use this; revisit once OTel Profiles GA.
- **Trace ID format**: Datadog historically used 64-bit IDs; OTel uses 128-bit. Datadog now accepts 128-bit and derives a 64-bit subset for legacy correlation. Should be transparent — verify in Datadog UI that traces from new OTel SDK appear correctly.

## From log shippers (Fluent Bit, Vector, Filebeat)

Existing log infrastructure is often Fluent Bit / Fluentd / Vector / Filebeat tailing files and shipping to a backend. Migration paths:

### Path A — keep the shipper, route through OTel Collector

Most shippers can output to OTel via OTLP:

```yaml
# Fluent Bit output to OTel Collector
[OUTPUT]
    Name opentelemetry
    Match *
    Host otel-gateway.observability.svc.cluster.local
    Port 4318
    metrics_uri /v1/metrics
    logs_uri /v1/logs
    traces_uri /v1/traces
```

Or use the Collector's `filelog` receiver to tail files directly, eliminating the shipper:

```yaml
receivers:
  filelog:
    include: [/var/log/vqms/*.log]
    operators:
      - type: json_parser
      - type: severity_parser
        parse_from: attributes.level
```

### Path B — replace shipper with OTel logging bridge in app

The OTel logging bridge (covered in `instrumentation-polyglot.md`) emits OTLP logs from the app process. The shipper goes away.

Pick this when:
- Apps already use a structured logger (not raw stdout)
- You want trace correlation in logs (the bridge auto-injects trace_id)
- File-based logging adds disk IO you'd rather not have

Skip this when:
- You have stable shipper infrastructure that works
- The shipper handles concerns (multi-line stack traces, log file rotation) that OTel SDK logging bridge doesn't yet
- Apps are diverse and rewriting logging in 50 services isn't pragmatic

### Gotchas

- **stdout double-capture**: On Cloud Run / ECS, stdout is auto-shipped by the platform. If you also use the OTel logging bridge to ship the same log lines, you pay twice. Pick one path. (See `gcp-pipeline.md` and `anti-patterns.md`.)
- **Multi-line parsing**: stack traces span multiple stdout lines. The shipper handles this with multi-line parsers; the OTel SDK logging bridge ships each `log.error(exc_info=e)` as one record (good). Hybrid approaches need care.

## Validation pattern — running old and new in parallel

For each service, during the bridge period, capture three numbers daily and graph them:

| Metric | Source | What you're checking |
|--------|--------|---------------------|
| Spans per minute, by operation | Both old and new backend | Coverage parity (within 5%) |
| p50 / p99 latency, by operation | Both backends | Data quality parity |
| Error rate (status=ERROR), by operation | Both backends | Sampling and recording fidelity |

A divergence > 5% is a signal to investigate before proceeding. Common causes:
- Library not auto-instrumented in OTel (need extra `opentelemetry-instrumentation-X` package)
- Sampling rate mismatched
- Different propagation formats causing trace breaks

Set a calendar item to review the comparison weekly. After 2-4 weeks of stable parity, decommission the old SDK.

## Decommissioning checklist

Before turning off the old pipeline:

```markdown
- [ ] All services in scope have run on OTel for ≥ 2 weeks
- [ ] Spans/metrics/logs parity verified (within 5% on key indicators)
- [ ] Dashboards updated to query the new backend (or new attribute names)
- [ ] Alerts updated to fire on the new backend
- [ ] Runbooks and on-call docs reference the new tools
- [ ] Historical data migrated or archived (vendor-specific)
- [ ] Old vendor licenses scheduled for non-renewal
- [ ] Vendor account access reviewed; old vendor switched to read-only mode for N more weeks
```

The "read-only mode for N more weeks" step is the safety belt. If a regression is discovered post-cutover, you can still query the old data without re-enabling write access.

## Common pitfalls

**Big-bang cutover.** A team migrates 50 services in one weekend. One library has a missing instrumentation, that service's traces are 30% incomplete, the team discovers it three weeks later during an incident. Always parallel-run.

**Forgetting propagator config.** OTel SDK with W3C propagation talking to legacy services with B3 / X-Ray / Jaeger headers. Trace breaks at the boundary. Register multiple propagators during migration.

**Letting old metrics dashboards rot.** Dashboards still query Prometheus while metrics moved to OTel-OTLP. Old graphs go flat; nobody updates them; alerts fall silent. Migrate dashboards as part of the per-service work, not after.

**Different attribute names between old and new.** Datadog's `env` vs OTel's `deployment.environment.name`. The Datadog OTel exporter maps these; verify the mapping holds for your custom dashboards.

**Sampling rate change.** Old SDK at 100%, new SDK at 10% — the new "request rate" graph is 10x lower for no real-world reason. Match sampling during the bridge period; change rates only after migration completes.

**Skipping the readiness phase.** Building the gateway, validating it with one canary service, sets up the trust foundation. Skipping straight to "every service migrate now" creates a situation where every issue is also a migration issue.

**Decommissioning before historical data is preserved.** Old vendor turn-off date arrives, only then someone realizes they haven't archived the last 6 months of trace data. Vendor-specific export tooling is often slow or paid; build in 2-4 weeks for export.

## Quick checklist

```markdown
## Migration plan review

- [ ] Phase 0 readiness: gateway Collector deployed and validated
- [ ] Coexistence pattern picked per source system (parallel SDK / Collector receiver / hybrid)
- [ ] Trace ID format compatibility chosen (X-Ray IDs vs W3C; B3 + W3C; etc.)
- [ ] Propagator(s) registered to match upstream/downstream services
- [ ] Sampling rate matched between old and new during bridge period
- [ ] Per-service migration tracked (Jira, spreadsheet, etc.)
- [ ] Validation queries defined (span count, latency parity, error rate)
- [ ] Dashboards and alerts updated as services migrate
- [ ] Old backend access reviewed; downgraded to read-only post-migration
- [ ] Historical data export/archive plan
- [ ] Decommissioning date booked; rollback plan documented
```

## Sources

- AWS X-Ray to OTel migration — aws.amazon.com/blogs/mt/migrating-x-ray-tracing-to-aws-distro-for-opentelemetry/
- Jaeger client deprecation — jaegertracing.io/docs/latest/client-libraries/
- Zipkin and OpenTelemetry — zipkin.io/pages/tracers_instrumentation.html
- Datadog OTLP support — docs.datadoghq.com/opentelemetry/
- Prometheus and OpenTelemetry — opentelemetry.io/docs/specs/otel/compatibility/prometheus_and_openmetrics/
- B3 propagation — github.com/openzipkin/b3-propagation
- W3C Trace Context — w3.org/TR/trace-context