# HTTP Caching Primitives — ETag, Cache-Control, Vary, Validators

The shared substrate for all three caching paths. Every path uses these mechanisms; the design decisions diverge later.

## ETags

### What an ETag is

Per RFC 9110 §8.8.3, an ETag is an opaque string in double quotes, optionally prefixed with `W/` for weak validators:

```
ETag: "abc123xyz"        ← strong
ETag: W/"abc123xyz"      ← weak
```

The value is opaque to clients — they only do equality comparison, never parse the contents. The double quotes are part of the syntax, not optional. 64 bits of entropy is usually plenty; 128 is bulletproof.

### Strong vs weak

- **Strong validator** guarantees byte-identical bodies when ETags match. Required for `If-Match` (concurrency) and `If-Range` (range requests).
- **Weak validator** guarantees semantic equivalence — same logical state, possibly different bytes. Acceptable for `If-None-Match` (revalidation) but not for `If-Match`.

The comparison rules differ by header:

| Header | Comparison | Strong ETags work? | Weak ETags work? |
|--------|------------|--------------------|--------------------|
| `If-None-Match` (cache validation) | Weak | Yes | Yes |
| `If-Match` (concurrency control) | **Strong** | Yes | **No** |
| `If-Range` (range requests) | Strong | Yes | No |

The `If-Match` rule is the trap. A weak ETag will never match an `If-Match` header — the server treats it as a precondition failure and returns 412. If you generate weak ETags and clients use `If-Match`, every write fails. Production bug, easy to ship.

**Default to strong ETags.** Only use weak when:
- You cannot reasonably produce a strong one (response includes timestamps, ad slots, generated IDs in HTML)
- AND you'll never use the resource for concurrency control

### Generation strategies

**1. Content hash (recommended default):**

```python
import hashlib

def etag_for(response_body: bytes) -> str:
    digest = hashlib.sha256(response_body).hexdigest()[:16]
    return f'"{digest}"'
```

Always correct. Strong validator by construction. Cost: requires materializing the response body before hashing. Acceptable for most APIs.

**2. Version counter:**

```sql
ALTER TABLE orders ADD COLUMN version INTEGER NOT NULL DEFAULT 0;
-- On every meaningful write:
UPDATE orders SET status = 'shipped', version = version + 1 WHERE id = 123;
```

```python
def etag_for(order) -> str:
    return f'"{order.version}"'
```

Cheap (no hashing). Naturally serves as the optimistic concurrency value. Limitation: only works if EVERY write path increments the version. Migrations that backfill data, debug endpoints that update flags, admin tools that bypass the ORM — any of these silently break the contract. Audit ruthlessly.

**3. Hybrid (version + content discriminator):**

```python
def etag_for(order, request) -> str:
    components = [
        str(order.version),
        request.headers.get("Accept", ""),
        request.headers.get("Accept-Language", ""),
    ]
    digest = hashlib.sha256("|".join(components).encode()).hexdigest()[:16]
    return f'"{digest}"'
```

Handles content negotiation correctly. Pair with a `Vary` response header listing the same components.

### Multi-representation

A single resource often has multiple representations: HTML and JSON, English and Arabic, gzipped and plain. Three approaches:

1. **Per-representation strong ETags** (recommended default). Each representation gets its own ETag. Pair with `Vary` so caches respect the negotiation.
2. **Shared weak ETag.** Same ETag across all representations, marked weak. Simpler, but breaks `If-Match` (can't use for concurrency control).
3. **Canonical-state ETag** (`draft-jurkovikj-httpapi-agentic-state-00`, late 2025). Derive ETag from the underlying canonical state, not the bytes. Useful when AI agents and human clients share a resource via different representations.

For most APIs, option 1 is the right default.

### Last-Modified — the fallback

When you genuinely cannot generate an ETag (legacy systems, stream-only responses), use `Last-Modified` + `If-Modified-Since` / `If-Unmodified-Since`:

```http
HTTP/1.1 200 OK
Last-Modified: Mon, 03 May 2026 14:32:00 GMT
```

Limitations:
- One-second resolution. Two writes within the same second produce the same `Last-Modified`. Concurrent edits at high frequency are silently lost.
- Date arithmetic varies by client and proxy. Timezone bugs are common.

Use ETag where possible. If you must use `Last-Modified`, also send an ETag — clients prefer ETags when both are present (RFC 9110 §13.2.2).

### Implementation tips

**Compute the ETag at the response-construction layer**, not the database layer. The response may include computed fields (joined data, derived state) that the database doesn't know about. An ETag based on raw DB rows can mismatch the actual response bytes.

```python
# Bad: ETag from DB row only
order = db.get_order(id)
etag = etag_for_db_row(order)
return jsonify(order, headers={"ETag": etag})

# Good: ETag from the actual response
order = db.get_order(id)
related = db.get_related(order.id)
response_body = serialize(order, related, request.user)
etag = etag_for(response_body)
return Response(response_body, headers={"ETag": etag})
```

For high-traffic GET endpoints, compute the ETag from the version (if you trust your version counter) and short-circuit before serializing — `If-None-Match: "v17"` matches → return 304 without ever building the response body.

## Cache-Control directives

The full grammar of `Cache-Control` is more nuanced than most engineers realize. The directives that matter:

### Visibility directives

- **`public`** — explicit permission for shared caches (CDN, corporate proxy) to cache. Required for edge caching.
- **`private`** — only browsers/private clients may cache. Mandatory for per-user data. Without `private`, intermediate caches may treat the response as cacheable and serve user A's data to user B.

### Freshness directives

- **`max-age=N`** — fresh for N seconds. Applies to all caches unless overridden.
- **`s-maxage=N`** — fresh for N seconds in shared caches only. Lets you set a longer CDN cache than browser cache.
- **`no-cache`** — must revalidate with origin before serving. Note: `no-cache` does NOT mean "don't cache" — it means "cache, but always check first". The misnaming is the source of much confusion.
- **`no-store`** — actually do not cache. Different from `no-cache`. Use for sensitive data that must not be persisted.
- **`must-revalidate`** — once stale, must revalidate; cannot serve stale.
- **`proxy-revalidate`** — same but applies only to shared caches.

### Stale content directives (RFC 5861)

- **`stale-while-revalidate=N`** — for N seconds after expiry, cache may serve stale content while fetching fresh in the background. Reduces tail latency.
- **`stale-if-error=N`** — for N seconds after expiry, if origin is unreachable, cache may serve stale. Resilience during outages.

### Other directives

- **`immutable`** — content will never change for the lifetime of the cache entry; no need to revalidate even on user reload. Used for asset URLs with hash-based names (`/static/abc123def.js`).
- **`no-transform`** — proxies must not modify the response (gzip recompression, image optimization).

### `Surrogate-Control` — the proxy-only sibling

`Surrogate-Control` is a separate header, conceptually parallel to `Cache-Control`, that targets only intermediate proxies/CDNs. Browsers ignore it; CDNs that support it (Fastly, Akamai, others) consume it and **strip it from the response** before forwarding to the client.

The use case: send different caching directives to the CDN vs. the browser on the same response. Common pattern — a private API response that the CDN can cache (because authentication is validated at the edge or because the response is genuinely shared) but that browsers should not cache:

```http
HTTP/1.1 200 OK
Cache-Control: private, no-cache
Surrogate-Control: max-age=300
ETag: "..."
```

Decoded:
- **Browser sees** `Cache-Control: private, no-cache` → revalidate every time, don't share.
- **CDN sees** `Surrogate-Control: max-age=300` → cache for 5 minutes, serve to subsequent matching requests without going to origin.
- **CDN strips `Surrogate-Control` before forwarding** → the browser never sees it.

When `Cache-Control` and `Surrogate-Control` both apply, CDNs that support `Surrogate-Control` honor it for their own caching decisions; they pass `Cache-Control` through unchanged for downstream clients. CDNs that don't support `Surrogate-Control` (Cloudflare, CloudFront historically) ignore it — the response then falls back to `Cache-Control` semantics.

`Surrogate-Control` accepts the same directive grammar as `Cache-Control` plus an optional `content="..."` parameter for vendor-specific extensions. See vendor docs for which directives each CDN respects.

This isn't strictly part of RFC 9111 — it originated in the W3C Edge Architecture Specification (2001) and has been adopted as a de-facto standard by some CDN vendors. Use when you have a CDN that supports it; otherwise, fall back to `Cache-Control` with `private` + `s-maxage` to achieve a similar effect (browsers respect `private` and ignore `s-maxage`; CDNs respect both — though shared caches need `public` or specific other directives to cache `Authorization`-bearing responses, see RFC 9111 §3.5).

### Common combinations

| Use case | Cache-Control | Notes |
|----------|---------------|-------|
| Static asset with hashed URL | `public, max-age=31536000, immutable` | Cache forever; never revalidate |
| Public API response, fresh-ish | `public, max-age=300, s-maxage=3600, stale-while-revalidate=86400` | Browser fresh 5min; CDN fresh 1hr; serve stale up to 1 day |
| Private user data, always revalidate | `private, no-cache` | API default for user-specific data |
| Auth-bearing response, never cache | `private, no-store` | Sensitive data path |
| Public content with fast invalidation | `public, max-age=60, s-maxage=86400` | Browsers re-fetch quickly; CDN keeps long, you control via purge |
| Cache at edge but not in browsers | `Cache-Control: private, no-cache` + `Surrogate-Control: max-age=300` | CDN caches; browser revalidates; only on Fastly/Akamai |

`private, no-cache` + `ETag` is the sweet spot for most authenticated API endpoints — clients always validate, but the validation is cheap when the resource hasn't changed.

**Note on `max-age=0, must-revalidate`**: you'll see this combination in older guidance and in deployed configs. It's semantically equivalent to `no-cache` (both force revalidation before reuse). It originated as a workaround for HTTP/1.0 caches that didn't support `no-cache`. By 2026, those caches are essentially extinct, and `no-cache` is the modern idiom — cleaner and more direct. If you encounter `max-age=0, must-revalidate` in existing code, leave it alone (it works); for new code, prefer `no-cache`.

### The "API default" recipe

For a typical authenticated REST API endpoint:

```http
Cache-Control: private, no-cache
ETag: "..."
```

Translation: clients always revalidate with `If-None-Match`; the validation is cheap when the resource hasn't changed (304 with no body); intermediate caches won't try to share the response across users.

This is the safe default. Override only when you have a specific reason (response is genuinely public, response is genuinely sensitive, etc.).

## Vary

`Vary` declares which request headers affect the response. Critical for content-negotiated APIs and for any response that varies by authentication state.

```http
HTTP/1.1 200 OK
ETag: "v17-en-json"
Vary: Accept, Accept-Language, Accept-Encoding
```

Translation: "this representation depends on Accept, Accept-Language, and Accept-Encoding. Caches must keep separate cached copies per combination of those headers."

### When `Vary` is mandatory

- Response body varies by `Accept-Language` (or any other `Accept-*` header)
- Response varies by authentication state (e.g., shows logged-in vs anonymous content) — `Vary: Cookie` or `Vary: Authorization`
- Response is gzipped vs uncompressed based on `Accept-Encoding`
- Response shape changes based on a custom request header

### Without `Vary`

An intermediate cache may serve the wrong representation. Cache stores English version → French speaker hits the same URL with `Accept-Language: fr` → without `Vary: Accept-Language`, the cache serves English.

Without `Vary: Cookie` or `Vary: Authorization` on auth-varying endpoints: a logged-in user's response gets cached, then served to anonymous users (or worse, to other logged-in users). This is one of the most damaging caching bugs.

### The `Vary: *` trap

`Vary: *` means "this response varies on something the cache cannot identify". Effectively disables caching by shared caches.

Sometimes used (e.g., by frameworks that don't know what the response varies on) as a defensive measure. But it's a sledgehammer — you lose all CDN caching benefit. Only use deliberately, and prefer being specific about what the response varies on.

### Vary and ETag interact

The cache key for shared caches is `(URL, varied request headers)`. The ETag identifies the response within that key. Two clients with different `Accept-Language` get different cache entries; revalidation works independently for each.

This means your ETag generation should incorporate the same components listed in `Vary`. If `Vary: Accept-Language` and your ETag doesn't account for language, an `If-None-Match` revalidation might return 304 for the wrong language. Use the hybrid ETag generation pattern to keep them in sync.

## Validators precedence rules

When both `ETag` and `Last-Modified` are present:

- **Client sending conditional request**: prefer `If-None-Match` over `If-Modified-Since`. ETag-based validation is more reliable (RFC 9110 §13.2.2).
- **Server evaluating**: per RFC 9110 §13.2, evaluate `If-Match` before `If-Unmodified-Since`, and `If-None-Match` before `If-Modified-Since`. The ETag check wins.
- **If both `If-Match` and `If-Unmodified-Since` are present**: `If-Match` is evaluated; `If-Unmodified-Since` is ignored.

In practice, send both `ETag` and `Last-Modified` on responses (cheap), but design clients and middleware around `ETag` as the primary validator.

## Sources

- RFC 9110 §8.8.3 — ETag field
- RFC 9110 §13 — Conditional Requests
- RFC 9111 — HTTP Caching (the companion spec to RFC 9110)
- RFC 5861 — `stale-while-revalidate` and `stale-if-error` Cache-Control extensions
- RFC 9111 §5.2 — Cache-Control directive grammar
- IETF draft-jurkovikj-httpapi-agentic-state-00 (Dec 2025) — Canonical-state ETags for multi-representation
