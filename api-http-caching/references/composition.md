# Composition with Idempotency, Error Responses, and Observability

How HTTP caching layers in with the other three skills (`api-idempotency`, `api-error-responses`, `otel-observability`) to produce coherent end-to-end designs.

## Each skill solves a distinct concern

| Skill | Solves | Mechanism |
|-------|--------|-----------|
| `api-idempotency` | Retry safety: "I retried because the network timed out — don't double-execute" | `Idempotency-Key` header + dedupe store |
| `api-http-caching` (this one) | Concurrency, revalidation, edge caching | ETag, Cache-Control, conditional headers, surrogate keys |
| `api-error-responses` | Error contract: machine-actionable failure responses | RFC 9457 Problem Details |
| `otel-observability` | Visibility into all of the above | Spans, metrics, logs with semantic attributes |

These compose orthogonally. The mechanisms operate independently but produce a coherent design when used together.

## When to use which combinations

Different endpoints need different combinations. A small decision matrix:

| Endpoint type | Idempotency-Key | If-Match | If-None-Match | CDN cache | Problem Details |
|---------------|-----------------|----------|---------------|-----------|-----------------|
| Public GET (catalog) | No | No | Yes (revalidation) | **Yes (edge)** | On errors |
| Private GET (user data) | No | No | **Yes (revalidation)** | No (`private`) | On errors |
| Create with server-assigned ID (POST) | **Yes** | No | No | No | Yes |
| Create with client ID (PUT, idempotent) | Optional | No | **`*` for create-if-absent** | No | Yes |
| Update on single-writer resource | Optional | Optional | No | No | Yes |
| Update on multi-writer resource | **Yes** | **Yes** | No | No | Yes |
| State-machine transition | **Yes** | **Yes** | No | No | Yes |

The "multi-writer resource" rule is the one to remember: if multiple clients can independently modify the same resource — and your API is the source of truth — the full stack of `Idempotency-Key` + `If-Match` + Problem Details + observability all earn their place.

## Worked example: VQMS ticket state transition

A single endpoint exercising all four concerns:

```
PATCH /v1/queues/{queue_id}/tickets/{ticket_id}
```

Required headers:
- `Authorization: Bearer <token>` — auth
- `Idempotency-Key: <uuid>` — retry safety (api-idempotency)
- `If-Match: <etag>` — concurrency control (api-http-caching, concurrency path)
- `Content-Type: application/merge-patch+json` — body format
- `traceparent: <w3c-trace-context>` — distributed tracing (otel-observability)

### Success path

```http
# Step 1: Terminal A reads the ticket
GET /v1/queues/q-123/tickets/tk-42 HTTP/1.1
Authorization: Bearer <token>

HTTP/1.1 200 OK
ETag: "v3"
Cache-Control: private, no-cache
Content-Type: application/json

{
  "id": "tk-42",
  "queue_id": "q-123",
  "state": "issued",
  "queue_position": 7
}

# Step 2: Terminal A transitions to "called"
PATCH /v1/queues/q-123/tickets/tk-42 HTTP/1.1
Authorization: Bearer <token>
Idempotency-Key: 9f86d081-0a1c-4f8c-9e42-3a4b2e1c5d76
If-Match: "v3"
Content-Type: application/merge-patch+json
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01

{"state": "called", "called_by": "staff-A"}

HTTP/1.1 200 OK
ETag: "v4"
Idempotency-Key: 9f86d081-0a1c-4f8c-9e42-3a4b2e1c5d76
Content-Type: application/json

{
  "id": "tk-42",
  "state": "called",
  "called_by": "staff-A",
  "called_at": "2026-05-03T14:00:00Z"
}
```

### Failure modes — each header type produces a distinct error

**Network retry, no concurrent change (Idempotency-Key replay):**

The same request retried after a network timeout returns the original response, marked as a replay:

```http
HTTP/1.1 200 OK
ETag: "v4"
Idempotent-Replayed: true
Idempotency-Key: 9f86d081-...
{...same body as before...}
```

**Concurrent write (different actor, If-Match mismatch):**

```http
PATCH /v1/queues/q-123/tickets/tk-42 HTTP/1.1
Idempotency-Key: 47d4f5b9-...   # different key (different operation)
If-Match: "v3"
{"state": "called", "called_by": "staff-B"}

HTTP/1.1 412 Precondition Failed
ETag: "v4"
Content-Type: application/problem+json

{
  "type": "https://api.vqms.example.com/problems/version-conflict",
  "title": "Ticket was modified by another terminal",
  "status": 412,
  "current_etag": "v4",
  "your_etag": "v3"
}
```

**Missing If-Match (428):**

```http
PATCH /v1/queues/q-123/tickets/tk-42 HTTP/1.1
Idempotency-Key: 47d4f5b9-...
{"state": "called"}

HTTP/1.1 428 Precondition Required
Content-Type: application/problem+json

{
  "type": "https://api.vqms.example.com/problems/precondition-required",
  "title": "If-Match header is required",
  "status": 428
}
```

**Missing Idempotency-Key (400):**

```http
PATCH /v1/queues/q-123/tickets/tk-42 HTTP/1.1
If-Match: "v3"
{"state": "called"}

HTTP/1.1 400 Bad Request
Content-Type: application/problem+json

{
  "type": "https://api.vqms.example.com/problems/idempotency-key-missing",
  "title": "Idempotency-Key header is required",
  "status": 400
}
```

### Server-side handler order

The order of validation matters. Recommended sequence:

1. **Auth** (return 401/403 first)
2. **Resource existence** (return 404 if the ticket doesn't exist; precondition checks are meaningless on a missing resource)
3. **Idempotency-Key check** (return 400 if missing; 422 if mismatched fingerprint; replay if seen-and-completed)
4. **If-Match check** (return 428 if missing; 412 if mismatched)
5. **State-machine validation** (return 409 if the transition is invalid — e.g., trying to "call" a ticket that's already "completed")
6. **Apply the write** atomically (compare-and-swap on the version column)
7. **Capture in idempotency store** (so future retries replay this response)

This produces the most useful errors. A client calling a deleted ticket gets a clear 404 instead of a 412 about a phantom version. A client missing both headers gets the 428 first (more actionable than the 400, since 428 specifically tells them to fetch first).

## Observability wiring — span attributes for all four concerns

Every mechanism above should be visible as span attributes. The trace tree for a successful request:

```
[server.call_next_ticket]                                       2.4ms
├── auth.verify_token                                           0.3ms
│   ├── auth.tenant_id: "syncobit-prod"
│   └── auth.user_id: "staff-A"
├── idempotency.check                                           0.4ms
│   ├── idempotency.key: "9f86d081-..."
│   ├── idempotency.action: "executed"
│   └── idempotency.fingerprint: "a1b2..."
├── ticket.fetch                                                0.6ms
│   ├── http.if_match: "v3"
│   ├── ticket.id: "tk-42"
│   └── ticket.current_etag: "v3"          ← matches, so write proceeds
├── ticket.transition                                           0.8ms
│   ├── ticket.from_state: "issued"
│   ├── ticket.to_state: "called"
│   ├── http.if_match.matched: true
│   ├── http.if_match.action: "applied"
│   └── ticket.new_etag: "v4"
└── http.response                                               0.1ms
    ├── http.response.status_code: 200
    ├── http.response.header.etag: "v4"
    └── idempotency.cached: true            ← stored for replay
```

Every concern is visible. A debugging engineer can see: auth succeeded, idempotency key was new, ETag matched, state transitioned, response sent. Full story in one trace.

For the error case (412), the same attributes show the failure:

```
[server.call_next_ticket]                                       1.2ms          [STATUS: ERROR]
├── auth.verify_token                                           0.2ms
├── idempotency.check                                           0.3ms
│   └── idempotency.action: "executed"
├── ticket.fetch                                                0.5ms
│   ├── http.if_match: "v3"
│   ├── ticket.current_etag: "v4"           ← mismatch
│   ├── http.if_match.matched: false
│   └── http.if_match.action: "rejected_412"
└── http.response                                               0.1ms
    ├── http.response.status_code: 412
    ├── problem.type: ".../version-conflict"
    └── exception.message: "expected v3, current v4"
```

## Edge caching observability

For the edge caching path, the relevant signals come from the CDN, not from origin spans (origin doesn't see cache hits). Two integration patterns:

**1. CDN-emitted metrics.** Most CDNs export metrics about cache hit rate, edge response times, etc. via their own observability platforms (Fastly Real-Time Metrics, Cloudflare Analytics, CloudWatch for CloudFront). Pipe these into your OTel-fed dashboards alongside origin metrics.

**2. CDN-emitted logs with trace correlation.** Configure the CDN to log requests with the `traceparent` header (or your own request ID). When a request misses cache and reaches origin, you have full trace correlation. When it hits cache, you have a CDN log entry showing the cache hit.

For Cloud Run with Cloudflare in front, a typical setup:
- Cloudflare logs include `cf-cache-status: HIT|MISS|EXPIRED|REVALIDATED`
- Cloudflare can stream logs to Cloud Logging via Logpush
- A trace started at the client carries `traceparent` through Cloudflare to origin, so cache misses link to origin spans

For SLOs on cacheable endpoints, key signals are:

- **Cache hit rate at the edge**: `count(cache.status=HIT) / count(*)` — should be 80%+ for well-cached public endpoints
- **Origin offload**: `count(origin requests) / count(edge requests)` — inverse of cache hit rate; useful for capacity planning
- **Stale-served rate**: `count(cache.status=STALE)` — non-zero when origin is slow or unreachable; should be low but non-zero is acceptable

## SLOs that span all four skills

Useful SLOs you can define once instrumentation is in place:

- **Idempotency replay rate**: `count(idempotency.action="replayed") / count(*)` — high replay rate signals upstream instability
- **412 rate per minute**: `count(http.if_match.matched=false)` — high rate signals contention; consider rethinking resource model
- **428 missing-precondition rate**: `count(http.if_match.action="rejected_428_missing")` — clients still missing the `If-Match` header; drive to zero by working with API consumers
- **Cache hit rate by endpoint**: `count(cache.status=HIT) by (http.route)` — informs which endpoints are worth more aggressive caching
- **End-to-end latency by tenant**: `p99(duration) by (tenant.id)` — instantly answers "is this slow for everyone or just one customer"

Each is a one-line query in any modern observability tool, but only because the attributes are present and consistent.

## Sources

- The other three skills in this family
- W3C Trace Context — w3.org/TR/trace-context
- OpenTelemetry trace semantic conventions — opentelemetry.io/docs/specs/semconv/general/trace
