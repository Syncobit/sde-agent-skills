# Core Pattern — Idempotency-Key Header

The full server-side algorithm, storage schema, and response semantics for the Idempotency-Key pattern. Grounded in IETF draft-ietf-httpapi-idempotency-key-header-07 (October 2025) and the de facto pattern shared by Stripe, PayPal, Square, Adyen, and Shopify.

## Server algorithm

The server processes every request to an idempotency-protected endpoint through this state machine:

```
1. Extract Idempotency-Key from request headers.
   - If missing and endpoint requires it → 400 with problem type "idempotency-key-missing".
   - If present, validate format (length 1..255 chars, opaque string). Stripe accepts up to 255; the IETF draft does not fix a length but recommends UUID-style entropy.

2. Compute request fingerprint:
   fingerprint = SHA-256(canonical_method || canonical_path || canonical_body)
   - Canonicalize JSON bodies (sort keys, strip insignificant whitespace) before hashing.
   - Canonical path includes query string, sorted by key.

3. Compute storage key:
   storage_key = sha256(tenant_id || endpoint_id || idempotency_key)
   - tenant_id MUST come from the authenticated principal, never from the request body.
   - endpoint_id is a stable identifier for the route (e.g., "POST /v1/charges").

4. Atomic insert-or-fetch on storage_key:
   a. Try to insert a new record with state = IN_FLIGHT, fingerprint, created_at, ttl.
      Use a unique-constraint insert (Postgres) or SET NX (Redis) — must be atomic.
   b. If insert succeeds → this is the first occurrence. Proceed to step 5.
   c. If insert fails (key exists) → load the existing record. Branch on state:
      - state == IN_FLIGHT → return 409 Conflict, problem type "idempotency-key-in-flight". Do not wait.
      - state == COMPLETED, fingerprint matches → replay: return stored status_code, body, replay headers. Add header "Idempotent-Replayed: true".
      - state == COMPLETED, fingerprint differs → return 422 Unprocessable Entity, problem type "idempotency-key-mismatch".

5. Execute the request handler.

6. On terminal response (status 2xx or 4xx):
   - Update the record to state = COMPLETED, store status_code, body, replay-safe headers.
   - Release any locks. Return the response.

7. On 5xx response or unhandled exception:
   - Delete the record (or mark it as FAILED with a short TTL — 60s).
   - Do NOT cache. The client must be able to retry.
   - Return the error response.
```

The two non-obvious correctness properties:

- **The atomic insert in step 4a is the entire concurrency story.** Without it, two parallel requests with the same key both pass step 4 and both execute step 5. With it, exactly one wins.
- **The 5xx-no-cache rule in step 7** is what lets retry-on-failure actually work. If you cache the 500, the client is stuck — every retry replays the same failure.

## Storage schema

### Redis (recommended for hot path)

```
Key:   idem:{tenant_id}:{endpoint_id}:{sha256(idempotency_key)}
Value: JSON {
  "state": "IN_FLIGHT" | "COMPLETED",
  "fingerprint": "<hex>",
  "status_code": 201,
  "body": "<base64-encoded response body>",
  "headers": {"Content-Type": "application/json", ...},
  "created_at": "2026-05-03T10:15:00Z"
}
TTL:   86400 seconds (24h) for COMPLETED, 60 seconds for IN_FLIGHT (so a crashed in-flight does not block forever).
```

Use `SET key value NX EX 60` for the initial in-flight insert. On completion, use `SET key value EX 86400` (no NX, since we own it).

### Postgres (durable / audit-friendly)

```sql
CREATE TABLE idempotency_records (
  storage_key      bytea       PRIMARY KEY,         -- sha256(tenant || endpoint || key)
  tenant_id        uuid        NOT NULL,
  endpoint         text        NOT NULL,
  fingerprint      bytea       NOT NULL,
  state            text        NOT NULL CHECK (state IN ('IN_FLIGHT', 'COMPLETED')),
  status_code      smallint,
  response_body    bytea,
  response_headers jsonb,
  created_at       timestamptz NOT NULL DEFAULT now(),
  expires_at       timestamptz NOT NULL,
  CONSTRAINT idem_in_flight_short_ttl
    CHECK (state <> 'IN_FLIGHT' OR expires_at < created_at + interval '5 minutes')
);

CREATE INDEX idem_expires_at_idx ON idempotency_records (expires_at);
-- Run a periodic job to DELETE WHERE expires_at < now().
```

Use `INSERT ... ON CONFLICT DO NOTHING RETURNING *` to perform the atomic check.

## Required response headers on replay

Per the IETF draft, a server replaying a stored response should signal it. Common conventions:

```
Idempotent-Replayed: true
Idempotency-Key: <original-key>
```

Some implementations also expose the original request timestamp as `Idempotency-Original-Date`. This is optional but useful for debugging.

## Error responses (RFC 9457 Problem Details)

Define these problem types in your API:

```http
HTTP/1.1 400 Bad Request
Content-Type: application/problem+json

{
  "type": "https://api.example.com/problems/idempotency-key-missing",
  "title": "Idempotency-Key header is required",
  "status": 400,
  "detail": "POST /v1/charges requires an Idempotency-Key request header."
}
```

```http
HTTP/1.1 409 Conflict
Content-Type: application/problem+json

{
  "type": "https://api.example.com/problems/idempotency-key-in-flight",
  "title": "A request with this Idempotency-Key is currently being processed",
  "status": 409,
  "detail": "Wait for the in-flight request to complete, then retry if needed."
}
```

```http
HTTP/1.1 422 Unprocessable Entity
Content-Type: application/problem+json

{
  "type": "https://api.example.com/problems/idempotency-key-mismatch",
  "title": "Idempotency-Key reused with a different request body",
  "status": 422,
  "detail": "This Idempotency-Key was previously used with a different request. Generate a new key for a new operation."
}
```

## Reference implementation (Python / FastAPI sketch)

```python
import hashlib, json
from fastapi import Request, HTTPException
import redis.asyncio as redis

r = redis.Redis()

IN_FLIGHT_TTL = 60
COMPLETED_TTL = 86_400

def canonical_fingerprint(method: str, path: str, body: bytes) -> str:
    canonical = method.upper() + "\n" + path + "\n" + body.decode()
    return hashlib.sha256(canonical.encode()).hexdigest()

def storage_key(tenant: str, endpoint: str, key: str) -> str:
    return "idem:" + hashlib.sha256(f"{tenant}|{endpoint}|{key}".encode()).hexdigest()

async def idempotency_middleware(request: Request, call_next):
    idem_key = request.headers.get("Idempotency-Key")
    if not idem_key:
        if request.method in {"POST", "PATCH"} and is_protected_endpoint(request.url.path):
            raise HTTPException(400, "Idempotency-Key header required")
        return await call_next(request)

    body = await request.body()
    fp = canonical_fingerprint(request.method, request.url.path, body)
    sk = storage_key(request.state.tenant_id, request.url.path, idem_key)

    # Atomic insert
    placeholder = json.dumps({"state": "IN_FLIGHT", "fingerprint": fp})
    acquired = await r.set(sk, placeholder, nx=True, ex=IN_FLIGHT_TTL)

    if not acquired:
        existing = json.loads(await r.get(sk))
        if existing["state"] == "IN_FLIGHT":
            raise HTTPException(409, "Request in flight")
        if existing["fingerprint"] != fp:
            raise HTTPException(422, "Idempotency-Key mismatch")
        # Replay
        return build_response(existing["status_code"],
                              existing["body"],
                              existing["headers"],
                              extra_headers={"Idempotent-Replayed": "true"})

    try:
        response = await call_next(request)
    except Exception:
        await r.delete(sk)
        raise

    if 500 <= response.status_code < 600:
        await r.delete(sk)
        return response

    completed = json.dumps({
        "state": "COMPLETED",
        "fingerprint": fp,
        "status_code": response.status_code,
        "body": response.body.decode(),
        "headers": dict(response.headers),
    })
    await r.set(sk, completed, ex=COMPLETED_TTL)
    return response
```

This is a sketch — production code needs proper async body capture for streaming responses, header allowlisting, and tracing. Use it as the structural reference, not copy-paste.

## Sources

- IETF draft-ietf-httpapi-idempotency-key-header-07 (15 October 2025) — current authoritative spec
- RFC 9110 §9.2.2 — definition of idempotent methods
- RFC 9457 — Problem Details for HTTP APIs
- Stripe API Reference — Idempotent Requests (the most influential reference implementation)
