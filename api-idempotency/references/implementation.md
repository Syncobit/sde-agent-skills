# Implementation — Storage, TTL, Fingerprinting, Scoping, Concurrency

The decisions you make here are usually the difference between an idempotency layer that works and one that creates new bugs. Each section ends with a concrete recommendation, not just options.

## Storage backend

Three viable choices:

**Redis** — the default for hot-path APIs.
- Pros: native TTL, single-digit ms latency, atomic `SET NX EX` for the in-flight insert, cluster-friendly.
- Cons: not durable across cluster failover unless you run Redis Enterprise / managed Redis with persistence; a key lost in failover is treated as "not seen" and the operation re-executes.
- Mitigation: only acceptable risk for operations whose double-execution cost is bounded. For payment authorization, downstream must also be idempotent (most card networks are).

**Postgres** — the default for low-volume, durability-critical APIs.
- Pros: durable, transactional with the business write (the same `INSERT` that records idempotency can be in the same transaction as the order creation), audit trail.
- Cons: higher latency, harder to scale horizontally for hot keys.
- Use the `INSERT ... ON CONFLICT DO NOTHING RETURNING` pattern for the atomic check.

**DynamoDB / Cassandra / similar** — viable for very-high-scale.
- Pros: built-in TTL, scales linearly, conditional writes provide atomicity (`PutItem` with `ConditionExpression: attribute_not_exists(pk)`).
- Cons: eventual consistency on reads can produce false "not seen" cases; use strongly consistent reads on the GET path of the idempotency layer.

**Recommendation:** Redis for stateless services with high throughput; Postgres when you can put the idempotency record in the same transaction as the business effect. Pick based on whether the business effect can be transactionally bound to the idempotency record — if yes, Postgres simplifies a lot of edge cases.

## TTL — how long to keep records

The TTL must be longer than the longest plausible client retry window. The cost of too-short is catastrophic (operation re-executes); the cost of too-long is only storage.

| Client type                      | Recommended TTL |
|----------------------------------|-----------------|
| Server-to-server, low-latency    | 1 hour          |
| Mobile clients, browser SPAs     | 24 hours        |
| B2B integrations, batch systems  | 7 days          |
| Webhook receivers (event ID dedupe) | 30 days      |

**24 hours is a safe default** — Stripe uses it, and it covers virtually all client retry behavior including overnight queue replays. Shorten only if storage cost is a real concern; lengthen for batch B2B.

## Request fingerprinting

The fingerprint binds the stored response to the original request. Without it, a client (or attacker) can reuse a key to fetch someone else's response or bypass intended idempotency.

**Canonicalize before hashing.** JSON bodies have multiple equivalent serializations (key order, whitespace). Hash a canonical form:

```python
def canonicalize_json(body: bytes) -> bytes:
    obj = json.loads(body)
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
```

Hash inputs to include:
- HTTP method (uppercased)
- Path (including resolved query string with sorted keys)
- Canonicalized body
- Optionally, headers that affect the response semantics (`Accept-Language`, etc.) — only if they are part of the contract

Hash inputs to **exclude**:
- Authorization headers (you scope by tenant separately; do not put credentials in a hash)
- `Date`, `User-Agent`, request ID, tracing headers
- Anything the client legitimately varies between retries

**Recommendation:** SHA-256 is fine. The hash is for equality checking, not security against motivated attackers — but using SHA-256 means you don't have to revisit it later.

## Key scoping

The single most common security bug in idempotency layers is global key scope. **Always scope by authenticated principal.**

```
storage_key = sha256(tenant_id || endpoint_id || idempotency_key)
```

`tenant_id` must come from the authenticated session, never from the request. If you derive tenant from a header or body field, an attacker can forge it and collide with another tenant's key.

`endpoint_id` is a stable identifier for the route. This prevents accidental cross-endpoint collision (a key reused across `POST /charges` and `POST /refunds` would otherwise collide, with bad results).

**Recommendation:** Always (tenant, endpoint, key) — three components, all required, hashed together.

## Concurrency — the in-flight problem

Two requests with the same key arriving in parallel is the trickiest case. There are three viable strategies:

**1. Reject the second (recommended default).**
The atomic insert succeeds for the first request; the second sees an `IN_FLIGHT` record and returns `409 Conflict` immediately. Client retries after a short backoff, by which time the first has completed and the retry replays the response.

Why this is the default: deterministic, no server-side blocking, no thundering-herd risk under retry storms.

**2. Wait for the first to complete (block-and-replay).**
The second request blocks on a lock or polls until the first transitions to `COMPLETED`, then replays. Transparent to the client.

When to use: low-throughput APIs where retry-with-backoff is hard to implement on the client side. Risks: if many clients pile up on the same key during a slow request, you can exhaust connections.

**3. Optimistic merge (rare, only for specific domains).**
Both requests proceed; the second's effect is squashed at write time via a database-level uniqueness constraint. This is essentially natural-key idempotency under a different name — only viable when there's a domain identifier.

**Recommendation:** Strategy 1 (deterministic 409). Document the expected client behavior: "On 409 idempotency-key-in-flight, retry with exponential backoff."

## In-flight TTL safeguard

A client that crashes mid-request can leave an `IN_FLIGHT` record forever, blocking all retries. Solve this by giving `IN_FLIGHT` records a short TTL (60s is reasonable) that gets replaced with the long TTL when the request completes.

If a request legitimately takes longer than 60s, refresh the TTL via heartbeats — but this is a sign the request should be async with a job ID.

## Header allowlist for replay

When replaying a stored response, do *not* replay every header. Replay only:

- `Content-Type`
- Custom domain headers (`X-Charge-Status`, etc.)
- The `Location` header on 201 responses

Do *not* replay:

- `Set-Cookie`, `Authorization`, `Cookie`
- `Date` (regenerate on each replay)
- `Strict-Transport-Security`, `Content-Security-Policy` (let the framework set these)
- Tracing headers (`Traceparent`, etc.) — generate fresh

Add `Idempotent-Replayed: true` so clients and operators can distinguish replays from fresh executions.

## Body size limits

Cap the response body you cache. A 10MB streaming response should not be replayed from the idempotency store — both because storage is expensive and because the original may have legitimately changed downstream state that the replay doesn't know about. Common caps: 1MB body, 64KB body for Redis stores.

For endpoints that legitimately return large bodies, return a small response body with a resource ID and let the client fetch the full content via GET.

## Webhook receiver special case

You are often the *receiver* of someone else's webhooks (Stripe events, GitHub events, payment provider callbacks). The provider sends an event ID; your job is to dedupe on it.

```sql
CREATE TABLE processed_webhook_events (
  provider_id    text PRIMARY KEY,    -- e.g., "evt_1Abc..." from Stripe
  provider       text NOT NULL,
  received_at    timestamptz NOT NULL DEFAULT now(),
  payload_hash   bytea NOT NULL
);
```

On every webhook delivery: `INSERT ON CONFLICT DO NOTHING`. If the insert affected 0 rows, this is a duplicate — return 200 OK without re-processing. If 1 row, process the event in the same transaction.

**TTL for webhook dedupe records is much longer.** Providers retry for hours or days. 30 days is a safe floor.

## Sources

- IETF draft-ietf-httpapi-idempotency-key-header-07 (October 2025)
- Stripe Engineering blog — "Designing robust and predictable APIs with idempotency"
- AWS API Reference — RequestToken pattern (DynamoDB-backed idempotency)
