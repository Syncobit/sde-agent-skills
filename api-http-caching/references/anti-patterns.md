# Anti-patterns and Review Checklist

Common HTTP caching bugs across all three paths (concurrency, revalidation, edge), organized by severity and tagged by path. Use during PR review, OpenAPI spec review, infrastructure review, or audits of existing endpoints.

## [Block] — Severe bugs, do not ship

### 1. `Cache-Control: public` on authenticated responses [edge]

**Symptom:** A response containing user-specific data is served with `Cache-Control: public, max-age=N` (or no Cache-Control at all, allowing default-public CDN behavior).

**Why it matters:** The most damaging caching bug there is. CDN caches user A's response; user B hits the same URL; user B sees user A's data. Privacy violation, security incident, possibly a regulatory issue.

**Fix:** Every authenticated response gets `Cache-Control: private` (or `no-store` for sensitive data). At the CDN, add a defensive rule that forces `private` on requests with auth headers/cookies. Audit all endpoints that include user-specific data.

### 2. State-mutating endpoint with no concurrency control on a multi-actor resource [concurrency]

**Symptom:** PUT/PATCH/DELETE on a resource that multiple clients can modify, with no `If-Match` requirement and no other concurrency mechanism.

**Why it matters:** Lost updates are silent. Two staff terminals both "call next" on the same queue ticket; two admins both "approve" the same expense; two scripts both "complete" the same task. Both report 200 OK; the business outcome is wrong.

**Fix:** Require `If-Match` on the endpoint and return `428 Precondition Required` when missing. Couple with database-level optimistic concurrency (`UPDATE ... WHERE version = ?`).

### 3. Weak ETags on a resource used for concurrency control [concurrency]

**Symptom:** Server generates `ETag: W/"..."` (weak) on responses, and an `If-Match` write endpoint exists.

**Why it matters:** `If-Match` requires strong comparison. Weak ETags never match. Every write fails with 412 — or, in some buggy implementations, the server falls back to "treat missing match as no-precondition" and writes unconditionally, defeating the entire mechanism.

**Fix:** Use strong ETags everywhere if any endpoint uses concurrency control. Audit the ETag generation path. See `primitives.md`.

### 4. ETag check and write are not atomic [concurrency]

**Symptom:** Server reads the ETag from the database, validates it against `If-Match`, then writes the resource — without a transaction, compare-and-swap, or lock.

**Why it matters:** Two concurrent requests with the same `If-Match` value both pass the validation step, both proceed to write, both succeed. The conditional check is theater.

**Fix:** Use database compare-and-swap (`UPDATE ... WHERE version = ?`) or an advisory lock that wraps the read-validate-write sequence atomically.

### 5. 5xx error responses cached at the CDN [edge]

**Symptom:** A transient origin error (502, 503, 504) gets cached because no `Cache-Control: no-store` was set on the error response. Subsequent requests get served the cached error for hours.

**Why it matters:** Origin recovers; the CDN keeps serving the error. Looks like a longer outage than it is.

**Fix:** Always set `Cache-Control: no-store` on 5xx responses. Use `stale-if-error` on successful responses so CDN can serve stale-but-valid content during origin issues instead of a cached error.

### 6. Missing `Vary` header on content-negotiated or auth-varying responses [edge]

**Symptom:** Response varies by `Accept-Language`, `Accept`, or auth state, but `Vary` header is absent.

**Why it matters:** Intermediate caches serve the wrong representation. English speakers get French content; logged-in users get the anonymous response (or worse).

**Fix:** Include in `Vary` every request header that influences the response body. If your ETag generation uses these headers as inputs, `Vary` is non-negotiable.

### 7. 412 returned for a deleted resource [concurrency]

**Symptom:** Client sends `If-Match: "v17"` against a resource that no longer exists. Server returns 412 because the precondition technically failed.

**Why it matters:** Client thinks the resource exists at a different version, retries forever or surfaces a confusing error. Eventually figures out via 404 on a re-fetch.

**Fix:** RFC 9110 §13.1.1 specifies that 404 takes precedence. Check resource existence before evaluating the precondition; return 404 if the resource is gone.

## [Fix] — Real bugs, but recoverable

### 8. No ETag on GET responses [revalidation]

**Symptom:** GET returns `200 OK` with no `ETag` header.

**Why it matters:** Conditional requests are impossible. Cache validation can't happen; concurrency control has no validator. Clients either re-fetch always (wasteful) or use `Last-Modified` (which has one-second resolution and other limitations).

**Fix:** Send strong ETag on every cacheable response. Default policy: every GET response gets an ETag unless explicitly opted out.

### 9. 412 response without the current ETag [concurrency]

**Symptom:** `412 Precondition Failed` returned with no `ETag` header on the response.

**Why it matters:** The client knows their version was stale but has no way to retry intelligently. They have to issue another GET to discover the new ETag, doubling the request count for every conflict.

**Fix:** Include `ETag: <current>` on every 412 response. Optionally include the new state in the body.

### 10. `Cache-Control` missing or contradictory [revalidation, edge]

**Symptom:** Response has `ETag` but no `Cache-Control`, OR has `no-cache` and `no-store` together (these mean different things and are contradictory in practice), OR has `Cache-Control: no-store` with an ETag (wasted; nothing will use it).

**Why it matters:** Without `Cache-Control`, intermediate caches default to heuristic freshness, producing unpredictable behavior. Contradictory directives have unpredictable interpretation across vendors.

**Fix:** Set explicit `Cache-Control` on every response. `private, no-cache` is a safe API default; tune for less-volatile resources.

### 11. Stale ETag served after content change [revalidation, concurrency]

**Symptom:** Response body changes but the ETag stays the same.

**Why it matters:** Clients with `If-None-Match` get 304s indefinitely; they never see the updated content. Often caused by version-counter ETags where some write paths forget to bump the version.

**Fix:** Audit every write path that touches the resource. For high-stakes resources, switch to content-hash ETags.

### 12. No invalidation strategy for edge-cached responses [edge]

**Symptom:** Long `s-maxage` (1+ hour) without any way to invalidate when content changes. Content updates take an hour to propagate.

**Why it matters:** Either you accept stale content for hours, or you keep TTLs short and lose cache hit rate.

**Fix:** Set `Surrogate-Key` (Fastly) or `Cache-Tag` (Cloudflare/CloudFront) on cacheable responses. Build a purge step into your content-update path. See `edge-caching-path.md`.

### 13. Cache key includes `Authorization` header [edge]

**Symptom:** CDN cache key includes the auth token. Every authenticated user gets a unique cache entry; cache fragmentation makes the CDN useless.

**Why it matters:** Either zero cache benefit (every user is a unique cache entry) or massive cache size growth.

**Fix:** Don't cache authenticated responses (use `private`). For public-but-auth-gated content, validate auth at the edge and cache without auth in the key.

### 14. Server ignores `If-None-Match` on non-2xx paths [revalidation]

**Symptom:** Server runs the conditional check on every request, including 404s, 401s, and errors.

**Why it matters:** RFC 9110 §13.1.2 specifies that conditional headers only apply when the response would otherwise be 2xx or 304. Returning 304 in response to what should be a 404 confuses clients.

**Fix:** Evaluate the conditional only after determining the response would be 2xx.

### 15. Client retries 412 with the same `If-Match` [concurrency]

**Symptom:** Client SDK auto-retries on 412 using the same stale ETag.

**Why it matters:** The retry will always fail (the resource hasn't reverted), so the client is stuck — or worse, falls back to retrying without `If-Match` and causes a lost update.

**Fix:** Document that 412 is not retryable without re-fetching. Update the SDK to surface 412 to the caller, never auto-retry.

### 16. `Last-Modified` used as the only validator on a high-frequency resource [revalidation]

**Symptom:** Resource updates frequently (multiple times per second under load), but the only validator is `Last-Modified` with one-second resolution.

**Why it matters:** Two updates within the same second produce the same `Last-Modified`. Conditional checks can't distinguish them. Lost updates and stale-cache hits both occur silently.

**Fix:** Switch to ETag-based validation. Last-Modified is fine as a secondary validator (clients prefer ETag when both are present, per RFC 9110 §13.2.2) but should never be the only one.

### 17. CORS preflight cached too aggressively during development [edge]

**Symptom:** `Access-Control-Max-Age: 86400` set globally including in development. CORS bugs are cached for 24 hours per browser.

**Why it matters:** Slow iteration during CORS configuration. Engineers think they "fixed" a CORS bug but their browser is still serving the cached failure.

**Fix:** Use `Access-Control-Max-Age: 60` or 0 in development. 86400 is fine in production.

### 18. Caching responses that include `Set-Cookie` [edge]

**Symptom:** A response sets a cookie (e.g., session cookie) and is also sent with `Cache-Control: public`.

**Why it matters:** Some CDNs default-skip caching when `Set-Cookie` is present (Fastly, Cloudflare); others don't. Where it does cache, the cookie gets shared across users.

**Fix:** Don't set cookies on cacheable responses. If you must, configure the CDN to either strip the cookie or skip caching.

## [Nit] — Worth fixing, lower urgency

### 19. ETag value lacks double quotes

`ETag: abc123` (without quotes) violates RFC 9110 §8.8.3 syntax. Most clients still accept it, but strict parsers (some intermediate caches, some HTTP libraries) will reject. Always quote: `ETag: "abc123"`.

### 20. Excessively long ETag values

ETags of 200+ characters waste bandwidth (every conditional request includes them) and don't add useful entropy. 16-32 hex characters (64-128 bits) is plenty.

### 21. ETag includes a timestamp formatted as date

`ETag: "2026-05-03T14:00:00Z"` works but conflates with `Last-Modified` semantics and reduces uniqueness for resources updated multiple times per second. Use a hash or version counter instead.

### 22. `Vary: User-Agent`

Every browser version is a unique value; cache fragmentation to uselessness. Use specific UA-detection headers (`Sec-CH-UA-Mobile`) or do UA detection at origin.

### 23. `Vary: *`

The "I don't know what this varies on" sledgehammer. Disables shared-cache benefit entirely. Sometimes used defensively but rarely the right answer; prefer being specific.

### 24. Documentation doesn't specify caching behavior

Even when the implementation is correct, undocumented behavior makes client implementation guesswork. Document: which endpoints emit ETags, whether they're strong or weak, the format conventions, the behavior on conflicts, the Cache-Control policy.

### 25. No 428 on a critical write endpoint [concurrency]

Endpoint accepts unconditional writes (treating missing `If-Match` as "I don't care about concurrency") even on a state-machine resource. Permissive but encourages bad client behavior. Prefer 428.

### 26. Static assets without `immutable` directive [edge]

Hash-named static assets (`/static/main.abc123.js`) deserve `Cache-Control: public, max-age=31536000, immutable`. The `immutable` directive prevents browsers from revalidating even on user reload — useful for assets that can't change without a new URL.

### 27. No origin shielding configured [edge]

Popular endpoints with many global CDN POPs each independently miss origin on cache eviction. Origin shield reduces this to one POP. Worth enabling for any high-traffic origin.

## Review checklist (paste into PR template)

```markdown
## HTTP caching review

### Concurrency path (PUT/PATCH/DELETE)
- [ ] State-changing endpoints on multi-actor resources require If-Match
- [ ] Missing If-Match returns 428 Precondition Required (not 200, not 400)
- [ ] If-Match validation and write are atomic (DB compare-and-swap or lock)
- [ ] 412 responses include the current ETag header
- [ ] Deleted resources return 404, not 412
- [ ] PUT with If-None-Match: * supported for atomic create-if-absent
- [ ] Error responses for 412 and 428 use Problem Details format
- [ ] Client SDK does not auto-retry on 412

### Revalidation path (private GET)
- [ ] GET responses include ETag header on every cacheable representation
- [ ] ETags are strong unless there's a documented reason for weak
- [ ] Same resource never mixes strong and weak ETags
- [ ] Cache-Control is set explicitly (private, no-cache or similar)
- [ ] 304 responses include Cache-Control, ETag, Vary, Date
- [ ] 304 responses do not include a body or body-describing headers
- [ ] Conditional checks only apply when response would be 2xx

### Edge caching path (public GET)
- [ ] Authenticated responses use Cache-Control: private (never public)
- [ ] Vary header lists all headers that influence the response or ETag
- [ ] Vary includes Accept-Encoding when responses are compressed
- [ ] Surrogate-Key (Fastly) or Cache-Tag (Cloudflare/CloudFront) set for invalidation
- [ ] 5xx error responses use Cache-Control: no-store
- [ ] stale-while-revalidate and stale-if-error set where appropriate
- [ ] Cache key does not include Authorization header
- [ ] CORS preflight Access-Control-Max-Age is reasonable for environment
- [ ] Origin shield enabled on high-traffic endpoints

### General
- [ ] Documentation specifies caching behavior per endpoint
- [ ] ETag values are quoted ("abc123") not bare (abc123)
```
