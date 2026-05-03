# Security — What Not to Leak in Error Responses

Error responses are the most common information-disclosure vector in API security audits. They get less attention than success responses because they're "errors" — but that means more sensitive content (DB messages, stack traces, internal IDs) ends up in them by default.

This reference covers what to filter, why each thing matters, and the common patterns that leak unintentionally.

## The leak categories, ranked by severity

### Severe — direct paths to compromise

**Stack traces.** Every modern web framework will return one in development mode, and sometimes in production if you forget to set the right config. Stack traces reveal:
- Framework name and version (so attackers know which CVEs apply)
- Internal class and module names
- File paths on the server
- Sometimes, environment variables and secrets if the exception captures context

**Always run a "catch-all" error handler that produces a clean 500 response with a request_id, even for unhandled exceptions.** Verify in staging that crashing the handler still produces a clean response.

**Database error messages.** PostgreSQL's "duplicate key value violates unique constraint 'users_email_key'" tells an attacker:
- That you use Postgres
- That `users.email` has a unique constraint (so this email exists)
- The naming convention of your constraints

Catch DB exceptions, log them internally with full detail, return a generic constraint-violation error to the client.

**Internal hostnames, service names, file paths.** From error messages like "could not connect to redis-shard-3.internal.acme.local" or "/var/lib/api/uploads/staging-only-bucket/...". Helps attackers map the internal network.

**Secrets in error context.** Sentry-style frameworks capture local variables on exceptions. If a function has `api_key = ...` in scope when it crashes, that variable can end up in the error response. Audit your error handler to make sure local-variable capture stays internal-only.

### High — enumeration and recon

**Distinguishing "user doesn't exist" from "wrong password"** in login flows. The classic enumeration vulnerability: error response varies by which case is hit, so attackers can confirm whether an email is registered. Always return the same error type, body, and timing for both cases.

**Distinguishing "resource not found" from "permission denied"** when existence is sensitive. Same principle, applied to authorization. If the user can't see the resource, returning 404 hides existence; returning 403 reveals it. Pick the rule and apply consistently — flipping based on permissions itself leaks (you can probe to see if a 403 ever appears for a particular ID).

**Validation order leaking information.** "We checked your password format, then checked the username exists, then checked the password matches" — if your error tells the attacker which step failed, you've leaked. Combine into one generic "credentials invalid" response.

**Rate-limit error revealing precise limits.** "You've made 1000 requests in the last hour" tells attackers exactly how to pace their attack. Include the limit in headers (per the RateLimit spec — that's its purpose), but for malicious-traffic responses you can return generic 429.

### Medium — internal IDs and structure

**Exposing internal numeric IDs** of users, accounts, or other resources. If your URLs look like `/users/12345` and you return `12345` in errors for related resources, attackers can iterate. Use opaque IDs (UUIDs, prefixed strings like `usr_abc123`) for any resource attackers might enumerate.

**Field names that reveal schema you didn't mean to expose.** If your validation error says "Field `internal_admin_flag` is required", you've revealed an internal field. Maintain an allowlist of field names that can appear in error responses; reject any that aren't in the allowlist.

**Validation rule details that aid bypass.** "Password must be 14 chars with at least 2 digits and 1 special char" tells attackers exactly what to optimize for. For password and crypto-relevant validation, use generic messages ("does not meet requirements"); document the rules in a docs page if needed.

### Low — operational metadata

**Server software and version** in `Server` header or error body ("Apache/2.4.41"). Helps attackers pick exploits. Strip the `Server` header at the edge; never echo backend identity.

**Trace IDs / span IDs in a recognizable format**. OpenTelemetry IDs are fine (designed to be safe to expose), but custom internal trace formats can leak structure. Use UUIDs or a documented opaque format.

## Worked examples

### Bad: leaks DB schema

```http
HTTP/1.1 500 Internal Server Error
Content-Type: application/json

{
  "error": "psycopg2.errors.UniqueViolation: duplicate key value violates unique constraint \"users_email_key\"\nDETAIL: Key (email)=(alice@example.com) already exists."
}
```

### Good: catches the constraint, returns a generic 422

```http
HTTP/1.1 422 Unprocessable Content
Content-Type: application/problem+json

{
  "type": "https://api.example.com/problems/email-already-registered",
  "title": "Email already registered",
  "status": 422,
  "detail": "An account with this email address already exists.",
  "instance": "/v1/users",
  "request_id": "req_2H4xY9zA"
}
```

(Note: this *does* confirm the email is registered — that's the enumeration tradeoff. For sign-up flows specifically, this is usually acceptable because users need the feedback. For login flows, do not confirm.)

### Bad: login flow leaks user existence

```http
# Wrong password for existing user
HTTP/1.1 401 Unauthorized
{ "error": "Incorrect password" }

# Non-existent user
HTTP/1.1 404 Not Found
{ "error": "No user with that email" }
```

### Good: login flow gives identical response

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/problem+json

{
  "type": "https://api.example.com/problems/invalid-credentials",
  "title": "Authentication failed",
  "status": 401,
  "detail": "The email or password is incorrect.",
  "instance": "/v1/auth/login"
}
```

### Bad: stack trace in 500 response

```http
HTTP/1.1 500 Internal Server Error
Content-Type: text/html

<pre>Traceback (most recent call last):
  File "/app/handlers/transfers.py", line 47, in create_transfer
    account = db.get_account(account_id, conn=settings.PRIMARY_DB_CONN)
  File "/app/db/accounts.py", line 22, in get_account
    return cur.execute(...)
psycopg2.OperationalError: could not connect to server: Connection refused
    Is the server running on host "primary-db.internal" (10.0.5.42)
</pre>
```

### Good: opaque 500 with request_id

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

The full stack trace and DB error get logged internally, indexed by `req_2H4xY9zA`. Support staff can find them; attackers cannot.

## Implementation patterns

**1. Top-level catch-all middleware.** The outermost middleware on every request catches any uncaught exception and produces the opaque 500 response above. This is the single most important defense — it's the safety net for everything below.

**2. Allowlist of safe fields in error responses.** When constructing an error response, validate that the fields you're including are on a known-safe list. Reject internal field names by policy.

**3. Constant-time auth responses.** Login/credential-check responses should take the same wall-clock time whether the user exists, the password is wrong, or any other failure mode. Use a constant-time comparison and add a fixed-duration sleep if needed. This prevents timing-based enumeration.

**4. Different log destinations for internal vs external info.** When logging an error, log the full stack trace, DB message, and context internally (for ops). Separately, build the response object from a curated subset of fields. Never use the same object for both.

**5. Test error responses in security review.** Write specific tests that:
   - Trigger a DB constraint violation and assert the response contains no SQL
   - Trigger an unhandled exception and assert the response contains no stack trace
   - Hit a 500 and assert the body is fixed-shape with only `type`, `title`, `status`, `instance`, `request_id`
   - Hit login with a non-existent user and an existing user with wrong password; assert responses are byte-identical (modulo `request_id`)

## Review checklist

When reviewing an API for error-response security:

- [ ] No stack traces in any 5xx response (test with a forced exception)
- [ ] No DB error messages reaching clients (test by violating a unique constraint)
- [ ] No internal hostnames, file paths, or service names in any error
- [ ] No secrets/env vars captured in error context
- [ ] Login errors don't distinguish "user doesn't exist" from "wrong password"
- [ ] 404 vs 403 policy is consistent for sensitive resources (pick one rule)
- [ ] Rate limit responses don't reveal exact request counts to malicious traffic
- [ ] Field names in validation errors are on an allowlist
- [ ] Password validation rules aren't fully exposed in error messages
- [ ] `Server` header is stripped or generic
- [ ] Top-level catch-all middleware handles all uncaught exceptions
- [ ] Test suite includes "what leaks in errors" cases

## Sources

- OWASP API Security Top 10 — API8:2023 Security Misconfiguration
- OWASP Cheat Sheet — Error Handling
- RFC 9457 §5 — Security Considerations
- Google AIP-193 — guidance on error message content
