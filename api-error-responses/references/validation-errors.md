# Validation Errors and Multi-Error Responses

The highest-volume error category for most APIs, and the one with the most decisions to make. This reference covers the patterns that work in practice.

## Single vs multi-error: pick deliberately

**Single-error** (return on first failure): server is simpler, faster (short-circuits validation), and produces less data. Acceptable for:
- Server-to-server APIs where the client is a script that fixes one problem at a time
- Endpoints where validation is sequential by nature (you can't validate field B until field A passes)
- Performance-critical paths where validation cost matters

**Multi-error** (collect all failures, return together): worse server logic, but dramatically better UX. Required for:
- Any API consumed by a UI form (users want to see all problems at once)
- Onboarding/registration flows
- Bulk operations where each item validates independently

The rule of thumb: **if a human will see your validation errors, return multi-error.** Otherwise, single-error is fine.

## Multi-error in RFC 9457

RFC 9457 doesn't natively support multi-error — Section 3.1 explicitly recommends returning the most pressing problem rather than batching. But every real API needs validation arrays, so this is solved with an extension member:

```json
{
  "type": "https://api.example.com/problems/validation-error",
  "title": "Request validation failed",
  "status": 422,
  "instance": "/v1/users",
  "errors": [
    {
      "pointer": "/email",
      "code": "INVALID_FORMAT",
      "detail": "Must be a valid email address."
    },
    {
      "pointer": "/age",
      "code": "OUT_OF_RANGE",
      "detail": "Must be between 18 and 120.",
      "min": 18,
      "max": 120
    },
    {
      "pointer": "/address/postalCode",
      "code": "INVALID_FORMAT",
      "detail": "Must be a 5-digit US ZIP code."
    }
  ]
}
```

Document this `errors` array as part of the contract for the `validation-error` problem type. Each entry should have:

- `pointer`: JSON Pointer (RFC 6901) to the offending field. Use this even for top-level fields (`"/email"` not `"email"`) for consistency.
- `code`: stable machine-readable code for the validation rule. Clients pattern-match on this.
- `detail`: human-readable description of what's wrong.
- Optional extensions specific to the rule: `min`/`max` for range errors, `pattern` for regex errors, `expected_type` for type errors, etc.

## Multi-error in JSON:API

This is JSON:API's native strength:

```json
{
  "errors": [
    {
      "status": "422",
      "code": "INVALID_FORMAT",
      "title": "Invalid email format",
      "detail": "Must be a valid email address.",
      "source": { "pointer": "/data/attributes/email" }
    },
    {
      "status": "422",
      "code": "OUT_OF_RANGE",
      "title": "Value out of range",
      "detail": "Age must be between 18 and 120.",
      "source": { "pointer": "/data/attributes/age" },
      "meta": { "min": 18, "max": 120 }
    }
  ]
}
```

The top-level `errors` is always an array — single-error responses just have one element. This is more uniform than RFC 9457's "single by default, extension for multi".

## JSON Pointer (RFC 6901) — the field-reference format

Use JSON Pointer for `pointer` fields. It's the standardized way to reference locations within JSON:

| Field path                          | JSON Pointer                |
|-------------------------------------|-----------------------------|
| Top-level `email` field             | `/email`                    |
| Nested: `address.postalCode`        | `/address/postalCode`       |
| Array element: `items[0].quantity`  | `/items/0/quantity`         |
| Whole document                      | `""` (empty string)         |
| Field with `/` in name              | escape as `~1` (`/foo~1bar` for `foo/bar`) |
| Field with `~` in name              | escape as `~0`              |

Why JSON Pointer beats the alternatives:

- **vs dotted paths (`address.postalCode`)**: dotted paths conflict with field names containing dots, and there's no agreed escaping rule. JSON Pointer has explicit escaping.
- **vs JSONPath (`$.address.postalCode`)**: JSONPath is a query language for *finding* nodes; it's overkill for *identifying* a single node. Also not standardized.
- **vs custom dot/bracket notation**: every team invents their own; clients have to write parsers per API.

JSON Pointer is small, parseable in 20 lines of code in any language, and it's what JSON Schema and JSON Patch already use. Use it.

For JSON:API specifically: pointers should target the request document structure, which means they include the `/data/attributes/` prefix per JSON:API conventions. For RFC 9457, point to the request body's natural structure (no envelope prefix).

## Validation error code catalog

Maintain a documented list of validation error codes. They should be:

- **Stable**: never rename one. Treat them like enum values.
- **Specific**: `INVALID_EMAIL_FORMAT` is more useful than `INVALID_FIELD`.
- **Reusable**: `OUT_OF_RANGE` should mean the same thing on every endpoint that uses it.

A starter set that covers most APIs:

| Code                       | Meaning                                                              |
|----------------------------|----------------------------------------------------------------------|
| `REQUIRED`                 | Field is required but missing or empty                               |
| `INVALID_FORMAT`           | Field is present but doesn't match the required format/pattern       |
| `INVALID_TYPE`             | Field is the wrong JSON type (string vs number, etc.)                |
| `OUT_OF_RANGE`             | Numeric value is below min or above max                              |
| `TOO_SHORT` / `TOO_LONG`   | String/array length out of bounds                                    |
| `INVALID_CHOICE`           | Value not in the allowed enumeration                                 |
| `DUPLICATE`                | Value collides with an existing record (uniqueness violation)         |
| `NOT_FOUND_REFERENCE`      | Foreign-key-style reference to a non-existent resource               |
| `IMMUTABLE`                | Field cannot be changed after creation                               |
| `CONFLICT`                 | Field's value conflicts with another field in the same request       |
| `FORBIDDEN_CHANGE`         | Caller doesn't have permission to set this specific field            |

Resist adding endpoint-specific codes. If `BANK_ACCOUNT_NOT_VERIFIED` appears on three endpoints, it should be a top-level domain error code in your registry (and likely a `409` rather than a validation error).

## Localization

API error messages will eventually need localization. Plan for it now:

**Option 1 (recommended)**: clients localize from the stable code. Server returns code + English `detail`; clients use the code to look up their own translated message. The `detail` is for developers, not end-users.

**Option 2**: server localizes based on `Accept-Language` header. Server maintains translation tables; `detail` is in the requested language. Simpler for clients but adds server-side complexity and translation maintenance burden.

**Option 3 (Google's approach)**: include both. `message` is English; `details` includes a `LocalizedMessage` with `locale` and `message` fields per RFC.

Whichever you pick, **the machine-readable code stays English/stable across all locales.** Never translate codes.

## Bulk operations — partial success

When the request is "create these 100 items", what do you return if 90 succeed and 10 fail?

**Option A: All-or-nothing.** If any item fails, fail the whole request with 422. Client retries with the failures removed. This is Google's recommendation per AIP-193: "APIs should not support partial errors". Cleaner contract, easier server-side transactions, but worse for genuinely independent items.

**Option B: 207 Multi-Status (WebDAV-derived).** Return per-item success/failure in the body:

```json
{
  "results": [
    { "index": 0, "status": "created", "id": "ord_abc" },
    { "index": 1, "status": "failed", "error": { "code": "INVALID_FORMAT", "detail": "..." } },
    ...
  ]
}
```

**Option C: Long-running operation.** Accept the request, return 202 with a job ID, expose a status endpoint. Errors per item appear in the operation status. Best for very large batches where the request would time out anyway.

For most APIs, pick Option A by default — simpler, easier to reason about, easier to retry. Move to B or C only when the use case clearly demands it (e.g., importing 10,000 records where rejecting all because of one is hostile).

## Sources

- RFC 9457 §3 — Members of a Problem Details Object (and §4.1 on extensions)
- RFC 6901 — JSON Pointer
- jsonapi.org v1.1 — Errors section
- Google AIP-193 — Errors (note on partial errors)
