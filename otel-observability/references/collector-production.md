# Collector — Production Hardening

The Collector configurations in `gcp-pipeline.md` and `aws-pipeline.md` are the starting point. This file covers what separates a working Collector from a production-grade one: memory protection, persistent queues, the gateway pattern, load-balanced tail sampling, transport tuning, and the self-telemetry that lets you trust the pipeline.

The discipline: the observability pipeline is itself a critical system. It must not OOM under load spikes, must not lose data on restart, must scale horizontally, and must be monitorable. A Collector that drops 10% of spans silently is worse than no Collector at all because it gives engineers false confidence.

## Topology — sidecar, agent, gateway, two-tier

Four canonical patterns. Pick by service count and tail-sampling needs.

| Pattern | Where Collector runs | When to use |
|---------|----------------------|-------------|
| **Sidecar** | Same pod/task as the app, one Collector per app instance | Cloud Run, ECS Fargate, small-scale K8s; <50 services |
| **Agent (DaemonSet)** | One per K8s node; apps export to node IP | Medium-scale K8s; shared per-node Collector reduces overhead |
| **Gateway** | Centralized Collector cluster behind a load balancer | >50 services, or any tail sampling at scale |
| **Two-tier (agent + gateway)** | DaemonSet agent does cheap work (resource detection, batching); forwards to gateway cluster for sampling, redaction, fan-out | Enterprise default — best resilience and operational control |

### When to graduate from sidecar to gateway

Sidecar works fine until any of these become true:
- You have >50 services and want a single place to change sampling/redaction
- You need tail sampling (which requires all spans of a trace at the same Collector — sidecars can't do this)
- Centralized cost — every sidecar consumes 256-512Mi memory; 200 services × 256Mi = 50Gi just for collectors
- Multi-vendor fan-out — sending to two backends from every sidecar is wasteful
- Compliance — centralized redaction is auditable; per-sidecar is not

### Two-tier architecture

```
                                    ┌─────────────────────────────────┐
                                    │  Gateway Collector Cluster      │
                                    │  (StatefulSet or Deployment)    │
┌─────────┐    ┌─────────┐          │  ┌───────────┐   ┌───────────┐  │
│  Pod    │───→│ DaemonSet│─────────│─→│ Collector │   │ Collector │──┼──→ Vendor backends
│  app    │    │  agent   │  OTLP   │  │ (sampling)│   │ (sampling)│  │
└─────────┘    └─────────┘          │  └───────────┘   └───────────┘  │
                                    │           ↑      ↑              │
                                    │   loadbalancing exporter ──────┐│
                                    └─────────────────────────────────┘
```

- **Agent layer**: cheap, in-pod or per-node. Handles resource detection (k8sattributes), batching, and forwards to the gateway. Configured once and rarely changed.
- **Gateway layer**: the brain. Tail sampling, redaction, multi-backend fan-out, traffic shaping. Updated by the observability team without touching app deployments.

The agent forwards via OTLP/gRPC to the gateway. For tail sampling, the agent uses the `loadbalancing` exporter to ensure all spans of one trace land on the same gateway instance.

## The processors every production Collector needs

In strict order: `memory_limiter` → `<feature processors>` → `batch`. Order matters; `memory_limiter` must be first to backpressure upstream before the feature processors do work that would OOM the process.

### `memory_limiter` — the first processor, always

```yaml
processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 1800           # hard cap; refuse new data when hit
    spike_limit_mib: 400      # soft cap; start refusing earlier
    # If running in K8s, make these match container memory limits with headroom

  batch:
    timeout: 10s
    send_batch_size: 1024
    send_batch_max_size: 2048

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, k8sattributes, batch]
      exporters: [otlp/gateway]
```

Without `memory_limiter`, a sudden traffic spike causes the Collector to buffer in memory until OOM, lose all in-flight data, and restart. With it, the Collector returns `ResourceExhausted` to upstream, the SDK applies its own backpressure (BatchSpanProcessor queue), and the system degrades rather than fails.

Sizing rule: set `limit_mib` to 75% of container memory, `spike_limit_mib` to 25% of `limit_mib`. K8s container limit at 2Gi → `limit_mib: 1500`, `spike_limit_mib: 375`.

### `batch` — always last

```yaml
batch:
  timeout: 10s              # max wait before sending a partial batch
  send_batch_size: 1024     # target batch size
  send_batch_max_size: 2048 # absolute cap (split larger batches)
```

Batching cuts export overhead by 10-100x. Without it, each span is a separate gRPC call. Tune by signal:

| Signal | `send_batch_size` | `timeout` |
|--------|-------------------|-----------|
| Traces | 1024-8192 spans | 5-10s |
| Metrics | 1024-8192 datapoints | 60s (matches scrape interval) |
| Logs | 8192-16384 records | 5s |

## Persistent queue — the difference between "lost an hour" and "delivery resumed"

The `sending_queue` on every exporter is in-memory by default. Process restart, OOM, container reschedule — buffered data is gone.

The `file_storage` extension persists the queue to disk:

```yaml
extensions:
  file_storage:
    directory: /var/lib/otelcol/queue
    timeout: 1s
    compaction:
      directory: /var/lib/otelcol/compaction
      on_start: true
      on_rebound: true
      rebound_needed_threshold_mib: 100
      rebound_trigger_threshold_mib: 10

exporters:
  otlp/backend:
    endpoint: otel-gateway.observability.svc.cluster.local:4317
    sending_queue:
      enabled: true
      num_consumers: 4
      queue_size: 5000              # max items in queue
      storage: file_storage         # ← references the extension above
    retry_on_failure:
      enabled: true
      initial_interval: 5s
      max_interval: 30s
      max_elapsed_time: 5m

service:
  extensions: [file_storage]        # ← register the extension
  pipelines:
    traces:
      ...
      exporters: [otlp/backend]
```

In K8s, mount a `PersistentVolumeClaim` at `/var/lib/otelcol/queue`. On Cloud Run, persistent queue is harder (writable volumes are limited) — accept higher data loss risk or move to GKE for production.

The disk queue survives Collector restart. When the backend is unreachable for 30 minutes, the queue grows on disk; when connectivity returns, it drains. Without persistent queue, those 30 minutes of data are gone.

## Gateway pattern — Kubernetes deployment

```yaml
apiVersion: opentelemetry.io/v1beta1
kind: OpenTelemetryCollector
metadata:
  name: gateway
  namespace: observability
spec:
  mode: statefulset                    # required for stable pod identity (used by load balancer)
  replicas: 3
  image: otel/opentelemetry-collector-contrib:0.121.0
  resources:
    requests: { memory: "2Gi", cpu: "500m" }
    limits:   { memory: "2Gi", cpu: "2" }
  volumeClaimTemplates:
    - metadata: { name: queue }
      spec:
        accessModes: [ReadWriteOnce]
        resources: { requests: { storage: "20Gi" } }
        storageClassName: standard
  volumeMounts:
    - { name: queue, mountPath: /var/lib/otelcol }
  config:
    extensions:
      file_storage:
        directory: /var/lib/otelcol/queue
      health_check:
        endpoint: 0.0.0.0:13133
      pprof:
        endpoint: 0.0.0.0:1777
      zpages:
        endpoint: 0.0.0.0:55679

    receivers:
      otlp:
        protocols:
          grpc: { endpoint: 0.0.0.0:4317, max_recv_msg_size_mib: 16 }
          http: { endpoint: 0.0.0.0:4318 }

    processors:
      memory_limiter:
        check_interval: 1s
        limit_mib: 1500
        spike_limit_mib: 375
      tail_sampling:
        decision_wait: 30s
        num_traces: 100000
        expected_new_traces_per_sec: 5000
        policies:
          - { name: errors, type: status_code, status_code: { status_codes: [ERROR] } }
          - { name: slow, type: latency, latency: { threshold_ms: 1000 } }
          - { name: probabilistic, type: probabilistic, probabilistic: { sampling_percentage: 5 } }
      batch:
        timeout: 5s
        send_batch_size: 8192

    exporters:
      otlp/datadog:                      # example backend
        endpoint: trace.agent.datadoghq.eu:443
        headers: { dd-api-key: "${env:DD_API_KEY}" }
        sending_queue: { enabled: true, queue_size: 10000, storage: file_storage }
        retry_on_failure: { enabled: true, max_elapsed_time: 5m }

    service:
      extensions: [file_storage, health_check, pprof, zpages]
      telemetry:
        metrics:
          level: detailed
          address: 0.0.0.0:8888
        logs:
          level: info
      pipelines:
        traces:
          receivers: [otlp]
          processors: [memory_limiter, tail_sampling, batch]
          exporters: [otlp/datadog]
```

## Load-balancing exporter — tail sampling at scale

Tail sampling requires all spans of one trace at the same Collector instance. With multiple gateway replicas, agent-tier Collectors must route by trace ID:

```yaml
# Agent (DaemonSet) Collector config
exporters:
  loadbalancing:
    routing_key: traceID                # hash trace ID → consistent gateway
    protocol:
      otlp:
        tls: { insecure: false, ca_file: /etc/otelcol/ca.pem }
        sending_queue: { enabled: true, queue_size: 5000 }
        retry_on_failure: { enabled: true }
    resolver:
      k8s:                               # discover gateway pods via K8s API
        service: gateway-collector.observability
        ports: [4317]
        # Or for a static set:
        # static: { hostnames: [gw-0.gateway:4317, gw-1.gateway:4317, gw-2.gateway:4317] }

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, k8sattributes, batch]
      exporters: [loadbalancing]        # → gateways
```

The `routing_key: traceID` ensures spans with the same trace ID always hit the same gateway, satisfying tail-sampling's locality requirement. The `k8s` resolver auto-discovers gateway pods; pod additions/removals propagate within a few seconds.

For metrics and logs, use the `otlp` exporter directly (no need for trace-locality):

```yaml
exporters:
  loadbalancing/traces: { ... }
  otlp/metrics:
    endpoint: gateway-collector.observability.svc.cluster.local:4317
    # no need for routing_key — metrics aggregate elsewhere

service:
  pipelines:
    traces:    { exporters: [loadbalancing/traces] }
    metrics:   { exporters: [otlp/metrics] }
    logs:      { exporters: [otlp/metrics] }       # same gateway, different pipeline
```

## Retry and backoff

Every exporter should have explicit retry config:

```yaml
exporters:
  otlp/backend:
    endpoint: ...
    retry_on_failure:
      enabled: true
      initial_interval: 5s              # first retry after 5s
      max_interval: 30s                 # cap exponential backoff
      max_elapsed_time: 300s            # give up after 5 minutes
      multiplier: 2.0                   # exponential factor
```

`max_elapsed_time` matters: if the backend is down for an hour, do you keep retrying forever and balloon disk queue, or drop after 5 minutes? For most production settings, 5-15 minutes with persistent queue is the right balance.

The retry happens *before* the persistent queue drains, so the order is: receive → process → queue (disk) → retry-with-backoff → export. If the queue fills up, the exporter applies backpressure to upstream.

## OTLP transport tuning

### gRPC vs HTTP

| | gRPC | HTTP/protobuf | HTTP/JSON |
|-|------|---------------|-----------|
| Performance | Best | Good | Worst |
| Network friendliness | Many proxies/LBs handle gRPC poorly | Universal | Universal |
| Browser support | No | No | Yes |
| Compression | gzip standard, zstd available | gzip standard | gzip standard |
| Use when | Backend → Collector, Collector → Collector | Cross-firewall, restrictive proxies | Browser → gateway only |

Default to gRPC inside your network, HTTP for browsers and across firewalls.

### Compression

Always enable compression. OTLP is verbose; gzip cuts payload size 5-10x.

```yaml
# Receiver
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
        max_recv_msg_size_mib: 16
        # gRPC compression is per-call, controlled by the client

# Exporter
exporters:
  otlp:
    endpoint: gateway:4317
    compression: gzip                   # or zstd if both ends support it
    # zstd is ~30% better compression at similar CPU cost
```

For the SDK side:
- Python OTLP gRPC: `OTLPSpanExporter(insecure=False, compression=Compression.Gzip)`
- Node OTLP gRPC: `new OTLPTraceExporter({ compression: 'gzip' })`
- Go OTLP gRPC: `otlptracegrpc.WithCompressor("gzip")`

### TLS

Production Collector receivers should require TLS, especially for gateways accepting traffic from outside the mesh:

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
        tls:
          cert_file: /etc/otelcol/tls.crt
          key_file: /etc/otelcol/tls.key
          ca_file: /etc/otelcol/ca.crt
          client_ca_file: /etc/otelcol/ca.crt   # mTLS — require client cert
        max_recv_msg_size_mib: 16
```

For mTLS authentication setup and per-tenant cert handling, see `security-and-compliance.md`.

## BatchSpanProcessor tuning (SDK side)

The Collector's `batch` processor is one half; the SDK's `BatchSpanProcessor` is the other. Default values are conservative — tune for your traffic.

```python
# Python
from opentelemetry.sdk.trace.export import BatchSpanProcessor

processor = BatchSpanProcessor(
    OTLPSpanExporter(),
    max_queue_size=4096,             # spans buffered in SDK before drop
    max_export_batch_size=512,       # max spans per OTLP request
    schedule_delay_millis=5000,      # max wait before flush
    export_timeout_millis=30000,     # OTLP request timeout
)
```

```javascript
// Node
new BatchSpanProcessor(exporter, {
  maxQueueSize: 4096,
  maxExportBatchSize: 512,
  scheduledDelayMillis: 5000,
  exportTimeoutMillis: 30000,
})
```

```go
// Go
sdktrace.NewBatchSpanProcessor(exp,
  sdktrace.WithMaxQueueSize(4096),
  sdktrace.WithMaxExportBatchSize(512),
  sdktrace.WithBatchTimeout(5*time.Second),
  sdktrace.WithExportTimeout(30*time.Second),
)
```

Sizing rules:
- `max_queue_size` ≥ peak_spans_per_second × export_timeout_seconds. If you handle 1000 spans/sec and the export takes 30s during a backend slowdown, the queue must hold 30,000 spans or you'll drop.
- `max_export_batch_size`: 512 is a good default; larger values reduce per-request overhead but increase tail latency.
- `schedule_delay_millis`: 5s for traces. Lower means more frequent small exports; higher means data is delayed but exports are efficient.

## Self-telemetry — monitor the pipeline

The Collector exposes its own metrics on `:8888/metrics` (Prometheus format) and OTLP. **Scrape these and alert on them.** This is the single most important production hygiene step.

### Critical metrics

| Metric | What it tells you | Alert when |
|--------|-------------------|------------|
| `otelcol_exporter_send_failed_spans` | Spans the exporter could not deliver | rate > 0 sustained for 5min |
| `otelcol_exporter_send_failed_metric_points` | Metric datapoints failed | rate > 0 sustained for 5min |
| `otelcol_exporter_send_failed_log_records` | Logs failed | rate > 0 sustained for 5min |
| `otelcol_processor_dropped_spans` | Spans dropped by a processor (memory_limiter, sampling) | track per processor; spike means upstream pressure |
| `otelcol_processor_refused_spans` | Spans rejected (memory_limiter at limit) | rate > 0 means hitting memory cap |
| `otelcol_exporter_queue_size` | Current queue depth | > 80% of `queue_size` for 5min |
| `otelcol_receiver_refused_spans` | Spans rejected at receiver (auth, rate limit, payload too large) | rate > 0 |
| `otelcol_process_runtime_heap_alloc_bytes` | Heap usage | > 80% of `limit_mib` |

### Recommended alerts

```yaml
# Prometheus alert rules — paste into your alerting config
groups:
  - name: otelcol
    rules:
      - alert: OtelColExporterFailing
        expr: rate(otelcol_exporter_send_failed_spans[5m]) > 0
        for: 5m
        annotations:
          summary: "Collector dropping spans: {{ $labels.exporter }}"

      - alert: OtelColMemoryHigh
        expr: otelcol_process_runtime_heap_alloc_bytes / (1024*1024) > 1500
        for: 5m
        annotations:
          summary: "Collector approaching memory limit"

      - alert: OtelColQueueFilling
        expr: otelcol_exporter_queue_size / otelcol_exporter_queue_capacity > 0.8
        for: 5m
        annotations:
          summary: "Collector queue >80% full — backend may be slow"

      - alert: OtelColRefusing
        expr: rate(otelcol_processor_refused_spans[5m]) > 0
        for: 2m
        annotations:
          summary: "memory_limiter is dropping data — increase capacity or reduce ingest"
```

### SLOs for the pipeline

A pipeline that drops 1% of spans is fine for some teams, unacceptable for others. Pick explicit numbers:

| SLO | Target |
|-----|--------|
| Span delivery success rate | 99.9% (drop ≤ 0.1% over 30 days) |
| End-to-end latency (span produced → queryable in backend) | p95 < 60s |
| Pipeline availability (Collector reachable from apps) | 99.95% |

Track these as SLIs computed from the Collector's own metrics + a synthetic probe (a periodic test trace that checks if it appears in the backend within N seconds).

## Health checks and probes

Always enable the `health_check` extension and configure K8s probes:

```yaml
extensions:
  health_check:
    endpoint: 0.0.0.0:13133
    path: /
    check_collector_pipeline:
      enabled: true
      interval: 5m
      exporter_failure_threshold: 5    # mark unhealthy if 5 consecutive export failures

service:
  extensions: [health_check]
```

```yaml
# K8s pod spec
livenessProbe:
  httpGet: { path: /, port: 13133 }
  initialDelaySeconds: 10
  periodSeconds: 30
readinessProbe:
  httpGet: { path: /, port: 13133 }
  initialDelaySeconds: 5
  periodSeconds: 10
```

Liveness must use a long enough threshold that a brief backend hiccup doesn't restart the Collector (which would lose in-memory queue if no persistent storage).

## Graceful shutdown

The Collector's `service` block accepts a shutdown timeout:

```yaml
service:
  telemetry:
    metrics: { level: detailed }
  # No direct shutdown_timeout in the service block; controlled per-exporter via:
exporters:
  otlp/backend:
    endpoint: ...
    timeout: 30s                         # max wait per export call during shutdown
```

K8s pod spec:
```yaml
terminationGracePeriodSeconds: 60        # give the Collector time to drain
```

Apps should also handle SIGTERM and call SDK shutdown — see `instrumentation-polyglot.md`. Without this, app shutdown drops in-flight spans before the sidecar/agent can flush them.

## Capacity planning

Rough sizing for a gateway Collector with tail sampling:

| Throughput (spans/sec) | Replicas | CPU per replica | Memory per replica | PVC per replica |
|-----------------------|----------|-----------------|--------------------|-----------------|
| <1K | 2 | 500m | 1Gi | 5Gi |
| 1K-10K | 3 | 1 | 2Gi | 20Gi |
| 10K-50K | 5 | 2 | 4Gi | 50Gi |
| 50K-200K | 10+ | 2-4 | 4-8Gi | 100Gi |
| >200K | scale horizontally; consider partitioning by service.name |

Tail sampling memory scales with `num_traces × decision_wait_seconds × spans_per_trace × bytes_per_span`. 100K traces × 30s × 20 spans × 2KB = 120GB across the cluster. Tune `num_traces` first if memory is the bottleneck.

## Common pitfalls

**`memory_limiter` not first in the pipeline.** If `batch` runs before `memory_limiter`, the Collector OOMs while batching. Order: `memory_limiter → ... → batch`, every pipeline.

**Persistent queue without disk monitoring.** The disk queue can fill to its capacity if the backend is down for hours, then start dropping. Monitor `otelcol_exporter_queue_size` and the underlying disk usage.

**Tail sampling with multiple Collectors but no load-balancer exporter.** Spans from one trace land on different Collectors, none has the full trace, sampling decisions are wrong. Always use `loadbalancing` exporter from agents to gateway when tail sampling is enabled.

**`:latest` image tag in production.** ADOT and the upstream Collector both ship config schema changes occasionally. An automatic image pull breaks a working pipeline. Pin: `otel/opentelemetry-collector-contrib:0.121.0`.

**One Collector deployment serving every signal at the same scale.** Logs are usually 10-100x higher volume than traces. Splitting into separate pipelines (or even separate Collector deployments) per signal lets you scale them independently.

**No backend timeout configured.** Default exporter timeout is 30s; if the backend hangs, the Collector blocks. Set explicit `timeout: 10s` on each exporter and rely on retry to handle transient failures.

**Resource detector misconfiguration on the gateway.** `resourcedetection` on the gateway tries to detect *its own* environment, which isn't what you want — you want the agent's detection. Run `k8sattributes` and `resourcedetection` on the agent tier; the gateway should mostly do sampling and fan-out.

**Health check passes while exporter fails.** Default `health_check` only checks the HTTP endpoint, not pipeline health. Enable `check_collector_pipeline` in the extension config so health reflects export status.

**Forgetting `pprof` and `zpages` in production.** When the Collector misbehaves, these are how you debug. `:1777/debug/pprof/` for CPU/heap profiles, `:55679/debug/tracez` for live span counts. Ship them enabled, restrict network access via NetworkPolicy or the cluster's edge.

## Quick checklist

```markdown
## Collector production review

- [ ] memory_limiter is the first processor in every pipeline
- [ ] memory_limiter limits match container resources (75% / 25%)
- [ ] batch processor configured (last in pipeline) per signal
- [ ] sending_queue with file_storage extension enabled on every exporter
- [ ] retry_on_failure with explicit max_elapsed_time on every exporter
- [ ] Persistent volume mounted for queue (K8s) or accepted data loss documented (Cloud Run)
- [ ] Gateway pattern in use if >50 services or tail sampling needed
- [ ] loadbalancing exporter from agents to gateway (if tail sampling at gateway)
- [ ] OTLP compression: gzip or zstd enabled end-to-end
- [ ] TLS on production receivers; mTLS for gateway accepting external traffic
- [ ] health_check extension with check_collector_pipeline enabled
- [ ] K8s liveness + readiness probes on health_check endpoint
- [ ] Self-telemetry scraped (otelcol_* metrics) into Prometheus
- [ ] Alerts: send_failed > 0, queue > 80%, refused > 0, memory > 80%
- [ ] pprof and zpages extensions enabled (for debugging)
- [ ] Image pinned to specific version (not :latest)
- [ ] terminationGracePeriodSeconds ≥ 60s in K8s pod spec
- [ ] BatchSpanProcessor in SDKs sized for peak load
- [ ] SLOs defined: span delivery rate, end-to-end latency, availability
```

## Sources

- OpenTelemetry Collector documentation — opentelemetry.io/docs/collector/
- memory_limiter processor — github.com/open-telemetry/opentelemetry-collector/tree/main/processor/memorylimiterprocessor
- file_storage extension — github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/extension/storage/filestorage
- loadbalancing exporter — github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/exporter/loadbalancingexporter
- Collector deployment patterns — opentelemetry.io/docs/collector/deployment/
- Collector self-monitoring — opentelemetry.io/docs/collector/internal-telemetry/
- Sizing guidance — opentelemetry.io/docs/collector/scaling/