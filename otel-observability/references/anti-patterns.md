# Anti-patterns and Review Checklist

A catalog of OTel observability bugs that show up in real production systems, organized by severity. Use during PR review, infrastructure review, or audits of existing telemetry pipelines.

## [Block] — Severe bugs, do not ship

### 1. SDK initialized but never started

**Symptom:** Code imports OTel libraries, configures a `TracerProvider`, but never calls `start()` or registers it globally. Spans are created but go nowhere.

**Why it matters:** Silent failure. The application looks instrumented but ships zero data. You discover it during an incident when you realize the trace UI is empty.

**Fix:** Always call `tracer_provider.set_global()` (Python), `sdk.start()` (Node), `otel.SetTracerProvider(tp)` (Go), or use the auto-instrumentation agent which handles this. Verify with a debug exporter in dev that data actually flows.

### 2. No graceful shutdown

**Symptom:** Process exits without calling `tracer_provider.shutdown()` (or equivalent). On Cloud Run/Lambda, SIGTERM arrives, container terminates, in-memory spans are lost.

**Why it matters:** The last few seconds of a process's life — often when interesting things happen — produce no telemetry. Especially bad on serverless where processes die frequently.

**Fix:** Register SIGTERM/SIGINT handlers that call SDK shutdown. Set a reasonable timeout (~5 seconds) so the flush actually happens before the platform kills you.

```python
import signal
def _shutdown(*_):
    tracer_provider.shutdown()
    metric_provider.shutdown()
    logger_provider.shutdown()
    exit(0)
signal.signal(signal.SIGTERM, _shutdown)
```

### 3. PII in span attributes

**Symptom:** Span attributes include user emails, raw credit card numbers, government IDs, full request bodies, or response bodies.

**Why it matters:** Trace UIs are visible to anyone with observability access — usually a wider group than data systems access. Privacy and compliance violation. Permanent record in trace storage.

**Fix:** Audit attribute-setting code for PII. Use the Collector's `redaction` processor as a safety net, but don't rely on it as the only defense. For request/response bodies, capture metadata (size, content-type, status code) instead of contents.

### 4. Sampling everything in production

**Symptom:** `OTEL_TRACES_SAMPLER_ARG=1.0` (100% sampling) on a high-traffic service.

**Why it matters:** Vendor bills explode. Cloud Trace at $0.20/M spans can mean five-figure monthly bills for a moderately busy service. Often discovered when finance asks about the bill.

**Fix:** 1-10% probabilistic sampling for normal traffic, plus tail-based sampling at the Collector to keep all errors and slow requests. See `sampling-and-cost.md`.

### 5. High-cardinality attributes on metrics

**Symptom:** Metric labeled with `user.id`, `session.id`, raw URL paths, or other unique-per-request values.

**Why it matters:** Each unique combination creates a new time series, billed by the vendor. CloudWatch, Cloud Monitoring, and most vendors charge per active series. A `user.id` label on a million-user app produces a million metrics. CloudWatch Metrics at $0.30/metric = $300K/month bill.

**Fix:** Drop high-cardinality labels at the Collector before export. Use `http.route` (the template) not `url.path`. Bucket numeric values. See `sampling-and-cost.md`.

### 6. Trace context not propagated across async boundaries

**Symptom:** A request's trace looks like 50 disconnected spans instead of a parent-child tree. Async tasks, message queue handlers, or worker pool jobs appear as separate traces.

**Why it matters:** You can't follow a request end-to-end. Debugging requires manually correlating timestamps across disconnected spans — basically impossible at scale.

**Fix:** Use the SDK's context propagation primitives (`contextvars` in Python, `AsyncLocalStorage` in Node, explicit `context.Context` plumbing in Go). Test by making a request that triggers an async path; verify the spans appear as children of the request span.

### 7. Exporter failures silently swallowed

**Symptom:** Spans are produced and queued for export, but the exporter's network calls fail (DNS issue, TLS misconfiguration, IAM permissions). The SDK logs the error at debug level and drops the spans.

**Why it matters:** Data loss without visibility. Engineers assume telemetry is flowing.

**Fix:** Set the SDK's diagnostic logger to surface exporter errors at WARN level. Monitor `otelcol_exporter_send_failed_spans` (the Collector emits its own self-metrics). Alert on it.

## [Fix] — Real bugs, but recoverable

### 8. Resource attributes hardcoded in app config

**Symptom:** Application sets `cloud.region: "us-central1"` in code or static config. When deployed to a different region, attribute is wrong.

**Why it matters:** Telemetry is mis-attributed. "Production us-east-1 latency" includes traces from us-west-2.

**Fix:** Use resource detectors. The OTel SDK detectors and Collector's `resourcedetection` processor read the actual environment (cloud metadata service, K8s downward API, etc.).

### 9. Inconsistent service.name across environments

**Symptom:** `vqms-api` in dev, `VQMS-API` in staging, `vqms_api_prod` in production.

**Why it matters:** Cross-environment dashboards, alerts, and cross-references break. Vendor UIs treat them as separate services.

**Fix:** Standardize naming. `<service>` in lowercase, hyphenated. Append environment via `deployment.environment.name`, not via service name suffix.

### 10. Logs not correlated with traces

**Symptom:** Logs in Cloud Logging or CloudWatch don't have `trace_id` or `span_id` fields. To debug an incident, engineers manually scroll logs by timestamp.

**Why it matters:** Pivoting between logs and traces is the most common debugging workflow. Without correlation, every incident takes 3-5x longer.

**Fix:** Use the OTel logging bridge for your language (Python `LoggingHandler`, Pino instrumentation for Node, MDC injection in Java agents). Verify by emitting a log inside an instrumented span and checking the log carries `trace_id`.

### 11. Sampling decision varies between SDK and Collector

**Symptom:** SDK samples at 10%, Collector samples at 50%. Net effect: 5% of traces.

**Why it matters:** Coverage is worse than intended. Math on "we should see N errors per hour" is wrong by 2-10x.

**Fix:** Pick one place to control sampling. Recommendation: minimal SDK sampling (90-100%) + Collector tail sampling for the real decisions. Don't double-sample.

### 12. Collector running with default config

**Symptom:** Collector deployed but using the upstream default config — no resource detection, no batch processor, no sampling.

**Why it matters:** Performance issues (no batching = many small exports), missing resource enrichment, no cost control.

**Fix:** Always provide a custom config. Use the `gcp-pipeline.md` or `aws-pipeline.md` reference configs as a starting point.

### 13. Metric instruments created on every request

**Symptom:** Code like `meter.create_counter("requests")` inside the request handler.

**Why it matters:** Some SDKs handle this by caching internally; others create new instruments per call, leaking memory and slowing requests.

**Fix:** Create instruments once at startup, reference the cached instance in handlers.

```python
# Bad
def handler(req):
    counter = meter.create_counter("requests")  # creates new every time
    counter.add(1)

# Good
counter = meter.create_counter("requests")    # at module load
def handler(req):
    counter.add(1)
```

### 14. No shutdown timeout on the Collector

**Symptom:** Collector container takes 60+ seconds to stop on rolling updates because spans are draining.

**Why it matters:** Slow deployments. On Cloud Run with min-instances changes, can cause traffic interruption.

**Fix:** Set a reasonable `shutdown_timeout` in the Collector config (default 5s). For most services, lose a few buffered spans rather than hold up shutdown.

### 15. ETag and request-correlation attributes missing on error spans

**Symptom:** A 412 Precondition Failed span has only `http.response.status_code: 412`. No way to see what ETag was expected vs. actual.

**Why it matters:** Traces are useful for happy paths but useless for debugging errors. Engineers fall back to logs and manual correlation.

**Fix:** Set descriptive attributes on error spans. For idempotency: `idempotency.key`, `idempotency.action: "replayed" | "in_flight" | "executed"`. For ETag: `http.if_match.matched: false`, `http.etag.expected`, `http.etag.actual`. See `composition.md`.

### 16. CloudWatch Logs ingestion bill from double-logging

**Symptom:** Service logs to stdout (captured by Cloud Run / ECS) AND emits OTel logs to Cloud Logging / CloudWatch. Same data ingested twice.

**Why it matters:** Logs are usually the largest line item in observability bills. Doubling it for no benefit.

**Fix:** Pick one path. Either log to stdout and let the platform capture, OR ship via OTel logs with rich context. Don't do both.

### 17. Collector deployed without health check / liveness probe

**Symptom:** Collector silently OOMs or hangs. Apps continue to ship to it; spans pile up in the SDK's BatchSpanProcessor and eventually drop.

**Why it matters:** Silent telemetry loss. Visible only when querying the trace UI for a recent incident and finding nothing.

**Fix:** Configure the Collector's `health_check` extension. Add liveness/readiness probes in K8s. Alert on Collector unavailability separate from the apps it serves.

## [Nit] — Worth fixing, lower urgency

### 18. Span names that include dynamic data

`span.set_name(f"GET /orders/{order_id}")` produces a span per order. Instead, use the route template (`GET /orders/:id`) and put the order ID in an attribute.

### 19. Custom attribute names without namespace

`tenant_id` instead of `vqms.tenant.id`. Risk of future collision with OTel conventions. Namespace your business attributes.

### 20. `OTEL_RESOURCE_ATTRIBUTES` env var with quoted values

`OTEL_RESOURCE_ATTRIBUTES="service.name=vqms-api"` — the quotes become part of the attribute value on some shells. Use `OTEL_SERVICE_NAME` for service name; for others, use unquoted comma-separated `key=value`.

### 21. Collector image pinned to `:latest` in production

Works until ADOT or Google releases a Collector with a config schema change. Pin to a specific tag (`v0.121.0`).

### 22. Documentation missing for which services are instrumented

Engineers can't tell from the code whether instrumentation is auto- or manually configured, what the sampling rate is, where data goes. A short `OBSERVABILITY.md` in each service repo saves debugging time.

## Review checklist (paste into PR template)

```markdown
## Observability review

- [ ] OTel SDK is initialized AND registered globally (verified with debug exporter or trace UI)
- [ ] Graceful shutdown handler calls SDK shutdown
- [ ] `service.name`, `service.version`, `deployment.environment.name` set
- [ ] Resource attributes come from detectors, not hardcoded
- [ ] No PII in attributes (audit recently-added attribute keys)
- [ ] HTTP server metrics use `http.route` (template) not `url.path` (raw)
- [ ] Sampling configured (probabilistic + tail-based for prod)
- [ ] No high-cardinality metric labels (user_id, session_id, raw paths)
- [ ] Trace context propagated across async boundaries (verified end-to-end)
- [ ] Exporter errors surface to logs at WARN level
- [ ] Logs correlated with traces (`trace_id`/`span_id` in log records)
- [ ] Collector config uses batch processor, resourcedetection, sampling
- [ ] Collector image pinned to specific version (not `:latest`)
- [ ] IAM/permissions for Collector service account documented
- [ ] Health check / liveness probe on the Collector
- [ ] Error spans include domain attributes for debugging (etag, idempotency_key, etc.)
- [ ] Single source of truth for log shipping (no double-billing)
```
