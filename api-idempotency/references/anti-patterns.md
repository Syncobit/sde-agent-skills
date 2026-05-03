# Anti-patterns and Review Checklist

A catalog of idempotency bugs that show up in real production systems, organized by severity. Use this when reviewing PRs, API specs, or existing endpoints. Each item has a name, a short description, why it matters, and what the fix looks like.

## [Block] — Severe bugs, do not ship

### 1. POST/PATCH with side effects and no idempotency layer

**Symptom:** The endpoint creates a charge, sends a message, ships an order, or provisions a resource, and accepts no `Idempotency-Key`. There is no natural unique key enforced at the database level either.

**Why it matters:** Every network timeout becomes a duplicate operation. Users get charged twice; messages send twice; orders ship twice.

**Fix:** Add the Idempotency-Key pattern (see `core-pattern.md`) or enforce a natural unique key at the DB level.

### 2. Caching 5xx responses

**Symptom:** Server stores the response on every request, including 5xx. Subsequent retries with the same key replay the 5xx.

**Why it matters:** A transient downstream failure becomes permanent for that idempotency key. Clients are stuck — they cannot recover by retrying.

**Fix:** Only cache terminal outcomes (2xx, 4xx). On 5xx or unhandled exception, delete the in-flight record so retry can actually retry.

### 3. Idempotency keys not scoped per principal

**Symptom:** The storage key is just the `Idempotency-Key` header value, with no tenant or user prefix.

**Why it matters:** Two tenants can collide on the same key — and worse, one tenant can read another tenant's response. A determined attacker can guess or grind keys to extract data.

**Fix:** `storage_key = sha256(tenant_id || endpoint_id || idempotency_key)`. Tenant must come from authenticated identity, not the request.

### 4. No request fingerprinting

**Symptom:** Server stores `{key → response}` without binding the response to the request body.

**Why it matters:** A client sending the same key with different bodies will receive the original response no matter what they send next. Worst-case: they intentionally reuse a key to read a stored response.

**Fix:** Compute and store a SHA-256 fingerprint of (method, path, canonicalized body). On replay, compare fingerprints; mismatch returns 422.

### 5. Non-atomic in-flight check

**Symptom:** Code looks like `if not exists(key): execute(); store(key, result)`. The check and the execute are separate operations.

**Why it matters:** Two parallel requests both pass the check, both execute, both store. Idempotency is bypassed entirely under any meaningful concurrency.

**Fix:** Use an atomic `INSERT ... ON CONFLICT DO NOTHING` (Postgres) or `SET NX EX` (Redis). The atomicity is the entire concurrency story; without it, the layer is broken.

### 6. Webhook receiver with no event-ID dedupe

**Symptom:** Webhook endpoint processes every delivery, trusting the provider not to send duplicates.

**Why it matters:** Every major webhook provider (Stripe, GitHub, Shopify, payment gateways) explicitly retries on non-200 responses. They will send duplicates. Your at-most-once processing assumption is wrong.

**Fix:** `INSERT ... ON CONFLICT DO NOTHING` on the provider's event ID before processing. If conflict, return 200 OK without re-processing.

## [Fix] — Real bugs, but recoverable

### 7. PATCH that increments or appends

**Symptom:** PATCH body like `{"counter": "+1"}` or `{"tags": {"$push": "new-tag"}}`.

**Why it matters:** This PATCH is not idempotent. A retry double-increments or double-appends. RFC 5789 makes this explicit; it's a common misreading of the spec.

**Fix:** Either redesign the body to express absolute target state (`{"counter": 42}`), or layer Idempotency-Key on the endpoint.

### 8. TTL shorter than the documented retry window

**Symptom:** Idempotency records expire after 1 hour, but client SDK retries for up to 6 hours on transient errors.

**Why it matters:** A retry beyond the TTL re-executes the operation. Hard to detect because it only happens during prolonged outages.

**Fix:** Set the TTL to be longer than the longest documented retry window. 24h is safe for most cases; 7d for B2B; 30d for webhook-receiver dedupe.

### 9. Replaying unsafe headers

**Symptom:** The cached response is replayed verbatim, including `Set-Cookie`, `Authorization`, or stale `Date` headers.

**Why it matters:** Cookies set on the original response (session refresh, CSRF token rotation) get replayed and may cause session confusion. Stale `Date` headers confuse caches and clients.

**Fix:** Maintain an explicit allowlist of headers to replay. Default-deny.

### 10. In-flight records that never expire

**Symptom:** A request crashes mid-execution, leaving an `IN_FLIGHT` record. All subsequent retries return 409 forever.

**Why it matters:** Clients cannot recover from server crashes that occur during the protected window.

**Fix:** Give `IN_FLIGHT` records a short TTL (e.g., 60s). When the request completes, refresh the record with the long TTL. If a request legitimately takes longer, it should be async with a job ID, not a long-held idempotency lock.

### 11. Idempotency-Key documented but not required, falling back to non-idempotent execution

**Symptom:** API accepts an Idempotency-Key but executes the request normally if it's missing. Documentation suggests but does not require it.

**Why it matters:** SDKs that don't know to send the header will silently execute non-idempotently. A retry triggered by a load balancer or proxy will duplicate.

**Fix:** Make the header required (return 400 when missing) on any endpoint that has real side effects. Or, generate a server-side key from a domain-natural identifier as fallback.

### 12. Storing the full response body when it's large

**Symptom:** Idempotency layer stores the full response body, including 5MB document downloads.

**Why it matters:** Storage cost and Redis memory pressure. Also: the cached body can be stale relative to the underlying resource.

**Fix:** Return small responses (resource ID + status) from idempotent endpoints. Have clients fetch large content via separate GETs.

## [Nit] — Worth fixing, lower urgency

### 13. No `Idempotent-Replayed` header on replays

Clients have no way to distinguish a fresh execution from a replay. Useful for client-side metrics and debugging. Add it.

### 14. Error responses not in RFC 9457 Problem Details format

Plain text or ad-hoc JSON makes machine handling harder. Use `application/problem+json` with stable `type` URLs.

### 15. Documentation does not specify Idempotency-Key requirements

Even a working idempotency layer is useless if the client SDK doesn't know to use it. Document: which endpoints require the header, the expected key format (UUIDv4), the TTL, the error codes (400, 409, 422), and the recommended retry-on-409 behavior.

### 16. Long-lived idempotency stores never garbage-collected

Postgres table grows unboundedly. Schedule a job to `DELETE WHERE expires_at < now()` every hour.

## Review checklist (paste into PR template)

```markdown
## Idempotency review

- [ ] Method is correct: PUT for "client knows ID", POST for "server creates ID"
- [ ] If POST/PATCH with side effects: Idempotency-Key required and validated
- [ ] If natural domain key exists (transaction ID, event ID): UNIQUE constraint at DB level
- [ ] Storage key includes (tenant_id, endpoint, idempotency_key) — all three
- [ ] Request fingerprint stored and compared on replay
- [ ] Atomic insert (ON CONFLICT / SET NX) used for in-flight check
- [ ] 5xx responses NOT cached
- [ ] In-flight records have short TTL (60s) with refresh on completion
- [ ] Replayed headers use explicit allowlist (Content-Type, Location, custom)
- [ ] Replay sends `Idempotent-Replayed: true`
- [ ] TTL ≥ documented client retry window
- [ ] Error responses use RFC 9457 Problem Details format
- [ ] Documentation: required header, key format, TTL, error codes, retry behavior
- [ ] Tests: replay, concurrent, mismatch, TTL expiry, 5xx-then-retry (all five)
```
