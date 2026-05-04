# Revalidation Flow — If-None-Match, 304 Not Modified

The cache-validation mechanism. Used both by private API clients (mobile apps, SPAs, internal services) revalidating their local caches and by CDN edge caches revalidating their cached copies against origin.

This is the meeting point of two paths:

- **API path use**: a mobile app holds a cached `GET /v1/orders/123` response. Before re-fetching, it sends `If-None-Match` to ask "is my cached copy still current?"
- **Edge path use**: a CDN edge node has a cached copy of `GET /v1/widgets/42`. The cache entry has expired. Before re-fetching the full response from origin, the CDN sends `If-None-Match` (or honors origin's `Cache-Control` to revalidate without it).

The mechanics are the same; the surrounding `Cache-Control` differs by path.

## The complete request/response cycle

### First request (cache miss)

```http
GET /v1/products/sku-42 HTTP/1.1
Host: api.example.com
```

```http
HTTP/1.1 200 OK
Date: Sun, 03 May 2026 14:00:00 GMT
ETag: "v17-en-json"
Cache-Control: private, no-cache
Vary: Accept-Language, Accept-Encoding
Content-Type: application/json
Content-Length: 2341

{"sku": "sku-42", "name": "Widget", ...}
```

The client now caches the response. With `no-cache`, every subsequent use requires revalidation.

### Subsequent request (revalidation)

```http
GET /v1/products/sku-42 HTTP/1.1
Host: api.example.com
If-None-Match: "v17-en-json"
```

If the resource hasn't changed:

```http
HTTP/1.1 304 Not Modified
Date: Sun, 03 May 2026 14:05:30 GMT
ETag: "v17-en-json"
Cache-Control: private, no-cache
Vary: Accept-Language, Accept-Encoding

(no body)
```

The client refreshes its cached metadata and reuses its cached body. No body transmitted. Server CPU saved.

If it has changed:

```http
HTTP/1.1 200 OK
Date: Sun, 03 May 2026 14:05:30 GMT
ETag: "v18-en-json"
Cache-Control: private, no-cache
Vary: Accept-Language, Accept-Encoding
Content-Type: application/json
Content-Length: 2378

{"sku": "sku-42", "name": "Widget Pro", ...}
```

New ETag, fresh body, new freshness window.

## What MUST be in a 304 response

Per RFC 9110 §15.4.5, a 304 response should include the headers that **would have been sent in the corresponding 200 response**, specifically:

- `Date`
- `ETag` (the matched ETag)
- `Cache-Control`
- `Content-Location` (if it would have been present)
- `Expires`
- `Vary`

**MUST NOT** include:
- A response body
- `Content-Length` (unless 0; some servers omit it entirely)
- Headers that describe the body (`Content-Type`, `Content-Encoding`)

The reason for the "headers that would have been sent" rule: the 304 effectively refreshes the client's cached representation, including its metadata. If `Cache-Control` would have changed (extending or shortening the cache window), the 304 propagates that change.

## Cache-Control composition for revalidation

`ETag` and `Cache-Control` work together. The mental model:

- `Cache-Control: max-age=N` defines the **freshness window** during which the cached response can be used without contacting the server.
- `ETag` defines the **revalidation token** for use after the freshness window expires.

For the API path (private revalidation):

| Use case | Cache-Control | Behavior |
|----------|---------------|----------|
| Always revalidate (API default) | `private, no-cache` | Every request validates with `If-None-Match`; 304 when unchanged |
| Brief private freshness | `private, max-age=60` | 1 minute fresh, then revalidate |
| Sensitive, must not cache anywhere | `private, no-store` | Don't cache at all; ETag irrelevant |

`private, no-cache` is semantically equivalent to `private, max-age=0, must-revalidate` (you'll see the latter in older code) — both force revalidation before reuse. `no-cache` is the modern idiomatic form; `max-age=0, must-revalidate` is a legacy workaround for HTTP/1.0 caches that didn't understand `no-cache`. In 2026, prefer `no-cache` for new code.

For the edge path (shared cache revalidation), see `edge-caching-path.md` — different `Cache-Control` recipes apply because of the shared-cache concerns.

`private, no-cache` + `ETag` is the sweet spot for most authenticated API endpoints — clients always validate, but the validation is cheap when the resource hasn't changed.

## Multiple ETags in If-None-Match

A client can list multiple ETags it's willing to accept:

```http
GET /v1/products/sku-42 HTTP/1.1
If-None-Match: "v15-en-json", "v16-en-json", "v17-en-json"
```

The server returns 304 if any of them match. Useful for clients that have multiple cached versions (e.g., separate caches per stale-while-revalidate level).

In practice, most clients only send one ETag. Worth supporting on the server side, but not critical.

## Comparison rules — `If-None-Match` is weak

**`If-None-Match` uses weak comparison** (RFC 9110 §13.1.2). Both strong and weak ETags can match. This is correct for cache validation; the cache only cares about logical equivalence.

This is the opposite of `If-Match`, which requires strong comparison. The asymmetry is intentional:

- `If-Match` (concurrency control) needs byte-level certainty — you're authorizing a write based on the version
- `If-None-Match` (cache validation) only needs semantic equivalence — you're avoiding a redundant transfer

If you generate weak ETags and use only `If-None-Match` (revalidation-only, no concurrency control), everything works. If you mix `If-Match` and weak ETags, every write fails.

## CDN-initiated revalidation

When a CDN sits in front of the origin, the CDN performs revalidation against origin on the client's behalf. The client may or may not see this — typically the CDN serves a fresh response from cache (without going to origin), or the CDN performs the revalidation invisibly.

A revalidation request from a CDN to origin looks the same as a client revalidation:

```http
# CDN → Origin
GET /v1/widgets/sku-42 HTTP/1.1
If-None-Match: "v17"
X-Forwarded-For: 203.0.113.42

# Origin → CDN, unchanged
HTTP/1.1 304 Not Modified
ETag: "v17"
Cache-Control: public, max-age=3600
```

The CDN updates its cached entry's freshness, then serves the cached body to the client. From the client's perspective, the response is a normal 200 — the revalidation is invisible.

For details on CDN-side revalidation behavior (what each major CDN respects, how to debug "why doesn't my CDN revalidate?"), see `edge-caching-path.md`.

## Implementation pitfalls

**1. Forgetting to send ETag on the 304.** The 304 response without an ETag leaves the client guessing what just got revalidated. Always include the matched ETag.

**2. Ignoring If-None-Match for non-2xx responses.** A 404 should not honor If-None-Match — return the 404 normally. RFC 9110 §13.1.2 specifies that conditional headers only apply when the response would otherwise be 2xx or 304.

**3. Not sending ETag on the 200.** Some frameworks default to skipping ETag generation. Verify with a real GET that the response includes an ETag header. Without ETag on the 200, no revalidation can happen on subsequent requests.

**4. Serializing the response body before checking If-None-Match.** Wasted CPU on cache hits. Compute the ETag from the version (if you can), check against the conditional header, and short-circuit before building the body.

**5. Using strong ETags with bodies that legitimately vary.** If your API includes a `generated_at: "2026-05-03T14:00:01Z"` timestamp in every response, every response gets a different strong ETag, and revalidation never returns 304. Either use a weak ETag, or omit the timestamp from the cache-relevant body.

**6. 304 with body-describing headers.** Including `Content-Type` or `Content-Encoding` on a 304 confuses some clients (they interpret it as describing a non-existent body). Audit framework defaults; some include these by mistake.

**7. Cache-Control on the 304 differs from the 200.** The 304 must include the Cache-Control that *would have been on the 200*. If your framework strips it from 304s, the client's freshness window doesn't refresh and you get a revalidation storm.

## Server-side optimization

For high-traffic GET endpoints, computing the response body just to discard it on a 304 is wasteful. Two optimizations:

1. **Compute ETag from version, not body.** If you trust your version counter, you can short-circuit before serializing. `If-None-Match: "v17"` matches the current version → return 304 without ever building the response body.

2. **Cache (request-fingerprint → ETag) in a fast store.** Useful when the response is expensive to generate but the ETag is stable. Look up the cached ETag, compare against `If-None-Match`, return 304 on match.

Both optimizations require careful invalidation. Skip them initially; add when you have measured pressure on the endpoint.

## Sources

- RFC 9110 §13.1.2 — If-None-Match semantics
- RFC 9110 §15.4.5 — 304 Not Modified
- RFC 9111 — HTTP Caching (the companion spec)
- RFC 5861 — stale-while-revalidate / stale-if-error Cache-Control extensions
