---
name: api-error-responses
description: Design, review, or document HTTP API error responses. Use this skill whenever the task involves designing error contracts for a REST API, picking a status code (4xx vs 5xx), writing OpenAPI error schemas, building exception handlers or middleware that translate exceptions into HTTP responses, returning validation errors from a form or request body, choosing between Problem Details (RFC 9457) / JSON:API / Google's google.rpc.Status / a custom shape, drafting error documentation, or reviewing existing error responses for consistency. Trigger broadly — any conversation about API errors, validation feedback, error codes, error envelopes, status code selection, or "how should the server tell the client what went wrong" should use this skill, even when the user does not say "error format". Composes with api-idempotency (for idempotency-specific error types) and any conditional-request work.
---

# REST API Error Response Design

A skill for designing error responses that are machine-actionable, human-debuggable, and consistent across an API surface — without leaking internals or making clients guess.

## Why this skill exists

Most API error responses in the wild are inconsistent: some endpoints return `{"error": "string"}`, some return `{"errors": [...]}`, some return `{"code": 42, "message": "..."}`, some return raw stack traces. Within a single API. Clients have to write per-endpoint error handling, which means they don't — they just check status codes and log the body.

A good error contract is the second-most-important part of an API contract (after the success path). It determines:

1. Whether clients can write generic, reusable error handling.
2. Whether you can change error wording without breaking integrations.
3. Whether you accidentally expose internals (stack traces, DB errors, internal IDs) to attackers.
4. Whether your validation errors are useful or just "Bad Request".

This skill produces the contract — picked from real, production-tested formats with known tradeoffs — and the operational rules that go around it.

## Step 1 — Pick an error format

There are four formats worth considering for a new API. Pick one and use it everywhere; consistency is more valuable than picking the "best" one.

### Option A: RFC 9457 Problem Details (recommended default)

```http
HTTP/1.1 403 Forbidden
Content-Type: application/problem+json

{
  "type": "https://api.example.com/problems/insufficient-funds",
  "title": "Insufficient funds",
  "status": 403,
  "detail": "Account balance is 30 USD, but the operation requires 50 USD.",
  "instance": "/v1/transfers/abc123",
  "balance": 30,
  "required": 50
}
```

- **Standardized**: IETF Standards Track (July 2023, obsoletes RFC 7807). Has an IANA registry of common problem types.
- **Media type**: `application/problem+json` (or `application/problem+xml`).
- **Required fields**: none, technically — but in practice always include `type`, `title`, `status`. `detail` and `instance` are recommended.
- **Extensible**: any additional fields are allowed and considered part of the problem type's contract (e.g., `balance` and `required` above).
- **Built-in support**: Spring Boot 3+, ASP.NET Core, FastAPI (via `fastapi-problem-details`), Express middleware, Go's `problem` library.
- **Tradeoff**: single-error per response (RFC explicitly recommends communicating the most pressing problem first rather than batching). Multi-error use cases need a custom extension.

**Pick this when**: You're designing a new public or internal API and don't have an external constraint forcing another format. This is the default for new work.

### Option B: JSON:API errors

```http
HTTP/1.1 422 Unprocessable Entity
Content-Type: application/vnd.api+json

{
  "errors": [
    {
      "id": "8b2a1f6c",
      "status": "422",
      "code": "INVALID_EMAIL",
      "title": "Invalid email address",
      "detail": "The email field must be a valid RFC 5322 address.",
      "source": { "pointer": "/data/attributes/email" },
      "links": { "about": "https://api.example.com/docs/errors#INVALID_EMAIL" }
    }
  ]
}
```

- **Standardized**: jsonapi.org v1.1 (community spec, widely adopted).
- **Media type**: `application/vnd.api+json`.
- **Multi-error native**: `errors` is always an array; one or many.
- **`source.pointer` is the killer feature**: JSON Pointer (RFC 6901) into the request body, so clients can highlight the offending field directly. `source.parameter` does the same for query strings.
- **Tradeoff**: only worth it if you're already using JSON:API for the success path. The response envelope conventions are tightly coupled.

**Pick this when**: Your API is already JSON:API end-to-end, or you need first-class multi-error responses (form validation with many fields) and don't want to extend RFC 9457.

### Option C: Google `google.rpc.Status` (AIP-193)

```http
HTTP/1.1 400 Bad Request
Content-Type: application/json

{
  "error": {
    "code": 400,
    "message": "Request contains an invalid argument.",
    "status": "INVALID_ARGUMENT",
    "details": [
      {
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "reason": "EMAIL_FORMAT",
        "domain": "users.example.com",
        "metadata": { "field": "email" }
      },
      {
        "@type": "type.googleapis.com/google.rpc.BadRequest",
        "fieldViolations": [
          { "field": "email", "description": "Must be a valid email address." }
        ]
      }
    ]
  }
}
```

- **Origin**: gRPC. Google's REST APIs use this format because they auto-generate REST from gRPC services.
- **Strict shape**: `code`, `message`, `status` (a string from the canonical gRPC code list: `NOT_FOUND`, `PERMISSION_DENIED`, etc.), and a typed `details` array.
- **`ErrorInfo` is required** in every Google API response (per AIP-193) for machine-readable error identification.
- **Tradeoff**: heavy, gRPC-flavored, and the `@type` URLs are awkward outside the protobuf ecosystem.

**Pick this when**: You're already using gRPC + REST gateway, you're building on Google Cloud, or your team is genuinely going to use protobuf-based clients. Otherwise it's overkill.

### Option D: Custom format

```http
HTTP/1.1 400 Bad Request
Content-Type: application/json

{
  "code": "INVALID_EMAIL",
  "message": "Email address is not valid",
  "field": "email",
  "request_id": "req_abc123"
}
```

- **Pick this only if**: you're already shipping a non-trivial API with this shape. New APIs should not invent a custom format.
- **If you must**: include at minimum a stable machine-readable error code (string, not the HTTP status), a human-readable message, and a request ID. Document the shape rigorously.

## Recommendation

**For new APIs: RFC 9457 Problem Details.** It's the only IETF Standards Track format, has the broadest framework support, and the extension mechanism handles validation errors and most other cases cleanly.

**Exception**: if you need rich multi-error responses (form validation across many fields) and can't extend Problem Details cleanly, JSON:API errors are the second-best choice.

For the recommended format in depth (problem type registry, extension fields, validation error patterns), see `references/rfc-9457-problem-details.md`. For a side-by-side feature comparison, see `references/format-comparison.md`.

## Step 2 — Choose the right status code

Status codes carry the bulk of the error meaning. The body is for context, not for replacing the code. Common pitfalls:

- **400 vs 422**: 400 means "I cannot parse this" (malformed JSON, missing required field at the syntactic level). 422 means "I parsed it, but it's semantically invalid" (email is well-formed but doesn't exist, business rule violated). Use 422 for validation failures of well-formed requests.
- **401 vs 403**: 401 means "I don't know who you are" (missing/expired/invalid auth). 403 means "I know who you are, you can't do this". Returning 401 when the user is authenticated but lacks permissions is a common bug.
- **404 vs 403 for "you can't see this"**: 404 hides existence; 403 reveals it. Pick deliberately based on whether existence itself is sensitive.
- **409 vs 422**: 409 is *state* conflict (resource changed underneath you, idempotency-key in flight, version mismatch). 422 is *content* conflict (request makes no sense given the rules).
- **429**: rate limited. Always include `Retry-After`.
- **500 vs 503**: 500 is "we have a bug". 503 is "we're temporarily unhealthy, retry will probably succeed". 503 should include `Retry-After`.

The full status-code decision tree, including the trickier cases (425 Too Early, 451 Unavailable for Legal Reasons, when to use 410 Gone), is in `references/status-codes.md`.

## Step 3 — Make error codes stable and versioned

The single most important rule for error contracts: **the machine-readable code must be stable; the human-readable message can change.**

- The `type` URI in RFC 9457 (or `code` in JSON:API, or `reason` in `ErrorInfo`) is part of your API contract. Changing it is a breaking change. Treat it like a route name.
- The `title` and `detail` fields are *not* part of the contract. You can rephrase them, localize them, A/B-test them. Clients must not pattern-match on them.
- Document each error code in a registry alongside the API docs. The `type` URI in Problem Details should ideally be dereferenceable to that documentation.
- When you deprecate an error code, follow the same process as deprecating an endpoint: announce, dual-emit, migrate, remove.

A bad pattern that shows up everywhere: clients writing `if (response.error.message.includes("not found"))`. They write that because there's no stable code. The error code exists to prevent this.

## Step 4 — What NOT to include

Error responses are an attack surface. Most info leaks happen here, not in success responses. Never include:

- **Stack traces.** Even in development; ship logging-instead-of-leaking from day one. Stack traces in production reveal framework versions, internal class names, and code paths that aid exploit development.
- **Database error messages or SQL fragments.** "duplicate key value violates unique constraint" tells attackers your schema.
- **Internal user IDs, tenant IDs, or row IDs** unless they're already part of the resource the user can see.
- **File paths, hostnames, or service names** of internal infrastructure.
- **Whether a username/email exists** (in auth contexts — return the same error for "wrong password" and "no such user" to prevent enumeration).
- **The exact validation rule that was checked**, when revealing it would help an attacker (e.g., password policies — say "doesn't meet requirements" not "needs 14 chars and 2 digits and we just checked digits").

What to include instead:

- A stable error code.
- A short human message free of internal details.
- A `request_id` or `instance` URI (or both) so support can correlate to internal logs without exposing them.

Full security guidance with examples in `references/security.md`.

## Step 5 — Handle validation errors deliberately

Validation errors are the highest-volume error response on most APIs. Two patterns, pick one:

**Single-error**: return on the first validation failure. Simpler server logic, but worse UX — the user fixes one field, submits, gets the next error, and so on. Acceptable for server-to-server APIs.

**Multi-error**: collect all validation failures and return them together. Required for any API consumed by a UI form. RFC 9457 doesn't natively support this, so use one of:

```json
// RFC 9457 with extension field (recommended)
{
  "type": "https://api.example.com/problems/validation-error",
  "title": "Request validation failed",
  "status": 422,
  "errors": [
    { "pointer": "/email", "code": "INVALID_FORMAT", "detail": "Must be a valid email." },
    { "pointer": "/age",   "code": "OUT_OF_RANGE", "detail": "Must be between 18 and 120." }
  ]
}
```

```json
// JSON:API native
{
  "errors": [
    { "status": "422", "code": "INVALID_FORMAT", "source": { "pointer": "/data/attributes/email" }, ... },
    { "status": "422", "code": "OUT_OF_RANGE", "source": { "pointer": "/data/attributes/age" }, ... }
  ]
}
```

Use **JSON Pointer (RFC 6901)** for the field reference in either format. It works for nested fields (`/address/postalCode`) and array elements (`/items/0/quantity`). It's standardized, parseable, and language-agnostic. Don't invent your own dotted-path notation.

For more on multi-error patterns and i18n of error messages, see `references/validation-errors.md`.

## Step 6 — Composition with other patterns

Error responses don't exist in isolation. A few common compositions worth getting right:

- **With idempotency** (`api-idempotency` skill): define problem types `idempotency-key-missing` (400), `idempotency-key-in-flight` (409), `idempotency-key-mismatch` (422).
- **With rate limiting**: 429 response includes `Retry-After` header AND a Problem Details body with `type: rate-limit-exceeded` and extension fields for `limit`, `remaining`, `reset`.
- **With conditional requests**: 412 Precondition Failed for `If-Match` mismatch, 428 Precondition Required if you require `If-Match` and didn't get one.
- **With async / long-running operations**: errors during the polled operation status response should follow the same format as synchronous errors — don't invent a separate shape for "the operation failed".

## Output style

When applying this skill, produce concrete artifacts:

- For **format selection**: a one-paragraph recommendation with the chosen format, plus the 2-3 reasons it fits the user's context, plus the rejected alternatives with one-line reasons.
- For **error contract design**: the literal HTTP responses (status, headers, body) for the top 5-10 error cases the API will produce. Include the problem type registry as a markdown table.
- For **review**: numbered findings tagged `[block]`/`[fix]`/`[nit]` with the specific endpoint, the issue, and the corrective response.
- For **OpenAPI specs**: actual schema fragments ready to paste, including `$ref` to a shared `Problem` schema component.

Always cite the standard you're applying (RFC 9457 §3 for the structure, §4.2 for the registry, RFC 9110 §15 for status codes, RFC 6901 for JSON Pointer) so the user can verify.

## When to dig into the references

- **Designing from scratch with Problem Details** → `references/rfc-9457-problem-details.md` for the full structure, registry, extension patterns, framework integrations.
- **Comparing formats in depth** → `references/format-comparison.md` for the side-by-side feature matrix and migration notes.
- **Picking status codes for tricky cases** → `references/status-codes.md`.
- **Form validation, multi-error, i18n** → `references/validation-errors.md`.
- **Security review of error responses** → `references/security.md`.
