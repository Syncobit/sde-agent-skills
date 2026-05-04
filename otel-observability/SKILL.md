---
name: otel-observability
description: Apply OpenTelemetry (OTel) for traces, metrics, and logs across backend and edge tiers. Use when instrumenting apps, configuring exporters or Collectors, deploying on GCP (Cloud Run, GKE, Cloud Functions, Apigee), AWS (ECS, EKS, Lambda, API Gateway, Step Functions, EventBridge), edge runtimes (Cloudflare Workers, Vercel Edge, Lambda@Edge), or browsers (RUM); debugging missing or broken telemetry; sampling and cardinality; semantic conventions; log levels and audit-vs-operational channels; production-hardening the Collector; routing to non-cloud backends (Datadog, Splunk, New Relic, Grafana Cloud, Honeycomb); multi-tenant isolation; or migrating from X-Ray, Jaeger, Zipkin, StatsD, Prometheus, or Datadog APM. Trigger broadly on missing traces, missing X-Ray segments, sampling cost, RUM correlation, logs-to-traces joins, PII-in-traces reviews, CI testing of instrumentation, even when the user does not say OpenTelemetry. Composes with api-idempotency, api-error-responses, api-http-caching.
---

# OpenTelemetry Observability

A skill for instrumenting services with OpenTelemetry and shipping the telemetry to Google Cloud or AWS backends. Covers all three signals (traces, metrics, logs), polyglot instrumentation, two deployment patterns, and the vendor-specific pipeline configuration for both clouds.

## Why this skill exists

Observability is a cross-cutting concern that fails silently. A service with broken instrumentation looks healthy until something goes wrong, and then engineers can't debug because the data they need was never captured. The most common failure modes:

- Telemetry library installed but never initialized — zero data shipped, no error message
- Resource attributes wrong, so traces from production show up tagged as "staging"
- Sampling too aggressive, so the failing request was the one that got dropped
- Trace context not propagated across async boundaries, so traces look like 50 disconnected spans
- Exporter failures swallowed silently — engineers think data is flowing when it isn't
- Logs not correlated with traces, so you have to pivot manually between systems
- Metrics with unbounded cardinality, blowing out monitoring costs

This skill encodes the workflow for getting OTel right the first time, including the vendor-specific pipeline decisions for GCP and AWS that aren't in the OpenTelemetry spec.

## Step 1 — Pick the architecture: direct exporter vs. Collector

The first and most consequential decision. Two viable patterns:

**Pattern A: Direct exporter from app to vendor backend**

```
[ App (OTel SDK + GCP/AWS exporter) ] ──→ [ Cloud Trace / Cloud Monitoring / X-Ray / CloudWatch ]
```

Simpler. No infrastructure between app and backend. The vendor exporter (e.g., `opentelemetry-exporter-gcp-trace`, `aws-otel-python-instrumentation`) translates OTLP to the vendor's protocol in-process.

**Pattern B: OTel Collector in the middle (recommended for Syncobit)**

```
[ App (OTel SDK + OTLP exporter) ] ──→ [ OTel Collector ] ──→ [ Cloud Trace / X-Ray / Prometheus / etc. ]
```

The Collector is a separate process (sidecar, daemonset, or standalone deployment). Apps export vendor-neutral OTLP. The Collector handles batching, sampling, redaction, enrichment, and routing to one or many backends.

### When to pick which

| Factor | Direct exporter (A) | Collector (B) |
|--------|---------------------|---------------|
| Vendor portability | App is locked to vendor at compile time | App stays vendor-neutral; switch backends by reconfiguring Collector |
| Multi-cloud / dual-export | Hard (run two exporters in app) | Trivial (configure two exporters in Collector) |
| Centralized sampling and redaction | Per-app config | Single Collector config, applies to all apps |
| Operational complexity | Lowest | Adds the Collector to deploy and operate |
| Cold-start cost (serverless) | Lower | Slightly higher (sidecar startup) |
| Cardinality control | Per-app | Centralized via processor pipeline |

**Recommendation for Syncobit**: Pattern B (Collector) for everything except very simple single-cloud, single-language services. The Collector pays for itself the first time you need to redact PII, change sampling, or dual-export during a cloud migration.

For the GCP-specific Collector deployment patterns (Cloud Run sidecar, GKE operator), see `references/gcp-pipeline.md`. For AWS, see `references/aws-pipeline.md`.

### Topology — sidecar, agent, gateway, or two-tier

Once you've picked Pattern B (Collector), the next decision is *where* the Collector runs. Four canonical patterns:

| Topology | Where | When |
|----------|-------|------|
| **Sidecar** | One Collector per app instance (same pod / task) | Cloud Run, ECS Fargate, small K8s estates (<50 services) |
| **Agent (DaemonSet)** | One per K8s node | Medium-scale K8s — shared per-node Collector reduces overhead |
| **Gateway** | Centralized Collector cluster behind a load balancer | >50 services, OR any tail sampling at scale |
| **Two-tier (agent + gateway)** | DaemonSet agent does cheap work; forwards to gateway cluster for sampling, redaction, fan-out | Enterprise default — best resilience and operational control |

Sidecar is the simplest and the right starting point. Graduate to gateway when any of these become true: tail sampling at scale (requires all spans of a trace at the same Collector), >50 services (sidecar memory cost = N × 256Mi), centralized redaction/PII compliance (auditable in one place), or multi-vendor fan-out (sending to two backends from every sidecar wastes work). For the full topology comparison, persistent-queue setup, `loadbalancing` exporter, and self-telemetry SLOs, see `references/collector-production.md`.

## Step 2 — Choose signals and stability posture

Three stable signals, one in RC:

| Signal | Status (May 2026) | Use for |
|--------|-------------------|---------|
| Traces | Stable across all major SDKs | Distributed request flow, latency analysis, dependency mapping |
| Metrics | Stable across all major SDKs | Aggregates: throughput, error rates, latency percentiles, business KPIs |
| Logs | Stable in spec; per-language SDK varies (Python/Node stable, Go reached GA in 2025) | Detailed event records correlated with traces |
| Profiles | RC, GA Q3 2026 | Continuous CPU/memory profiling correlated with spans |

Recommendation: enable all three stable signals from day one. The cost is negligible compared to retrofitting them later when you have an incident and realize logs aren't correlated to traces.

Hold on Profiles until GA unless you have a specific need — RC means breaking changes are still possible.

For the bridging pattern (existing logs → OTel logs without rewriting log statements) and the per-language SDK status, see `references/instrumentation-polyglot.md`. For the discipline of *using* log levels correctly — choosing INFO vs WARN vs ERROR, the relationship between span status and log severity, and the anti-patterns that drive observability bills and alert fatigue — see `references/log-levels.md`.

## Step 3 — Instrument the application

Three layers, in order of preference:

**1. Auto-instrumentation (preferred when available)**

Every major language has an OTel auto-instrumentation package that hooks into common libraries (HTTP clients/servers, database drivers, message queues) without code changes. Run with one environment variable (`OTEL_SERVICE_NAME`) and a sidecar/agent, get traces for free.

- Python: `opentelemetry-instrument python app.py` (the `opentelemetry-distro` package wires in HTTP, database, and other instrumentations automatically)
- Node.js: `node --require @opentelemetry/auto-instrumentations-node/register app.js`
- Java: `-javaagent:opentelemetry-javaagent.jar` (the most polished auto-instrumentation in the ecosystem)
- Go: no true auto-instrumentation (compile-time language); use the `otel*` packages and instrument explicitly. eBPF-based auto-instrumentation (OBI) is alpha; not production-ready

**2. Library instrumentation packages**

For frameworks not covered by auto-instrumentation, OTel ships official instrumentation packages: `opentelemetry-instrumentation-fastapi`, `@opentelemetry/instrumentation-grpc`, etc. Add the dependency, register it once at startup. Same effect as auto-instrumentation, just explicit.

**3. Manual instrumentation (the last 20%)**

For business logic that no library can know about: explicit spans around critical operations, custom metrics, structured log events with trace correlation. Reach for this last, after auto- and library-level instrumentation are in place.

```python
# Manual span — Python example
from opentelemetry import trace

tracer = trace.get_tracer(__name__)

def process_order(order_id: str):
    with tracer.start_as_current_span("process_order") as span:
        span.set_attribute("order.id", order_id)
        span.set_attribute("order.tenant_id", get_tenant_id())
        # ... business logic ...
        if failed:
            span.set_status(trace.Status(trace.StatusCode.ERROR, "validation failed"))
```

For per-language idioms (Python/Node/Go/Java), the bridge pattern for logs, and common pitfalls, see `references/instrumentation-polyglot.md`.

## Step 4 — Set resource attributes correctly

Resource attributes describe the entity producing the telemetry — the service, the host, the deployment environment. They are the difference between useful telemetry and a pile of unattributed data.

Minimum required for production:

```yaml
service.name: "vqms-api"                      # required by OTel spec
service.version: "1.4.7"                      # for deployment correlation
service.namespace: "vqms"                     # logical grouping
deployment.environment.name: "production"     # vs. staging, dev
service.instance.id: "<uuid-or-pod-name>"     # individual instance
```

Cloud-specific attributes that matter for correlation in vendor consoles:

```yaml
# GCP
gcp.project_id: "syncobit-prod"
gcp.location: "us-central1"
gcp.cloud_run.service_name: "vqms-api"        # Cloud Run
k8s.cluster.name: "vqms-prod-1"               # GKE
k8s.namespace.name: "vqms"
k8s.pod.name: "vqms-api-7c4d-x8j2"

# AWS
cloud.account.id: "123456789012"
cloud.region: "us-east-1"
aws.ecs.cluster.arn: "arn:aws:ecs:..."        # ECS
aws.ecs.task.arn: "arn:aws:ecs:..."
k8s.cluster.name: "vqms-prod"                 # EKS
```

These should be set automatically by **resource detectors** (built-in to the OTel SDK or via the Collector's `resourcedetection` processor), not hardcoded. Hardcoding is a maintenance trap — the value drifts from reality.

For the canonical OTel semantic conventions, the Weaver tooling, and how to extend with custom attributes without breaking convention, see `references/semantic-conventions.md`.

## Step 5 — Sampling and cost control

Tracing every request to production is usually wasteful and expensive. Sampling reduces volume; the question is which strategy and where.

Three approaches:

**Head-based sampling (decide at trace start)**
- Cheapest. Sampling decision is made at the entry span; propagates to all child spans via the `sampled` flag on the trace context.
- Simple to implement (`ParentBased(TraceIdRatioBased(0.1))` for 10%).
- Limitation: you can't sample *more* of failed traces, because you've already decided.

**Tail-based sampling (decide after the trace completes)**
- Done in the Collector via `tail_sampling_processor`. Holds spans in memory until the trace finishes, then decides based on the full trace (status, latency, attributes).
- Lets you sample 100% of errors and 1% of successes. The right strategy for production.
- Costs Collector memory; needs careful tuning.

**Probabilistic + special cases**
- Sample 1-10% of normal traffic, plus everything that errors, plus everything from canary deployments.
- The pragmatic combination. Start here.

Metrics need different cost control: cardinality limits. A metric tagged with `user_id` blows up to millions of time series. Use the Collector's `transform` processor or per-SDK views to drop or aggregate high-cardinality attributes.

For the full sampling decision tree, the math on cost vs coverage, and cardinality patterns to avoid, see `references/sampling-and-cost.md`.

## Step 6 — Wire to the vendor backend

Once instrumentation is in place and the Collector is deployed, the last step is the Collector → backend pipeline. This is where GCP and AWS diverge.

**For GCP** (Cloud Run, GKE):
- Use the Google-Built OpenTelemetry Collector image (`us-docker.pkg.dev/cloud-ops-agents-artifacts/google-cloud-opentelemetry-collector/otelcol-google`) — Google maintains this with GCP exporters built in
- Cloud Run: deploy the Collector as a sidecar container with container-dependency annotations
- GKE: deploy via the OpenTelemetry Operator as a DaemonSet or sidecar
- Backends: Cloud Trace (traces), Cloud Monitoring (metrics, also accepts Prometheus via Managed Service for Prometheus), Cloud Logging (logs)
- IAM: service account needs `roles/monitoring.metricWriter`, `roles/cloudtrace.agent`, `roles/logging.logWriter`

Full configuration in `references/gcp-pipeline.md`.

**For AWS** (ECS, EKS, Lambda, API Gateway, Step Functions, EventBridge):
- Use AWS Distro for OpenTelemetry (ADOT) Collector — AWS-curated build with AWS exporters and resource detectors
- ECS: ADOT as a sidecar container in the task definition
- EKS: ADOT via the OpenTelemetry Operator (similar to GKE)
- Lambda: ADOT as a Lambda Layer (special case, cold-start sensitive)
- API Gateway, AppSync, Step Functions: native X-Ray + W3C dual propagation in downstream services
- Pub/Sub-style: encode `traceparent` in EventBridge/SNS/SQS message attributes
- Backends: X-Ray (traces), CloudWatch Metrics, CloudWatch Logs
- IAM: task role / instance role needs `AWSXRayDaemonWriteAccess` plus CloudWatch permissions

Full configuration in `references/aws-pipeline.md`.

**For non-cloud backends** (Datadog, Splunk Observability Cloud, New Relic, Grafana Cloud, Honeycomb, Dynatrace, Elastic):
- All accept OTLP natively — apps stay vendor-neutral, Collector exports per-vendor
- Per-vendor authentication, regional endpoints, payload limits in `references/non-cloud-backends.md`
- Cost-tier routing (errors and slow traces → expensive vendor; bulk → cheap backend) is a one-processor change

**For edge and RUM** (Cloudflare Workers, Vercel Edge, Lambda@Edge, browser):
- Edge runtimes use vendor-specific OTel SDKs (`@microlabs/otel-cf-workers`, `@vercel/otel`)
- Browser SDK propagates `traceparent` to first-party origins (CORS must allow it)
- Browsers ship through your gateway Collector — never expose vendor API keys to client code
- CDN logs (Cloudflare Logpush, CloudFront real-time, Cloud CDN) materialized as spans for end-to-end visibility
- Full configuration in `references/edge-and-rum.md`

## Step 7 — Compose with the API skills

OTel is the substrate other observability concerns sit on top of. It composes naturally with the existing API engineering skills:

| Concern | Mechanism | Skill |
|---------|-----------|-------|
| Trace context across services | W3C `traceparent` / `tracestate` headers (propagators) | this one |
| Request correlation in error responses | `request_id` in Problem Details, set from current trace's `span_id` | `api-error-responses` + this |
| Idempotency-Key tracing | Set `idempotency.key` as a span attribute on the dedupe lookup | `api-idempotency` + this |
| ETag mismatch debugging | Set `http.if_match.matched` (boolean) and `http.etag.current` as span attributes on 412 responses | `api-http-caching` + this |

The integration point is **span attributes**. Every API skill's mechanisms become observable when you record their outcomes as attributes on the relevant span. A 412 with `http.if_match.matched=false, http.etag.expected="v17", http.etag.actual="v18"` is debuggable. A 412 without those attributes is a mystery in the logs.

For the full worked example (a request flowing through all four skills with end-to-end correlation), see `references/composition.md`.

## Output style

When applying this skill, produce concrete deliverables:

- For **instrumentation setup**: actual code (per language) with imports, initialization, and a sample manual span. Not pseudocode.
- For **Collector configuration**: complete YAML ready to paste, including receivers, processors, exporters, and the full pipeline definition.
- For **deployment**: the actual `gcloud run deploy` command, Cloud Run service YAML, or Kubernetes manifest. Not abstract instructions.
- For **review**: numbered findings tagged `[block]`/`[fix]`/`[nit]` with the specific configuration line and the corrective action.
- For **debugging**: a checklist of "places telemetry can fail" and how to verify each — silent exporter failures, propagation gaps, resource attribute issues, sampling drops.

Cite primary sources: OpenTelemetry specification at opentelemetry.io/docs/specs, GCP docs at cloud.google.com/stackdriver, AWS docs at aws-otel.github.io.

## When to dig into the references

**Instrumentation and signal mechanics**
- **Per-language SDK setup, auto-instrumentation, manual spans, log bridging** → `references/instrumentation-polyglot.md`
- **Edge runtimes (Cloudflare Workers, Vercel Edge, Lambda@Edge), browser SDK, RUM, CDN log correlation** → `references/edge-and-rum.md`
- **Resource attributes, OTel semantic conventions, Weaver, custom-attribute namespacing** → `references/semantic-conventions.md`
- **Sampling strategies (head, tail, per-tenant), cardinality control, cost math** → `references/sampling-and-cost.md`

**Logging at enterprise scale**
- **Choosing log levels (INFO vs WARN vs ERROR), span-vs-log discipline, structured logging, audit-vs-operational channels, retention tiers, SIEM integration, MDC baseline, runtime level control, volume budgets** → `references/log-levels.md`

**Cloud and backend pipelines**
- **GCP pipeline — Cloud Run sidecar, GKE Operator, gateway pattern, Cloud Functions Gen 2, Apigee, Pub/Sub propagation, Eventarc, Workflows, VPC-SC private networking** → `references/gcp-pipeline.md`
- **AWS pipeline — ADOT, ECS/EKS/Lambda, gateway pattern, API Gateway, AppSync, Step Functions, EventBridge/SNS/SQS propagation, PrivateLink** → `references/aws-pipeline.md`
- **Non-cloud OTLP backends — Datadog, Splunk Observability Cloud, New Relic, Grafana Cloud (Tempo/Mimir/Loki), Honeycomb, Dynatrace, Elastic** → `references/non-cloud-backends.md`

**Production hardening**
- **Collector production — memory_limiter, persistent queue, gateway pattern, loadbalancingexporter, BatchSpanProcessor tuning, retry/backoff, OTLP transport, self-telemetry SLOs, capacity planning** → `references/collector-production.md`
- **Security and compliance — mTLS / OIDC on receivers, redaction processor, attribute hashing, CMEK / KMS, VPC Service Controls / PrivateLink, data residency, audit-log channel separation, PCI/HIPAA/SOC 2/GDPR posture, right-to-be-forgotten** → `references/security-and-compliance.md`
- **Multi-tenancy — tenant.id propagation, isolation models, per-tenant sampling, chargeback / cost attribution, per-tenant retention, tenant deletion** → `references/multi-tenancy.md`

**Migration and validation**
- **Migrating from X-Ray SDK / Jaeger / Zipkin / StatsD / Prometheus / Datadog APM / log shippers — coexistence patterns, ID compatibility, parallel-running validation, decommissioning** → `references/migration.md`
- **Testing instrumentation — InMemorySpanExporter, snapshot tests, propagation tests, otelcol validate, Weaver schema check, PII scanning in CI** → `references/testing-and-ci.md`

**Reviews and composition**
- **Common observability bugs and review checklist (22 anti-patterns, severity-tagged)** → `references/anti-patterns.md`
- **Composing with idempotency, ETag, and Problem Details — span attributes that bridge the API skills** → `references/composition.md`
