---
name: api-conditional-requests
description: Apply HTTP conditional requests (ETag, If-Match, If-None-Match, Last-Modified) when designing or reviewing REST APIs. Use this skill whenever the task involves cache validation, the lost-update problem, optimistic concurrency control, 304 Not Modified responses, 412 Precondition Failed, designing PUT/PATCH endpoints where two clients might race, atomic create-if-absent semantics, or any state-machine API where multiple actors can transition the same resource. Trigger broadly — concurrent edits, version mismatches, "what if two users save at the same time", "how do I cache this efficiently", and any review of a stateful resource API should use this skill, even when the user does not say "ETag" or "conditional request". Composes with api-idempotency (which handles retry safety) and api-error-responses (for the 412/428 problem types).
---

# REST API Conditional Requests

A skill for using HTTP conditional requests (RFC 9110 §13) to solve two distinct problems: cache validation and concurrency control.

## Why this skill exists

Two real problems live under "conditional requests", and conflating them produces broken designs:

1. **Cache validation (efficiency).** A client has a cached copy of a GET response. Before re-fetching the full body, ask the server "is my cached copy still current?" If yes, get a small `304 Not Modified` instead of the full payload. Saves bandwidth, latency, and server CPU.

2. **Concurrency control (correctness).** A client wants to update a resource. Before applying the write, prove to the server "I'm updating the version I last saw, not blindly overwriting whatever's there now." If another client wrote in between, fail loudly with `412 Precondition Failed` instead of silently clobbering their change. This is the **lost-update problem**, and conditional requests are the standard solution.

These use *related* mechanisms (ETags, conditional headers) but they are different problems. The headers, status codes, and design rules differ. Treat them as separate concerns.

## Step 1 — Identify which problem you're solving

Before you reach for any header, decide which problem this endpoint has:

- **GET-heavy, repeated polling, large response bodies, mobile/edge clients** → cache validation. Use `If-None-Match` + `304 Not Modified`.
- **PUT/PATCH/DELETE on a resource that multiple clients can modify** → concurrency control. Use `If-Match` + `412 Precondition Failed`.
- **Both** → use both. They compose cleanly. Send ETags on GET responses; require them on writes.
- **Atomic "create only if absent" on PUT** → `If-None-Match: *`. Special case of concurrency control where the precondition is "this resource doesn't exist yet".

If you can't articulate which of these the endpoint needs, don't add conditional requests yet — clarify the use case first.

## Step 2 — Generate ETags

The ETag is the validator. Two questions to settle for every resource:

### Strong vs weak

- **Strong ETag** (e.g., `"abc123"`): byte-for-byte identity. Two responses with the same strong ETag have identical bodies.
- **Weak ETag** (e.g., `W/"abc123"`): semantic equivalence. Two responses with the same weak ETag represent the same logical state but may differ in encoding, whitespace, or non-meaningful fields.

**Use strong ETags by default.** They work for both cache validation and concurrency control. Weak ETags work for cache validation but `If-Match` (concurrency control) requires strong comparison — weak ETags will never match.

Use weak ETags only when you cannot reasonably produce a strong one — typically because the response is dynamic (timestamps, ad slots, generated IDs in HTML) but the underlying logical state hasn't changed.

### Generation strategy

Three real-world patterns:

1. **Content hash** (e.g., `SHA-256("{...response body...}")[:16]` quoted): always correct, but requires materializing the response before computing. Acceptable cost for most APIs.
2. **Version counter**: an integer column on the resource, incremented on every write. ETag is the version, e.g., `"v17"`. Cheap, but only works if every meaningful change increments the counter — partial updates that miss the increment cause silent bugs.
3. **Hybrid**: hash of `(version, updated_at, content_type)`. Common in production: covers content negotiation, encoding changes, and version bumps in one validator.

Detail in `references/etag-generation.md`, including: representation-specific ETags (one resource, two media types = two ETags), the multi-representation problem (recent IETF draft "Agentic State Transfer" addresses this for AI-agent scenarios), and Last-Modified as a fallback.

## Step 3 — Cache validation flow (GET / HEAD)

The pattern:

1. **Server, on the original GET response**: include `ETag` header on every cacheable representation.
2. **Client, on subsequent requests**: include `If-None-Match: <stored-etag>`.
3. **Server, evaluating the condition**:
   - If the current ETag matches any value in `If-None-Match` → return `304 Not Modified` with **no body** but with: `Cache-Control`, `Content-Location`, `Date`, `ETag`, `Expires`, `Vary` (per RFC 9110 §15.4.5). The 304 response refreshes the client's cache freshness; the client reuses its body.
   - If no match → return the full `200 OK` with the new body and a fresh `ETag`.

Worked example:

```http
# Original request
GET /v1/orders/123 HTTP/1.1

HTTP/1.1 200 OK
ETag: "v17"
Cache-Control: max-age=60
Content-Type: application/json

{"id": 123, "status": "shipped", ...}

# Later: client has the response cached, wants to revalidate
GET /v1/orders/123 HTTP/1.1
If-None-Match: "v17"

# Resource hasn't changed
HTTP/1.1 304 Not Modified
ETag: "v17"
Cache-Control: max-age=60

# (no body)

# Or: resource has changed
HTTP/1.1 200 OK
ETag: "v18"
Cache-Control: max-age=60
Content-Type: application/json

{"id": 123, "status": "delivered", ...}
```

Server-side rule: **`If-None-Match` uses weak comparison** — both strong and weak ETags can match. This is correct for cache validation; the cache only cares about logical equivalence.

For the integration with `Cache-Control`, CDN/edge cache behavior, and the `Vary` header for content negotiation, see `references/caching-flow.md`.

## Step 4 — Concurrency control flow (PUT / PATCH / DELETE)

The pattern (often called optimistic concurrency control or optimistic locking):

1. **Client GETs the resource first**, receiving the current ETag.
2. **Client modifies locally** and submits the write with `If-Match: <etag-from-step-1>`.
3. **Server, evaluating the condition**:
   - If the current ETag matches → apply the write, return `200/204` with the new ETag.
   - If no match → return `412 Precondition Failed`. The resource was modified by someone else after step 1; the client must re-fetch and retry.
   - If `If-Match` was missing entirely and the endpoint requires it → return `428 Precondition Required` with a Problem Details body explaining what the client should send.

Worked example — order status transitioning, two terminals racing to mark "shipped":

```http
# Terminal A reads
GET /v1/orders/123 HTTP/1.1

HTTP/1.1 200 OK
ETag: "v17"
{"id": 123, "status": "ready", ...}

# Terminal B reads (concurrently)
GET /v1/orders/123 HTTP/1.1

HTTP/1.1 200 OK
ETag: "v17"
{"id": 123, "status": "ready", ...}

# Terminal A writes first
PATCH /v1/orders/123 HTTP/1.1
If-Match: "v17"
Content-Type: application/merge-patch+json

{"status": "shipped"}

HTTP/1.1 200 OK
ETag: "v18"
{"id": 123, "status": "shipped", ...}

# Terminal B writes — gets rejected
PATCH /v1/orders/123 HTTP/1.1
If-Match: "v17"
Content-Type: application/merge-patch+json

{"status": "cancelled"}

HTTP/1.1 412 Precondition Failed
ETag: "v18"
Content-Type: application/problem+json

{
  "type": "https://api.example.com/problems/version-conflict",
  "title": "Resource was modified",
  "status": 412,
  "detail": "The order was modified by another client. Re-fetch and retry.",
  "current_etag": "v18"
}
```

Terminal B's correct behavior on 412: re-fetch (gets the new state — order is already shipped), decide whether the operation still makes sense (it doesn't — you can't cancel a shipped order), and either retry with the new ETag or surface the conflict to the user.

Server-side rule: **`If-Match` uses strong comparison** (RFC 9110 §13.1.1). Weak ETags will never match an `If-Match` header. This is intentional — concurrency control needs byte-level identity, not just semantic equivalence.

### Atomic create-if-absent

A useful related pattern — the create that fails if the resource already exists:

```http
PUT /v1/orders/abc-123 HTTP/1.1
If-None-Match: *
Content-Type: application/json

{"status": "draft", ...}

# If the resource didn't exist:
HTTP/1.1 201 Created
ETag: "v1"

# If it did:
HTTP/1.1 412 Precondition Failed
```

Useful when the client controls the resource ID (a UUID, a slug, an external reference) and wants to create-or-fail rather than create-or-update. The wildcard `*` matches "any current representation" — so the precondition fails if anything exists at the URL.

For implementation patterns including the database-level uniqueness coordination, see `references/concurrency-control.md`.

## Step 5 — Require `If-Match` deliberately

By default, RFC 9110 lets servers ignore missing `If-Match` and process the write unconditionally. This is permissive — convenient for clients, dangerous for state-critical resources.

For resources where lost updates would be a real problem (financial transactions, state machines, anything regulatory), be explicit:

- **Document `If-Match` as required** for the endpoint.
- **Reject missing-precondition writes** with `428 Precondition Required` (RFC 6585) and a Problem Details body explaining what to send.
- **Don't fall back to "first writer wins"** — that's exactly the silent overwrite you're trying to prevent.

```http
PATCH /v1/transfers/abc HTTP/1.1
Content-Type: application/json

{"amount": 5000}

# Server requires If-Match for state-changing operations
HTTP/1.1 428 Precondition Required
Content-Type: application/problem+json

{
  "type": "https://api.example.com/problems/precondition-required",
  "title": "If-Match header is required",
  "status": 428,
  "detail": "PATCH /v1/transfers/{id} requires an If-Match header containing the resource's current ETag.",
  "instance": "/v1/transfers/abc"
}
```

This is the recommended posture for any API that has multi-actor write contention. The 428 trains client implementations to do the right thing (always send `If-Match`) rather than discovering the requirement only when they happen to lose a race.

## Step 6 — Compose with idempotency and error responses

These three skills (this one, `api-idempotency`, `api-error-responses`) cover overlapping but distinct concerns. They compose:

| Concern | Mechanism | Skill |
|---------|-----------|-------|
| "I retried because the network timed out — don't double-charge me" | `Idempotency-Key` header + server-side dedupe | `api-idempotency` |
| "Two users edited the same record — don't let one silently overwrite the other" | `If-Match` + `412` | this one |
| "Format the 412/428 response so the client can act on it" | RFC 9457 Problem Details | `api-error-responses` |

A well-designed write endpoint on a contested resource uses **all three together**:

```http
PATCH /v1/orders/123 HTTP/1.1
Idempotency-Key: 9f86d081-...
If-Match: "v17"
Content-Type: application/merge-patch+json

{"status": "shipped"}
```

`Idempotency-Key` makes the write safe to retry. `If-Match` makes it safe to write at all. The Problem Details body on any error makes the failure machine-actionable.

For the worked end-to-end example (a queue ticket transitioning through states with multiple terminals racing) and the implementation patterns for combining all three, see `references/composition.md`.

## Output style

When applying this skill, produce:

- For **endpoint design**: literal HTTP request/response pairs for the success path and each error case (304, 412, 428). Include exact headers, not prose.
- For **review**: numbered findings tagged `[block]`/`[fix]`/`[nit]`. Common findings: "no ETag on GET response", "PUT accepts unconditional writes", "Last-Modified used where ETag would be safer", "412 returned without ETag header so client can't see new state".
- For **OpenAPI**: schema fragments for the response headers and 304/412/428 response definitions, ready to paste.

Cite RFC 9110 §13 for conditional requests semantics, §15.4.5 for 304 specifics, §15.5.13 for 412, RFC 6585 for 428.

## When to dig into the references

- **ETag generation strategies, strong vs weak, multi-representation handling** → `references/etag-generation.md`
- **Caching-specific concerns: 304 response headers, Cache-Control composition, CDN integration** → `references/caching-flow.md`
- **Concurrency-specific concerns: 412 handling, 428 pattern, lost-update prevention end-to-end, database coordination** → `references/concurrency-control.md`
- **Common mistakes in real APIs** → `references/anti-patterns.md`
- **Composing with idempotency keys and Problem Details, with full worked example** → `references/composition.md`
