---
name: api-idempotency
description: Apply idempotency patterns when designing, implementing, or reviewing REST APIs and webhook receivers. Use this skill whenever the task involves designing a POST or PATCH endpoint, writing a billing or payments call, building provisioning or order-creation logic, drafting a webhook receiver, sketching API specs (OpenAPI, Spec Kit, etc.), or reviewing existing endpoints — even if the user does not say the word "idempotency". Also use when the user mentions retries, network timeouts, exactly-once delivery, duplicate requests, double-charging, message dedupe, or "what happens if the client retries". The skill produces concrete server-side designs (storage schema, request fingerprinting, replay semantics, concurrency handling, TTL, error responses) grounded in IETF draft-ietf-httpapi-idempotency-key-header-07 and RFC 9110, plus a review checklist for catching common idempotency bugs.
---

# REST API Idempotency Patterns

A skill for designing, implementing, and reviewing fault-tolerant REST endpoints that survive retries without duplicate side effects.

## Why this skill exists

Networks fail. Clients retry. Without idempotency, a single user action can charge a card twice, ship two orders, or send two SMS messages. Idempotency is the contract that lets a client retry safely and the server still produce exactly one effect.

This is not optional polish — it is a correctness property of any non-idempotent endpoint that has real-world side effects (money, communications, provisioning, inventory, ticket issuance).

## Step 1 — Decide if you need an idempotency layer

Walk this checklist on every endpoint under discussion:

1. **What HTTP method?** Per RFC 9110, `GET`, `HEAD`, `OPTIONS`, `PUT`, and `DELETE` are idempotent by spec. `POST` and `PATCH` are not. PATCH is a frequent confusion — it is *not* idempotent unless the server makes it so.
2. **Does the operation have a side effect beyond writing to one DB row the client controls?** Examples that demand idempotency: charging a card, sending a message, calling a downstream API, decrementing inventory, issuing a queue ticket, provisioning a SIM, creating an order.
3. **Can the client retry?** If the endpoint is reachable over a network (it always is), the answer is yes — clients, proxies, mobile radios, and queue consumers all retry on timeouts.
4. **Is there a natural unique key the domain already enforces?** A bank transaction reference, an order number provided by the client, an external webhook event ID. If yes, use **natural-key idempotency** (Step 2A). If no, use the **Idempotency-Key header pattern** (Step 2B).

If the answer to (2) and (3) is yes, this endpoint must be idempotent. Do not ship it without an explicit idempotency story.

## Step 2 — Pick the right pattern

### 2A. Natural-key idempotency (preferred when available)

Use when the domain already gives you a globally unique identifier for the operation: an external transaction ID, a webhook event ID, a client-generated order ID. Enforce it with a UNIQUE constraint at the database level and translate the constraint violation into a "already processed, here is the prior result" response.

This is preferred because the dedupe key is meaningful (you can debug it), it survives forever (no TTL), and there is no extra header to negotiate.

### 2B. Idempotency-Key header pattern (the general case)

When the operation has no natural key, follow the model in IETF draft-ietf-httpapi-idempotency-key-header-07 (also the de facto pattern used by Stripe, PayPal, Square, Adyen, and Shopify):

1. **Client** generates a UUIDv4 (or v7) per logical operation and sends it in the `Idempotency-Key` request header. The same key is reused on every retry of that same operation. A new logical operation gets a new key.
2. **Server** looks up the key, scoped to (auth principal, endpoint). Three cases:
   - **First time seen** → process normally, store `{key, request_fingerprint, status_code, response_body, selected_headers}` with a TTL (24h is the Stripe default and a reasonable starting point), then return.
   - **Seen, already complete, fingerprint matches** → replay the stored response byte-for-byte. Same status code, same body.
   - **Seen, already complete, fingerprint differs** → reject with `422 Unprocessable Entity` (or `409 Conflict`). The client is misusing the key by reusing it for a different request.
   - **Seen, in flight** → return `409 Conflict` immediately. Do not block the second request waiting for the first; that creates pile-ups under retry storms.
3. **Server never caches `5xx` responses.** A retry of a transient failure must be allowed to actually retry the operation, not replay the failure. Cache only terminal outcomes (`2xx` and `4xx`).

The full server algorithm, including the concurrency state machine and storage schema, is in `references/core-pattern.md`.

## Step 3 — Specify the implementation details

Every idempotency design has the same set of decisions. Make them explicit, do not let them be implicit:

- **Storage backend.** Redis is the common choice (TTL is native, latency is single-digit ms). Postgres works if you need durability and don't have Redis. See `references/implementation.md`.
- **Request fingerprint.** Hash the canonicalized request body + path + method. Without this, an attacker (or buggy client) can reuse a key to read someone else's response.
- **Key scope.** Always scope by authenticated principal (user, tenant, API key). A global key namespace is a security bug — one tenant's key collision can leak another tenant's response.
- **TTL.** Match or exceed the longest plausible client retry window. 24h handles mobile clients with intermittent connectivity. Longer (7d) for B2B integrations.
- **In-flight handling.** Lock on key insertion using a unique constraint or `SET NX` in Redis. The second concurrent request gets a deterministic 409, not a race.
- **What gets replayed.** Status code, body, and a curated list of safe headers (`Content-Type`, custom domain headers). Never replay `Set-Cookie`, `Date`, `Location` of a fresh resource, or auth tokens.
- **Error responses.** Use RFC 9457 Problem Details JSON. Define problem types for `idempotency-key-mismatch`, `idempotency-key-in-flight`, `idempotency-key-expired`.

## Step 4 — Test the contract, do not assume it

Idempotency bugs hide in concurrency. The five test cases that catch real bugs:

1. **Replay test.** Same key, same body, twice. Both responses must be byte-identical (modulo `Date`).
2. **Concurrent test.** Same key, same body, fired in parallel (N=10+). Exactly one execution; the other N-1 get 409 or a replay.
3. **Mismatch test.** Same key, different body. Server returns 422 and does *not* execute the second body.
4. **TTL expiry test.** Same key after the TTL elapses. Server treats it as a fresh request and re-executes — this is correct behavior, the client should have used a new key.
5. **Failure-then-retry test.** First call returns 500 (e.g., DB blip). Second call with same key must actually retry, not replay the 500.

Full test scaffolding patterns in `references/testing.md`.

## Step 5 — Review checklist (use this when reviewing existing APIs)

Read through the endpoint and flag any of these:

- POST or PATCH endpoint with side effects and no idempotency mechanism → **block**.
- Idempotency keys that are not scoped per principal → **security bug**.
- No request fingerprinting → **silent data corruption risk**.
- 5xx responses being cached and replayed → **stuck-failure bug**.
- TTL shorter than the documented client retry window → **flapping bug**.
- Concurrent same-key requests not handled deterministically → **race bug**.
- "Idempotent" PATCH that mutates state in non-idempotent ways (e.g., `{"counter": "+1"}`) → **spec violation**.
- Webhook receiver without dedupe on the provider's event ID → **double-processing bug**.
- Documentation that does not specify Idempotency-Key requirements, TTL, or error codes → **interop risk**.

Full anti-pattern catalog with examples in `references/anti-patterns.md`.

## When to dig into the references

- **Building from scratch** → read `references/core-pattern.md` for the full server algorithm and storage schema, then `references/implementation.md` for storage and concurrency.
- **Reviewing an existing API** → read `references/anti-patterns.md` for the catalog of bugs to look for.
- **Writing tests** → read `references/testing.md` for the five canonical test cases with code.
- **Method-semantics questions** ("is PATCH idempotent?", "should I use PUT or POST?") → read `references/http-semantics.md`.

## Output style

When applying this skill, produce concrete deliverables, not abstract advice:

- For **design** requests: a server algorithm in pseudocode or the requested language, a storage schema, the exact HTTP responses (status codes, headers, Problem Details JSON), and a list of test cases.
- For **review** requests: a numbered list of findings tagged `[block]`, `[fix]`, or `[nit]`, each with the specific line/section, the problem, and the corrective action.
- For **spec** requests (OpenAPI, Spec Kit, etc.): the actual header definitions, response schemas, and error types ready to paste in.

Always cite the standard you are applying (RFC 9110 §9.2.2 for method idempotency, draft-ietf-httpapi-idempotency-key-header-07 for the header pattern, RFC 9457 for error format) so the user can verify.
