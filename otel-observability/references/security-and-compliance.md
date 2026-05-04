# Security and Compliance — Telemetry as a Data System

Telemetry data is data. Spans, logs, and metrics record what users did, what they typed, where they came from, and what the system did about it. Trace UIs are typically accessible to a wider engineering audience than production databases, which inverts the usual access-control assumption — the most sensitive material can end up in the most accessible place.

This file covers the security controls and compliance posture that make a telemetry pipeline defensible: authentication on receivers, redaction at the source, encryption at rest, network isolation, data residency, and the audit-log discipline required by SOC2/HIPAA/PCI.

## Threat model

Before controls, the threats. Pipeline-level attack surfaces:

| Threat | Attack | Mitigation |
|--------|--------|-----------|
| Unauthenticated OTLP receiver accepts attacker-injected spans | Cost amplification, log injection, false alerts | mTLS or OIDC on receivers |
| API key in browser/edge code | Vendor account abuse, exfiltration | Proxy through your own gateway; never expose vendor keys |
| PII in span attributes / log records | Insider threat, breach scope expansion, GDPR violation | Redact at emission AND at Collector (defense in depth) |
| Telemetry crossing region boundaries | GDPR data residency violation | Region-pinned Collector + region-scoped exporters |
| Engineer with trace UI access reads payment data | Privilege escalation via observability tools | Don't put the data in the trace in the first place |
| Compromised Collector node has plaintext credentials | Lateral movement | Workload Identity (GCP) / IRSA (AWS) — no static creds |
| Trace data exists indefinitely after user account deletion | Right-to-be-forgotten violation | Bounded retention + per-user purge process |

The defense pattern: assume traces will be read by humans who shouldn't see PII. Build for that assumption.

## Receiver authentication

### mTLS — strongest, recommended for gateway receivers

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
        tls:
          cert_file: /etc/otelcol/tls.crt
          key_file: /etc/otelcol/tls.key
          ca_file: /etc/otelcol/ca.crt              # for verifying client certs
          client_ca_file: /etc/otelcol/client-ca.crt # require client cert (mTLS)
          reload_interval: 1h                        # auto-reload for cert rotation
```

Apps export with their own certificate:

```python
# Python OTLP gRPC with mTLS
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from grpc import ssl_channel_credentials

with open("/etc/app/ca.crt", "rb") as f: ca = f.read()
with open("/etc/app/client.crt", "rb") as f: cert = f.read()
with open("/etc/app/client.key", "rb") as f: key = f.read()

exporter = OTLPSpanExporter(
    endpoint="otel-gateway.observability.svc.cluster.local:4317",
    credentials=ssl_channel_credentials(
        root_certificates=ca,
        private_key=key,
        certificate_chain=cert,
    ),
)
```

For workloads, use **SPIFFE/SPIRE** to issue short-lived workload identities — mTLS without manual cert distribution. On GKE this is Mesh CA / Workload Identity; on EKS this is IAM Roles for Service Accounts plus a service mesh (Istio, Linkerd) for the mTLS plumbing.

### OIDC / bearer tokens — for browser and edge

mTLS doesn't work for browsers (no client certs), and is overkill for edge runtimes. Use signed JWT bearer tokens validated by the gateway's `oidcauth` extension:

```yaml
extensions:
  oidc:
    issuer_url: https://auth.vqms.example.com
    audience: otel-ingest
    attribute: authorization                # header to read

receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
        auth:
          authenticator: oidc
        cors:
          allowed_origins: [https://app.vqms.example.com]
          allowed_headers: [traceparent, tracestate, baggage, authorization]

service:
  extensions: [oidc]
```

Browsers send `Authorization: Bearer <session-jwt>` on OTLP requests. Tokens are short-lived (15-30 min), tied to user sessions. The gateway validates signature + audience + expiry before accepting the request.

For machine-to-machine on edge runtimes, use a separate audience (`otel-edge-ingest`) with a different signing key managed by your secret store. Rotate frequently.

### Per-tenant authentication

For multi-tenant SaaS where each tenant has its own SDK API key:

```yaml
extensions:
  bearertokenauth/tenant-acme:
    scheme: "Bearer"
    token: "${env:TENANT_ACME_TOKEN}"
  bearertokenauth/tenant-beta:
    scheme: "Bearer"
    token: "${env:TENANT_BETA_TOKEN}"

receivers:
  otlp/acme:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
        auth: { authenticator: bearertokenauth/tenant-acme }
  otlp/beta:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4318
        auth: { authenticator: bearertokenauth/tenant-beta }
```

Or — better for hundreds of tenants — front the gateway with an API gateway that validates the API key, sets a `tenant.id` header, and forwards. The Collector trusts the upstream API gateway via mTLS.

## Redaction — defense in depth

Two layers: at emission (most reliable), at the Collector (safety net). Never rely on only one.

### At emission — explicit allow-list, not deny-list

Emit only the attributes you've explicitly allowed. Anything not in the list is by definition not safe.

```python
# Bad — implicit allow, accidental PII
span.set_attribute("user", json.dumps(user.__dict__))

# Bad — deny list misses new fields added later
SAFE_USER_FIELDS = {"id", "tenant_id"}
def safe_user(u):
    return {k: v for k, v in u.__dict__.items() if k not in {"email", "phone", "ssn"}}

# Good — explicit allow list
def user_attributes(u):
    return {
        "user.id": u.id,
        "user.role": u.role,
        "tenant.id": u.tenant_id,
    }
span.set_attributes(user_attributes(user))
```

The discipline: a code reviewer can answer "could this leak PII?" by reading the explicit list. They can't easily verify the absence of PII in a serialized object.

### At the Collector — `redaction` and `attributes` processors

The `redaction` processor does pattern-based scrubbing:

```yaml
processors:
  redaction:
    # Allow only these keys; drop all others
    allowed_keys:
      # OTel semantic conventions
      - service.name
      - service.version
      - service.namespace
      - deployment.environment.name
      - http.request.method
      - http.route
      - http.response.status_code
      - url.scheme
      - url.path                              # only if you've already templated dynamic segments
      - db.system.name
      - db.collection.name
      - db.operation.name
      - rpc.system
      - rpc.method
      - exception.type
      - exception.message
      # Your business attributes
      - vqms.tenant.id
      - vqms.queue.id
      - vqms.ticket.id
      - vqms.idempotency.key
      - vqms.idempotency.action
    # Among allowed keys, redact values matching these regexes
    blocked_values:
      - "[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}"  # email
      - "\\b\\d{4}[-\\s]?\\d{4}[-\\s]?\\d{4}[-\\s]?\\d{4}\\b" # card number
      - "\\b\\d{3}-\\d{2}-\\d{4}\\b"                          # US SSN
      - "\\b(?:[0-9]{1,3}\\.){3}[0-9]{1,3}\\b"                # IPv4 (often PII for individuals)
    summary: debug                            # log redaction stats at debug level

  # Hash high-cardinality identifiers we want to keep (for grouping) but not in cleartext
  attributes/hash_user:
    actions:
      - key: user.id
        action: hash                          # SHA256
      - key: session.id
        action: hash
      - key: http.user_agent
        action: hash                          # for fingerprinting without PII

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, redaction, attributes/hash_user, batch]
      exporters: [otlp/backend]
```

`allowed_keys` is the powerful guard — it drops attributes you forgot existed. Maintain the list; review additions in PR.

### Specific PII patterns to block by default

```yaml
blocked_values:
  - "[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}"      # email
  - "\\b\\d{4}[-\\s]?\\d{4}[-\\s]?\\d{4}[-\\s]?\\d{4}\\b"  # 16-digit card
  - "\\b\\d{15,16}\\b"                                       # bare card-like digits
  - "\\b\\d{3}-\\d{2}-\\d{4}\\b"                             # US SSN
  - "\\bAKIA[0-9A-Z]{16}\\b"                                 # AWS access key ID
  - "\\b[A-Za-z0-9_-]{36}\\b"                                # UUIDs in places they shouldn't be
  - "(?i)bearer\\s+[a-zA-Z0-9._~+/=-]{20,}"                  # bearer tokens
  - "(?i)basic\\s+[a-zA-Z0-9+/=]{20,}"                       # basic auth
  - "(?i)password['\":\\s=]+[^\\s\"',}]+"                    # password fields
  - "[A-Za-z0-9+/=]{40,}"                                    # long base64 (likely keys/tokens)
```

These are minimums — add country-specific ID formats your service handles (passport numbers, national IDs, etc.).

## Encryption at rest — CMEK on GCP, KMS on AWS

Vendor backends encrypt at rest by default. For regulated workloads, customer-managed keys are usually required.

### GCP — Customer-Managed Encryption Keys (CMEK)

Cloud Logging, Cloud Trace, and Cloud Monitoring all support CMEK at the resource level (Logging) or project level (Trace/Monitoring).

```bash
# Cloud Logging — apply CMEK to a project's default log bucket
gcloud kms keyrings create observability --location=us-central1
gcloud kms keys create logs-encryption \
    --location=us-central1 --keyring=observability \
    --purpose=encryption

# Grant Cloud Logging service account access to the key
PROJECT_NUMBER=$(gcloud projects describe syncobit-prod --format='value(projectNumber)')
gcloud kms keys add-iam-policy-binding logs-encryption \
    --location=us-central1 --keyring=observability \
    --member="serviceAccount:cmek-${PROJECT_NUMBER}@gcp-sa-logging.iam.gserviceaccount.com" \
    --role=roles/cloudkms.cryptoKeyEncrypterDecrypter

# Configure log bucket to use the key
gcloud logging buckets update _Default \
    --location=us-central1 \
    --cmek-key=projects/syncobit-prod/locations/us-central1/keyRings/observability/cryptoKeys/logs-encryption
```

### AWS — KMS keys for CloudWatch Logs and X-Ray

```bash
# CloudWatch Logs — KMS encryption per log group
aws kms create-key --description "Observability encryption" --tags TagKey=purpose,TagValue=otel
KEY_ARN=arn:aws:kms:us-east-1:ACCOUNT:key/...

aws logs associate-kms-key \
    --log-group-name /aws/ecs/vqms-api \
    --kms-key-id $KEY_ARN

# X-Ray — KMS at the encryption configuration level (account-wide)
aws xray put-encryption-config --type KMS --key-id $KEY_ARN
```

Key policies must allow the service principal (`logs.us-east-1.amazonaws.com` for CWL) to encrypt/decrypt. Without that, log group writes silently fail.

## Network isolation — keeping telemetry off the public internet

### GCP — VPC Service Controls

VPC-SC creates a perimeter around services so they cannot send data outside. For observability, this means Cloud Trace / Cloud Logging / Cloud Monitoring API calls only succeed from inside the perimeter.

```bash
# Create a perimeter that includes observability services
gcloud access-context-manager perimeters create syncobit-prod-perimeter \
    --resources=projects/PROJECT_NUMBER \
    --restricted-services=logging.googleapis.com,cloudtrace.googleapis.com,monitoring.googleapis.com \
    --policy=POLICY_ID
```

Combined with **Private Google Access** in the VPC, the Collector connects to GCP observability APIs over the private network — no public IP, no NAT egress.

```yaml
# GKE node pool config
node_config:
  enable_private_nodes: true
  master_ipv4_cidr_block: 172.16.0.0/28
private_cluster_config:
  enable_private_endpoint: true
  enable_private_nodes: true

# Required for observability APIs to resolve to private IPs
networkPolicy:
  enabled: true
```

### AWS — VPC Endpoints (PrivateLink)

CloudWatch Logs, CloudWatch Monitoring, and X-Ray all support interface VPC endpoints. Traffic never traverses the public internet.

```bash
# Create endpoints in the VPC
aws ec2 create-vpc-endpoint \
    --vpc-id vpc-xxx \
    --service-name com.amazonaws.us-east-1.logs \
    --vpc-endpoint-type Interface \
    --subnet-ids subnet-aaa subnet-bbb \
    --security-group-ids sg-xxx

aws ec2 create-vpc-endpoint \
    --vpc-id vpc-xxx \
    --service-name com.amazonaws.us-east-1.monitoring \
    --vpc-endpoint-type Interface \
    --subnet-ids subnet-aaa subnet-bbb \
    --security-group-ids sg-xxx

aws ec2 create-vpc-endpoint \
    --vpc-id vpc-xxx \
    --service-name com.amazonaws.us-east-1.xray \
    --vpc-endpoint-type Interface \
    --subnet-ids subnet-aaa subnet-bbb \
    --security-group-ids sg-xxx
```

Set the endpoint's security group to accept traffic only from the Collector's security group. Combined with private subnets and no NAT gateway, this proves telemetry never left your VPC.

## Workload Identity — no static credentials

Static service account JSON keys (GCP) and IAM access keys (AWS) embedded in Collector configs are an audit failure waiting to happen. Use workload identity instead.

### GCP — Workload Identity Federation

```bash
# Create a Google service account for the Collector
gcloud iam service-accounts create otel-collector

# Bind the K8s service account to the Google SA
gcloud iam service-accounts add-iam-policy-binding \
    otel-collector@syncobit-prod.iam.gserviceaccount.com \
    --role="roles/iam.workloadIdentityUser" \
    --member="serviceAccount:syncobit-prod.svc.id.goog[observability/otel-collector]"

# Annotate the K8s SA
kubectl annotate serviceaccount otel-collector \
    -n observability \
    iam.gke.io/gcp-service-account=otel-collector@syncobit-prod.iam.gserviceaccount.com
```

The Collector reads tokens from the metadata service automatically. No JSON key file anywhere.

### AWS — IAM Roles for Service Accounts (IRSA)

```bash
eksctl create iamserviceaccount \
    --cluster=vqms-prod \
    --namespace=observability \
    --name=otel-collector \
    --attach-policy-arn=arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess \
    --attach-policy-arn=arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy \
    --approve
```

Same pattern: the K8s SA assumes the IAM role via OIDC federation. No long-lived credentials.

## Data residency

EU users' telemetry must stay in the EU under GDPR. US healthcare data may be HIPAA-restricted. The pattern: **per-region Collectors, region-pinned exporters**, and routing decisions based on user/tenant region.

### Region routing in the Collector

```yaml
# Gateway accepts all traffic, fans out per-region
processors:
  routing:
    from_attribute: vqms.user.region
    table:
      - value: eu
        exporters: [otlp/eu]
      - value: us
        exporters: [otlp/us]
      - value: apac
        exporters: [otlp/apac]
    default_exporters: [otlp/us]              # fallback only if region absent

exporters:
  otlp/eu:
    endpoint: trace-eu.vqms.example.com:443   # EU-region backend
  otlp/us:
    endpoint: trace-us.vqms.example.com:443
  otlp/apac:
    endpoint: trace-apac.vqms.example.com:443

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, redaction, routing, batch]
      exporters: [otlp/eu, otlp/us, otlp/apac]
```

The `routing` connector reads `vqms.user.region` (set by the application based on the request's tenant/user) and dispatches to the correct backend. Each backend lives in its respective region; cross-region transit never happens.

For deeper isolation, run separate Collector clusters per region — the routing decision is "which Collector cluster handles this tenant?", made at the API gateway tier rather than inside the observability pipeline.

## Audit logs vs operational logs

A common mistake: shipping security-relevant events (logins, permission grants, sensitive reads) to the same Cloud Logging bucket as INFO-level operational logs. Audit logs and operational logs have different requirements:

| Concern | Operational logs | Audit logs |
|---------|-----------------|------------|
| Retention | 7-30 days hot, 30-365 days cold | 1-7 years (regulatory minimum) |
| Access control | Engineering team | Security/compliance team only |
| Mutability | Can be edited (sampling, downsampling) | Immutable (write-once) |
| PII exposure | Redacted | Often must include user identity |
| Schema | Free-form structured logs | Strict schema (who, what, when, where) |
| Destination | Cloud Logging / CloudWatch / SIEM | Dedicated audit log bucket / CloudTrail / SIEM |

Separate the channels at emission:

```python
import logging
audit_logger = logging.getLogger("audit")          # separate logger
audit_logger.setLevel(logging.INFO)
# Configured with its own handler — does NOT use the OTel bridge

operational_logger = logging.getLogger("vqms")     # bridged to OTel logs
```

```yaml
# Collector — separate pipelines for the two
service:
  pipelines:
    logs/operational:
      receivers: [otlp]
      processors: [memory_limiter, redaction, batch]
      exporters: [googlecloud]                  # default Cloud Logging bucket
    logs/audit:
      receivers: [filelog/audit]                # tail a separate audit log file
      processors: [memory_limiter, batch]       # NO redaction — audit logs need full identity
      exporters: [googlecloud/audit]            # separate bucket with longer retention, restricted access
```

GCP-specific: enable **Cloud Audit Logs** for Admin Activity, Data Access, and System Event categories — these are managed by Google and immutable. Don't try to recreate them in your application code.

AWS-specific: **CloudTrail** is the equivalent. Application-level audit events go to a dedicated CloudWatch log group with stricter retention and access policies.

## Compliance frameworks — what each demands

### PCI DSS (payment card data)

- Card numbers (PAN) must never appear in spans, logs, or metrics. Block via redaction at multiple layers.
- Trace data containing payment context must be retained per PCI rules and have access logged.
- Cryptographic keys for the pipeline (TLS, KMS) must be managed per PCI key management requirements.

Practical: don't trace payment processing in detail. Capture the operation outcome (`payment.outcome: "approved"|"declined"`, `payment.amount_cents: 1500`, `payment.currency: "USD"`), never the card number, never the CVV. Use a dedicated payment service with hardened logging.

### HIPAA (US healthcare)

- Protected Health Information (PHI) cannot appear in observability data without a Business Associate Agreement (BAA) with the vendor.
- Minimum necessary disclosure: redact aggressively.
- Vendor backend must be HIPAA-eligible (Cloud Logging, AWS CloudWatch, Datadog HIPAA tier all qualify with BAA).

Practical: tag tenants whose data is PHI-bearing and route their traces to a HIPAA-eligible backend tier; do not commingle with non-PHI traffic.

### SOC 2

- Audit trail of who accessed observability data (vendor IAM/audit logs).
- Encryption at rest and in transit.
- Documented incident response process tied to observability alerts.
- Backup/restore of observability data (most vendors handle this; verify retention).

Practical: enable vendor audit logging on all observability tools. Periodically prove access reviews; orphaned access to trace UIs is a SOC 2 finding.

### GDPR / UK DPA

- Lawful basis to process personal data — observability is "legitimate interest" for system debugging, but only if the data is actually needed.
- Right to erasure: when a user requests deletion, their data in trace/log archives must be deleted within 30 days.
- Data residency for EU subjects.

Practical: use hashed user IDs in spans (`user.id: <hash>`), not raw PII. The hash makes erasure tractable — a deletion request maps to a hash list, you query and purge by hash. Without hashing, locating a user's trace records across years of archives is infeasible.

## Right to be forgotten — implementation

The pipeline must support erasure. Two patterns:

### Bounded retention (the simplest answer)

Trace and log retention ≤ 30 days for production, 7 days for staging. Erasure becomes "wait for natural expiry, no special action needed".

This is the right default unless regulatory requirements demand longer retention (audit logs, financial records). For typical operational telemetry, 30 days covers incident debugging without retention bloat.

### Targeted purge (when long retention is mandatory)

For backends that store telemetry indefinitely (rare but real for some audit configurations):

1. Tag every span/log with hashed `user.id` (never raw)
2. Maintain a deletion queue: when a user requests deletion, enqueue their hash
3. Run a daily/weekly job that issues delete-by-attribute queries to the backend (most major backends support this — Datadog `delete-events`, Splunk `delete` macro, Cloud Logging `gcloud logging logs delete`)
4. Audit the deletion (when, who, what hash) in a separate immutable log

The hash is forensically irreversible if a user disputes deletion later — that's the point.

## Common pitfalls

**Trusting redaction processor as the only line of defense.** A new attribute name not on the `allowed_keys` list slips through if you toggle to deny-mode by accident, or a config typo disables the processor. Layer at emission too.

**Vendor API key in K8s ConfigMap.** ConfigMaps are not secrets. Use Secret resources, mount as files, never as env vars (env vars leak via process listings).

**TLS ca_file from a CA that signs everything in the org.** `client_ca_file` must be a CA dedicated to OTel ingest, not the org-wide root, or any service can mint a cert and inject spans.

**Audit logs going through redaction.** If you redact email addresses from operational logs, don't accidentally apply the same redaction to audit logs that need user identity. Separate pipelines, separate processors.

**OTLP HTTP receiver with no auth on a public endpoint.** A common mis-deployment: the gateway service is exposed via LoadBalancer for browser RUM, then someone forgets to add `auth: { authenticator: oidc }`. Result: anyone on the internet can ship spans into your trace UI. Always enforce auth on Internet-facing receivers.

**Cross-region exporter pointing at the wrong region.** A `googlecloud` exporter with a `project: syncobit-prod` (US-region project) receiving traces tagged for EU residency. The traces leave the EU. Verify regional alignment in deployment.

**Log line containing a stack trace with credentials.** A library throws an exception whose message includes the connection string. The stack trace is logged at ERROR; the connection string ships to your log backend. Treat stack traces as untrusted; redact connection-string-shaped patterns.

**Workload Identity / IRSA misconfigured — falls back to node service account.** If the K8s SA isn't bound to the Google/AWS SA, the Collector picks up the node's identity (which may have broader permissions than intended). Always verify `gcloud auth list` / `aws sts get-caller-identity` from inside the Collector pod.

## Quick checklist

```markdown
## Security & compliance review

- [ ] Receiver auth: mTLS or OIDC on every production OTLP endpoint
- [ ] No vendor API keys in browser code, edge code, or ConfigMaps
- [ ] Redaction at emission (allow-list) AND at Collector (regex deny-list)
- [ ] PII patterns (email, card, SSN, tokens, IP) blocked at Collector
- [ ] CMEK / KMS configured for vendor backends with regulated data
- [ ] VPC Service Controls (GCP) or PrivateLink (AWS) for telemetry traffic
- [ ] Workload Identity / IRSA used; no static service account JSON keys
- [ ] Region routing for data residency (EU vs US vs APAC)
- [ ] Audit logs in separate pipeline/bucket from operational logs
- [ ] Audit log retention ≥ regulatory minimum; bucket immutable
- [ ] Operational log retention bounded (30d default for prod)
- [ ] Hashed user.id and session.id (never raw PII in attributes)
- [ ] HTTP user-agent hashed (or dropped) — fingerprinting risk
- [ ] User deletion process documented and tested (purge by hash)
- [ ] Vendor BAA in place for HIPAA-eligible workloads
- [ ] Cross-region telemetry traffic blocked or explicitly routed
- [ ] Access reviews on trace UI / log explorer / metric dashboards (quarterly)
```

## Sources

- OpenTelemetry Collector security guide — opentelemetry.io/docs/collector/configuration/#security-best-practices
- redaction processor — github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/processor/redactionprocessor
- attributes processor — github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/processor/attributesprocessor
- oidcauth extension — github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/extension/oidcauthextension
- GCP CMEK for Cloud Logging — cloud.google.com/logging/docs/routing/managed-encryption
- AWS KMS for CloudWatch Logs — docs.aws.amazon.com/AmazonCloudWatch/latest/logs/encrypt-log-data-kms.html
- VPC Service Controls — cloud.google.com/vpc-service-controls
- AWS PrivateLink — docs.aws.amazon.com/vpc/latest/privatelink/
- SPIFFE / SPIRE — spiffe.io
- GDPR Article 17 (right to erasure) — gdpr-info.eu/art-17-gdpr/