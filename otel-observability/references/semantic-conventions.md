# Semantic Conventions

OpenTelemetry's semantic conventions are the canonical attribute names for common concepts. Using them means your tooling, dashboards, and alerts work across services, languages, and vendors without per-service customization.

## Why this matters

Three services emit a span when they handle an HTTP request. Service A tags it `http_method`, service B tags it `request.method`, service C tags it `http.method`. Now your dashboard "average latency by HTTP method" needs three queries union'd together, your alert "GET requests above 500ms" misses two of the three services, and onboarding a new engineer requires explaining each service's idiosyncratic conventions.

Semantic conventions solve this by standardizing the attribute names. `http.request.method`, `http.response.status_code`, `service.name`, `db.system.name` mean the same thing everywhere. Tools built on top (Cloud Trace's UI, Grafana's prebuilt dashboards, Datadog's APM views) assume these conventions.

## The canonical conventions to know

The semantic conventions are large and evolving. These are the ones that matter most for typical service work.

### Resource attributes (describe the entity producing telemetry)

```yaml
service.name: "vqms-api"                      # required by the spec
service.version: "1.4.7"
service.namespace: "vqms"                      # logical group of services
service.instance.id: "<uuid-or-pod-name>"      # specific running instance

deployment.environment.name: "production"      # vs "staging", "dev"

# Cloud-specific
cloud.provider: "gcp"                          # or "aws", "azure"
cloud.region: "us-central1"
cloud.availability_zone: "us-central1-a"
cloud.account.id: "syncobit-prod"              # GCP project_id, AWS account ID

# Kubernetes
k8s.cluster.name: "vqms-prod-1"
k8s.namespace.name: "vqms"
k8s.pod.name: "vqms-api-7c4d-x8j2"
k8s.deployment.name: "vqms-api"
k8s.node.name: "gke-prod-pool-1-abc"

# Cloud Run-specific
gcp.cloud_run.service_name: "vqms-api"
gcp.cloud_run.revision_name: "vqms-api-00042-tab"

# AWS ECS-specific
aws.ecs.cluster.arn: "arn:aws:ecs:..."
aws.ecs.task.arn: "arn:aws:ecs:..."
aws.ecs.task.revision: "47"
```

These should be set by **resource detectors**, not hardcoded. The OTel SDKs include detectors that read GCP/AWS metadata services, Kubernetes downward API, and environment variables. The Collector's `resourcedetection` processor does the same on the pipeline side.

### HTTP attributes (spans for HTTP servers and clients)

```yaml
# Server-side (incoming requests)
http.request.method: "PATCH"
http.request.method_original: "patch"          # if case differed
http.route: "/v1/queues/{queue_id}/tickets/{ticket_id}"   # parameterized
http.response.status_code: 200
url.scheme: "https"
url.path: "/v1/queues/q-123/tickets/tk-42"
url.query: "include=audit"
network.peer.address: "203.0.113.42"
user_agent.original: "Mozilla/5.0..."
server.address: "api.vqms.example.com"
server.port: 443

# Client-side (outgoing requests)
http.request.method: "POST"
url.full: "https://api.partner.com/v1/notify"
http.response.status_code: 201
```

The `http.route` is the **template**, not the actual URL. `/v1/orders/{id}` not `/v1/orders/12345`. Critical for cardinality — without templating, every order ID becomes a separate metric.

### Database attributes

```yaml
db.system.name: "postgresql"                   # standardized values: postgresql, mysql, mongodb, redis, etc.
db.namespace: "vqms_production"                # database name
db.collection.name: "tickets"                  # table or collection
db.query.text: "SELECT * FROM tickets WHERE id = $1"
db.operation.name: "SELECT"
server.address: "10.0.5.42"
server.port: 5432
```

`db.query.text` may include sensitive data. Be careful: redact at the Collector or use parameterized form.

### Messaging attributes

```yaml
messaging.system: "kafka"                      # or "rabbitmq", "sqs", "pubsub"
messaging.operation.name: "publish"            # or "receive", "process"
messaging.destination.name: "tickets-events"
messaging.message.id: "msg-abc123"
messaging.kafka.message.key: "tk-42"
```

### RPC attributes

```yaml
rpc.system: "grpc"
rpc.service: "vqms.QueueService"
rpc.method: "CallNextTicket"
rpc.grpc.status_code: 0                        # 0 = OK
```

### Exception recording

```yaml
exception.type: "ValueError"
exception.message: "ticket already in 'completed' state"
exception.stacktrace: "<full stack trace>"
exception.escaped: false                       # true if the exception escaped the span
```

Use `span.recordException(err)` (or equivalent) — the SDKs handle the attribute names automatically.

## Custom attributes — when and how

Semantic conventions cover infrastructure concepts. They don't cover your business domain. For business attributes, you'll define your own.

**Naming convention**: dot-separated, lowercase, namespaced.

```yaml
# Good
queue.id: "q-123"
ticket.id: "tk-42"
ticket.state: "called"
ticket.queue_position: 7
tenant.id: "bank-al-etihad"
tenant.tier: "enterprise"

# Bad — unnamespaced, ambiguous
id: "tk-42"                                    # which id?
state: "called"
position: 7

# Bad — collides with potential future official conventions
http.tenant: "bank-al-etihad"                  # http.* is reserved
```

Use a stable namespace prefix for your own attributes (e.g., `vqms.*`, `good2go.*`) to avoid future collisions with OTel-defined conventions. A few specific recommendations:

- For **business identifiers**: `<domain>.<entity>.id` — `vqms.ticket.id`, `good2go.subscriber.msisdn`
- For **business state**: `<domain>.<entity>.state` — `vqms.ticket.state`, `good2go.activation.state`
- For **enrichment**: `<domain>.<entity>.<attribute>` — `vqms.queue.tenant_id`, `good2go.subscriber.plan_tier`

## The Weaver tool and code generation

OpenTelemetry now ships **Weaver**, a CLI tool for managing semantic conventions as code. It lets you:

- Define your conventions in YAML
- Validate them against OTel's schema
- Generate constants/types in your application code (Python, Go, Java, JS, Rust)
- Catch convention violations at build time

For Syncobit-scale work, Weaver is overkill in early stages but becomes valuable once you have multiple services and want to enforce consistency. Start without it; adopt when you have enough services that ad-hoc convention drift becomes painful.

## Stability tiers

Not all conventions are stable. The OTel spec marks each convention as one of:

- **Stable**: backward-compatible. Use freely.
- **Development** (formerly "Experimental"): in flux. May change. Use cautiously.
- **Deprecated**: scheduled for removal.

As of May 2026, most foundational conventions (HTTP, RPC, database, messaging, resource attributes) are stable. Newer signal-specific conventions (GenAI, FaaS) are in development.

When the SDK emits a deprecated attribute, modern Collectors auto-translate to the stable name via the `schemaurl` mechanism — but only if the SDK version and Collector version both support it. Pin known-stable SDK versions in production.

## Mapping to vendor backends

Vendor backends consume OTel attributes but may have idiosyncratic mappings:

### Google Cloud
- `service.name` → service name in Cloud Trace UI
- `cloud.region`, `cloud.availability_zone` → labels in Cloud Monitoring
- `gcp.project_id` → required by `googlecloud` exporter; auto-detected on GCP
- `k8s.*` attributes → labels in GKE workload views

### AWS
- `service.name` → service name in X-Ray
- `aws.ecs.*` → ECS-specific metadata visible in X-Ray
- `cloud.account.id`, `cloud.region` → standard AWS metadata
- For X-Ray's `Origin` attribute, the AWS Collector exporter auto-derives from `cloud.platform`

If a vendor UI is missing data you expect, check the conventions doc for the exact attribute name the vendor maps from. Often the issue is a mismatch between what your SDK emits and what the vendor expects.

## Practical recommendations

1. **Use `service.name` consistently.** Set it via `OTEL_SERVICE_NAME` env var in every deployment. Inconsistency here breaks every dashboard and alert.

2. **Set `deployment.environment.name`** on every service in every environment. Without it, you can't filter "production only" in any vendor UI.

3. **Use `http.route` (the template), not `url.path` (the actual URL)** for HTTP server metrics. Cardinality difference is enormous.

4. **Don't put PII in attributes.** Email addresses, names, raw card numbers — these end up in trace UIs visible to anyone with observability access. Hash, redact, or omit. The Collector's `redaction` processor can help.

5. **Use the `service.namespace` attribute** to group related services. `vqms.api`, `vqms.worker`, `vqms.scheduler` all having `service.namespace=vqms` makes group-wide queries trivial.

6. **Adopt new conventions when they stabilize, not before.** A `WARN` deprecation in your CI logs is cheap; a breaking change in a "development"-tier attribute that ships to production is expensive.

## Sources

- OpenTelemetry semantic conventions — opentelemetry.io/docs/specs/semconv
- Conventions repository — github.com/open-telemetry/semantic-conventions
- Weaver tool — github.com/open-telemetry/weaver
- Stability levels — opentelemetry.io/docs/specs/otel/document-status
