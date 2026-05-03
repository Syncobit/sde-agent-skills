# ETag Generation — Strategies, Strong vs Weak, Multi-Representation

The validator is the foundation. Get it right and conditional requests work; get it wrong and you have silent bugs that only surface under load or specific user flows.

## What an ETag actually is

Per RFC 9110 §8.8.3, an ETag is an opaque string in double quotes, optionally prefixed with `W/` to indicate a weak validator:

```
ETag: "abc123xyz"           ← strong
ETag: W/"abc123xyz"         ← weak
```

The value is opaque to clients — they only do equality comparison, never parse the contents. The double quotes are part of the syntax, not optional. The maximum useful entropy depends on collision tolerance: 64 bits is usually plenty, 128 bits is bulletproof.

## Strong vs weak — the precise rules

**Strong validator** guarantees that two responses with the same ETag have **byte-identical bodies**. The implication: any change that affects the bytes (whitespace reformatting, field order, encoding) must produce a new ETag.

**Weak validator** guarantees that two responses with the same ETag are **semantically equivalent** — same logical state, possibly different bytes. The server decides what counts as semantic equivalence.

The comparison rules differ by header:

| Header | Comparison | Strong ETags work? | Weak ETags work? |
|--------|------------|--------------------|--------------------|
| `If-None-Match` (cache validation) | Weak | Yes | Yes |
| `If-Match` (concurrency control) | **Strong** | Yes | **No** |
| `If-Range` (range requests) | Strong | Yes | No |

The `If-Match` rule is the trap. A weak ETag will never match an `If-Match` header — the server treats it as a precondition failure and returns 412. If you generate weak ETags and clients use `If-Match`, every write fails. Production bug, easy to ship.

**Default to strong ETags.** Only use weak when the cost of regenerating the ETag on every byte-level change exceeds the value, AND you'll never use the resource for concurrency control.

## Generation strategies in practice

### 1. Content hash

```python
import hashlib
import json

def etag_for(response_body: bytes) -> str:
    digest = hashlib.sha256(response_body).hexdigest()[:16]
    return f'"{digest}"'
```

**Pros**: always correct, unforgeable, works for any content type. Strong validator by construction.

**Cons**: requires materializing the response body before hashing. For streaming responses or expensive-to-generate bodies, this doubles the cost (generate once to hash, generate again to send — though most frameworks let you compute the hash during streaming).

**When to use**: default choice for read-heavy APIs where response bodies are small and bounded (under ~100 KB). Don't try to be clever.

### 2. Version counter

Add a column to your resource:

```sql
ALTER TABLE orders ADD COLUMN version INTEGER NOT NULL DEFAULT 0;
```

Increment on every meaningful write:

```sql
UPDATE orders SET status = 'shipped', version = version + 1 WHERE id = 123;
```

ETag is the version:

```python
def etag_for(order) -> str:
    return f'"{order.version}"'
```

**Pros**: cheap (no hashing), works without materializing the response, naturally serves as the optimistic concurrency value.

**Cons**: only works if EVERY write path increments the version. A migration that backfills data without bumping versions, a debug endpoint that updates a flag, an admin tool that bypasses the ORM — any of these silently break the contract. Audit ruthlessly.

Also: the version is a strong validator only if your serialization is deterministic (same data → same bytes). If field order varies, two clients with the same version may see different bodies. Combine with canonical JSON serialization to be safe.

**When to use**: write-heavy resources with controlled write paths. State-machine entities (orders, queue tickets, accounts) are a natural fit.

### 3. Hybrid — version + content discriminator

When responses depend on negotiated content (Accept, Accept-Encoding, Accept-Language) or include time-varying details:

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

**Pros**: handles content negotiation correctly. A version bump invalidates all representations; a language change produces a different ETag for the same version.

**Cons**: more moving parts; easy to forget a relevant component.

**When to use**: APIs serving multiple representations of the same logical resource (e.g., JSON and XML, or multiple languages). Pair with a `Vary` response header listing the same components.

### 4. Last-Modified — the fallback

When you genuinely cannot generate an ETag (legacy systems, stream-only responses), the alternative is `Last-Modified` + `If-Modified-Since` / `If-Unmodified-Since`. Same pattern, different validator.

```http
HTTP/1.1 200 OK
Last-Modified: Mon, 03 May 2026 14:32:00 GMT
```

**Limitations**:
- One-second resolution. Two writes within the same second produce the same `Last-Modified`. Concurrent edits at high frequency are invisible — silent lost updates.
- Date arithmetic varies by client and proxy. Timezone bugs are common.
- Only useful for content that has a "modification time" concept; doesn't fit synthesized or computed responses.

**Use ETag where possible.** `Last-Modified` is a fallback, not a primary mechanism. If you must use it, also send an ETag — clients prefer ETags when both are present, per RFC 9110 §13.2.2.

## Multi-representation problem

A single resource often has multiple representations: HTML and JSON, English and Arabic, gzipped and plain. Each representation has its own bytes — but they describe the same logical state.

Three approaches:

1. **Per-representation strong ETags.** Each representation gets its own ETag based on its bytes. The cleanest from an HTTP-semantics perspective. Pair with `Vary` so caches respect the negotiation.

2. **Shared weak ETag.** Same ETag across all representations, marked weak. Simpler, but breaks `If-Match` (can't use for concurrency control).

3. **Canonical-state ETag (RFC 9110-compliant pattern).** A new IETF draft (draft-jurkovikj-httpapi-agentic-state, late 2025) formalizes what some implementations already do: derive the ETag from the underlying canonical state, not from the bytes. A change to the canonical state invalidates all representations together. Useful when AI agents and human clients share a resource via different representations.

For most APIs, **option 1 is the right default** — strong ETags per representation, with `Vary` headers. Reserve option 3 for cases where you genuinely need cross-representation concurrency.

## Implementation patterns

### Computing the ETag at the right layer

The ETag is part of the response, not the resource. Generate it at the response-construction layer, not in the database access layer. Why: the response may include computed fields (joined data, derived state, environment info) that the database doesn't know about. An ETag based on raw DB rows can mismatch the actual response bytes.

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

### Caching the ETag

For high-traffic GET endpoints, computing the response body just to discard it on a 304 is wasteful. Two optimizations:

1. **Compute ETag from version, not body.** If you trust your version counter, you can short-circuit before serializing. `If-None-Match: "v17"` matches the current version → return 304 without ever building the response body.

2. **Cache (request-fingerprint → ETag) in a fast store.** Useful when the response is expensive to generate but the ETag is stable. Look up the cached ETag, compare against `If-None-Match`, return 304 on match.

Both optimizations require careful invalidation. Skip them initially; add when you have measured pressure on the endpoint.

## Sources

- RFC 9110 §8.8.3 — ETag field
- RFC 9110 §13 — Conditional Requests
- RFC 9110 §13.1.1 — If-Match (strong comparison required)
- RFC 9110 §13.1.2 — If-None-Match (weak comparison)
- IETF draft-jurkovikj-httpapi-agentic-state-00 (Dec 2025) — Canonical-state ETags for multi-representation
