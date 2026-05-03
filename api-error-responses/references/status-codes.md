# Status Code Selection

A practical guide to picking the right HTTP status code, with the cases that come up in real APIs and the ones that get used wrong most often.

## The two-axis decision

Every error response answers two questions:

1. **Whose fault is it?** Client (4xx) or server (5xx)?
2. **What kind of problem is it?** Auth, parsing, business rule, conflict, etc.

Get question 1 right first. Returning 5xx for a client error means clients will retry forever; returning 4xx for a server error means clients won't retry when they should. This is one of the highest-leverage decisions in API design.

## 4xx — Client errors

The client did something wrong, and retrying without changing the request will fail again.

### 400 Bad Request
**Use for**: malformed requests at the syntactic level — invalid JSON, missing required field at the schema level, wrong content type, malformed query parameters.

**Don't use for**: semantically valid requests that fail validation rules. That's 422.

A common mistake: many APIs use 400 for everything client-side. This is technically not wrong (400 is a valid catch-all), but it loses the more specific signals 422 and 409 provide.

### 401 Unauthorized
**Use for**: missing, expired, or invalid authentication credentials.

**The naming is unfortunate** — it should really be "Unauthenticated". 401 means "I don't know who you are". Always include `WWW-Authenticate` header per RFC 9110.

**Don't use for**: authenticated users who lack permission. That's 403.

### 403 Forbidden
**Use for**: authenticated users attempting actions they don't have permission for.

**Don't use for**: missing/expired credentials (401).

**The 403 vs 404 question for sensitive resources**: if revealing whether a resource exists is itself a leak (e.g., private repos, internal documents), return 404 instead of 403. If existence is public, 403 is fine. Pick one rule and apply it consistently — flipping between 403 and 404 based on permissions can itself leak existence (timing attacks, behavior differences).

### 404 Not Found
**Use for**: the requested resource doesn't exist, or the user lacks permission and existence is sensitive (see above).

**Don't use for**: the route exists but the requested operation doesn't make sense. Use 422 or 405.

### 405 Method Not Allowed
**Use for**: the path is valid but the HTTP method isn't supported (e.g., POST to a read-only endpoint).

**Required**: include `Allow` header listing the supported methods, per RFC 9110.

### 406 Not Acceptable
**Use for**: client's `Accept` header asks for a format the server can't produce.

**In practice**: rarely used; most APIs serve JSON only and ignore `Accept`. Worth including if you support multiple formats.

### 408 Request Timeout
**Use for**: the client started a request but didn't send the body within the server's timeout.

**In practice**: also rare in modern APIs. Most server frameworks just close the connection.

### 409 Conflict
**Use for**: the request conflicts with the current state of the resource. Examples:
- Optimistic concurrency failure (when not using `If-Match` / 412)
- Idempotency-Key in flight
- Duplicate creation when uniqueness is enforced
- Trying to delete a resource that has dependents
- State machine violation ("can't ship an order that's already shipped")

**Vs 422**: 409 is *state* conflict. 422 is *content* conflict. "You can't do this *right now*" → 409. "This request doesn't make sense *at all*" → 422.

### 410 Gone
**Use for**: the resource existed but is permanently deleted, and you want to tell the client to stop asking.

**Use over 404 when**: the URI was previously valid and you want clients/crawlers to remove it from their caches/indices. Search engines treat 410 differently from 404.

### 412 Precondition Failed
**Use for**: a conditional request (`If-Match`, `If-Unmodified-Since`, `If-None-Match`) failed because the resource changed.

**Don't use for**: failed business preconditions ("the order isn't in 'paid' state"). That's 409 or 422. 412 is specifically for HTTP-level conditional requests.

### 415 Unsupported Media Type
**Use for**: client sent a body with a `Content-Type` the server doesn't accept.

### 422 Unprocessable Content (formerly Unprocessable Entity)
**Use for**: the request was syntactically valid (parsed fine) but failed semantic validation. The most common error code on most APIs.

Examples:
- Email field is well-formed but doesn't pass your validation rules
- Amount is negative when only positive is allowed
- Referenced foreign key doesn't exist
- Business rule violation that isn't a state conflict

**Note**: RFC 9110 renamed this from "Unprocessable Entity" (the WebDAV name) to "Unprocessable Content". Both names refer to the same code; clients don't care.

### 425 Too Early
**Use for**: server is unwilling to process a request that might be replayed (early-data scenarios in TLS 1.3).

**In practice**: very rare in application code; mostly used by edge servers / CDNs.

### 428 Precondition Required
**Use for**: server requires a conditional request (`If-Match`) but didn't get one. Use this on update endpoints where you want to enforce optimistic concurrency control.

### 429 Too Many Requests
**Use for**: the client has been rate limited.

**Required**: include `Retry-After` header (integer seconds or HTTP date). Strongly recommended: include `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset` headers per the IETF rate-limit-headers draft.

### 451 Unavailable For Legal Reasons
**Use for**: content is blocked due to legal demands (court orders, GDPR, sanctions). Named after Fahrenheit 451.

**In practice**: used by content platforms and search engines; rarely needed in business APIs.

## 5xx — Server errors

The server failed. The client did nothing wrong, and retrying may or may not succeed.

### 500 Internal Server Error
**Use for**: an unexpected error you don't have a more specific code for. Catch-all.

**The body should leak nothing.** Stack traces, error messages from underlying services, internal IDs — none of it. Include only a `request_id` so support can trace it.

### 501 Not Implemented
**Use for**: the server doesn't support the HTTP method *at all* (not just for this resource — that's 405).

**In practice**: rarely used.

### 502 Bad Gateway
**Use for**: a server acting as a gateway/proxy got an invalid response from the upstream. If you're not a gateway, you usually shouldn't return this — use 500 instead.

### 503 Service Unavailable
**Use for**: temporary unavailability — overload, maintenance, dependency outage you expect to recover from.

**Required**: include `Retry-After` header.

**Vs 500**: 503 says "retry, this should resolve". 500 says "we have a bug, retry probably won't help".

This distinction matters because well-written clients use exponential-backoff retry on 503 but give up faster on 500. Misclassifying server bugs as 503 produces retry storms; misclassifying transient unavailability as 500 produces unnecessary failures.

### 504 Gateway Timeout
**Use for**: a gateway/proxy timed out waiting for an upstream. Same caveat as 502 — only if you're actually a gateway.

### 507 Insufficient Storage
**Use for**: storage-quota or disk-full conditions.

### 511 Network Authentication Required
**Use for**: captive-portal scenarios. Rarely used in API contexts.

## The most common mistakes

These are the errors that show up in API reviews repeatedly. Worth checking explicitly:

1. **400 used as a catch-all** for any client problem. → Differentiate: 401, 403, 404, 409, 422 each carry useful information clients can act on.
2. **401 returned to authenticated users who lack permission**. → Use 403 instead.
3. **404 used when the route exists but the body is invalid**. → Use 422.
4. **500 returned for client errors** (validation, permissions). → This causes infinite retry loops. Server is responsible for error class; pick the right 4xx.
5. **422 vs 409 confusion** — most teams pick one and use it for both. → 422 = content invalid; 409 = state conflict. They mean different things to clients.
6. **No `Retry-After` on 429 or 503**. → Spec violation. Required by RFC 9110.
7. **5xx responses bypass the standard error format** because the framework's default handler kicks in. → Make sure your error middleware catches 5xx too, or you'll leak stack traces.

## Quick decision tree

```
Is the request well-formed (parses)?
├── No → 400
└── Yes
    ├── Auth missing/invalid? → 401
    ├── Auth valid but lacks permission?
    │   ├── Existence is sensitive? → 404
    │   └── Otherwise → 403
    ├── Resource doesn't exist? → 404 (or 410 if previously existed)
    ├── Method not supported on this path? → 405
    ├── Conditional request failed? → 412
    ├── Conflicts with current state? → 409
    ├── Semantic validation failure? → 422
    ├── Rate limited? → 429
    └── Otherwise (success) → 2xx

Server-side problem?
├── Transient / will recover? → 503 (with Retry-After)
└── Bug / unexpected? → 500
```

## Sources

- RFC 9110 (HTTP Semantics) §15 — Status Codes (the authoritative reference)
- RFC 6585 — Additional HTTP Status Codes (428, 429, 431, 511)
- RFC 7725 — 451 Unavailable For Legal Reasons
- IETF draft-ietf-httpapi-ratelimit-headers — RateLimit headers
