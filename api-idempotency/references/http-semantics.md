# HTTP Semantics — Method Idempotency and Conditional Requests

What RFC 9110 says about which methods are idempotent, where the spec is commonly misread, and when to use conditional requests instead of the Idempotency-Key pattern.

## Method-level idempotency (RFC 9110 §9.2.2)

| Method  | Idempotent by spec? | Common reality                                                                                  |
|---------|---------------------|-------------------------------------------------------------------------------------------------|
| GET     | Yes                 | True in practice. Treat side-effecting GETs as a design bug.                                    |
| HEAD    | Yes                 | True.                                                                                           |
| OPTIONS | Yes                 | True.                                                                                           |
| PUT     | Yes                 | True *if the body fully describes the target state*. PUT is "replace", not "update".            |
| DELETE  | Yes                 | True. Second DELETE on a missing resource returns 404 or 204; the *effect* is the same.         |
| POST    | **No**              | The canonical non-idempotent method. Always needs an Idempotency-Key for safety-critical use.   |
| PATCH   | **No**              | Frequently misread as idempotent. RFC 5789 explicitly says PATCH is not idempotent in general.  |

Two consequences:

1. **Use PUT when you can.** If the client knows the resource ID and is sending the full new state, PUT is naturally idempotent and you don't need an Idempotency-Key layer at all. Reserve POST for "server creates and assigns the ID" cases.
2. **Do not assume PATCH is idempotent.** A PATCH with a body like `{"counter": "+1"}` is non-idempotent — replaying it increments twice. A PATCH with `{"status": "shipped"}` is idempotent only because the target state is absolute. Treat PATCH endpoints case-by-case and add Idempotency-Key when in doubt.

## Conditional requests (the other idempotency tool)

Sometimes the right answer is not Idempotency-Key but conditional requests with `ETag`/`If-Match`. Use this when:

- The endpoint is a PUT or PATCH against a known resource.
- The concern is not retry safety but **lost-update prevention** (two clients editing the same resource).

Pattern:

```http
GET /v1/orders/123
HTTP/1.1 200 OK
ETag: "v17"
{"id": 123, "status": "draft", ...}

# Client edits, then sends:
PATCH /v1/orders/123
If-Match: "v17"
{"status": "submitted"}

# Server: if current ETag is still "v17", apply the change and return new ETag.
# If current ETag is "v18" (someone else updated), return:
HTTP/1.1 412 Precondition Failed
```

This is the right pattern for collaborative editing, configuration changes, and any "update if you have the latest version" flow. It composes with Idempotency-Key — you can use both on the same endpoint.

`If-None-Match: *` on a PUT is the standard "create only if it doesn't exist" idempotent-create pattern. It's a lightweight alternative to Idempotency-Key when the client controls the resource ID.

## When to pick which pattern

Decision flow for any mutating endpoint:

1. **Does the client know the resource ID up front?**
   - Yes → use `PUT /resource/{id}` with `If-None-Match: *` for create or `If-Match: <etag>` for update. Naturally idempotent, no extra storage layer.
   - No (server assigns ID) → continue to step 2.

2. **Is there a domain-natural unique key (transaction ref, external event ID)?**
   - Yes → enforce uniqueness in the database, return the prior result on conflict. See `core-pattern.md` "Natural-key idempotency".
   - No → continue to step 3.

3. **Use the Idempotency-Key header pattern.** See `core-pattern.md`.

## Status code reference for idempotency interactions

These are the codes that show up in idempotency flows. Be deliberate about which you use:

| Code | When to use                                                                                                  |
|------|--------------------------------------------------------------------------------------------------------------|
| 200  | Replay of a successful operation. Body identical to the original response.                                   |
| 201  | Replay of a successful create. Include the original `Location` header.                                       |
| 400  | Idempotency-Key header missing on an endpoint that requires it. Or malformed key.                            |
| 409  | Idempotency-Key currently in flight (concurrent request). Or natural-key conflict on a fresh request.         |
| 412  | Conditional request precondition failed (`If-Match` mismatch). Lost-update prevention.                       |
| 422  | Idempotency-Key reused with a different request fingerprint. Client bug.                                     |
| 425  | Too Early — for retry-too-soon scenarios on resumable operations. Rarely used; document if you do.           |

Avoid using 409 for the fingerprint-mismatch case — 422 is more accurate because the issue is semantic (wrong body), not state-conflict.

## Sources

- RFC 9110 (HTTP Semantics) §9.2.2 — Idempotent Methods
- RFC 9110 §13 — Conditional Requests
- RFC 5789 — PATCH Method for HTTP (note on non-idempotency)
- RFC 7232 — Conditional Requests (now folded into 9110)
