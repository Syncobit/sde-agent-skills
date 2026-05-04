# Edge Caching Path — Public Caching, CDNs, Surrogate Keys, Invalidation

The full design for caching responses at the CDN edge so requests never reach origin. This is the public/shared-cache path; for private API revalidation, see `revalidation-flow.md`; for state-changing endpoints, see `concurrency-path.md`.

## What "edge caching" means precisely

The CDN sits between clients and origin. The first request for a URL flows: client → CDN → origin. The CDN caches the response. Subsequent requests for the same URL (potentially from different clients) flow: client → CDN (cache hit) → done. Origin sees zero traffic.

The wins are large:
- **Latency**: clients hit the nearest edge POP (10-50ms) instead of origin (often 100-500ms+ globally)
- **Origin load**: a popular URL with 1M req/sec to clients can result in <1 req/sec to origin if cached well
- **Resilience**: `stale-if-error` lets CDN serve cached content during origin outages

The risks are also large. The most damaging caching bugs occur at the edge — leaking one user's private data to another via a shared cache, serving stale content for far too long, accidentally caching a 500 error response.

## The complete edge-cacheable response

```http
HTTP/1.1 200 OK
Cache-Control: public, max-age=3600, s-maxage=86400, stale-while-revalidate=604800, stale-if-error=604800
Vary: Accept, Accept-Language, Accept-Encoding
ETag: "v17-en-json"
Surrogate-Key: products product-sku-42 catalog-en      ← Fastly (space-separated)
Cache-Tag: products,product-sku-42,catalog-en           ← Cloudflare (comma-separated)
Content-Type: application/json
{...}
```

The directives, decoded:
- `public` — explicit permission for shared caches to cache.
- `max-age=3600` — browsers cache for 1 hour.
- `s-maxage=86400` — shared caches (CDN) cache for 24 hours. Different from browser TTL.
- `stale-while-revalidate=604800` — for 7 days after expiry, the CDN can serve stale content while revalidating in the background. Reduces tail latency.
- `stale-if-error=604800` — for 7 days after expiry, if origin is unreachable, serve stale. Resilience during outages.
- `Vary` — different cache entries per varying request header.
- `Surrogate-Key` (Fastly), `Cache-Tag` (Cloudflare), or your configured header (CloudFront) — purge-able grouping; the most important header most teams forget to set.

## The decision flow for `Cache-Control` on a public endpoint

For each public-cacheable endpoint, walk these decisions:

1. **How long is the content fresh?** Pick `s-maxage` first (the shared-cache TTL). Use the longest TTL you're comfortable with given your invalidation strategy. With surrogate keys, you can serve stale up to 24 hours and purge on demand — go long.

2. **How long should browsers cache before revalidating?** `max-age` is usually shorter than `s-maxage`. Why: clients are harder to invalidate than CDN. Browsers re-fetch every 5-10 minutes; CDN holds longer.

3. **What's your tolerance for stale content during origin outages?** `stale-if-error` should be longer than `s-maxage` — typically 1-7 days. Almost always worth setting; resilience is free.

4. **What's your tolerance for stale content during slow origin responses?** `stale-while-revalidate` lets CDN serve stale immediately and refresh in background. Almost always worth setting.

5. **Does the content vary by request header?** If yes, set `Vary` exhaustively. If no, omit.

6. **Are there grouping concepts that need targeted invalidation?** If yes, set `Surrogate-Key` (Fastly) or `Cache-Tag` (Cloudflare/CloudFront).

A typical recipe for a stable public catalog endpoint:

```http
Cache-Control: public, max-age=300, s-maxage=86400, stale-while-revalidate=604800, stale-if-error=604800
Vary: Accept-Language, Accept-Encoding
Surrogate-Key: catalog product-{id} category-{cat_id}
ETag: "..."
```

A typical recipe for a frequently-changing public endpoint (news feed, leaderboard):

```http
Cache-Control: public, max-age=10, s-maxage=60, stale-while-revalidate=300, stale-if-error=86400
Surrogate-Key: feed feed-tenant-{tid}
ETag: "..."
```

## Vendor-specific surrogate-key/cache-tag patterns

The mechanism is the same across vendors; the header name and quirks differ.

### Fastly — `Surrogate-Key`

Fastly popularized the pattern. Header is `Surrogate-Key`, values are **space-separated**.

```http
Surrogate-Key: products product-sku-42 catalog-en
```

Purge:
```bash
curl -X POST "https://api.fastly.com/service/SERVICE_ID/purge/product-sku-42" \
    -H "Fastly-Key: TOKEN"
```

Multiple keys per response (any can be used to purge), no documented limit on header size or key count.

Fastly strips the `Surrogate-Key` header before forwarding to the client — values are not visible to end users.

### Cloudflare — `Cache-Tag`

Header is `Cache-Tag`, values are **comma-separated**.

```http
Cache-Tag: products,product-sku-42,catalog-en
```

Purge via API:
```bash
curl -X POST "https://api.cloudflare.com/client/v4/zones/ZONE_ID/purge_cache" \
    -H "Authorization: Bearer TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"tags": ["product-sku-42"]}'
```

Limits: Cache-Tag header up to 16KB total (roughly 1,000 unique tags per response). For purges, individual tags are capped at 1,024 characters and a single API call can include up to 30 tags.

As of April 2025, all Cloudflare purge methods (URL, prefix, hostname, tag, everything) are available on **all plans including Free** — previously, tag-based purging was Enterprise-only. The differences across tiers are now in rate limits, not feature availability:

| Plan | Purge rate limit (approx.) |
|------|----------------------------|
| Free | 5 requests/minute, 25-token bucket |
| Pro | Higher per-minute, larger bucket |
| Business | 10 requests/second |
| Enterprise | 500-token bucket, raisable on request |

Cloudflare strips the `Cache-Tag` header before forwarding to the client — values are not visible to end users. Note that some legacy Cloudflare documentation pages still describe purge-by-tag as Enterprise-only; the current authoritative source is the Cache changelog at developers.cloudflare.com/cache/changelog.

### CloudFront — configurable cache-tag header (added April 2026)

AWS CloudFront added cache-tag invalidation in April 2026. **Unlike Fastly and Cloudflare, the header name is configurable** rather than fixed — you specify it in the CloudFront console, AWS CLI, or API when configuring the distribution. AWS's reference implementation defaults to `Edge-Cache-Tag` (note the dash style):

```http
Edge-Cache-Tag: products,product-sku-42,catalog-en
```

Values are comma-separated. Purge via the CloudFront API or `aws` CLI; consult current AWS docs for the exact API shape since this feature is new and may evolve.

Pricing: each cache tag is priced as one invalidation path, so an invalidation hitting 100 tagged objects costs the same as 100 path-based invalidations. Plan accordingly when designing tag granularity.

Performance (per AWS announcement): invalidations take effect in under 5 seconds at p95 globally, with end-to-end completion (including status reporting) under 25 seconds at p95.

Before April 2026, CloudFront supported only path-based invalidation (`aws cloudfront create-invalidation --paths "/products/*"`), which is much less flexible. If you're working with an existing CloudFront setup, verify cache-tag support is enabled in your distribution and that the header name you're emitting matches what the distribution is configured to read.

### Other CDNs

- **Bunny CDN** uses `CDN-Tag` header
- **Netlify** uses `Cache-Tag`
- **KeyCDN** uses `Cache-Tag` (purge by tag)
- **Varnish** (self-hosted) uses `xkey` module with `Surrogate-Key` header

The portable approach: emit `Surrogate-Key` (Fastly), `Cache-Tag` (Cloudflare), and whatever you've configured in CloudFront from origin — CDNs ignore headers they don't use, so multi-emission costs nothing. Ports cleanly between vendors if you migrate. A defensive choice is to configure CloudFront to read `Cache-Tag` so the same header works for both Cloudflare and CloudFront:

```http
Surrogate-Key: products product-sku-42
Cache-Tag: products,product-sku-42
```

Note the format difference: Fastly's `Surrogate-Key` is space-separated, Cloudflare's `Cache-Tag` is comma-separated. You need both headers because the parsing differs.

## Cache key composition

The cache key determines whether two requests hit the same cache entry. By default, the key is roughly `(method, host, path, query string, varying request headers)`.

### Tuning the cache key

Most CDNs let you customize the cache key:

- **Drop query string** — `?utm_source=...` shouldn't fragment the cache. Most CDNs support "ignore query string" or a query-string allowlist.
- **Include request body for POSTs** — usually a bad idea for caching; POSTs aren't typically cached. If you do cache POSTs (GraphQL queries), cache key must include body.
- **Include specific headers** — extending what `Vary` does. Useful for headers that affect response but aren't HTTP-standard.

### The `Vary` header in practice

`Vary` tells the CDN which request headers contribute to the cache key. The most common values:

- `Vary: Accept-Encoding` — cache gzipped and uncompressed separately. Almost always needed.
- `Vary: Accept-Language` — separate cache entries per language. Needed for content-negotiated responses.
- `Vary: Accept` — separate per content type (JSON vs HTML). Needed when the same URL serves multiple representations.
- `Vary: Cookie` or `Vary: Authorization` — separate per auth state. **Use carefully** — see authentication section below.

Avoid `Vary: User-Agent` — there are millions of unique User-Agent strings, fragmenting the cache to uselessness. Use UA detection at origin (or a smaller `Vary` substitute like `Sec-CH-UA-Mobile`).

## Authentication and shared caching

The single most damaging caching bug: caching authenticated responses at the CDN. User A logs in, makes a request; their personalized response gets cached; user B hits the same URL and sees user A's data. Privacy violation, security incident.

Defensive layers:

**Layer 1: Use `Cache-Control: private` on every authenticated response.** This is the contract — `private` tells shared caches "don't cache". Most CDNs respect it; some default-deny `private` responses, but verify the CDN's behavior.

**Layer 2: At the CDN, have a rule that forces `private` on authenticated requests.** Don't rely solely on origin getting `Cache-Control` right; have a CDN-level guard. In Cloudflare, this is a Page Rule "Bypass Cache on Cookie". In Fastly, it's a VCL snippet checking `req.http.Authorization` or specific cookies.

**Layer 3: For genuinely public-but-personalized responses** (e.g., showing logged-in user name in a marketing page), use a different mechanism. Edge Workers/Functions that personalize at the edge after fetching a generic cached response. Don't try to cache personalized responses keyed on the user.

The general rule: **any response that includes the user's name, email, balance, or similar must have `Cache-Control: private`** (or `no-store`). The CDN should never see it as cacheable.

### When you actually want public caching with auth

There's a legitimate case: public content that's only visible to authenticated users (e.g., a paid catalog). The content is the same for all paying users; the auth check is just gating access.

Two approaches:

1. **Authenticate at the edge.** CDN validates the auth token (typically a JWT signed by your service), and if valid, serves the cached public response. The cache key doesn't include the user. Requires CDN that supports edge auth (Cloudflare Workers, Fastly Compute@Edge, CloudFront Functions).

2. **Two-tier caching.** A public-cacheable origin response, then origin (or an edge-side handler) does the auth check and either serves the cached response or returns 401. The cache key is still public.

For most APIs, this is overkill. Default to Layer 1+2 above.

## Negative caching

### What "heuristically cacheable" means

Per RFC 9111 §4.2.2 and RFC 9110 §9.1, the following status codes are **heuristically cacheable by default** — meaning a cache may store and reuse them even without an explicit `Cache-Control` header:

```
200, 203, 204, 206, 300, 301, 308, 404, 405, 410, 414, 501
```

The implication: if you don't send explicit `Cache-Control` on these responses, the cache picks a heuristic freshness window (typically 10% of the time since `Last-Modified`, capped at some implementation-specific limit). For a freshly-deployed 404 endpoint with no `Last-Modified`, the heuristic might be hours.

This is why "we deleted the resource and it's still cached" happens — a 404 without explicit `Cache-Control` got heuristically cached for an unexpectedly long time.

The fix: **set explicit `Cache-Control` on every response, including error responses.** Don't rely on heuristics.

### Per-status-code recommendations

- **`404 Not Found`**: usually cache briefly. A request for `/v1/products/non-existent-id` should return 404 fast on retries. Cache for 1-5 minutes; longer caching causes stale 404s when content gets created.
- **`410 Gone`**: cache aggressively. The resource is permanently gone; long TTL is appropriate. The whole point of 410 (over 404) is to signal "don't bother asking again".
- **`301 Moved Permanently`** / **`308 Permanent Redirect`**: cache long. Permanent redirects are good candidates for `max-age=86400` or longer.
- **`414 URI Too Long`**: heuristically cacheable, but likely indicates a client bug — caching probably doesn't matter.
- **`429 Too Many Requests`**: typically don't cache. The rate limit is per-user and time-dependent; caching defeats the purpose. Use `Cache-Control: no-store` and rely on the `Retry-After` header to tell clients when to retry.
- **`500 Internal Server Error` and other 5xx (`502`, `503`, `504`, `505`)**: **don't cache.** A transient origin error shouldn't propagate to other users. Use `Cache-Control: no-store` on 5xx responses, OR rely on `stale-if-error` on the previous successful response to serve stale instead.
- **`501 Not Implemented`**: heuristically cacheable per RFC 9111 — surprising, but the rationale is that "the server doesn't support this method" is a stable property. Set explicit `Cache-Control` if you want to override.

### The stale-if-error pattern for 5xx

A useful CDN pattern: configure `stale-if-error` widely, then origin can return 5xx without it being cached. The CDN serves the previous successful response (now stale, but better than 500). When origin recovers, fresh responses are cached.

```http
# Successful response (sets up the future stale-if-error window)
HTTP/1.1 200 OK
Cache-Control: public, max-age=300, s-maxage=3600, stale-if-error=86400
ETag: "..."

# Later: origin is broken, returns 500
HTTP/1.1 500 Internal Server Error
Cache-Control: no-store

# CDN: don't cache the 500. If a previous successful response is within the stale-if-error
# window, serve that instead. Otherwise, propagate the 500 to the client.
```

Some CDNs also support `Cache-Control: no-cache` on 4xx responses to force revalidation while still allowing the response to be stored — useful for not-yet-existent resources where you expect quick churn.

## CORS preflight caching

The trap: a misconfigured CORS preflight response gets cached, masking the real CORS bug for hours.

```http
# Preflight request from browser
OPTIONS /v1/widgets HTTP/1.1
Origin: https://app.example.com
Access-Control-Request-Method: PATCH
Access-Control-Request-Headers: if-match, idempotency-key

# Preflight response
HTTP/1.1 204 No Content
Access-Control-Allow-Origin: https://app.example.com
Access-Control-Allow-Methods: GET, POST, PATCH, DELETE
Access-Control-Allow-Headers: if-match, idempotency-key, content-type, authorization
Access-Control-Max-Age: 86400
```

`Access-Control-Max-Age: 86400` tells the browser to cache the preflight response for 24 hours. If the response is wrong (missing a header in `Allow-Headers`, missing a method), the bug is cached for a day per browser.

**Recommendation during development**: use `Access-Control-Max-Age: 60` or even 0. Short cache means CORS bugs surface immediately.

**In production**: 86400 (24 hours) is a reasonable upper bound. The browser-level cache for preflight is per-browser, not shared, so a misconfigured preflight only hurts users who hit it before you fixed it.

CDN caching of preflight responses adds another layer. If your CDN caches OPTIONS, ensure the cache key includes `Origin`, `Access-Control-Request-Method`, and `Access-Control-Request-Headers` — or disable CDN caching of OPTIONS entirely.

## Origin shielding

Most major CDNs support "origin shield" — a single edge POP that funnels all cache misses to origin. Without shield, every regional POP hits origin independently on cache miss. With shield, only the shield POP hits origin; other POPs fetch from the shield.

For a popular endpoint: 100 regional POPs × 1 cache miss each = 100 origin requests. With shield: 1 shield POP × 1 origin request, 99 cache hits at the shield level.

Worth enabling for any high-traffic origin. Configuration is per-CDN (Fastly: shield POP selection; Cloudflare: Argo Tiered Cache; CloudFront: origin shield setting).

## Common implementation pitfalls

**1. Forgetting `Vary: Accept-Encoding`.** CDN caches a gzipped response, then a client without `Accept-Encoding: gzip` gets the gzipped bytes and can't decompress them. Almost universal in older configurations.

**2. Setting `Cache-Control: public` on authenticated responses.** Disaster mode. CDN caches user A's data and serves it to user B. Audit every authenticated endpoint to ensure `private` (or `no-store`).

**3. No invalidation strategy.** Setting long TTLs without surrogate keys means content updates take hours to propagate. Either set short TTLs (sacrificing cache hit rate) or invest in surrogate-key infrastructure.

**4. Cache key includes `Authorization`.** Every authenticated request has a unique token, fragmenting the cache to one entry per user. Either don't cache authenticated requests, or strip auth before caching, or use edge auth (validate at edge, cache without auth in key).

**5. Caching POST/PATCH responses.** Almost always wrong. POSTs change state; caching responses means subsequent POSTs see stale state. The few exceptions (GraphQL POST queries treated as GETs) require explicit configuration.

**6. Surrogate-Key header too long.** Cloudflare's 16KB limit, Fastly's per-key constraints. Pages with hundreds of related items can hit the limit. Group keys hierarchically (`tenant-X` instead of listing every item under tenant X).

**7. Forgetting to purge during deploys.** Static asset URLs should be hash-named (`/assets/main.abc123.js`) so they self-invalidate; HTML/API responses need explicit purging at deploy time. Have a deploy step that purges relevant tags.

**8. Caching responses with `Set-Cookie`.** Some CDNs default-skip caching when `Set-Cookie` is present (Fastly, Cloudflare); others don't (older configurations). The safe rule: don't set cookies on cacheable responses. If you must, ensure the CDN excludes them.

**9. CORS `Access-Control-Max-Age` too long during development.** Bugs cached for hours. Keep short until production.

## Sources

- RFC 9111 — HTTP Caching
- RFC 5861 — `stale-while-revalidate` and `stale-if-error`
- Fastly `Surrogate-Key` — docs.fastly.com/en/guides/getting-started-with-surrogate-keys
- Cloudflare `Cache-Tag` — developers.cloudflare.com/cache/how-to/purge-cache/purge-by-tags
- CloudFront cache tags (April 2026) — aws.amazon.com/about-aws/whats-new/2026/04/cloudfront-invalidation-cache-tag
- W3C CORS / Fetch standard — fetch.spec.whatwg.org
