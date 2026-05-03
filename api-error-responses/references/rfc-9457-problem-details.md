# RFC 9457 Problem Details — Deep Dive

The full specification of the recommended default format, with the IANA registry, extension patterns, framework integrations, and worked examples for the common cases.

## The structure

RFC 9457 defines a JSON object (or XML; JSON is the practical default) with these members:

| Field      | Type    | Required? | Purpose                                                                                                  |
|------------|---------|-----------|----------------------------------------------------------------------------------------------------------|
| `type`     | URI     | Recommended | Stable identifier for the problem class. Should resolve to human-readable docs when dereferenced.        |
| `title`    | string  | Recommended | Short, human-readable summary. SHOULD NOT change between occurrences (except for localization).          |
| `status`   | integer | Recommended | The HTTP status code. Duplicates the response status; included so the body is self-contained.            |
| `detail`   | string  | Optional    | Human-readable, occurrence-specific explanation. Can vary between occurrences.                            |
| `instance` | URI     | Optional    | Identifies the specific occurrence. Often the request path; sometimes a unique incident URI.             |
| *anything else* | any | Optional    | Extension members specific to the problem `type`. Part of the `type`'s contract.                          |

The media type is **`application/problem+json`** (or `application/problem+xml` for XML APIs). Setting this content type is what signals to clients that the body is a problem document.

When `type` is absent, it defaults to `"about:blank"` — meaning "use the HTTP status code's standard meaning, no extra info". Returning `about:blank` is perfectly valid for trivial cases but loses the ability to carry extension fields.

## The IANA registry

RFC 9457 §4.2 establishes a public registry at `https://www.iana.org/assignments/http-problem-types`. As of 2025, it's still small but growing. You can either:

1. **Register your problem types publicly** — appropriate for widely reused types (e.g., a payments-industry consortium might register `https://iana.org/assignments/http-problem-types#out-of-credit`).
2. **Mint your own URIs** — most APIs do this. Use a stable URI under your own domain, e.g., `https://api.example.com/problems/insufficient-funds`. Make it dereference to the docs page for that error type.

The URI is just an identifier — clients compare it as a string. They do NOT need to fetch it. But making it dereferenceable is a kindness to humans debugging.

**Anti-pattern**: using URIs that point to RFC sections (`https://tools.ietf.org/html/rfc9110#section-15.5.5`) as the `type`. Some frameworks default to this. It's technically valid but useless — it tells clients nothing more than the status code already does. Always set a meaningful, namespace-stable `type` URI for non-trivial errors.

## Extension members

The extension mechanism is what makes Problem Details actually useful. Any field beyond the core five is part of the problem type's documented contract.

Worked example — out-of-credit error:

```json
{
  "type": "https://api.example.com/problems/out-of-credit",
  "title": "You do not have enough credit",
  "status": 403,
  "detail": "Your current balance is 30, but the requested transfer is 50.",
  "instance": "/v1/account/12345/transfers",
  "balance": 30,
  "required": 50,
  "currency": "USD",
  "topup_url": "https://app.example.com/topup"
}
```

`balance`, `required`, `currency`, and `topup_url` are extension members. They're part of the contract for `out-of-credit` — clients implementing handling for this problem type can rely on them. They're NOT part of the contract for any other problem type.

Rules for designing extension members:

- Use `snake_case` or `camelCase` consistently across your API. RFC 9457 doesn't mandate either; pick one for the whole API.
- Don't reuse field names for different meanings across problem types. If `balance` means dollars in one error and message-count in another, you'll get bug reports.
- Avoid putting sensitive data (internal IDs, secrets, PII) in extensions. Extension fields are visible to anyone who can see the response.

## Worked examples for common cases

### Authentication failures

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/problem+json
WWW-Authenticate: Bearer

{
  "type": "https://api.example.com/problems/invalid-credentials",
  "title": "Authentication failed",
  "status": 401,
  "detail": "The access token is invalid or expired.",
  "instance": "/v1/users/me"
}
```

Note: do NOT differentiate between "no such user" and "wrong password" in the response. Single error type, identical body, prevents enumeration. See `security.md`.

### Authorization failures

```http
HTTP/1.1 403 Forbidden
Content-Type: application/problem+json

{
  "type": "https://api.example.com/problems/insufficient-permissions",
  "title": "Insufficient permissions",
  "status": 403,
  "detail": "Your role does not include the 'transfers:create' permission.",
  "instance": "/v1/transfers",
  "required_permission": "transfers:create"
}
```

Including `required_permission` is debatable from a security standpoint — it tells the client what they'd need. For internal/B2B APIs this is helpful; for consumer apps it can be omitted.

### Resource not found

```http
HTTP/1.1 404 Not Found
Content-Type: application/problem+json

{
  "type": "https://api.example.com/problems/resource-not-found",
  "title": "Resource not found",
  "status": 404,
  "detail": "Order with ID 'ord_abc123' does not exist or has been deleted.",
  "instance": "/v1/orders/ord_abc123"
}
```

### Rate limited

```http
HTTP/1.1 429 Too Many Requests
Content-Type: application/problem+json
Retry-After: 30
RateLimit-Limit: 100
RateLimit-Remaining: 0
RateLimit-Reset: 30

{
  "type": "https://api.example.com/problems/rate-limit-exceeded",
  "title": "Rate limit exceeded",
  "status": 429,
  "detail": "You have exceeded 100 requests per minute. Try again in 30 seconds.",
  "instance": "/v1/transfers",
  "limit": 100,
  "window_seconds": 60,
  "retry_after_seconds": 30
}
```

`Retry-After` header is mandatory for 429 and 503. Duplicating `retry_after_seconds` in the body is a kindness for clients that don't surface headers easily (some browser fetch wrappers).

### Server error

```http
HTTP/1.1 500 Internal Server Error
Content-Type: application/problem+json

{
  "type": "about:blank",
  "title": "Internal Server Error",
  "status": 500,
  "instance": "/v1/transfers",
  "request_id": "req_2H4xY9zA"
}
```

For 500s, return as little as possible. The `request_id` is the only useful field for debugging — support can use it to find the corresponding logs and stack trace internally. NEVER return the actual stack trace.

### Idempotency-related errors (composes with `api-idempotency` skill)

```http
HTTP/1.1 422 Unprocessable Entity
Content-Type: application/problem+json

{
  "type": "https://api.example.com/problems/idempotency-key-mismatch",
  "title": "Idempotency-Key reused with a different request body",
  "status": 422,
  "detail": "The Idempotency-Key was previously used with a different request. Generate a new key for a new operation.",
  "instance": "/v1/charges",
  "key_first_seen_at": "2026-05-03T10:15:00Z"
}
```

## OpenAPI 3.1 schema

A reusable component for any OpenAPI spec:

```yaml
components:
  schemas:
    Problem:
      type: object
      properties:
        type:
          type: string
          format: uri
          default: "about:blank"
          description: A URI reference identifying the problem type.
        title:
          type: string
          description: Short, human-readable summary.
        status:
          type: integer
          format: int32
          minimum: 100
          maximum: 599
        detail:
          type: string
          description: Human-readable explanation specific to this occurrence.
        instance:
          type: string
          format: uri
          description: A URI reference identifying the specific occurrence.
      additionalProperties: true   # extensions allowed

    ValidationProblem:
      allOf:
        - $ref: '#/components/schemas/Problem'
        - type: object
          properties:
            errors:
              type: array
              items:
                type: object
                properties:
                  pointer: { type: string, description: "JSON Pointer per RFC 6901" }
                  code:    { type: string, description: "Stable validation error code" }
                  detail:  { type: string }

  responses:
    BadRequest:
      description: Malformed request
      content:
        application/problem+json:
          schema: { $ref: '#/components/schemas/Problem' }
    ValidationFailed:
      description: Request body fails validation
      content:
        application/problem+json:
          schema: { $ref: '#/components/schemas/ValidationProblem' }
    NotFound:
      description: Resource not found
      content:
        application/problem+json:
          schema: { $ref: '#/components/schemas/Problem' }
```

Then reference these in every operation:

```yaml
paths:
  /v1/orders/{id}:
    get:
      responses:
        '200': { ... }
        '404': { $ref: '#/components/responses/NotFound' }
        '500': { $ref: '#/components/responses/Problem' }
```

This is the fastest way to get consistency across an entire API surface.

## Framework integrations (current as of 2025)

- **Spring Boot 3+**: native support via `ProblemDetail` class. Enable with `spring.mvc.problemdetails.enabled=true` (Spring MVC) or `spring.webflux.problemdetails.enabled=true` (WebFlux). Customize via `@ControllerAdvice` extending `ResponseEntityExceptionHandler`.
- **ASP.NET Core (.NET 8+)**: `Results.Problem(...)`, `ProblemDetails` class, `IProblemDetailsService` for customization, and `IExceptionHandler` for global mapping in .NET 8. .NET 9 added `StatusCodeSelector` for cleaner exception-to-status mapping.
- **FastAPI / Starlette**: not built-in, use `fastapi-problem-details` or write a small exception handler — it's about 30 lines.
- **Express / Node**: no canonical library. The npm package `http-problem-details` and `problem-json` both work; pick one and pin the major version.
- **Go**: standard library doesn't include it. `github.com/moogar0880/problems` is the most-used package, but verify recent maintenance. Often it's simpler to write a 50-line helper.
- **Django REST Framework**: not built-in. Use a custom exception handler — DRF gives you the hook via `EXCEPTION_HANDLER` setting.

When picking a library, the key checks are: does it set `application/problem+json` correctly, does it support extension members, and does it integrate with your routing layer's exception path. Most libraries get the first two right; the third is where they vary.

## Migration from RFC 7807

If you have an existing API on RFC 7807, migration to 9457 is essentially free:
- 9457 is backward compatible. The structure and field names are unchanged.
- The main practical difference is the IANA registry (new) and clarified language around `type` (no semantic change for existing implementations).
- Clients written against 7807 work unchanged with 9457 servers.

You don't need a "migration" — just update your docs to cite 9457 instead of 7807.

## Sources

- RFC 9457 (July 2023) — Problem Details for HTTP APIs (Standards Track, obsoletes RFC 7807)
- IANA HTTP Problem Types Registry — https://www.iana.org/assignments/http-problem-types
- RFC 6901 — JSON Pointer (used for `pointer` fields in extensions)
- Spring Framework reference — `ProblemDetail` documentation
- Microsoft Learn — Problem Details for ASP.NET Core APIs
