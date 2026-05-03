# Anti-patterns and Review Checklist

A catalog of conditional-request bugs that appear in real APIs, organized by severity. Use during PR review, OpenAPI spec review, or audits of existing endpoints.

## [Block] — Severe bugs, do not ship

### 1. State-mutating endpoint with no concurrency control on a multi-actor resource

**Symptom:** PUT/PATCH/DELETE on a resource that multiple clients can modify, with no `If-Match` requirement and no other concurrency mechanism (no Idempotency-Key dedupe of state transitions, no DB-level state-machine guards).

**Why it matters:** Lost updates are silent. Two staff terminals both "call next" on the same queue ticket; two admins both "approve" the same expense; two scripts both "complete" the same task. The HTTP layer reports both as 200 OK; the business outcome is wrong.

**Fix:** Require `If-Match` on the endpoint and return `428 Precondition Required` when missing. Couple with database-level optimistic concurrency (`UPDATE ... WHERE version = ?`).

### 2. Weak ETags on a resource used for concurrency control

**Symptom:** Server generates `ETag: W/"..."` (weak) on responses, and an `If-Match` write endpoint exists.

**Why it matters:** `If-Match` requires strong comparison (RFC 9110 §13.1.1). Weak ETags never match. Every write fails with 412 — or, in some buggy implementations, the server falls back to "treat missing match as no-precondition" and writes unconditionally, defeating the entire mechanism.

**Fix:** Use strong ETags everywhere if any endpoint uses concurrency control. Audit the ETag generation path.

### 3. ETag check and write are not atomic

**Symptom:** Server reads the ETag from the database, validates it against `If-Match`, then writes the resource — without a transaction, compare-and-swap, or lock.

**Why it matters:** Two concurrent requests with the same `If-Match` value both pass the validation step, both proceed to write, both succeed. The conditional check is theater.

**Fix:** Use database compare-and-swap (`UPDATE ... WHERE version = ?`) or an advisory lock that wraps the read-validate-write sequence atomically.

### 4. 412 response without the current ETag

**Symptom:** `412 Precondition Failed` returned with no `ETag` header on the response.

**Why it matters:** The client knows their version was stale but has no way to retry intelligently. They have to issue another GET to discover the new ETag, doubling the request count for every conflict.

**Fix:** Include `ETag: <current>` on every 412 response. Optionally include the new state in the body so clients can decide whether to retry without an additional fetch.

### 5. 412 returned for a deleted resource

**Symptom:** Client sends `If-Match: "v17"` against a resource that no longer exists. Server returns 412 because the precondition technically failed.

**Why it matters:** The client thinks the resource exists at a different version. They re-fetch, get 404, retry, get 412 again. Or worse, the client SDK loops forever.

**Fix:** RFC 9110 §13.1.1 specifies that 404 takes precedence. Check resource existence before evaluating the precondition; return 404 if the resource is gone.

## [Fix] — Real bugs, but recoverable

### 6. No ETag on GET responses

**Symptom:** GET returns `200 OK` with no `ETag` header.

**Why it matters:** Conditional requests are impossible. Cache validation can't happen; concurrency control has no validator. Clients either re-fetch always (wasteful) or use `Last-Modified` (which has one-second resolution and other limitations).

**Fix:** Send strong ETag on every cacheable response. Default policy: every GET response gets an ETag unless explicitly opted out.

### 7. Cache-Control missing or contradictory

**Symptom:** Response has `ETag` but no `Cache-Control`, OR has `Cache-Control: no-cache` AND `ETag` (which is fine — `no-cache` means revalidate, not "don't store"), OR has `Cache-Control: no-store` with an ETag (wasted; nothing will use it).

**Why it matters:** Without `Cache-Control`, intermediate caches default to heuristic freshness (typically 10% of the time since `Last-Modified`), which produces unpredictable behavior.

**Fix:** Set explicit `Cache-Control` on every response. `private, max-age=0, must-revalidate` is a safe API default; tune up for less-volatile resources.

### 8. Stale ETag served after content change

**Symptom:** Response body changes but the ETag stays the same.

**Why it matters:** Clients with `If-None-Match` get 304s indefinitely; they never see the updated content. Often caused by version-counter ETags where some write paths forget to bump the version.

**Fix:** Audit every write path that touches the resource. For high-stakes resources, switch to content-hash ETags (slower but bulletproof).

### 9. Vary header missing on content-negotiated responses

**Symptom:** Response varies by `Accept`, `Accept-Language`, or `Accept-Encoding`, but `Vary` header is absent.

**Why it matters:** Intermediate caches (CDN, browser, corporate proxy) may serve the wrong representation to a different client. English speakers get French content; JSON clients get HTML.

**Fix:** Include in `Vary` every request header that influences the response body. If your ETag generation uses these headers as inputs, `Vary` is non-negotiable.

### 10. Server ignores If-None-Match on non-2xx paths

**Symptom:** Server runs the conditional check on every request, including 404s, 401s, and errors.

**Why it matters:** RFC 9110 §13.1.2 specifies that conditional headers only apply when the response would otherwise be 2xx or 304. Returning 304 in response to what should be a 404 confuses clients (they think they have a fresh cache; they're missing the 404 signal).

**Fix:** Evaluate the conditional only after determining the response would be 2xx. Most frameworks do this correctly; verify yours.

### 11. Client retries 412 with the same If-Match

**Symptom:** Client SDK auto-retries on 412 using the same stale ETag.

**Why it matters:** Defeats the entire mechanism. The retry will always fail (the resource hasn't reverted), so the client is stuck — or worse, the SDK falls back to retrying without `If-Match` and causes a lost update.

**Fix:** Document that 412 is not retryable without re-fetching. Update the SDK to surface 412 to the caller, never auto-retry.

### 12. Last-Modified used as the only validator on a high-frequency resource

**Symptom:** Resource updates frequently (multiple times per second under load), but the only validator is `Last-Modified` with one-second resolution.

**Why it matters:** Two updates within the same second produce the same `Last-Modified`. Conditional checks can't distinguish them. Lost updates and stale-cache hits both occur silently.

**Fix:** Switch to ETag-based validation. Last-Modified is fine as a secondary validator (clients prefer ETag when both are present, per RFC 9110 §13.2.2) but should never be the only one.

## [Nit] — Worth fixing, lower urgency

### 13. ETag value lacks double quotes

`ETag: abc123` (without quotes) violates RFC 9110 §8.8.3 syntax. Most clients still accept it, but strict parsers (some intermediate caches, some HTTP libraries) will reject. Always quote: `ETag: "abc123"`.

### 14. Excessively long ETag values

ETags of 200+ characters waste bandwidth (every conditional request includes them) and don't add useful entropy. 16-32 hex characters (64-128 bits) is plenty.

### 15. ETag includes a timestamp formatted as date

`ETag: "2026-05-03T14:00:00Z"` works but conflates with `Last-Modified` semantics and reduces uniqueness for resources updated multiple times per second. Use a hash or version counter instead.

### 16. Documentation doesn't specify ETag format or behavior

Even when the implementation is correct, undocumented behavior makes client implementation guesswork. Document: which endpoints emit ETags, whether they're strong or weak, the format conventions, the behavior on conflicts.

### 17. No 428 on a critical write endpoint

Endpoint accepts unconditional writes (treating missing `If-Match` as "I don't care about concurrency") even on a state-machine resource. Permissive but encourages bad client behavior. Prefer 428.

## Review checklist (paste into PR template)

```markdown
## Conditional requests review

- [ ] GET responses include ETag header on every cacheable representation
- [ ] ETags are strong unless there's a documented reason for weak
- [ ] Same resource never mixes strong and weak ETags
- [ ] Cache-Control is set explicitly (not relying on heuristics)
- [ ] Vary header lists all headers that influence the response or ETag
- [ ] PUT/PATCH/DELETE on multi-actor resources require If-Match
- [ ] Missing If-Match returns 428 Precondition Required (not 200, not 400)
- [ ] If-Match validation and write are atomic (DB compare-and-swap or lock)
- [ ] 412 responses include the current ETag header
- [ ] Deleted resources return 404, not 412
- [ ] Conditional checks only apply when response would be 2xx
- [ ] PUT with If-None-Match: * is supported for atomic create-if-absent
- [ ] Error responses for 412 and 428 use Problem Details format
- [ ] Documentation specifies which endpoints use conditional requests
- [ ] Client SDK does not auto-retry on 412
```
