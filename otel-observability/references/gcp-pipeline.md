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
