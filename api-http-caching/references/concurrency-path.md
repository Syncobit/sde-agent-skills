# API Concurrency Path — If-Match, 412, 428, Lost-Update Prevention

The full design for using HTTP conditional requests as a correctness mechanism on state-changing endpoints. This is the PUT/PATCH/DELETE side of the caching surface; for GET-side cache validation, see `revalidation-flow.md`; for CDN edge caching, see `edge-caching-path.md`.

## The lost-update problem, precisely

Two clients fetch the same resource at version 17. Both modify it locally. Both submit writes. Without conditional requests:

- Client A's write is applied → resource is now version 18.
- Client B's write is applied next → resource is now version 19, with B's changes overwriting A's.

Client A has **no signal** that its work was discarded. The HTTP layer reports both writes as 200 OK. The lost update is silent. The user discovers it later, often in production, often in an audit.

This is the problem `If-Match` solves.

## The complete write flow with optimistic concurrency

```http
# Step 1: Client GETs current state
GET /v1/orders/ord-123 HTTP/1.1

HTTP/1.1 200 OK
ETag: "v17"
Cache-Control: private, no-cache
Content-Type: application/json

{"id": "ord-123", "status": "ready", "items": [...], ...}

# Step 2: Client modifies locally and writes back with If-Match
PATCH /v1/orders/ord-123 HTTP/1.1
If-Match: "v17"
Content-Type: application/merge-patch+json

{"status": "shipped"}

# Step 3a: Success — server confirms version, applies, returns new ETag
HTTP/1.1 200 OK
ETag: "v18"
Content-Type: application/json

{"id": "ord-123", "status": "shipped", ...}

# Step 3b: Conflict — another writer changed the resource since step 1
HTTP/1.1 412 Precondition Failed
ETag: "v18"
Content-Type: application/problem+json

{
  "type": "https://api.example.com/problems/version-conflict",
  "title": "Resource was modified by another client",
  "status": 412,
  "detail": "The order was modified after you fetched it. Re-fetch and retry.",
  "instance": "/v1/orders/ord-123",
  "current_etag": "v18",
  "your_etag": "v17"
}
```

The client's correct response to a 412:

1. Re-fetch the resource (gets the current state and current ETag).
2. Inspect what changed. Usually the client can re-apply its intended change against the new state — or surface a conflict to the user, depending on what changed.
3. Retry the write with the new ETag.

What the client must NOT do: blindly retry with a fresh `If-Match` against the new ETag without checking the new state. That re-introduces the lost-update problem.

## Server-side implementation

### The atomic check-and-write

The conditional check and the actual write must be a single atomic operation. If the server reads the ETag, validates it, then writes — without a transaction or compare-and-swap — two concurrent requests can both pass the check and both write. The ETag mechanism becomes useless.

Two implementation patterns:

**1. Database-level optimistic concurrency** (recommended when DB supports it):

```sql
UPDATE orders
SET status = 'shipped', version = version + 1
WHERE id = 'ord-123' AND version = 17;
```

Returns rows affected. If 1 row → success. If 0 rows → version mismatch (or the resource doesn't exist anymore). The compare-and-update is atomic.

```python
def update_order(id, patch, expected_version):
    cursor = db.execute(
        "UPDATE orders SET status = %s, version = version + 1 "
        "WHERE id = %s AND version = %s RETURNING version",
        (patch.status, id, expected_version)
    )
    row = cursor.fetchone()
    if row is None:
        # Either resource missing OR version mismatch
        current = db.execute("SELECT version FROM orders WHERE id = %s", (id,)).fetchone()
        if current is None:
            raise NotFound()
        raise PreconditionFailed(current_version=current.version)
    return row.version
```

**2. Application-level locking** (when DB doesn't support compare-and-swap on the relevant fields):

Acquire a per-resource lock (Redis SET NX, advisory lock in Postgres, etc.), validate the ETag, write, release. Slower but more flexible. Avoid if the DB option works.

### Distinguishing 412 from 404

If the resource doesn't exist at all, return 404 — not 412. RFC 9110 §13.1.1 specifies that `If-Match` with a missing resource is a precondition failure, BUT if the request would otherwise be a 404, the 404 takes precedence. Translation: don't tell clients "your version is wrong" when actually the resource is gone.

A common bug: returning 412 for both "version mismatch" and "resource deleted". The client retries against a phantom resource and confuses itself.

### The 428 Precondition Required pattern

For state-critical resources, don't allow unconditional writes at all. Reject any write missing `If-Match`:

```http
PUT /v1/transfers/tr-abc HTTP/1.1
Content-Type: application/json

{"amount": 5000, "recipient": "rcp-789"}

HTTP/1.1 428 Precondition Required
Content-Type: application/problem+json

{
  "type": "https://api.example.com/problems/precondition-required",
  "title": "If-Match header is required",
  "status": 428,
  "detail": "PUT /v1/transfers/{id} requires an If-Match header containing the resource's current ETag. Fetch the resource first to obtain the current ETag.",
  "instance": "/v1/transfers/tr-abc"
}
```

This is the recommended posture for:

- Financial transactions
- State-machine entities (orders, tickets, accounts) where wrong-state transitions cause real damage
- Resources with regulatory/compliance audit requirements
- Any resource where you'd rather have a slightly worse client UX than a silent lost update

The 428 trains client implementations to send `If-Match` from the start. Without 428, developers discover the requirement only when they happen to lose a race in production — a much worse failure mode.

### The atomic create — `If-None-Match: *`

A useful related pattern: create-or-fail when the client controls the resource ID.

```http
PUT /v1/orders/ord-abc HTTP/1.1
If-None-Match: *
Content-Type: application/json

{"customer_id": "cust-1", "items": [...]}

# If resource didn't exist:
HTTP/1.1 201 Created
ETag: "v1"
Location: /v1/orders/ord-abc

# If resource already existed:
HTTP/1.1 412 Precondition Failed
ETag: "v17"
```

Useful when:
- The client generates the resource ID (UUID, or external reference like an order number from another system)
- You want create-only semantics, not create-or-update — accidentally re-creating an existing order is a bug
- You want HTTP-level atomicity rather than relying on a uniqueness constraint that surfaces a confusing 500 from a constraint violation

The wildcard `*` matches "any current representation" — the precondition fails if anything exists at the URL.

## End-to-end pattern: queue ticket state machine

A virtual queue management system where multiple terminals (mobile apps, kiosks, staff dashboards) can transition the same queue ticket. State machine:

```
issued → called → serving → completed
              ↘ no-show
```

Two staff terminals both call ticket #42 at the same moment. Without conditional requests, both calls succeed; the customer is confused; the audit log is wrong. With conditional requests:

```http
# Terminal A reads
GET /v1/tickets/tk-42 HTTP/1.1

HTTP/1.1 200 OK
ETag: "v3"
Content-Type: application/json

{"id": "tk-42", "state": "issued", "queue_position": 7, ...}

# Terminal B reads (concurrently)
GET /v1/tickets/tk-42 HTTP/1.1

HTTP/1.1 200 OK
ETag: "v3"
{"id": "tk-42", "state": "issued", "queue_position": 7, ...}

# Terminal A calls first
PATCH /v1/tickets/tk-42 HTTP/1.1
If-Match: "v3"
Content-Type: application/merge-patch+json

{"state": "called", "called_by": "terminal-A", "called_at": "2026-05-03T14:00:00Z"}

HTTP/1.1 200 OK
ETag: "v4"
{"id": "tk-42", "state": "called", "called_by": "terminal-A", ...}

# Terminal B tries to call — gets 412
PATCH /v1/tickets/tk-42 HTTP/1.1
If-Match: "v3"
Content-Type: application/merge-patch+json

{"state": "called", "called_by": "terminal-B", "called_at": "2026-05-03T14:00:01Z"}

HTTP/1.1 412 Precondition Failed
ETag: "v4"
Content-Type: application/problem+json

{
  "type": "https://api.vqms.example.com/problems/version-conflict",
  "title": "Ticket was modified by another terminal",
  "status": 412,
  "detail": "Ticket tk-42 was already called by another terminal. Re-fetch to see current state.",
  "current_etag": "v4"
}
```

Terminal B's correct behavior: re-fetch, see that the ticket is already in `called` state by Terminal A, surface a UI message ("This ticket was already called by another staff member — refresh to see the current queue") rather than retrying. The state-machine semantics dictate that double-calling is a logical error, not a retryable conflict.

This is the pattern for any state-machine resource. The 412 is not a problem to work around; it's the correct outcome telling the client "your assumption about the state was wrong, reconsider".

## Distinguishing scenarios

| Symptom | What it means | Right response |
|---------|---------------|----------------|
| 412, current ETag is newer than yours | Someone wrote between your GET and your write | Re-fetch, decide whether to retry |
| 412, current ETag is the same as yours | Bug in your client (sending stale cached ETag from a different resource?) | Investigate client logic |
| 428 | Endpoint requires If-Match, you didn't send one | Add If-Match to the client |
| 404 on a previously-existing resource | Resource was deleted | Don't retry; surface to user |
| 409 (not 412) | Resource state conflict unrelated to versioning ("can't ship a cancelled order") | Re-fetch, evaluate state machine |

The 412 vs 409 distinction matters: 412 is specifically "the version you specified isn't current". 409 is the broader "this operation conflicts with current state". Use 412 only when you're checking a precondition header. Use 409 for state-machine violations that don't involve versioning.

## Common implementation pitfalls

**1. Returning 412 without the current ETag.** The client now needs the new ETag to retry — your response should include it. Always send `ETag: <current>` on a 412 response.

**2. Treating 412 as a retryable transient error.** It's not. A 412 means the client's view is stale. Automatic retry without re-fetching the new state re-introduces the lost-update problem. Document this in your client SDKs.

**3. Not handling the deleted-resource case.** If the resource was deleted between the client's GET and PATCH, the `If-Match` header points to a nonexistent ETag. Return 404, not 412.

**4. Strong vs weak ETag confusion.** `If-Match` requires strong comparison. If your ETags are weak, every write fails. Audit your ETag generation; don't mix weak and strong on the same resource. See `primitives.md` for the rules.

**5. ETag-and-write not being atomic.** Read the ETag, write the resource — between those operations, another client can write. Use a database-level compare-and-swap or distributed lock; never read-then-write with `If-Match` validation in application code alone.

**6. Forgetting that PATCH semantics are operation-dependent.** `application/json-patch+json` (RFC 6902) explicitly tests preconditions; `application/merge-patch+json` (RFC 7396) is simpler but less expressive. Pick deliberately and document.

## Sources

- RFC 9110 §13.1.1 — If-Match
- RFC 9110 §15.5.13 — 412 Precondition Failed
- RFC 6585 — Additional HTTP Status Codes (428 Precondition Required)
- RFC 6902 — JSON Patch (operation-level preconditions)
- RFC 7396 — JSON Merge Patch
