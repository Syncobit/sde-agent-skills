# Composition with Idempotency and Error Responses

The three API skills (`api-idempotency`, `api-error-responses`, this one) cover overlapping but distinct concerns. This reference shows how to combine them in a single coherent design, with a worked end-to-end example.

## What each skill solves

| Skill | Solves | Mechanism |
|-------|--------|-----------|
| `api-idempotency` | Retry safety: "I retried because the network timed out — don't double-execute" | `Idempotency-Key` request header + server-side dedupe store |
| `api-conditional-requests` (this one) | Concurrency control: "Two clients edited the same record — don't lose one's changes" | `If-Match` request header + `412 Precondition Failed` |
| `api-error-responses` | Communication: "The 412/428/etc. needs to be machine-actionable" | RFC 9457 Problem Details JSON envelope |

These are independent. An API can use any subset:

- Read-only endpoints need none of them (just `If-None-Match` for caching, optionally).
- Simple POST endpoints (creates) often need only idempotency.
- State-machine endpoints (updates with contention) need all three.

## When to use which

A small decision matrix for write endpoints:

| Endpoint type | Idempotency-Key? | If-Match? | Problem Details? |
|---------------|------------------|-----------|------------------|
| Create with server-assigned ID (POST) | Yes | No | Yes |
| Create with client-controlled ID (PUT, immutable) | Yes (or rely on If-None-Match: *) | `If-None-Match: *` for create-if-absent | Yes |
| Update on a single-writer resource | Optional | Optional | Yes |
| Update on a multi-writer resource | Yes | **Yes** | Yes |
| Delete on a multi-writer resource | Optional | **Yes** | Yes |
| State-machine transition with concurrent actors | Yes | **Yes** | Yes |

The "multi-writer resource" rule is the key one. If two clients can independently modify the same resource — and your API is the source of truth — `If-Match` is non-negotiable.

## Worked example: queue ticket state transition (VQMS-style)

A virtual queue management system. Multiple terminals (mobile apps, kiosks, staff dashboards) interact with queue tickets. Tickets have state: `issued → called → serving → completed`. Multiple staff members can race to call/serve the same ticket.

This endpoint exercises all three patterns.

### The endpoint

```
PATCH /v1/queues/{queue_id}/tickets/{ticket_id}
```

Required headers:
- `Authorization: Bearer <token>` — auth
- `Idempotency-Key: <uuid>` — retry safety
- `If-Match: <etag>` — concurrency control
- `Content-Type: application/merge-patch+json` — body format

### Success path

```http
# Step 1: Terminal A reads the ticket
GET /v1/queues/q-123/tickets/tk-42 HTTP/1.1
Authorization: Bearer <token>

HTTP/1.1 200 OK
ETag: "v3"
Cache-Control: private, no-cache
Content-Type: application/json

{
  "id": "tk-42",
  "queue_id": "q-123",
  "state": "issued",
  "queue_position": 7,
  "issued_at": "2026-05-03T13:50:00Z"
}

# Step 2: Terminal A transitions to "called"
PATCH /v1/queues/q-123/tickets/tk-42 HTTP/1.1
Authorization: Bearer <token>
Idempotency-Key: 9f86d081-0a1c-4f8c-9e42-3a4b2e1c5d76
If-Match: "v3"
Content-Type: application/merge-patch+json

{"state": "called", "called_by": "staff-A"}

HTTP/1.1 200 OK
ETag: "v4"
Idempotency-Key: 9f86d081-0a1c-4f8c-9e42-3a4b2e1c5d76
Content-Type: application/json

{
  "id": "tk-42",
  "state": "called",
  "called_by": "staff-A",
  "called_at": "2026-05-03T14:00:00Z",
  ...
}
```

### Failure modes — each header type produces a distinct error

**Network retry, no concurrent change (Idempotency-Key replay):**

```http
# Same request retried after a network timeout
PATCH /v1/queues/q-123/tickets/tk-42 HTTP/1.1
Idempotency-Key: 9f86d081-0a1c-4f8c-9e42-3a4b2e1c5d76
If-Match: "v3"
Content-Type: application/merge-patch+json

{"state": "called", "called_by": "staff-A"}

# Server replays the original response — exactly once execution
HTTP/1.1 200 OK
ETag: "v4"
Idempotent-Replayed: true
Idempotency-Key: 9f86d081-0a1c-4f8c-9e42-3a4b2e1c5d76
Content-Type: application/json

{...same body as before...}
```

The Idempotency-Key handled this. The `If-Match` was still validated — but against the state at the time of the original request, captured in the dedupe store.

**Concurrent write (different actor, If-Match mismatch):**

```http
# Terminal B tries to call the same ticket, also using v3 ETag
PATCH /v1/queues/q-123/tickets/tk-42 HTTP/1.1
Idempotency-Key: 47d4f5b9-...   # different key
If-Match: "v3"
Content-Type: application/merge-patch+json

{"state": "called", "called_by": "staff-B"}

# Server rejects — ticket is now at v4 (Terminal A already wrote)
HTTP/1.1 412 Precondition Failed
ETag: "v4"
Content-Type: application/problem+json

{
  "type": "https://api.vqms.example.com/problems/version-conflict",
  "title": "Ticket was modified by another terminal",
  "status": 412,
  "detail": "Ticket tk-42 was already called by another staff member. Re-fetch to see current state.",
  "instance": "/v1/queues/q-123/tickets/tk-42",
  "current_etag": "v4",
  "your_etag": "v3"
}
```

This is the lost-update prevention working as designed. Terminal B's correct response: re-fetch, see `state: called, called_by: staff-A`, surface to the user "this ticket was just called by another staff member" rather than retrying.

**Missing If-Match (428):**

```http
# Lazy client implementation forgot If-Match
PATCH /v1/queues/q-123/tickets/tk-42 HTTP/1.1
Idempotency-Key: 47d4f5b9-...
Content-Type: application/merge-patch+json

{"state": "called"}

HTTP/1.1 428 Precondition Required
Content-Type: application/problem+json

{
  "type": "https://api.vqms.example.com/problems/precondition-required",
  "title": "If-Match header is required",
  "status": 428,
  "detail": "PATCH /v1/queues/{queue_id}/tickets/{ticket_id} requires an If-Match header. Fetch the ticket first to obtain the current ETag.",
  "instance": "/v1/queues/q-123/tickets/tk-42"
}
```

**Missing Idempotency-Key (400):**

```http
# Lazy client also missed Idempotency-Key
PATCH /v1/queues/q-123/tickets/tk-42 HTTP/1.1
If-Match: "v3"
Content-Type: application/merge-patch+json

{"state": "called"}

HTTP/1.1 400 Bad Request
Content-Type: application/problem+json

{
  "type": "https://api.vqms.example.com/problems/idempotency-key-missing",
  "title": "Idempotency-Key header is required",
  "status": 400,
  "detail": "POST and PATCH endpoints with side effects require an Idempotency-Key header containing a unique identifier (UUID v4 recommended) for the operation.",
  "instance": "/v1/queues/q-123/tickets/tk-42"
}
```

**Idempotency-Key reused with different body (422):**

```http
# Client bug: reused a key for a different operation
PATCH /v1/queues/q-123/tickets/tk-42 HTTP/1.1
Idempotency-Key: 9f86d081-0a1c-4f8c-9e42-3a4b2e1c5d76   # already used
If-Match: "v4"
Content-Type: application/merge-patch+json

{"state": "serving"}                                       # different from original

HTTP/1.1 422 Unprocessable Content
Content-Type: application/problem+json

{
  "type": "https://api.vqms.example.com/problems/idempotency-key-mismatch",
  "title": "Idempotency-Key reused with a different request",
  "status": 422,
  "detail": "This Idempotency-Key was previously used with a different request body. Generate a new key for a new operation."
}
```

### Server-side handler order

The order of validation matters. Recommended sequence:

1. **Auth** (return 401/403 first; nothing else applies if the caller is unauthenticated).
2. **Resource existence** (return 404 if the ticket doesn't exist; precondition checks are meaningless on a missing resource).
3. **Idempotency-Key check** (return 400 if missing; 422 if mismatched fingerprint; replay if seen-and-completed).
4. **If-Match check** (return 428 if missing; 412 if mismatched).
5. **State-machine validation** (return 409 if the transition is invalid — e.g., trying to "call" a ticket that's already "completed").
6. **Apply the write** atomically (compare-and-swap on the version column).
7. **Capture in idempotency store** (so future retries replay this response).

This order produces the most useful errors. A client calling a deleted ticket gets a clear 404 instead of a 412 about a phantom version. A client missing both headers gets the 428 first (more actionable than the 400, since 428 specifically tells them to fetch first).

## Implementation hints for combining the three

### Use one middleware layer per concern

Don't try to handle all three in a single middleware function — the logic gets tangled. Layer them:

```
[ Idempotency middleware ]
        ↓
[ Conditional-request middleware ]
        ↓
[ Route handler ]
        ↓
[ Problem Details middleware (catches all errors) ]
```

The Idempotency middleware short-circuits replays before the request even reaches the conditional-request layer. The Problem Details middleware sits at the bottom and converts any thrown exception into the appropriate JSON envelope.

### Capture If-Match outcomes in the idempotency record

When storing the response for a successful operation, include the ETag *before* and *after* the operation. Helpful for debugging:

```json
{
  "state": "COMPLETED",
  "fingerprint": "...",
  "status_code": 200,
  "body": "...",
  "etag_before": "v3",
  "etag_after": "v4",
  ...
}
```

If a replay comes in (same Idempotency-Key, same If-Match: "v3"), the response is identical to the original — clients see `ETag: v4` and know they're up to date.

### Document the contract together

In your API docs, describe these patterns once at the top, not per-endpoint:

> Endpoints that modify resources require:
> - `Idempotency-Key`: UUID v4 unique per logical operation. Retries must reuse the same key.
> - `If-Match`: Current ETag of the resource (from the last GET response). Endpoints reject missing or mismatched values.
> - All errors use [RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457.html) format.

Then per-endpoint, just document the success behavior and any operation-specific errors. The cross-cutting concerns are stated once.

## Sources

- RFC 9110 §13 — Conditional Requests
- IETF draft-ietf-httpapi-idempotency-key-header-07 — Idempotency-Key header
- RFC 9457 — Problem Details for HTTP APIs
- RFC 6585 — 428 Precondition Required
