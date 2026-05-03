# Cache Validation Flow — 304 Responses, Cache-Control, CDN Integration

The full design for using conditional requests as a cache-efficiency mechanism. This is the GET/HEAD side; the write side is in `concurrency-control.md`.

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
Cache-Control: private, max-age=300, must-revalidate
Vary: Accept-Language, Accept-Encoding
Content-Type: application/json
Content-Length: 2341

{"sku": "sku-42", "name": "Widget", ...}
```

The client now caches the response with a freshness window of 5 minutes (`max-age=300`).

### During the freshness window

The client uses the cached response without contacting the server. Zero network traffic. This is the most efficient path; conditional requests don't even come into play.

### After freshness expires (revalidation)

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
Cache-Control: private, max-age=300, must-revalidate
Vary: Accept-Language, Accept-Encoding

(no body)
```

The client extends its freshness window. No body transmitted. Server CPU saved.

If it has changed:

```http
HTTP/1.1 200 OK
Date: Sun, 03 May 2026 14:05:30 GMT
ETag: "v18-en-json"
Cache-Control: private, max-age=300, must-revalidate
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

## Cache-Control composition

`ETag` and `Cache-Control` work together. The mental model:

- `Cache-Control: max-age=N` defines the **freshness window** during which the cached response can be used without contacting the server.
- `ETag` defines the **revalidation token** for use after the freshness window expires.

Common patterns:

| Use case | Cache-Control | Behavior |
|----------|---------------|----------|
| Static-ish resource, frequent reads | `public, max-age=3600, stale-while-revalidate=86400` | Use cache for 1 hour without contact, then revalidate, but serve stale up to 1 day if origin is slow |
| Dynamic but cheap-to-revalidate | `private, max-age=0, must-revalidate` | Always revalidate; rely on ETag for the optimization |
| User-specific data | `private, no-cache` | Force revalidation on every request; never serve stale |
| Truly static, immutable | `public, max-age=31536000, immutable` | Cache forever; never revalidate |
| Sensitive, must not cache | `no-store` | Don't cache at all; ETag irrelevant |

`max-age=0` + `must-revalidate` + `ETag` is the sweet spot for most API endpoints — clients always validate, but the validation is cheap when the resource hasn't changed.

`stale-while-revalidate` is underused and worth knowing: it lets clients use stale content briefly while revalidating in the background. Reduces tail latency without sacrificing freshness.

## The Vary header

`Vary` declares which request headers affect the response. Critical for content-negotiated APIs:

```http
HTTP/1.1 200 OK
ETag: "v17-en-json"
Vary: Accept, Accept-Language, Accept-Encoding
```

Translation: "this representation depends on Accept, Accept-Language, and Accept-Encoding. Caches must keep separate cached copies per combination of those headers."

Without `Vary`, an intermediate cache may serve the wrong representation. Example: cache stores the English version, then a French speaker hits the same URL with `Accept-Language: fr` — without `Vary: Accept-Language`, the cache serves the English version.

Rule: include in `Vary` every request header that influences the response body OR the ETag computation. If your hybrid ETag includes `Accept-Language`, then `Vary: Accept-Language` is non-negotiable.

## Multiple ETags in If-None-Match

A client can list multiple ETags it's willing to accept:

```http
GET /v1/products/sku-42 HTTP/1.1
If-None-Match: "v15-en-json", "v16-en-json", "v17-en-json"
```

The server returns 304 if any of them match. Useful for clients that have multiple cached versions (e.g., separate caches per stale-while-revalidate level).

In practice, most clients only send one ETag. Worth supporting on the server side, but not critical.

## CDN and edge cache integration

When you put a CDN (CloudFront, Fastly, Cloudflare) in front of an API:

- **The CDN respects `Cache-Control` and `Vary`.** A correct origin response automatically benefits from CDN caching. If you've set up Cache-Control properly, the CDN does the right thing.
- **The CDN forwards conditional headers.** `If-None-Match` from the client reaches your origin (potentially as part of a stale-cache revalidation flow that the CDN initiates).
- **The CDN can serve 304s on its own** if it has a fresh cached copy, without contacting origin. This is the biggest win — origin gets zero traffic for content that hasn't changed.
- **Watch out for ETag mangling.** Some CDNs (notably AWS CloudFront) modify ETags when applying compression. If you use ETags for concurrency control, configure the CDN to preserve them or generate ETags that are robust to compression. Many implementations bypass CDN caching entirely for write endpoints.

The general rule: configure ETags and Cache-Control correctly at the origin, and the CDN amplifies the savings.

## Common implementation pitfalls

**1. Forgetting to send ETag on the 304.** The 304 response without an ETag leaves the client guessing what just got revalidated. Always include the matched ETag.

**2. Ignoring If-None-Match for non-2xx responses.** A 404 should not honor If-None-Match — return the 404 normally. RFC 9110 §13.1.2 specifies that conditional headers only apply when the response would otherwise be 2xx or 304.

**3. Not sending ETag on the 200.** Some frameworks default to skipping ETag generation. Verify with a real GET that the response includes an ETag header.

**4. Serializing the response body before checking If-None-Match.** Wasted CPU on cache hits. Compute the ETag from the version (if you can), check against the conditional header, and short-circuit before building the body.

**5. Using strong ETags with bodies that legitimately vary.** If your API includes a `generated_at: "2026-05-03T14:00:01Z"` timestamp in every response, every response gets a different strong ETag, and revalidation never returns 304. Either use a weak ETag, or omit the timestamp from the cache-relevant body.

## Sources

- RFC 9110 §13.1.2 — If-None-Match semantics
- RFC 9110 §15.4.5 — 304 Not Modified
- RFC 9111 — HTTP Caching (the companion spec)
- RFC 5861 — stale-while-revalidate Cache-Control extension
