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
```

## Sources

- OpenTelemetry logs specification — opentelemetry.io/docs/specs/otel/logs
- OTel severity number model — opentelemetry.io/docs/specs/otel/logs/data-model/#field-severitynumber
- Structured logging best practices — multiple sources; the consistent advice across all of them
