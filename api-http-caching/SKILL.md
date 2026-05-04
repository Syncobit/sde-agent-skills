---
name: api-http-caching
description: Apply HTTP caching (Cache-Control, ETag, Vary, If-Match, If-None-Match) when designing REST APIs and the CDN/edge layer. Use whenever the task involves cache headers, response caching, CDN integration (Fastly, Cloudflare, CloudFront), cache invalidation, surrogate keys, cache tags, optimistic concurrency control, the lost-update problem, 304 Not Modified, 412 Precondition Failed, PUT/PATCH endpoints where clients race, atomic create-if-absent, public vs private caching, or "how do I make this endpoint cacheable". Trigger broadly on concurrent edits, version mismatches, "two users saved at the same time", "make this cache at the edge", "what Cache-Control should I send", and any review of a stateful or read-heavy API — even when the user does not say ETag, Cache-Control, or CDN. Composes with api-idempotency (retry safety), api-error-responses (Problem Details for 412/428), and otel-observability (cache metrics and trace correlation).
---

# REST API HTTP Caching

A skill for the full HTTP caching surface — from optimistic concurrency control on writes to public response caching at the CDN edge. Grounded in RFC 9110 (HTTP Semantics) and RFC 9111 (HTTP Caching), with vendor-specific notes for Fastly, Cloudflare, and CloudFront.

## Why this skill exists

HTTP caching is one mechanism that solves three different design problems:

1. **Concurrency control** on writes — `If-Match` + `412` to prevent two clients silently overwriting each other.
2. **Revalidation** of private cached responses — `If-None-Match` + `304` so a client (mobile app, SPA, server-side cache) doesn't re-download data it already has.
3. **Edge caching** of public responses — `Cache-Control: public` + CDN behavior + surrogate keys so a request never reaches origin at all.

These three problems use overlapping HTTP primitives (ETag, conditional headers, Cache-Control), which is why HTTP collapses them into one mechanism. But the design decisions diverge sharply once you move past the shared substrate. A `Cache-Control: max-age=300, public` is the right answer for one path and an active security incident for another.

The skill's job is to:
- Identify which of the three paths your endpoint needs (often more than one)
- Apply the right primitives correctly for that path
- Avoid the mistakes that occur at the boundaries

## The three paths

```
                    HTTP Caching Surface
                            │
        ┌───────────────────┼───────────────────┐
        │                   │                   │
   API Concurrency      Revalidation        Edge Caching
   (PUT/PATCH/DELETE)   (private GET)       (public GET)
        │                   │                   │
   If-Match               If-None-Match       Cache-Control: public
   412 / 428              304 Not Modified    Vary
   Lost-update            Cache-Control:      Surrogate keys
   prevention             private             CDN invalidation
```

A given endpoint may touch one, two, or all three paths. A typical REST resource:

- `GET /v1/widgets/{id}` (publicly cacheable catalog item) — **edge caching path**
- `GET /v1/orders/{id}` (private, per-user) — **revalidation path**
- `PATCH /v1/orders/{id}` (state-changing) — **concurrency path**

You don't pick one path for the whole API; you pick the right path per endpoint.

## Step 1 — Identify which path(s) this endpoint needs

For each endpoint, walk these questions. Note: an endpoint may touch multiple paths simultaneously — these aren't mutually exclusive. A `PATCH` request uses the concurrency path on the way in (`If-Match`) AND the revalidation path on the way out (the response includes a fresh `ETag` for future GETs to revalidate against).

1. **Is it state-changing (`POST`/`PUT`/`PATCH`/`DELETE`)?**
   - **Yes**: concurrency path is in scope — multiple clients can race; you need `If-Match` to prevent lost updates. Continue to question 2 to also determine the response-side caching posture (the response of a state-changing endpoint typically returns the updated resource and should emit an `ETag` so future GETs can use the revalidation path).
   - **No** (`GET`/`HEAD`): concurrency path is not in scope. Continue to question 2.

2. **Can the response be safely shared between users?**
   - **No** (per-user data, authenticated, contains PII): revalidation path. `Cache-Control: private` + ETag + `If-None-Match`. This is the path response of a state-changing endpoint should follow too — the updated resource is returned with a new `ETag` and `Cache-Control: private`.
   - **Yes** (public catalog data, marketing pages, public reference data): edge caching path. `Cache-Control: public` + CDN configuration.
   - **Mixed** (response varies by auth state — anonymous vs. logged-in users): edge caching path with careful `Vary` configuration.

3. **Does the resource have multiple representations (content negotiation, multiple languages, encodings)?**
   - **Yes**: `Vary` is mandatory regardless of path. See `references/primitives.md` for the rules.

The mechanisms compose without conflict. A `PATCH` endpoint on a private resource uses concurrency on the request side and revalidation on the response side — both apply, and the design for each is independent.

## Step 2 — Concurrency path (state-changing endpoints)

The lost-update problem: two clients fetch a resource at version 17, both modify, both write. Without conditional requests, the second write silently overwrites the first. With `If-Match`, the second write fails with `412 Precondition Failed` and the client can re-fetch and retry.

The pattern in compressed form:

```http
# Client GETs current state, gets ETag
GET /v1/orders/123 HTTP/1.1
HTTP/1.1 200 OK
ETag: "v17"

# Client modifies, writes back with If-Match
PATCH /v1/orders/123 HTTP/1.1
If-Match: "v17"
Content-Type: application/merge-patch+json
{"status": "shipped"}

# Success
HTTP/1.1 200 OK
ETag: "v18"

# OR: another client wrote first
HTTP/1.1 412 Precondition Failed
ETag: "v18"
Content-Type: application/problem+json
{"type": ".../version-conflict", "title": "Resource was modified", ...}
```

For state-critical resources, **require** `If-Match` and return `428 Precondition Required` when missing — don't allow unconditional writes that defeat the mechanism.

`If-Match` requires **strong** ETag comparison. Weak ETags (`W/"..."`) never match. If you generate weak ETags and use `If-Match`, every write fails. See `references/primitives.md` for ETag generation strategies.

The atomic check-and-write rule: the conditional check and the actual write must be a single atomic operation (DB compare-and-swap, advisory lock). Read-validate-write in application code without atomicity defeats the entire mechanism.

For the full concurrency design (atomic-create with `If-None-Match: *`, the 412 vs 404 distinction, end-to-end worked example with state machines), see `references/concurrency-path.md`.

## Step 3 — Revalidation path (private GET responses)

A client (mobile app, SPA, internal service) has a cached copy of an authenticated GET response. Before re-fetching the full body, it asks: "is my cached copy still current?" Answer is `304 Not Modified` (no body) if yes, full `200 OK` if no.

The pattern:

```http
# Server emits ETag and private Cache-Control
GET /v1/orders/123 HTTP/1.1
HTTP/1.1 200 OK
ETag: "v17"
Cache-Control: private, no-cache
Content-Type: application/json
{"id": 123, "status": "shipped", ...}

# Client re-requests later with the stored ETag
GET /v1/orders/123 HTTP/1.1
If-None-Match: "v17"

# If unchanged
HTTP/1.1 304 Not Modified
ETag: "v17"
Cache-Control: private, no-cache

# If changed
HTTP/1.1 200 OK
ETag: "v18"
Cache-Control: private, no-cache
{"id": 123, "status": "delivered", ...}
```

Key rules:
- **`Cache-Control: private`** is non-negotiable on user-specific data. Without it, an intermediate cache may serve user A's data to user B.
- **`no-cache`** (modern form, equivalent to `max-age=0, must-revalidate` in older code) is the API-friendly default — clients always validate, but the validation is cheap when nothing changed.
- **304 must include certain headers** (`Cache-Control`, `ETag`, `Vary`, `Date`) and **must not include a body** or body-describing headers. The 304 effectively refreshes the client's cached metadata.
- **`If-None-Match` uses weak comparison** — both strong and weak ETags can match. This is intentional and correct for cache validation.

For the full revalidation flow (header rules for 304s, integration with `Vary`, the multiple-ETag pattern, common implementation pitfalls), see `references/revalidation-flow.md`.

## Step 4 — Edge caching path (public GET responses)

A response that can be safely shared between users belongs at the CDN edge. The first request reaches origin, the response is cached at the edge, subsequent requests for the same URL never reach origin until the cache entry expires or is invalidated.

The pattern:

```http
HTTP/1.1 200 OK
Cache-Control: public, max-age=3600, s-maxage=86400, stale-while-revalidate=604800, stale-if-error=604800
Vary: Accept, Accept-Language, Accept-Encoding
ETag: "v17"
Surrogate-Key: products product-sku-42 catalog-en
Content-Type: application/json
```

The directives, decoded:
- `public` — explicit permission for shared caches (CDN, corporate proxy) to cache.
- `max-age=3600` — browsers cache for 1 hour.
- `s-maxage=86400` — shared caches (CDN) cache for 24 hours. Different from browser TTL.
- `stale-while-revalidate=604800` — for 7 days after expiry, the CDN can serve stale content while revalidating in the background. Reduces tail latency.
- `stale-if-error=604800` — for 7 days after expiry, if origin is unreachable, serve stale. Resilience during outages.
- `Vary` — different cache entries per `Accept-Language`, etc.
- `Surrogate-Key` (Fastly, space-separated), `Cache-Tag` (Cloudflare, comma-separated), or a configurable header (CloudFront) — purge-able grouping, lets you invalidate "all product caches" without knowing every URL.

Key rules:
- **Never use `public` on authenticated or per-user responses.** This is the most damaging caching bug — leaking one user's data to another via a shared cache.
- **Always set `Vary`** for content-negotiated responses. Without it, a French speaker may get an English cached copy.
- **Plan invalidation up front.** TTL-based expiry is the easy part; targeted invalidation (this product changed, purge its caches) requires surrogate keys or cache tags configured from day one.
- **Negative caching matters.** A 404 for a deleted resource shouldn't cache for 24 hours; a 500 shouldn't cache at all. Set `Cache-Control` deliberately on error responses.

For the full edge caching design (vendor-specific surrogate-key/cache-tag patterns for Fastly, Cloudflare, CloudFront, invalidation strategies, authentication interaction, CORS preflight caching, negative caching rules), see `references/edge-caching-path.md`.

## Step 5 — The shared substrate

All three paths rely on:

- **ETag generation** — strong vs weak, content-hash vs version-counter, the multi-representation problem
- **`Cache-Control` directives** — the full grammar, what each directive means, common combinations
- **`Vary`** — when it's required, the `Vary: *` trap, integration with ETags
- **Validators** — ETags vs `Last-Modified`, why ETags are usually better

These are common to every path. See `references/primitives.md` for the full reference.

## Step 6 — Compose with the other skills

The four skills (this one, `api-idempotency`, `api-error-responses`, `otel-observability`) compose orthogonally:

| Concern | Mechanism | Skill |
|---------|-----------|-------|
| Retry safety: "client retried after timeout" | `Idempotency-Key` + dedupe store | `api-idempotency` |
| Concurrency control: "two clients raced" | `If-Match` + 412 (this skill, concurrency path) | this |
| Cache efficiency: "skip the round-trip" | `If-None-Match` + 304 (this skill, revalidation path) | this |
| Edge caching: "skip origin entirely" | `Cache-Control: public` + CDN (this skill, edge path) | this |
| Error contract: "format the failure" | RFC 9457 Problem Details | `api-error-responses` |
| Visibility: "did the cache hit, did the lock fail" | Span attributes, cache-hit metrics | `otel-observability` |

A well-designed write endpoint on a contested resource uses three skills together:

```http
PATCH /v1/orders/123 HTTP/1.1
Idempotency-Key: 9f86d081-...
If-Match: "v17"
Content-Type: application/merge-patch+json
{"status": "shipped"}
```

`Idempotency-Key` makes it safe to retry. `If-Match` makes it safe to write at all. The 412/428 response uses Problem Details. The trace tags `idempotency.action`, `http.if_match.matched`, and `cache.hit` so the whole flow is debuggable.

For the full worked example (a request flowing through all four skills with end-to-end correlation, plus cache observability patterns), see `references/composition.md`.

## Output style

When applying this skill, produce:

- For **endpoint design**: literal HTTP request/response pairs for each scenario (success, 304, 412, 428, edge cache hit/miss). Include exact headers.
- For **review**: numbered findings tagged `[block]`/`[fix]`/`[nit]`, separated by path so the engineer can see "your concurrency design is fine, your edge caching has these issues".
- For **CDN configuration**: vendor-specific examples (Fastly VCL, Cloudflare Workers/page rules, CloudFront behaviors) with the matching origin response headers.
- For **OpenAPI**: schema fragments for cacheable response definitions, response headers (`ETag`, `Cache-Control`, `Vary`), and the 304/412/428 responses.

Cite RFC 9110 §13 for conditional requests, RFC 9111 for caching mechanics, RFC 5861 for stale-while-revalidate / stale-if-error, RFC 6585 for 428.

## When to dig into the references

- **Shared substrate: ETag generation, Cache-Control directives, Vary** → `references/primitives.md`
- **API concurrency: `If-Match`, 412, 428, lost-update prevention** → `references/concurrency-path.md`
- **Revalidation: `If-None-Match`, 304, the meeting point of API and edge** → `references/revalidation-flow.md`
- **Edge caching: CDN-specific patterns, surrogate keys, invalidation, negative caching** → `references/edge-caching-path.md`
- **Common bugs across all paths** → `references/anti-patterns.md`
- **Composition with idempotency, error responses, and OTel observability** → `references/composition.md`
