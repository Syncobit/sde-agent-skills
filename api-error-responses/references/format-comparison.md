# Format Comparison — Side by Side

A feature-by-feature comparison of the four error format options, plus migration notes for moving between them.

## Feature matrix

| Feature                              | RFC 9457 Problem Details      | JSON:API errors           | google.rpc.Status        | Custom                |
|--------------------------------------|-------------------------------|---------------------------|--------------------------|-----------------------|
| Standardization body                 | IETF (Standards Track)         | jsonapi.org (community)   | Google AIP / gRPC        | None                  |
| Year of current spec                 | 2023 (obsoletes 7807)          | 2022 (v1.1)               | Ongoing AIP-193          | N/A                   |
| Media type                           | `application/problem+json`     | `application/vnd.api+json`| `application/json`       | varies                |
| Multi-error in one response          | Via extension                  | Native (errors array)     | Via `details` array      | varies                |
| Field-level validation pointing      | Via extension                  | Native (`source.pointer`) | Via `BadRequest` detail  | varies                |
| Stable machine-readable code         | `type` URI                     | `code` string             | `status` enum + `ErrorInfo.reason` | varies         |
| Extensibility                        | Open (any extension fields)    | `meta` object + extensions| Typed `details` array    | open                  |
| Localization story                   | None standard                  | None standard             | `LocalizedMessage` type  | varies                |
| Public registry of error types       | Yes (IANA)                     | No                        | No (per-API)             | No                    |
| Framework support                    | Broad (Spring, .NET, FastAPI)  | Narrower (JSON:API stack) | Google libs, gRPC tooling| N/A                   |
| Best for                             | New REST APIs, public APIs     | JSON:API ecosystems       | gRPC + REST gateway, GCP | Existing custom APIs  |

## Verbosity comparison — same error, four formats

A 422 validation error on an email field, expressed in each format:

**RFC 9457 (with extension):**
```json
{
  "type": "https://api.example.com/problems/validation-error",
  "title": "Request validation failed",
  "status": 422,
  "errors": [
    { "pointer": "/email", "code": "INVALID_FORMAT", "detail": "Must be a valid email." }
  ]
}
```

**JSON:API:**
```json
{
  "errors": [
    {
      "status": "422",
      "code": "INVALID_FORMAT",
      "title": "Invalid email format",
      "detail": "Must be a valid email.",
      "source": { "pointer": "/data/attributes/email" }
    }
  ]
}
```

**google.rpc.Status:**
```json
{
  "error": {
    "code": 422,
    "message": "Request validation failed",
    "status": "INVALID_ARGUMENT",
    "details": [
      {
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "reason": "VALIDATION_FAILED",
        "domain": "users.example.com"
      },
      {
        "@type": "type.googleapis.com/google.rpc.BadRequest",
        "fieldViolations": [
          { "field": "email", "description": "Must be a valid email." }
        ]
      }
    ]
  }
}
```

**Custom (typical):**
```json
{
  "code": "INVALID_EMAIL",
  "message": "Email must be a valid format",
  "field": "email"
}
```

Custom is the smallest. RFC 9457 with extensions is the next smallest. Google's format is the largest because it carries gRPC metadata that REST clients don't need.

## Decision criteria — which to actually pick

Walk this top-down:

1. **Are you already using JSON:API for the success path?** → JSON:API errors. Don't mix formats.
2. **Are you fronting a gRPC service with a REST gateway, or building on Google Cloud APIs?** → google.rpc.Status. The gateway will produce it for you anyway.
3. **Do you have an existing custom format on a shipped API with active integrations?** → Keep it (with caveats below). Migration is expensive; consistency for existing clients matters more than format purity.
4. **Otherwise (new API, REST-first)** → RFC 9457 Problem Details.

The last bucket is the largest. RFC 9457 wins by default because:
- It's the only IETF Standards Track option.
- It has the broadest framework support (built into Spring Boot 3+, ASP.NET Core).
- Its extension mechanism is unrestricted, so you can express anything the others can.
- The `application/problem+json` media type is a real protocol-level signal, not just a convention.

## Migration paths

### Custom → RFC 9457

The most common migration. Strategy:

1. **Add Problem Details alongside the custom format**, behind content negotiation. Clients sending `Accept: application/problem+json` get the new format; everyone else gets the old.
2. **Document a sunset date** for the custom format (typically 6-12 months for B2B APIs).
3. **Switch the default response** at sunset; remove the legacy code path 3+ months later after monitoring shows no holdouts.
4. **Versioning option**: introduce as part of a major API version bump (`/v2/`). Cleaner but requires actual v2 work.

Field mapping is usually straightforward — your custom `code` → `type` URI; custom `message` → `detail`; custom `field` → an extension `pointer`.

### RFC 7807 → RFC 9457

No migration needed. 9457 is backward compatible. Update your docs and the `Content-Type` references; the wire format is identical.

### JSON:API → RFC 9457

Possible but rarely worth it. JSON:API errors are tightly coupled to the JSON:API success-response envelope; if you're keeping the success format, keep the error format. Only migrate if you're abandoning JSON:API entirely.

### google.rpc.Status → RFC 9457

Only attempt if you're moving off gRPC. Otherwise the gateway will keep producing google.rpc.Status responses, and you'll be running two formats in parallel.

## When to reject all four and write your own

Almost never — but the legitimate cases:

- **Performance-critical, wire-size-sensitive APIs** (e.g., embedded device control). Here, a binary or compact custom format may win. Don't use these large JSON envelopes.
- **APIs that are part of a larger standardized protocol** that defines its own error format (OAuth 2.0 errors, OData, OCPP, etc.). Use the format the protocol mandates.

If you're tempted to write a custom format for any other reason ("ours is cleaner", "RFC 9457 is too verbose"), the consistency cost across clients and frameworks outweighs the verbosity. Pick a standard.

## Sources

- RFC 9457 (Problem Details for HTTP APIs, July 2023)
- jsonapi.org v1.1 specification
- Google AIP-193 (Errors), aip.dev/193
- google.rpc.Status protobuf definition (googleapis/googleapis on GitHub)
