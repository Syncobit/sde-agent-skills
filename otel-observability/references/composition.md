# Composition with the API Skills

How OTel observability layers on top of `api-idempotency`, `api-error-responses`, and `api-conditional-requests`. The combination produces requests that are debuggable end-to-end, with full correlation between client retries, server state changes, and observed telemetry.

## The overall picture

The four skills cover orthogonal concerns:

| Skill | Concern | Mechanism |
|-------|---------|-----------|
| `api-idempotency` | Retry safety | `Idempotency-Key` header + dedupe store |
| `api-conditional-requests` | Concurrency control | `If-Match` + 412 |
| `api-error-responses` | Error contract | RFC 9457 Problem Details |
| `otel-observability` (this) | Visibility into all of the above | Spans, metrics, logs with semantic attributes |

The integration point is **span attributes**. Every mechanism in the other three skills should be observable as attributes on the relevant span. The result: a trace that tells the full story of a request — what was attempted, what state the server saw, what decision was made, what was returned to the client.

## Recommended attributes for each mechanism

### Idempotency

On any span where idempotency check happens (typically a middleware span or the entry span):

```yaml
idempotency.key: "9f86d081-..."                # the key from the header
idempotency.action: "executed" | "replayed" | "in_flight" | "key_mismatch" | "key_missing"
idempotency.fingerprint: "<sha256-prefix>"     # optional, helps debugging mismatches
idempotency.ttl_seconds: 86400                 # for context on expiry
```

The `idempotency.action` attribute is the most valuable — at a glance, you see whether the request was actually executed or replayed from cache.

### Conditional requests (ETag / If-Match)

On the span that performs the conditional check:

```yaml
http.request.header.if_match: "v17"            # what the client sent
http.response.header.etag: "v17" | "v18"        # the current ETag (after any update)
http.if_match.matched: true | false            # the outcome
http.if_match.action: "applied" | "rejected_412" | "rejected_428_missing"
http.if_none_match.action: "matched_304" | "not_matched_200"   # for cache validation
```

For optimistic concurrency, `http.if_match.matched: false` immediately tells you "this is a 412 conflict" without parsing the response.

### Problem Details (error responses)

On any span that produces a 4xx or 5xx response:

```yaml
http.response.status_code: 412
problem.type: "https://api.vqms.example.com/problems/version-conflict"
problem.title: "Resource was modified"
exception.type: "VersionConflictError"          # if an exception was the source
exception.message: "..."
```

The `problem.type` URI is the same one in the response body — clients and servers see consistent identifiers for the same error class.

## End-to-end worked example: VQMS ticket transition

A complete request flowing through all four skills, with the spans you'd see in the trace UI.

### The request

```http
PATCH /v1/queues/q-123/tickets/tk-42 HTTP/1.1
Host: api.vqms.example.com
Authorization: Bearer <token>
Idempotency-Key: 9f86d081-0a1c-4f8c-9e42-3a4b2e1c5d76
If-Match: "v17"
Content-Type: application/merge-patch+json
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01

{"state": "called", "called_by": "staff-A"}
```

The client sends `traceparent` (W3C trace context) — this trace already has an upstream parent (e.g., the staff terminal's mobile app trace). The server's spans become children of that.

### The trace tree

```
[server.call_next_ticket]                                       2.4ms
├── auth.verify_token                                           0.3ms
│   ├── auth.tenant_id: "syncobit-prod"
│   └── auth.user_id: "staff-A"
├── idempotency.check                                           0.4ms
│   ├── idempotency.key: "9f86d081-..."
│   ├── idempotency.action: "executed"
│   ├── idempotency.fingerprint: "a1b2..."
│   └── idempotency.storage: "redis://..."
├── ticket.fetch                                                0.6ms
│   ├── http.if_match: "v17"
│   ├── ticket.id: "tk-42"
│   └── ticket.current_etag: "v17"           ← matches, so write proceeds
├── ticket.transition                                           0.8ms
│   ├── ticket.from_state: "issued"
│   ├── ticket.to_state: "called"
│   ├── http.if_match.matched: true
│   ├── http.if_match.action: "applied"
│   └── ticket.new_etag: "v18"
├── audit.write_event                                           0.2ms
│   ├── audit.event_type: "TicketCalled"
│   └── audit.actor: "staff-A"
└── http.response                                               0.1ms
    ├── http.response.status_code: 200
    ├── http.response.header.etag: "v18"
    └── idempotency.cached: true              ← stored for replay
```

Every concern is visible. A debugging engineer can see: auth succeeded, idempotency key was new, ETag matched, state transitioned, audit recorded, response sent. The entire story in one trace.

### When it goes wrong: the 412 case

A second terminal tries the same operation a moment later with the same `If-Match: "v17"`:

```
[server.call_next_ticket]                                       1.2ms          [STATUS: ERROR]
├── auth.verify_token                                           0.2ms
├── idempotency.check                                           0.3ms
│   ├── idempotency.key: "47d4f5b9-..."        ← different key (different operation)
│   └── idempotency.action: "executed"
├── ticket.fetch                                                0.5ms
│   ├── http.if_match: "v17"
│   ├── ticket.current_etag: "v18"             ← mismatch
│   ├── http.if_match.matched: false
│   └── http.if_match.action: "rejected_412"
└── http.response                                               0.1ms
    ├── http.response.status_code: 412
    ├── problem.type: "https://api.vqms.example.com/problems/version-conflict"
    ├── problem.title: "Ticket was modified by another terminal"
    └── exception.message: "expected v17, current v18"
```

The error span carries everything needed to understand the failure without checking logs.

### When it goes more wrong: idempotency replay of a previous error

A network timeout caused the client to retry with the same `Idempotency-Key`. The server's second handling:

```
[server.call_next_ticket]                                       0.3ms
├── auth.verify_token                                           0.2ms
└── idempotency.check                                           0.1ms
    ├── idempotency.key: "47d4f5b9-..."
    ├── idempotency.action: "replayed"          ← short-circuited
    ├── idempotency.original_status: 412
    └── idempotency.original_response_age_ms: 1200
[idempotency-replayed]                                          (no further spans)
```

The trace is short — most of the work is skipped. The `idempotency.action: "replayed"` plus `original_status: 412` tells the engineer "this was a retry of an already-failed operation, no actual work happened".

## Implementation tips

### Set attributes at the right span

The most natural span for each attribute:

- **Auth attributes** → on the auth-verification child span (or the root span if auth is inline)
- **Idempotency attributes** → on a dedicated `idempotency.check` span (or the root span's attributes if no separate span)
- **ETag/conditional attributes** → on the resource-load span and the write span
- **Problem Details attributes** → on the response span and on the root span (so the root carries the final error context)

The pattern is: child spans capture the local outcome, root span captures the request's overall outcome.

### Use the "root span enrichment" pattern

The root server span should carry the most-important business attributes for filtering and aggregation:

```python
def handler(request):
    span = trace.get_current_span()
    span.set_attributes({
        "tenant.id": get_tenant_id(),
        "user.role": get_user_role(),
        # Set early; if the request errors later, these are still on the span
    })

    try:
        result = process(request)
        span.set_attribute("idempotency.action", "executed")
        return result
    except VersionConflict as e:
        span.set_attribute("http.if_match.matched", False)
        span.set_attribute("problem.type", e.problem_type)
        span.set_status(Status(StatusCode.ERROR, e.message))
        raise
```

This makes "show me all 412s on this tenant in the last hour" a simple attribute filter rather than a log scan.

### Logs that complement, not duplicate

With trace correlation in place, log content should be detail that doesn't fit on a span:

- Span attributes: high-cardinality stable values that filter or aggregate well (`tenant.id`, `idempotency.action`, status codes)
- Logs: rich context, debugging detail, error stack traces, parameters too large for an attribute

```python
# Good — span carries the structured outcome, log carries the detail
span.set_attribute("idempotency.action", "key_mismatch")
log.warn("idempotency-key-mismatch",
         extra={"key": idem_key, "expected_fingerprint": expected, "got": got})

# Bad — duplicating the same info in two places
span.set_attribute("idempotency.action", "key_mismatch")
span.set_attribute("idempotency.expected_fingerprint", expected)
log.warn("Idempotency key mismatch", expected=expected, got=got)
```

The duplication wastes attribute budget (some backends limit attribute count per span) without adding insight.

### Collector-side enrichment for cross-cutting attributes

If every service should tag spans with `deployment.environment.name`, `service.namespace`, etc., set these once at the Collector level via a `transform` processor — don't duplicate in every SDK config:

```yaml
processors:
  transform/enrich:
    trace_statements:
      - context: span
        statements:
          - set(attributes["deployment.environment.name"], "production") where attributes["deployment.environment.name"] == nil
          - set(attributes["service.namespace"], "vqms") where IsMatch(attributes["service.name"], "^vqms-")
```

## Putting it together: SLOs that span all four skills

With instrumentation in place, you can define service-level objectives that span all four concerns:

- **Idempotency replay rate**: `count(idempotency.action="replayed") / count(*)` — high replay rate means clients are retrying a lot, which often signals upstream instability.
- **412 rate per minute**: `count(http.if_match.matched=false)` — high 412 rate signals contention; might want to surface conflict resolution UX or rethink the resource model.
- **428 missing-precondition rate**: `count(http.if_match.action="rejected_428_missing")` — clients still missing the `If-Match` header. Drive to zero by working with API consumers.
- **Idempotency-Key collision rate (different fingerprint)**: `count(idempotency.action="key_mismatch")` — should be near zero; non-zero means client SDK bugs.
- **End-to-end latency by tenant**: `p99(duration) by (tenant.id)` — instantly answers "is this slow for everyone or just one customer".

Each of these is a one-line query in any modern observability tool, but only because the attributes are present and consistent.

## Sources

- OpenTelemetry trace semantic conventions — opentelemetry.io/docs/specs/semconv/general/trace
- W3C Trace Context — w3.org/TR/trace-context
- The other three skills in this family
