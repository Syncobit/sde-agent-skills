# GCP Pipeline — Cloud Run, GKE, and the Google-Built Collector

How to ship OTel telemetry to Google Cloud's observability stack: Cloud Trace, Cloud Monitoring, and Cloud Logging. Covers both Cloud Run and GKE deployment patterns.

## The Google-Built OpenTelemetry Collector

Google maintains a curated build of the OTel Collector with all GCP exporters and resource detectors pre-installed. Use it instead of the upstream `otel/opentelemetry-collector-contrib` image when shipping to GCP — fewer moving parts, better-tuned defaults, and Google handles security patches.

Image: `us-docker.pkg.dev/cloud-ops-agents-artifacts/google-cloud-opentelemetry-collector/otelcol-google:<version>`

Verify the latest stable version at [`googlecloudplatform/opentelemetry-operations-collector`](https://github.com/GoogleCloudPlatform/opentelemetry-operations-collector) before pinning.

## Cloud Run pattern: Collector as sidecar

Cloud Run supports multi-container deployments (sidecars) via the `containers` array in the service spec, with `dependsOn` ordering and shared loopback networking.

### The architecture

```
┌─────────────────────────────────────┐
│    Cloud Run Service Instance       │
│  ┌──────────┐    ┌────────────────┐ │
│  │   App    │───→│   Collector    │─┼──→ Cloud Trace / Monitoring / Logging
│  │ (OTLP)   │    │ (Google-Built) │ │
│  └──────────┘    └────────────────┘ │
│   localhost:4317 (gRPC) or :4318 (HTTP) │
└─────────────────────────────────────┘
```

The app exports OTLP to localhost. The sidecar Collector receives, processes, exports to GCP.

### Service YAML (`service.yaml`)

```yaml
apiVersion: serving.knative.dev/v1
kind: Service
metadata:
  name: vqms-api
  annotations:
    run.googleapis.com/launch-stage: GA
spec:
  template:
    metadata:
      annotations:
        run.googleapis.com/container-dependencies: '{"collector":["app"]}'
    spec:
      containers:
        # Main application
        - name: app
          image: us-docker.pkg.dev/PROJECT_ID/vqms/api:1.4.7
          ports:
            - containerPort: 8080
          env:
            - name: OTEL_SERVICE_NAME
              value: "vqms-api"
            - name: OTEL_EXPORTER_OTLP_ENDPOINT
              value: "http://localhost:4317"
            - name: OTEL_RESOURCE_ATTRIBUTES
              value: "deployment.environment.name=production,service.namespace=vqms"
            - name: OTEL_TRACES_SAMPLER
              value: "parentbased_traceidratio"
            - name: OTEL_TRACES_SAMPLER_ARG
              value: "0.1"
          resources:
            limits:
              cpu: "1"
              memory: "512Mi"

        # Collector sidecar
        - name: collector
          image: us-docker.pkg.dev/cloud-ops-agents-artifacts/google-cloud-opentelemetry-collector/otelcol-google:0.121.0
          args:
            - "--config=/etc/otelcol-google/config.yaml"
          volumeMounts:
            - name: collector-config
              mountPath: /etc/otelcol-google
          resources:
            limits:
              cpu: "500m"
              memory: "256Mi"

      volumes:
        - name: collector-config
          secret:
            secretName: vqms-collector-config
            items:
              - key: latest
                path: config.yaml
```

The `container-dependencies` annotation ensures the Collector starts before the app and shuts down after — preventing data loss during Cloud Run's lifecycle events.

### Collector config (`config.yaml`)

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  # Auto-detect Cloud Run resource attributes
  resourcedetection:
    detectors: [env, gcp]
    timeout: 2s

  # Batch for efficiency
  batch:
    timeout: 10s
    send_batch_size: 1024

  # Tail sampling — keep 100% of errors and slow requests, 10% of others
  tail_sampling:
    decision_wait: 30s
    policies:
      - name: errors
        type: status_code
        status_code: { status_codes: [ERROR] }
      - name: slow
        type: latency
        latency: { threshold_ms: 1000 }
      - name: probabilistic
        type: probabilistic
        probabilistic: { sampling_percentage: 10 }

  # Drop high-cardinality attributes
  transform/drop_cardinality:
    metric_statements:
      - context: datapoint
        statements:
          - delete_key(attributes, "user.id")
          - delete_key(attributes, "session.id")

exporters:
  googlecloud:
    project: syncobit-prod
    log:
      default_log_name: "vqms-api"
    sending_queue:
      enabled: true
      queue_size: 1000

  googlemanagedprometheus:
    project: syncobit-prod

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [resourcedetection, tail_sampling, batch]
      exporters: [googlecloud]
    metrics:
      receivers: [otlp]
      processors: [resourcedetection, transform/drop_cardinality, batch]
      exporters: [googlemanagedprometheus]
    logs:
      receivers: [otlp]
      processors: [resourcedetection, batch]
      exporters: [googlecloud]
```

Store this in Secret Manager:
```bash
gcloud secrets create vqms-collector-config --data-file=config.yaml
gcloud secrets add-iam-policy-binding vqms-collector-config \
    --member=serviceAccount:vqms-api@PROJECT.iam.gserviceaccount.com \
    --role=roles/secretmanager.secretAccessor
```

### IAM (service account permissions)

The Cloud Run service account needs:

```bash
SA="vqms-api@PROJECT_ID.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding PROJECT_ID --member="serviceAccount:${SA}" --role="roles/cloudtrace.agent"
gcloud projects add-iam-policy-binding PROJECT_ID --member="serviceAccount:${SA}" --role="roles/monitoring.metricWriter"
gcloud projects add-iam-policy-binding PROJECT_ID --member="serviceAccount:${SA}" --role="roles/logging.logWriter"
gcloud projects add-iam-policy-binding PROJECT_ID --member="serviceAccount:${SA}" --role="roles/monitoring.viewer"   # for Managed Prometheus
```

### Deploy

```bash
gcloud run services replace service.yaml --region=us-central1
```

## GKE pattern: OpenTelemetry Operator

For GKE, the standard pattern is the OpenTelemetry Operator, which lets you declare Collectors as Kubernetes resources and inject SDK auto-instrumentation into pods via annotations.

### Install the operator

```bash
kubectl apply -f https://github.com/open-telemetry/opentelemetry-operator/releases/latest/download/opentelemetry-operator.yaml
```

### Deploy a Collector as a DaemonSet

```yaml
apiVersion: opentelemetry.io/v1beta1
kind: OpenTelemetryCollector
metadata:
  name: gateway
  namespace: observability
spec:
  mode: daemonset      # one Collector per node
  image: us-docker.pkg.dev/cloud-ops-agents-artifacts/google-cloud-opentelemetry-collector/otelcol-google:0.121.0
  serviceAccount: otel-collector
  config:
    receivers:
      otlp:
        protocols:
          grpc:
            endpoint: 0.0.0.0:4317
          http:
            endpoint: 0.0.0.0:4318
    processors:
      k8sattributes:                      # enrich with K8s metadata
        auth_type: serviceAccount
        passthrough: false
        extract:
          metadata:
            - k8s.pod.name
            - k8s.pod.uid
            - k8s.deployment.name
            - k8s.namespace.name
            - k8s.node.name
      resourcedetection:
        detectors: [env, gcp]
      batch: {}
    exporters:
      googlecloud:
        project: syncobit-prod
      googlemanagedprometheus:
        project: syncobit-prod
    service:
      pipelines:
        traces:
          receivers: [otlp]
          processors: [k8sattributes, resourcedetection, batch]
          exporters: [googlecloud]
        metrics:
          receivers: [otlp]
          processors: [k8sattributes, resourcedetection, batch]
          exporters: [googlemanagedprometheus]
        logs:
          receivers: [otlp]
          processors: [k8sattributes, resourcedetection, batch]
          exporters: [googlecloud]
```

Apps export OTLP to the Collector via the node-local endpoint:
```yaml
env:
  - name: OTEL_EXPORTER_OTLP_ENDPOINT
    value: "http://$(NODE_IP):4317"
  - name: NODE_IP
    valueFrom:
      fieldRef:
        fieldPath: status.hostIP
```

### Workload Identity (recommended over node service accounts)

```bash
# Create a Google service account for the Collector
gcloud iam service-accounts create otel-collector

# Grant observability roles
PROJECT_ID="syncobit-prod"
GSA="otel-collector@${PROJECT_ID}.iam.gserviceaccount.com"
for role in roles/cloudtrace.agent roles/monitoring.metricWriter roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding ${PROJECT_ID} --member="serviceAccount:${GSA}" --role="${role}"
done

# Bind to the Kubernetes service account (Workload Identity)
gcloud iam service-accounts add-iam-policy-binding ${GSA} \
    --member="serviceAccount:${PROJECT_ID}.svc.id.goog[observability/otel-collector]" \
    --role="roles/iam.workloadIdentityUser"

# Annotate the K8s service account
kubectl annotate serviceaccount otel-collector -n observability \
    iam.gke.io/gcp-service-account=${GSA}
```

### Auto-instrumentation injection (optional but powerful)

The operator can inject SDK auto-instrumentation into pods via annotations, eliminating the need to update application Dockerfiles:

```yaml
apiVersion: opentelemetry.io/v1alpha1
kind: Instrumentation
metadata:
  name: vqms-instrumentation
  namespace: vqms
spec:
  exporter:
    endpoint: http://gateway-collector.observability:4317
  propagators:
    - tracecontext
    - baggage
  sampler:
    type: parentbased_traceidratio
    argument: "0.1"
  python:
    image: ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-python:latest
  nodejs:
    image: ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-nodejs:latest
  java:
    image: ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-java:latest
```

Then annotate pods to enable injection:
```yaml
metadata:
  annotations:
    instrumentation.opentelemetry.io/inject-python: "vqms/vqms-instrumentation"
```

## Gateway pattern on GCP

For >50 services or any tail sampling, graduate from sidecars to a centralized gateway Collector cluster. The general production-hardening guidance lives in `collector-production.md` — this section is the GCP-specific deployment.

### GKE-hosted gateway (recommended)

Run the gateway as a `StatefulSet` in a dedicated `observability` namespace, with persistent volumes for the file-storage queue:

```yaml
apiVersion: opentelemetry.io/v1beta1
kind: OpenTelemetryCollector
metadata:
  name: gateway
  namespace: observability
spec:
  mode: statefulset
  replicas: 3
  image: us-docker.pkg.dev/cloud-ops-agents-artifacts/google-cloud-opentelemetry-collector/otelcol-google:0.121.0
  serviceAccount: otel-gateway
  volumeClaimTemplates:
    - metadata: { name: queue }
      spec:
        accessModes: [ReadWriteOnce]
        resources: { requests: { storage: "20Gi" } }
        storageClassName: standard-rwo
  volumeMounts:
    - { name: queue, mountPath: /var/lib/otelcol }
```

Apps in any GCP environment (Cloud Run, GKE, Cloud Functions, GCE) export OTLP to the gateway via internal load balancer. For Cloud Run apps reaching the gateway in GKE, use **Internal HTTP(S) Load Balancing** with VPC-direct egress so the traffic stays on the private network:

```yaml
# Cloud Run service annotations for VPC egress
metadata:
  annotations:
    run.googleapis.com/vpc-access-connector: projects/PROJECT/locations/us-central1/connectors/obs-connector
    run.googleapis.com/vpc-access-egress: private-ranges-only
```

The internal LB DNS name (`otel-gateway.observability.svc.cluster.local` from inside the cluster, or a static internal IP exposed via private DNS for cross-cluster access) becomes the OTLP endpoint for every app.

### Two-tier (DaemonSet agent + StatefulSet gateway)

For larger estates, agents on each GKE node forward to the gateway with the `loadbalancing` exporter:

```yaml
# Agent DaemonSet config (excerpt)
exporters:
  loadbalancing:
    routing_key: traceID
    protocol:
      otlp:
        tls: { insecure: false, ca_file: /etc/otelcol/ca.pem }
    resolver:
      k8s:
        service: gateway-collector.observability
        ports: [4317]
```

See `collector-production.md` for the full topology rationale, persistent queue setup, and self-telemetry SLOs.

## Cloud Functions Gen 2

Cloud Functions Gen 2 runs on Cloud Run under the hood — use the Cloud Run sidecar pattern from above.

For Gen 1 functions (still supported for legacy code), the sidecar pattern doesn't apply (no multi-container support). Options:
- **Direct exporter**: ship OTLP from the function code straight to your gateway. Adds cold-start cost.
- **Cloud Trace via Google Client Library**: use `google-cloud-trace` and skip OTel entirely on Gen 1. Acceptable if you only need traces and only on GCP.

Gen 2 example (Python, deployed via `gcloud functions deploy`):

```python
# main.py
import functions_framework
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

@functions_framework.http
def handle(request):
    with tracer.start_as_current_span("handle_request") as span:
        span.set_attribute("http.method", request.method)
        span.set_attribute("http.path", request.path)
        # ... business logic ...
        return "ok", 200
```

Deploy with the sidecar collector via service YAML same as Cloud Run.

### Cold-start consideration

Cloud Functions cold starts plus Collector sidecar startup can be 1-3s. For latency-sensitive callbacks, `min-instances: 1` keeps one warm at the cost of always-on billing.

## Apigee — `X-Cloud-Trace-Context` and W3C dual propagation

Apigee proxies emit traces using GCP's pre-W3C header, `X-Cloud-Trace-Context`. Modern OTel SDKs default to W3C `traceparent`. To preserve trace continuity from Apigee to backend services, register both propagators:

```python
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagators.cloud_trace_propagator import CloudTraceFormatPropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator

set_global_textmap(CompositePropagator([
    TraceContextTextMapPropagator(),    # W3C — for OTel-native services
    W3CBaggagePropagator(),
    CloudTraceFormatPropagator(),       # X-Cloud-Trace-Context — for Apigee, Cloud Run LB
]))
```

GCP load balancers (HTTPS LB, Internal HTTPS LB) also emit `X-Cloud-Trace-Context`. Without the Cloud Trace propagator, traces appear to start at your service rather than at the LB or Apigee — losing the front-door tier from the trace.

In Apigee, enable distributed tracing in the proxy configuration:

```xml
<!-- Apigee proxy config -->
<DistributedTrace>
    <Enabled>true</Enabled>
    <SamplingPercentage>10</SamplingPercentage>
</DistributedTrace>
```

Apigee shipps trace data to Cloud Trace directly (not via your Collector). Two paths into Cloud Trace — Apigee's direct ship and your Collector — appear as the same trace if `traceparent`/`X-Cloud-Trace-Context` matches.

## Pub/Sub trace propagation

Pub/Sub doesn't propagate trace context automatically. The pattern: encode `traceparent` as a message attribute on publish, extract on receive.

### Publisher (Python)

```python
from google.cloud import pubsub_v1
from opentelemetry import trace, propagate

publisher = pubsub_v1.PublisherClient()
topic_path = publisher.topic_path("PROJECT", "TOPIC")
tracer = trace.get_tracer(__name__)

def publish_event(payload: dict):
    with tracer.start_as_current_span("pubsub.publish") as span:
        span.set_attribute("messaging.system", "gcp_pubsub")
        span.set_attribute("messaging.destination.name", topic_path)

        # Encode trace context into message attributes
        attrs = {}
        propagate.inject(attrs)        # writes traceparent, tracestate, baggage

        future = publisher.publish(
            topic_path,
            data=json.dumps(payload).encode(),
            **attrs,                    # pubsub message attributes
        )
        message_id = future.result()
        span.set_attribute("messaging.message.id", message_id)
```

### Subscriber (Python pull)

```python
from opentelemetry import context
from opentelemetry.trace import SpanKind

def handle_message(message: pubsub_v1.subscriber.message.Message):
    # Extract trace context from the message attributes
    ctx = propagate.extract(dict(message.attributes))

    with tracer.start_as_current_span(
        "pubsub.process",
        context=ctx,
        kind=SpanKind.CONSUMER,
    ) as span:
        span.set_attribute("messaging.system", "gcp_pubsub")
        span.set_attribute("messaging.message.id", message.message_id)
        # ... process the payload ...
        message.ack()
```

The publisher and subscriber spans now appear in the same trace, even though they may run minutes or hours apart on different services. Without this, every subscriber starts a new trace.

For high-throughput Pub/Sub, the auto-instrumentation `opentelemetry-instrumentation-google-cloud-pubsub` (community-maintained, check stability) handles this automatically.

## Eventarc

Eventarc routes events from various GCP sources (Pub/Sub, Cloud Storage, Audit Logs) to consumers (Cloud Run, GKE, Workflows). The trace propagation story:

- For Pub/Sub-backed Eventarc events, trace context flows in message attributes — same pattern as direct Pub/Sub.
- For CloudEvents-formatted Eventarc events, `traceparent` should be in the CloudEvents extension attributes.

Verify by inspecting an actual delivered event:

```python
@functions_framework.cloud_event
def handle_eventarc(event):
    # CloudEvent extensions include traceparent if upstream set it
    traceparent = event.get("traceparent")
    ctx = propagate.extract({"traceparent": traceparent}) if traceparent else None
    with tracer.start_as_current_span("eventarc.handle", context=ctx) as span:
        span.set_attribute("ce.type", event["type"])
        span.set_attribute("ce.source", event["source"])
        # ... handle event ...
```

If your Eventarc events lack `traceparent`, the upstream emitter isn't propagating — fix at the source, or accept that Eventarc starts a new trace.

## Cloud Workflows

Cloud Workflows orchestrates a series of steps across services. Tracing across workflow steps requires propagating `traceparent` in each HTTP call:

```yaml
# workflow.yaml
main:
  params: [event]
  steps:
    - init:
        assign:
          - traceparent: ${event.headers["traceparent"]}
    - call_service_a:
        call: http.post
        args:
          url: https://service-a.run.app/process
          headers:
            traceparent: ${traceparent}
            x-workflow-execution: ${sys.get_env("GOOGLE_CLOUD_WORKFLOW_EXECUTION_ID")}
          body: ${event.body}
        result: result_a
    - call_service_b:
        call: http.post
        args:
          url: https://service-b.run.app/process
          headers:
            traceparent: ${traceparent}
          body: ${result_a.body}
```

The workflow step itself doesn't currently auto-emit OTel spans (as of May 2026). Workaround: a thin "workflow tracer" Cloud Function called by each step that creates a span representing the step. Enrich with `gcp.workflow.execution_id`, `gcp.workflow.step.name` attributes.

Cloud Workflows' execution logs are in Cloud Logging — correlate via `executions.googleapis.com%2Fexecutions/<id>` resource label.

## Private networking — VPC-SC and Private Google Access

Production GCP workloads usually run in private VPCs without public IPs. The observability pipeline must work over the private network:

### Private Google Access for Collector → Cloud Trace/Logging/Monitoring

Enable Private Google Access on the subnet where Collectors run. Cloud Trace, Cloud Logging, Cloud Monitoring APIs resolve to private IPs (`private.googleapis.com` / `restricted.googleapis.com`). No public egress.

```bash
gcloud compute networks subnets update obs-subnet \
    --region=us-central1 \
    --enable-private-ip-google-access
```

For VPC-SC perimeters, use `restricted.googleapis.com` (only services protected by your perimeter) instead of `private.googleapis.com` (all GCP services).

### Cloud Run / Cloud Functions VPC egress

Apps in Cloud Run that need to reach a GKE-hosted gateway use a Serverless VPC Access connector:

```bash
gcloud compute networks vpc-access connectors create obs-connector \
    --region=us-central1 \
    --network=vpc-prod \
    --range=10.8.0.0/28
```

Cloud Run service annotation:
```yaml
run.googleapis.com/vpc-access-connector: projects/PROJECT/locations/us-central1/connectors/obs-connector
run.googleapis.com/vpc-access-egress: private-ranges-only
```

Now the Cloud Run service can reach the GKE gateway via its internal load balancer IP, and to GCP APIs via Private Google Access.

For VPC Service Controls setup (perimeter creation, telemetry API restriction), see `security-and-compliance.md`.

## Mapping OTel signals to GCP backends

| OTel signal | Default GCP backend | Alternative |
|-------------|---------------------|-------------|
| Traces | Cloud Trace (via `googlecloud` exporter) | — |
| Metrics | Managed Service for Prometheus (via `googlemanagedprometheus` exporter) | Cloud Monitoring (via `googlecloud` exporter) — older path |
| Logs | Cloud Logging (via `googlecloud` exporter) | — |

**Recommendation: use Managed Service for Prometheus for metrics.** It accepts Prometheus-style metrics with labels (translated from OTel attributes) and is what most modern GCP observability work uses. The older `googlecloud` metrics exporter maps to Cloud Monitoring "custom metrics" which has tighter cardinality limits.

## Common pitfalls

**Cold start trace loss on Cloud Run.** Cloud Run scales to zero by default; a cold start sets up the Collector sidecar, but the first request can complete before the Collector is ready. Mitigations:
- Use the `container-dependencies` annotation (shown above) so the app waits for the Collector
- Set min-instances to 1 for latency-critical services (eliminates cold starts at the cost of always-on billing)
- Configure a generous `BatchSpanProcessor` queue in the SDK so spans don't drop while the Collector starts

**Resource attribute conflicts.** `OTEL_RESOURCE_ATTRIBUTES` (env var) and the `resourcedetection` processor both contribute attributes. The Collector's `resourcedetection` overrides the SDK's view. To make the SDK's attributes win, use `override: false` in the resourcedetection processor config.

**`gcp.project_id` missing.** The `googlecloud` exporter requires the `project` config field even when running on GCP. The resource detector won't always populate it. Set explicitly.

**Quotas and rate limits.** Cloud Trace ingestion has API quotas (default 200,000 spans/min per project). Sampling at the Collector is the protection — make sure your sampling rate fits within quota.

**Cloud Logging double-billing.** If your app already logs to stdout (Cloud Run captures these automatically into Cloud Logging) AND you export logs via OTel, you pay for the same log lines twice. Pick one path:
- Best: stop logging to stdout, ship everything via OTel logs
- Pragmatic: keep stdout for free Cloud Run capture, only ship OTel logs that need rich attribute context

## Sources

- Google's OTel Collector docs — cloud.google.com/stackdriver/docs/instrumentation/opentelemetry-collector
- Cloud Run sidecar pattern — cloud.google.com/run/docs/tutorials/custom-metrics-opentelemetry-sidecar
- OpenTelemetry Operator — github.com/open-telemetry/opentelemetry-operator
- Managed Service for Prometheus — cloud.google.com/stackdriver/docs/managed-prometheus
