# Log Levels — Choosing Severity Correctly

The most common observability mistake isn't missing instrumentation — it's misusing the instrumentation that's already there. Log levels chosen wrong produce three real problems: alert fatigue (engineers ignore real errors), cost inflation (paying to ship and store routine traffic as warnings), and incidents that take longer to debug because the relevant error is buried in 10,000 lines of INFO chatter.

This reference is the workflow for choosing levels deliberately and consistently.

## OpenTelemetry's severity model

The OTel logs spec defines 24 numeric severity levels grouped into 6 named tiers:

| Tier | Numeric range | Meaning |
|------|---------------|---------|
| TRACE | 1-4 (`TRACE`, `TRACE2`, `TRACE3`, `TRACE4`) | Most verbose. Step-by-step execution detail. |
| DEBUG | 5-8 (`DEBUG`, `DEBUG2`, `DEBUG3`, `DEBUG4`) | Diagnostic info useful when investigating known issues. |
| INFO | 9-12 (`INFO`, `INFO2`, `INFO3`, `INFO4`) | Significant lifecycle events. Not "every request handled". |
| WARN | 13-16 (`WARN`, `WARN2`, `WARN3`, `WARN4`) | Concerning but not failing. Recoverable issues. |
| ERROR | 17-20 (`ERROR`, `ERROR2`, `ERROR3`, `ERROR4`) | The current operation could not complete. System continues running. |
| FATAL | 21-24 (`FATAL`, `FATAL2`, `FATAL3`, `FATAL4`) | Unrecoverable state. Process must exit. Rare. |

Most code uses just the base names (DEBUG, INFO, WARN, ERROR, FATAL). The numbered sub-levels exist for fine-grained filtering when needed; default to the base names.

The bridge layer (Python `logging`, Node Pino, Java SLF4J) maps the language's native levels to OTel severity numbers automatically — you don't usually call OTel's severity API directly. But the tier semantics are the same regardless of which API you use.

## The decision rule

A reliable heuristic for choosing the level: **ask what action a human operator should take when they see this log entry.**

| Operator action | Level |
|----------------|-------|
| Page someone or investigate now | ERROR |
| Crash the process; restart needed | FATAL |
| Look at this if you have time, or during a regular review | WARN |
| Useful for context; don't act on it | INFO |
| Only relevant when actively debugging a specific issue | DEBUG |
| Detailed execution flow for deep debugging | TRACE |

If the answer to "what should the operator do?" is "nothing, this is just normal behavior", the entry shouldn't exist as a log at all — it should be a span attribute, a metric, or nothing.

## What each level is and is not

### ERROR

**Is**: an operation the system *attempted to complete* failed for an unexpected reason. A database write failed because the database is unreachable. A downstream API returned 503. An assertion violated.

**Is not**: an expected business outcome. A user enters a wrong password — that's not an error in the operational sense; the system worked correctly. A validation rule rejects bad input — also not an error. A 404 for a resource that doesn't exist — not an error.

The discipline: ERROR means "something we expected to work didn't". Expected rejections are INFO at most, more often nothing (the span captures the outcome).

### WARN

**Is**: a recoverable problem that succeeded eventually, or a condition that may become a problem. A retry succeeded after one failure. A deprecated API is being used. A circuit breaker tripped briefly. Cache hit ratio fell below threshold. A request came in at the soft rate limit.

**Is not**: every minor anomaly. WARN should be rare enough that an engineer can scan a day's WARN logs and find them all interesting. If WARN is firing 1000 times an hour in normal operation, the level is wrong.

### INFO

**Is**: significant lifecycle events. Service started. Configuration loaded. A scheduled job completed. A deployment finished migration. A new tenant onboarded.

**Is not**: every request handled, every database query, every cache lookup. These are what spans and metrics are for. INFO logs should be rare enough that they tell a coherent story when read sequentially.

If you're reaching for "log every request", that's a sign you should reach for a span instead — `tracer.start_span("handle_request")` plus relevant attributes captures the same information in a queryable way without log volume cost.

### DEBUG

**Is**: detailed information that's useful when actively investigating a problem. Variable values at decision points. Why a particular code path was taken. Counts and timings inside a hot path.

**Is not**: enabled in production by default. DEBUG is volume-heavy and often contains sensitive data. Production runs at INFO or WARN; DEBUG is enabled temporarily for an active investigation, scoped to a specific service.

### TRACE

**Is**: step-by-step execution detail. Every line of a complex algorithm. Every iteration of a loop.

**Is not**: in scope for almost any production service. If you find yourself reaching for TRACE, you usually want a span instead — distributed tracing is purpose-built for this kind of detail and queries better.

### FATAL

**Is**: an unrecoverable condition where the only valid response is to exit. A required configuration is missing at startup. A required dependency is permanently unreachable. Memory corruption detected.

**Is not**: any failure of a request or operation. Those are ERRORs. FATAL is for "the process should not continue running".

In practice, FATAL is rare in modern services because most "fatal" conditions at startup are caught by the platform (Cloud Run/K8s health checks fail the deployment). FATAL exists for edge cases like detected data corruption or required-but-missing capability.

## Span status vs log severity

These are independent. A span has a status (`OK`, `ERROR`, `UNSET`); logs have severity. A span can have ERROR status while emitting only INFO logs (the error is captured *in the span itself*). A span can have OK status while emitting WARN logs (recoverable issues happened during the span).

The right way to think about it:

- **Span status** answers: "Did this operation succeed overall?"
- **Log severity** answers: "Is this entry noteworthy enough that an operator should react?"

A 412 Precondition Failed response, for example: the span has ERROR status (the write didn't apply) but no log emission is needed at all — the trace UI shows the failure with full context. A WARN log on top would be redundant noise.

Conversely, a recoverable retry storm: spans complete with OK status (retries succeeded), but a WARN log is appropriate to surface the underlying instability for human review.

## Logs vs spans vs metrics — when to reach for each

The hardest discipline. The right tool depends on what you'll do with the data:

**Use a span attribute when**: the data is part of the per-request story. Tenant ID, idempotency action, ETag matched/not — these belong on the span, not in a log line.

**Use a metric when**: you want to aggregate across many events. Request rate, error rate, latency percentiles, queue depth. Metrics are cheap to query at scale; logs are not.

**Use a log when**: you need rich detail that won't fit on a span (full stack trace, large parameter values), OR the event has independent significance that doesn't sit naturally on a request span (background job completion, scheduled task results), OR you need standalone retrieval (search by some field that isn't in any trace).

A practical heuristic: if you're about to emit `log.info("handled request X with result Y")` inside a request handler, stop and ask whether `span.set_attribute("result", Y)` does the same job. Almost always yes.

## Structured logging — the message vs attribute split

All production logs should be **structured** (JSON or key-value pairs), never f-string templated. Templated messages defeat the entire purpose of structured logging — you can't filter, aggregate, or correlate on the variable parts.

The split:

- **Message field**: a stable, human-readable string describing the event class. "ticket called", "config reloaded", "deduped duplicate event".
- **Attributes**: the variable parts that describe the specific instance. `ticket.id`, `staff.id`, `dedupe.original_event_id`.

```python
# Bad — message contains variable data, breaks aggregation
log.info(f"Ticket {ticket.id} called by staff member {staff.id}")

# Good — stable message, structured attributes
log.info("ticket called", extra={"ticket.id": ticket.id, "staff.id": staff.id})
```

The aggregation difference: with the structured form, `count() by (message)` gives you "ticket called: 1234, config reloaded: 7" cleanly. With the templated form, every ticket ID becomes a separate "message" — useless.

The same rule that applies to span attributes applies here: **namespace your attributes** (`vqms.ticket.id` not `id`), **avoid PII**, and **stay within the OTel semantic conventions** for HTTP/DB/RPC fields where they apply.

## The "log once" rule

A single failure should produce one log entry, not three. The common bad pattern:

```python
# Repository layer
def fetch_ticket(id):
    try:
        return db.query(...)
    except DatabaseError as e:
        log.error("database error fetching ticket", error=str(e))   # 1
        raise

# Service layer
def get_ticket(id):
    try:
        return repo.fetch_ticket(id)
    except DatabaseError as e:
        log.error("failed to get ticket", ticket_id=id, error=str(e))   # 2
        raise

# Handler
def handler(req):
    try:
        return service.get_ticket(req.ticket_id)
    except Exception as e:
        log.error("request failed", error=str(e))   # 3
        return error_response(500)
```

One database failure produces three log entries, each with a partial view of the context. The original error gets harder to find, not easier, because it's surrounded by re-logged copies.

The discipline: **log at the layer that has the most context**, raise from inner layers without logging. Most often that's the request handler or a top-level error middleware — the one place that knows about the request, the tenant, the trace ID, and the failure together.

```python
# Repository
def fetch_ticket(id):
    return db.query(...)   # let DatabaseError propagate

# Service
def get_ticket(id):
    return repo.fetch_ticket(id)   # let propagate

# Handler / middleware (top-level)
def error_middleware(req, next):
    try:
        return next(req)
    except DatabaseError as e:
        log.error("database error",
                  exc_info=e,
                  extra={"tenant.id": req.tenant_id,
                         "operation": req.operation,
                         "trace_id": current_trace_id()})
        return error_response(503, problem_type="database-unavailable")
```

One log entry, full context, traceable.

## Production posture

For Syncobit's services, the recommended baseline:

| Environment | Default level | Rationale |
|-------------|---------------|-----------|
| Local dev | DEBUG | Engineers want detail while iterating |
| Staging | INFO | Realistic production-like volume; catches issues |
| Production | INFO | Default; WARN+ for very high-volume services |
| Production (high-volume) | WARN | Some services emit so much routine INFO that it's not affordable |

**Per-service override**: a particular service can run at a different level than the default. For an active investigation, ratchet that one service to DEBUG temporarily — but always with a time limit (an hour or two), and revert to INFO when done. Leaving DEBUG enabled in production is how PII ends up in log archives.

**Per-logger override**: most logging frameworks let you set different levels for different loggers (`com.example.security` vs `com.example.metrics`). Use this to keep noisy subsystems quiet without hiding everything else. A common pattern: noisy library loggers (HTTP clients, ORM SQL logging) at WARN even when application code is at INFO.

## Enterprise operational layer

The severity decisions above govern individual log statements. The operational layer governs how log streams flow through the organization — channels, retention, access, runtime control. This is what separates "we use OTel logs" from "our logging meets SOC 2 / HIPAA / financial-services controls".

### Audit logs are not operational logs

The single most common enterprise mistake: shipping security-relevant events through the same pipeline as routine INFO traffic. They have different requirements:

| Concern | Operational logs | Audit logs |
|---------|-----------------|------------|
| Captures | Service lifecycle, errors, debug info | Authentication, authorization, data access, configuration changes, privileged actions |
| Retention | 7-30 days hot, 30-90 days cold | 1-7 years (regulatory; 3y baseline for SOC 2, 6y for HIPAA, 7y for SOX/PCI cardholder) |
| Access | Engineering team broadly | Security/compliance team only; engineering on need-to-know |
| Mutability | Sampling, redaction, downsampling allowed | Immutable; write-once storage |
| PII | Aggressively redacted | Often must include user identity (the whole point) |
| Volume | High | Low to moderate |
| Schema | Structured, free-form | Strict (actor, action, resource, outcome, timestamp, request_id) |
| Destination | Cloud Logging, CloudWatch, vendor APM | Dedicated audit log bucket, CloudTrail, SIEM |

Separate the channels at emission. Two named loggers with different handlers:

```python
import logging

# Operational — bridged to OTel logs, flows through gateway redaction
operational = logging.getLogger("vqms")
operational.info("ticket called", extra={"ticket.id": ticket_id})

# Audit — direct to dedicated handler; never touches OTel redaction processor
audit = logging.getLogger("audit")
audit.handlers = [AuditHandler(target="audit-bucket")]   # separate stream
audit.propagate = False                                    # do NOT bubble to root logger
audit.info("permission_grant", extra={
    "audit.actor.id": current_user.id,
    "audit.action": "grant_role",
    "audit.target.id": target_user.id,
    "audit.role": "queue_admin",
    "audit.outcome": "success",
    "audit.request_id": request.id,
})
```

The `propagate = False` is critical — without it, the audit log also goes through the operational handler, gets redacted, and you lose the user identity that makes audit logs useful in the first place.

GCP-specific: enable **Cloud Audit Logs** for Admin Activity, Data Access, and System Event categories. These are managed by Google, immutable, and cover IAM and infrastructure events without application code. AWS equivalent: **CloudTrail**. Application-level audit events (business operations: "user X granted role Y") are still your responsibility.

### Retention tiers and lifecycle

Enterprise retention follows a hot/warm/cold/archive ladder. Keep the model explicit per log channel:

| Tier | Latency to query | Cost | Typical use | Retention |
|------|-----------------|------|------------|-----------|
| Hot | seconds | High | Active incidents, real-time dashboards | 7-30 days |
| Warm | seconds-minutes | Medium | Recent post-mortems, weekly reviews | 30-90 days |
| Cold | minutes-hours | Low | Trend analysis, long-tail investigation | 90-365 days |
| Archive | hours-days (rehydrate) | Very low | Compliance, legal hold | 1-7 years |

GCP implementation:
```bash
# Cloud Logging bucket with explicit retention
gcloud logging buckets update _Default \
    --location=us-central1 \
    --retention-days=30                                    # hot tier

# Sink older logs to BigQuery (warm/cold)
gcloud logging sinks create cold-archive \
    bigquery.googleapis.com/projects/PROJECT/datasets/logs_archive \
    --log-filter='severity >= "INFO"'

# BigQuery dataset with table expiration for archive
bq update --time_partitioning_expiration 31536000 PROJECT:logs_archive   # 1 year
```

AWS implementation:
```bash
# CloudWatch Logs retention per group
aws logs put-retention-policy --log-group-name /aws/ecs/vqms-api --retention-in-days 30

# S3 lifecycle for archive — export from CWL via subscription filter or vended logs
aws s3api put-bucket-lifecycle-configuration --bucket vqms-logs-archive \
    --lifecycle-configuration file://lifecycle.json
```

`lifecycle.json` rolls objects from S3 Standard → S3 Standard-IA at 30 days → Glacier at 90 days → Glacier Deep Archive at 365 days, with deletion at 7 years. Log retention is one of the largest cloud bills if not managed.

### SIEM integration

Security Information and Event Management tools (Splunk, Microsoft Sentinel, Google Chronicle, Datadog Cloud SIEM, Elastic Security) consume security-relevant log streams for detection and compliance. The pipeline:

```
[ App audit log ] ──→ [ Audit-only Collector pipeline ] ──→ [ SIEM ]
                              │
                              └─→ [ Immutable archive bucket ]
```

Most SIEMs accept syslog, JSON over HTTPS, or vendor-specific formats. The Collector handles transformation:

```yaml
# Audit-only Collector pipeline
receivers:
  filelog/audit:
    include: [/var/log/vqms/audit/*.log]
    operators:
      - type: json_parser

processors:
  memory_limiter: { ... }
  transform/sentinel_format:
    log_statements:
      - context: log
        statements:
          - set(attributes["TimeGenerated"], time)
          - set(attributes["EventType"], attributes["audit.action"])

exporters:
  otlphttp/sentinel:
    endpoint: "https://${env:SENTINEL_WORKSPACE}.ods.opinsights.azure.com/api/logs"
    headers:
      authorization: "SharedKey ${env:SENTINEL_KEY}"
  awss3/audit_archive:
    s3uploader:
      region: us-east-1
      s3_bucket: vqms-audit-archive
      s3_prefix: audit/
      compression: gzip

service:
  pipelines:
    logs/audit:
      receivers: [filelog/audit]
      processors: [memory_limiter, transform/sentinel_format]
      exporters: [otlphttp/sentinel, awss3/audit_archive]      # SIEM + immutable archive
```

The `awss3/audit_archive` writes raw audit logs to immutable S3 storage with Object Lock. Even if the SIEM is compromised or misconfigured, the archive is the source of truth.

### Always-on context fields (MDC baseline)

Every log line — operational and audit — should carry the same set of contextual fields, set by middleware at request entry. Without this baseline, cross-service queries are impossible.

Required on every log:

```yaml
trace_id: "4bf92f3577b34da6a3ce929d0e0e4736"   # from OTel span context
span_id: "00f067aa0ba902b7"                     # from OTel span context
service.name: "vqms-api"                        # from resource
service.version: "1.4.7"                        # from resource
deployment.environment.name: "production"       # from resource
cloud.region: "us-central1"                     # from resource detector

# Request-scoped (set by middleware)
request.id: "req-7c4d-x8j2"                     # synthetic if not provided by upstream
tenant.id: "syncobit-prod"                      # multi-tenant SaaS
user.id: "<hashed>"                             # if authenticated; never raw email
correlation.id: "corr-9f86d081"                 # for non-OTel cross-system tracing
```

The `correlation.id` is essential where trace context is broken — webhook retries from external systems, message queue handlers that deserialize without context propagation, scheduled jobs. It's the fallback "join key" when `trace_id` isn't reliable.

Implementation pattern (Python; equivalent in Node MDC, Go context, Java MDC):

```python
from contextvars import ContextVar
import logging

_request_context: ContextVar[dict] = ContextVar("request_context", default={})

class ContextFilter(logging.Filter):
    def filter(self, record):
        ctx = _request_context.get()
        for k, v in ctx.items():
            setattr(record, k, v)
        return True

# Middleware — set once per request
def context_middleware(request, handler):
    _request_context.set({
        "request.id": request.id,
        "tenant.id": request.tenant_id,
        "user.id": hash_user_id(request.user_id) if request.user_id else None,
        "correlation.id": request.headers.get("x-correlation-id"),
    })
    return handler(request)

logging.getLogger().addFilter(ContextFilter())
```

Now every log emitted during the request automatically carries the context. Engineers don't have to remember to add it on every call.

### Runtime log-level control

"Increase the log level for service X to DEBUG for the next hour" is a common debugging request. Without a mechanism, this requires a deploy, which is slow and disruptive. Three viable mechanisms:

1. **Centralized config service** — LaunchDarkly, Unleash, AWS AppConfig, GCP Runtime Config. Application polls every 30-60s for the current level.
2. **Feature flag with TTL** — set "service-X-log-level=DEBUG" with auto-expiry at 1 hour. Removes the "we forgot to revert" risk.
3. **Admin endpoint** — `POST /admin/log-level {level: "DEBUG", duration_seconds: 3600}` on the service itself, behind strict auth.

Pattern (Python with LaunchDarkly):

```python
import logging
import ldclient
from ldclient.config import Config

ldclient.set_config(Config(sdk_key="LD_KEY"))
client = ldclient.get()

def update_log_level():
    level_name = client.variation("log-level-vqms-api", user, "INFO")
    level = getattr(logging, level_name)
    logging.getLogger().setLevel(level)

# Schedule update_log_level() to run every 60s
```

Rules:
- Always TTL the override (1 hour default; 4 hours max). DEBUG indefinitely is how PII ends up in archives.
- Audit log every override (who, what service, what level, duration) to the audit channel.
- Override is per-service or per-logger, never global.
- Production override authority is restricted (security/SRE team), not engineering at large.

### Log-based metrics

Some signals naturally exist as logs but are queried as metrics: "rate of 5xx responses", "count of permission denials", "p99 of payment processing time per tenant". You can either:

1. **Emit a metric in code** — explicit OTel counter/histogram. Cheaper to query, requires forethought.
2. **Derive a metric from logs** — Cloud Logging log-based metrics, CloudWatch metric filters, Splunk indexed extractions. Slower, more flexible, retroactive.

Use log-based metrics for *exploratory* and *unforeseen* aggregations; use code-emitted metrics for the dashboard you check daily.

GCP example — log-based counter for permission denials:
```bash
gcloud logging metrics create permission_denials \
    --description="Count of authorization failures" \
    --log-filter='resource.type="cloud_run_revision"
                  jsonPayload.audit.action="authz_denied"
                  severity="WARNING"' \
    --label-extractors='tenant_id=EXTRACT(jsonPayload.tenant.id),
                        action=EXTRACT(jsonPayload.audit.action_attempted)'
```

AWS example — CloudWatch metric filter:
```bash
aws logs put-metric-filter \
    --log-group-name /aws/ecs/vqms-api \
    --filter-name PermissionDenials \
    --filter-pattern '{ $.audit.action = "authz_denied" }' \
    --metric-transformations \
        metricName=PermissionDenials,metricNamespace=Vqms,metricValue=1
```

Cost note: log-based metrics scan the log stream — they are billed per GB scanned on AWS. For high-volume logs, a code-emitted metric is often cheaper.

### Volume budgets and chargeback

At enterprise scale, log volume becomes a financial signal. A team that emits 10x the logs of its peers is either fighting hard incidents (interesting) or has a bug (worth flagging). Budgeting per service/team:

| Mechanism | How |
|-----------|-----|
| Per-service budget | Tag every log with `team` resource attribute; aggregate cost in vendor bill or via volume metric |
| Soft alert | Alert team owner when their service exceeds 2x the median for the namespace |
| Hard cap | Drop INFO logs from services above N MB/hour at the Collector |
| Chargeback | Cost allocation by `team` tag fed to FinOps |

Collector-side rate limiting:

```yaml
processors:
  ratelimit/per_service:
    # Hypothetical processor; in practice use sampling + alerting in vendor backend
    rate_limit_key: service.name
    limit_per_second: 1000
```

In practice, most teams enforce budgets via vendor billing alerts and team-level dashboards rather than hard caps — hard caps drop data right when it's most useful (during an incident).

The structural fix is upstream: review log emission rates in code review. A new `log.info` inside a hot loop is a budget event.

## PII at each level

The PII risk increases sharply as level decreases:

- **WARN/ERROR**: rare events. Auditing them for PII is feasible. Usually small structured payloads.
- **INFO**: medium volume. Still feasible to audit attributes. Most leakage prevention happens here.
- **DEBUG**: high volume. Often contains raw request/response bodies, parameter values, stack frames with locals. **PII risk is highest here.**
- **TRACE**: rarely shipped to backends, but if it is, assume everything is exposed.

The defensive posture: **redact at log emission time, not at the backend**. Once raw PII enters the log pipeline, it's hard to be sure all copies are scrubbed (collector buffers, retry queues, archive storage).

```python
# Bad — raw object can contain anything
log.debug("processing request", extra={"request": request.to_dict()})

# Good — explicit allow-list of safe fields
log.debug("processing request", extra={
    "request.method": request.method,
    "request.path": request.path,
    "request.size_bytes": len(request.body),
    "tenant.id": request.tenant_id,
})
```

For DEBUG-only paths in production, treat them as if they'll leak. Don't log secrets, tokens, raw PII, or full request/response bodies, even at DEBUG. If an engineer needs to see those during an active investigation, capture them in a debug-only path that requires explicit opt-in.

## Mapping to language-specific log levels

Most language logging libraries have their own level names. The OTel logging bridge maps them automatically, but it helps to know the mapping:

| OTel | Python `logging` | Node Pino | Java SLF4J | Go `slog` |
|------|------------------|-----------|------------|-----------|
| TRACE | `logging.DEBUG - 5` (custom) | `trace` | `TRACE` | (none — use DEBUG) |
| DEBUG | `logging.DEBUG` | `debug` | `DEBUG` | `slog.LevelDebug` |
| INFO | `logging.INFO` | `info` | `INFO` | `slog.LevelInfo` |
| WARN | `logging.WARNING` | `warn` | `WARN` | `slog.LevelWarn` |
| ERROR | `logging.ERROR` | `error` | `ERROR` | `slog.LevelError` |
| FATAL | `logging.CRITICAL` | `fatal` | `ERROR` (no FATAL) | (custom) |

The Python `logging` module has only 5 levels by default; FATAL maps to CRITICAL. Java SLF4J has no FATAL — use ERROR with explicit context. Go's `slog` is the cleanest mapping.

Use the language's idiomatic level names in your code. The OTel bridge translates to the standard severity numbers when shipping.

## Anti-patterns

### 1. Logging every request at INFO

`log.info("handled request to /v1/tickets")` on every API call. Volume is dominated by routine traffic; real signals drown.

**Fix**: spans handle "this happened" semantics. INFO is for events; spans are for operations.

### 2. ERROR for expected business outcomes

Logging at ERROR when a user enters invalid input, a 404 lookup fails, or a validation rejects a request. These are not errors; they're the system working correctly.

**Fix**: WARN at most for unusual-but-expected outcomes; INFO if it's worth noting; nothing if the span captures it.

### 3. Logging exceptions at multiple layers

Catching an exception, logging it, re-raising, catching again at the next layer, logging again. One failure produces three log entries.

**Fix**: log once at the top layer that has full context. Inner layers should let exceptions propagate.

### 4. Templating variable data into the message field

`log.info(f"User {user_id} did {action}")`. The variable parts become unfilterable; aggregation is broken.

**Fix**: stable message + structured attributes.

### 5. No level discipline across services

Service A logs request handling at INFO; service B logs the same at DEBUG; service C uses WARN. Cross-service queries are impossible.

**Fix**: document a service-wide convention. The conventions in this reference are a starting point.

### 6. DEBUG enabled in production indefinitely

Engineer enables DEBUG to investigate an issue, forgets to revert. Logs balloon, PII flows to backend, costs spike.

**Fix**: time-bounded debug enablement (deployment ttl, feature flag with auto-expiry). Audit production log levels weekly.

### 7. FATAL used for non-fatal errors

`log.fatal("could not connect to database, retrying...")`. The process didn't exit; it shouldn't have logged FATAL.

**Fix**: ERROR for failed operations, FATAL only when the process is about to exit.

## Quick checklist

```markdown
## Log level review

- [ ] No INFO logs for routine per-request handling (use spans instead)
- [ ] No ERROR logs for expected business outcomes (validation rejections, 404s, etc.)
- [ ] No exceptions logged at multiple layers (log once, at the top with full context)
- [ ] Messages are stable strings; variable parts are in structured attributes
- [ ] DEBUG paths don't log secrets, tokens, raw PII, or full request bodies
- [ ] Production default level documented and consistent across services
- [ ] Per-environment level overrides documented
- [ ] FATAL only used when process is exiting
- [ ] WARN volume is low enough to be reviewable (rule of thumb: <100/hour per service)
- [ ] Audit logs separated from operational logs (different logger, handler, destination)
- [ ] Audit log retention meets regulatory minimum (3y SOC 2, 6y HIPAA, 7y PCI/SOX)
- [ ] Operational log retention bounded (30d hot default for production)
- [ ] Tier lifecycle configured (hot → warm → cold → archive)
- [ ] SIEM pipeline for audit events with immutable archive
- [ ] MDC baseline (trace_id, span_id, tenant.id, request.id, correlation.id) on every log
- [ ] Runtime log-level override mechanism (with TTL and audit trail)
- [ ] Log-based metrics chosen over code metrics only for exploratory queries
- [ ] Per-team / per-service volume tagged for chargeback or budget alerts
```

## Sources

- OpenTelemetry logs specification — opentelemetry.io/docs/specs/otel/logs
- OTel severity number model — opentelemetry.io/docs/specs/otel/logs/data-model/#field-severitynumber
- Structured logging best practices — multiple sources; the consistent advice across all of them
