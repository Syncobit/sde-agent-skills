# Edge Services and RUM — Workers, Edge Functions, Lambda@Edge, Browser

The patterns for instrumenting code that runs *outside* a long-lived server: edge runtimes (Cloudflare Workers, Vercel Edge, Lambda@Edge, CloudFront Functions) and browsers (Real User Monitoring). All of these share two constraints that distinguish them from backend OTel: short-lived isolate processes that can't run a Collector sidecar, and untrusted/uncontrolled execution environments.

The goal: end-to-end traces that start in the user's browser, cross the CDN/edge tier, and continue into backend services without breaking.

## The shape of edge observability

```
┌──────────┐      ┌──────────┐      ┌──────────────┐      ┌────────┐      ┌─────────┐
│ Browser  │ ───→ │ CDN/WAF  │ ───→ │ Edge runtime │ ───→ │ Origin │ ───→ │ Backend │
│ (Web SDK)│      │ logs     │      │ (Worker etc.)│      │ ALB/LB │      │ services│
└──────────┘      └──────────┘      └──────────────┘      └────────┘      └─────────┘
     │                  │                   │                                 │
     └─── traceparent ──┴── traceparent ────┴────── traceparent ──────────────┘
              all carry the same trace_id; spans become a single trace
```

Three distinct instrumentation surfaces (browser, edge runtime, CDN), one shared trace context (W3C `traceparent`). When this is wired correctly, an engineer sees a single trace in the backend's tracing UI that begins with the user's `document.fetch` and ends at the database query.

## Cloudflare Workers

Workers run on V8 isolates. No Node.js, no filesystem, no long-running process — each request is a fresh isolate that may be reused for subsequent requests but can be evicted at any time. The constraints:

- No setTimeout-based batch flushing — the runtime suspends timers between requests
- No sidecar Collector — must export OTLP directly to a remote endpoint
- 30s CPU time limit per request (50ms by default for free, more on paid)
- `ctx.waitUntil()` is the only way to defer work past the response

### The library

`@microlabs/otel-cf-workers` is the production-quality OTel SDK for Workers. It wraps the Workers `fetch` handler, instruments the fetch API for outgoing calls, and instruments KV/D1/R2/Queues bindings with spans. Flushes via `ctx.waitUntil()`.

```typescript
// src/index.ts
import { instrument, ResolveConfigFn } from '@microlabs/otel-cf-workers';

export interface Env {
  OTEL_EXPORTER_OTLP_ENDPOINT: string;
  OTEL_EXPORTER_OTLP_HEADERS: string;          // "x-api-key=..."
  CACHE: KVNamespace;
}

const handler = {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    // Workers KV is auto-instrumented — this fetch becomes a child span
    const cached = await env.CACHE.get(request.url);
    if (cached) return new Response(cached);

    // Outgoing fetch is auto-instrumented; traceparent header is auto-added
    const upstream = await fetch('https://api.vqms.example.com/v1/tickets', {
      headers: { 'authorization': request.headers.get('authorization')! },
    });
    return upstream;
  },
};

const config: ResolveConfigFn = (env: Env) => ({
  exporter: {
    url: env.OTEL_EXPORTER_OTLP_ENDPOINT,
    headers: Object.fromEntries(
      env.OTEL_EXPORTER_OTLP_HEADERS.split(',').map((h) => h.split('=')),
    ),
  },
  service: { name: 'vqms-edge', version: '1.4.7' },
});

export default instrument(handler, config);
```

`wrangler.toml`:
```toml
name = "vqms-edge"
main = "src/index.ts"
compatibility_date = "2026-04-01"
compatibility_flags = ["nodejs_compat"]   # required by the OTel package

[vars]
OTEL_EXPORTER_OTLP_ENDPOINT = "https://otel-gateway.vqms.example.com/v1/traces"
```

Headers and the endpoint should reference your own OTLP gateway (a Collector deployment in your backend), not a vendor's public endpoint directly — see "The proxy pattern" below.

### What gets instrumented automatically
- Inbound `fetch` request → root span (`fetch <method> <route>`)
- Outbound `fetch` → child span with `http.url`, `http.status_code`, `traceparent` injected
- KV (`get`/`put`/`delete`/`list`)
- D1 (queries)
- R2 (object operations)
- Queues (send/receive)
- Durable Objects

### Limitations
- No metrics SDK in Workers (as of May 2026 — the OTel metrics API on V8 isolates is still maturing). Track counters by emitting span events or by aggregating in the Collector from incoming spans.
- No file-based persistent queue — if your OTLP endpoint is down at flush time, the spans are lost. Run a highly available gateway.
- The `compatibility_flags = ["nodejs_compat"]` requirement adds startup cost — measure cold-start impact under your traffic mix.

## Vercel Edge Functions / Next.js middleware

Vercel Edge runs on V8 isolates with the WinterCG runtime — similar constraints to Workers. Next.js middleware also runs in this environment.

The official Vercel OTel package is `@vercel/otel`:

```typescript
// instrumentation.ts (Next.js convention; Vercel auto-loads)
import { registerOTel } from '@vercel/otel';

export function register() {
  registerOTel({
    serviceName: 'vqms-web',
    traceExporter: 'auto',                    // OTLP via VERCEL_OTEL_* env vars
  });
}
```

```bash
# Set in Vercel project env
VERCEL_OTEL_ENDPOINT=https://otel-gateway.vqms.example.com/v1/traces
VERCEL_OTEL_API_KEY=...
```

Vercel auto-instruments incoming requests, outgoing fetch, and Next.js routing. As of May 2026 it does not instrument runtime APIs as deeply as Workers' offering, but it covers the most common cases.

### Limitations
- Edge Functions can't load arbitrary npm modules — anything that requires Node APIs without a polyfill won't work
- No metrics or logs SDKs at edge (use spans for everything)
- Cold-start cost similar to Workers; cold runs are common at low traffic

## AWS Lambda@Edge

Lambda@Edge runs Node.js or Python at CloudFront edge locations. Unlike Workers/Vercel Edge, it's a real Lambda runtime — full Node.js, longer-lived warm containers — but with restrictions: no environment variables, limited execution time per trigger (5s for viewer events, 30s for origin events), and only specific runtime versions.

X-Ray supports Lambda@Edge natively. For OTel:

```python
# index.py
import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
from opentelemetry.propagators.aws import AwsXRayPropagator
from opentelemetry import propagate

# Lambda@Edge does NOT support env vars — bake config into code or use SSM Parameter Store
OTLP_ENDPOINT = "https://otel-gateway.vqms.example.com/v1/traces"

propagate.set_global_textmap(AwsXRayPropagator())
provider = TracerProvider(id_generator=AwsXRayIdGenerator())
provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT))
)
trace.set_tracer_provider(provider)

tracer = trace.get_tracer("vqms-edge-lambda")

def handler(event, context):
    with tracer.start_as_current_span("edge.viewer_request") as span:
        request = event["Records"][0]["cf"]["request"]
        span.set_attribute("http.method", request["method"])
        span.set_attribute("http.uri", request["uri"])
        span.set_attribute("cloudfront.distribution_id",
                          event["Records"][0]["cf"]["config"]["distributionId"])
        # ... edge logic ...
        return request
```

Use the X-Ray ID generator and propagator if you want trace continuity with origin services that read `X-Amzn-Trace-Id`. CloudFront's own X-Ray segments link up automatically.

### Cold-start cost
Lambda@Edge cold starts are *worse* than regular Lambda because the bundle ships to all edge locations. Adding OTel can take cold starts from ~200ms to ~1s+. For viewer-events (latency-critical), prefer X-Ray-only or skip tracing on the viewer hop and start the trace at the origin.

## CloudFront Functions

CloudFront Functions run a restricted JavaScript runtime, no network IO at all. **You cannot instrument these with OTel.** They run for ~1ms and have no way to export telemetry.

The pattern: emit a custom CloudFront real-time log entry containing `traceparent` (received or generated), then have a Kinesis Firehose → Lambda → OTLP gateway pipeline materialize a span from the log entry.

```javascript
// CloudFront Function (cannot import or fetch)
function handler(event) {
  var request = event.request;
  // Generate traceparent if absent — downstream services pick it up
  if (!request.headers['traceparent']) {
    var traceId = generateTraceId();          // 32 hex chars
    var spanId = generateSpanId();             // 16 hex chars
    request.headers['traceparent'] = {
      value: '00-' + traceId + '-' + spanId + '-01'
    };
  }
  return request;
}
```

The trace ID is then carried through to Lambda@Edge / origin / backend, all of which see the same trace. The CloudFront Function itself appears as a "synthetic" span materialized from real-time logs (see "CDN log correlation" below).

## Browser SDK and RUM

Real User Monitoring — instrumenting code in the user's browser to capture page loads, route changes, fetch/XHR, errors, and user interactions, with trace context that links to backend services.

### Stack

```bash
npm install \
  @opentelemetry/api \
  @opentelemetry/sdk-trace-web \
  @opentelemetry/exporter-trace-otlp-http \
  @opentelemetry/instrumentation \
  @opentelemetry/instrumentation-document-load \
  @opentelemetry/instrumentation-fetch \
  @opentelemetry/instrumentation-xml-http-request \
  @opentelemetry/instrumentation-user-interaction \
  @opentelemetry/context-zone
```

### Setup

```typescript
// otel.ts — load before any app code
import { WebTracerProvider } from '@opentelemetry/sdk-trace-web';
import { BatchSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { ZoneContextManager } from '@opentelemetry/context-zone';
import { registerInstrumentations } from '@opentelemetry/instrumentation';
import { DocumentLoadInstrumentation } from '@opentelemetry/instrumentation-document-load';
import { FetchInstrumentation } from '@opentelemetry/instrumentation-fetch';
import { XMLHttpRequestInstrumentation } from '@opentelemetry/instrumentation-xml-http-request';
import { UserInteractionInstrumentation } from '@opentelemetry/instrumentation-user-interaction';
import { Resource } from '@opentelemetry/resources';

const resource = Resource.default().merge(new Resource({
  'service.name': 'vqms-web',
  'service.version': window.__APP_VERSION__,   // baked at build time
  'deployment.environment.name': window.__ENV__,
  // Session ID — generate per session, persist in sessionStorage
  'session.id': getOrCreateSessionId(),
}));

const provider = new WebTracerProvider({ resource });
provider.addSpanProcessor(new BatchSpanProcessor(new OTLPTraceExporter({
  url: 'https://otel-gateway.vqms.example.com/v1/traces',
  // Use beacon API for transmission during page unload
})));
provider.register({ contextManager: new ZoneContextManager() });

registerInstrumentations({
  instrumentations: [
    new DocumentLoadInstrumentation(),         // page load timing as spans
    new FetchInstrumentation({
      // Inject traceparent only on calls to your own backends
      propagateTraceHeaderCorsUrls: [
        /^https:\/\/api\.vqms\.example\.com\//,
      ],
      // Don't propagate to third-party services (CORS will reject anyway)
    }),
    new XMLHttpRequestInstrumentation({
      propagateTraceHeaderCorsUrls: [/^https:\/\/api\.vqms\.example\.com\//],
    }),
    new UserInteractionInstrumentation({
      eventNames: ['click', 'submit'],
    }),
  ],
});
```

### What you get

- **Document Load** — span tree for navigation, DNS, TLS, request, response, DOM parsing, paint events. Correlates with Core Web Vitals.
- **Fetch / XHR** — every API call with timing breakdown; `traceparent` propagated to the listed origins so backend traces show as children.
- **User Interaction** — clicks and submits become spans; child spans (fetches triggered by the interaction) attach correctly via Zone context.
- **Errors** — wrap in `span.recordException` from a global error handler.

### Critical gotcha — CORS and propagateTraceHeaderCorsUrls

Browsers strip custom headers from cross-origin requests unless the server returns `Access-Control-Allow-Headers: traceparent, tracestate, baggage`. **Your backend's CORS config must explicitly allow these headers** or the trace breaks at the browser/backend boundary. This is the single most common bug.

```yaml
# Backend CORS preflight response
Access-Control-Allow-Headers: authorization, content-type, traceparent, tracestate, baggage
```

### The proxy pattern (do not expose the OTLP endpoint directly)

Browsers shipping OTLP directly to vendor endpoints (Honeycomb, Datadog, Grafana Cloud) is technically possible but problematic:
- API keys leak — anything in browser code is public
- No redaction — raw user agents, URLs, error messages with PII go straight to vendor
- Rate limit and cost have no per-tenant control

**The right pattern**: ship browser OTLP to a **gateway Collector** in your own infrastructure. The gateway authenticates the request (signed cookie, short-lived JWT), redacts PII, applies sampling, and forwards to the backend. Browsers never hold your vendor API key.

```yaml
# Gateway Collector — receives browser OTLP
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
        cors:
          allowed_origins:
            - https://app.vqms.example.com
          allowed_headers: [traceparent, tracestate, baggage, authorization]
        auth:
          authenticator: oidc                  # validate session JWT

processors:
  redaction:
    allowed_keys: [http.method, http.url, http.status_code, ...]
    blocked_values:
      - "[\\w.+-]+@[\\w-]+\\.[\\w.-]+"        # email addresses
      - "\\b\\d{4}[\\s-]?\\d{4}[\\s-]?\\d{4}[\\s-]?\\d{4}\\b"   # card numbers
  attributes:
    actions:
      - key: http.user_agent
        action: hash                           # redact UA fingerprinting

  tail_sampling:
    decision_wait: 30s
    policies:
      - { name: errors, type: status_code, status_code: { status_codes: [ERROR] } }
      - { name: probabilistic, type: probabilistic, probabilistic: { sampling_percentage: 5 } }
```

The 5% sampling rate for browser RUM is intentionally low — browser traffic is high-volume and most sessions are uneventful. Keep all errors, sample everything else hard.

### Session correlation

Backend trace + browser trace + session = the full picture. Set `session.id` (a stable identifier per browser session) on every browser span and propagate it to the backend via a custom header (e.g., `x-session-id`) that backend middleware reads and sets as a span attribute.

```yaml
# Browser span
session.id: "sess-9f86d081"
user.id: "u-42"                                # if logged in; never raw email
deployment.environment.name: "production"

# Backend span (root)
session.id: "sess-9f86d081"                    # set from x-session-id header
user.id: "u-42"
```

Now `count(spans) by (session.id)` gives you the user's full journey, frontend included.

## CDN log correlation

CDNs and WAFs produce structured access logs that contain `traceparent` (if present in the request) and a synthetic request ID. These can be materialized as spans in your trace UI by streaming the logs through a transformer.

### Cloudflare — Logpush to OTLP

```bash
# Configure Logpush to push HTTP request logs to a destination
curl -X POST "https://api.cloudflare.com/client/v4/zones/$ZONE/logpush/jobs" \
  -H "Authorization: Bearer $CF_API_TOKEN" \
  -d '{
    "name": "http-requests-to-otel",
    "destination_conf": "https://otel-ingest.vqms.example.com/cloudflare-logs",
    "logpull_options": "fields=ClientIP,ClientRequestHost,ClientRequestMethod,ClientRequestURI,EdgeStartTimestamp,EdgeEndTimestamp,EdgeResponseStatus,RayID,RequestHeaders",
    "dataset": "http_requests",
    "enabled": true
  }'
```

A small ingest service parses the JSON log, extracts `traceparent` from `RequestHeaders`, and emits an OTLP span:

```python
def cloudflare_log_to_span(log_entry: dict):
    traceparent = log_entry["RequestHeaders"].get("traceparent")
    if not traceparent:
        return None  # skip if not part of an existing trace
    # Materialize as a CDN-tier span attached to the existing trace
    span = build_span_from_traceparent(
        traceparent,
        name="cloudflare.edge",
        start_time=log_entry["EdgeStartTimestamp"],
        end_time=log_entry["EdgeEndTimestamp"],
        attributes={
            "cloudflare.ray_id": log_entry["RayID"],
            "cloudflare.colo": log_entry.get("EdgeColoCode"),
            "http.method": log_entry["ClientRequestMethod"],
            "http.host": log_entry["ClientRequestHost"],
            "http.status_code": log_entry["EdgeResponseStatus"],
        },
    )
    return span
```

Now Cloudflare's edge timing appears in the trace tree above the backend spans.

### CloudFront — real-time logs

Same pattern. Configure a CloudFront real-time log subscription with the fields you need (including `cs-headers-traceparent`), pipe to Kinesis Firehose → Lambda → OTLP gateway. The Lambda materializes spans the same way.

```json
// Real-time log fields to enable
{
  "Fields": [
    "timestamp", "c-ip", "cs-method", "cs-uri-stem", "cs-host",
    "sc-status", "x-edge-request-id", "x-edge-location",
    "cs-headers-traceparent", "time-to-first-byte"
  ]
}
```

### GCP Cloud CDN

Cloud CDN logs flow through Cloud Logging. The `httpRequest.traceId` field is auto-populated when `X-Cloud-Trace-Context` is present (GCP's pre-W3C header — Cloud Run/Load Balancer set both). Configure a log sink to BigQuery or Pub/Sub, then materialize spans with the same pattern.

## Edge resource attributes

Use these consistently across edge SDKs:

```yaml
service.name: "vqms-edge"                      # not "vqms-api"; edge tier is a distinct service
service.namespace: "vqms"
deployment.environment.name: "production"

# Edge-specific
cloud.platform: "cloudflare_workers"           # or "vercel_edge", "aws_lambda_edge", "cloudfront_function"
cloud.region: "auto"                            # edge runs everywhere; "auto" is the convention
cloudflare.colo: "FRA"                          # if available
faas.name: "vqms-edge"                          # for Lambda@Edge
faas.coldstart: true                            # if cold

# RUM-specific
session.id: "sess-9f86d081"
browser.name: "Chrome"
browser.version: "121"
browser.platform: "macOS"
geo.country.iso_code: "GB"                     # from CDN headers
```

`cloud.platform` lets you filter "show me only cold starts on Cloudflare" cleanly. The edge-specific values are not yet stabilized in OTel semantic conventions; namespacing under your own prefix (`vqms.edge.*`) is fine until they are.

## Sampling for edge and RUM

Edge and RUM traffic is *much* higher volume than backend (every page load, every fetch, every user interaction). Sample harder:

| Tier | Default sampling | Rationale |
|------|------------------|-----------|
| Browser RUM (uneventful) | 1-5% | High volume, low information density |
| Browser RUM (errors / slow) | 100% | Captured by tail-sampling at the gateway |
| Edge runtime (uneventful) | 5-10% | Higher than RUM; edge often runs business logic |
| Edge runtime (errors / slow) | 100% | Same |
| CDN logs (synthetic spans) | 1% | Massive volume; you only need representative coverage |

Tail sampling at the gateway Collector is the right place to enforce these — head-based sampling at the SDK can't see "this trace eventually erred at the backend".

## Common pitfalls

**Trace breaks at the browser/backend boundary.** Almost always CORS — `Access-Control-Allow-Headers` doesn't include `traceparent`. Backend CORS middleware must allow it.

**Edge cold-start dominates p99 latency.** Adding OTel to a Worker / Edge Function adds ~5-30ms to cold starts. For latency-critical paths, consider sampling at the SDK (1%) so 99% of cold starts don't pay the OTLP export cost.

**Multiple traces per page load.** Document load, route navigation, and the first fetch each starting their own trace. Use a single `WebTracerProvider` and `BatchSpanProcessor` — don't initialize per route.

**Vendor API keys in browser code.** Always proxy through your own gateway. Even "anonymous ingest" tokens get abused — rate-limit at your gateway with per-session quotas.

**`sendBeacon` not used on page unload.** The OTLP HTTP exporter in `@opentelemetry/exporter-trace-otlp-http` uses `fetch` by default; on `beforeunload`, the request may be cancelled. Configure the exporter to fall back to `navigator.sendBeacon` for unload-time spans.

**Workers `nodejs_compat` adds startup time.** Profile your cold-start latency with and without OTel. If the regression is >50ms and you're on the hot path, consider sampling more aggressively at the SDK or moving non-critical instrumentation to a non-blocking Durable Object.

**Lambda@Edge env-var limitation.** Lambda@Edge doesn't support custom env vars. Hardcode the OTLP endpoint or fetch from SSM Parameter Store at runtime (cold-start cost). Don't try to read `OTEL_EXPORTER_OTLP_ENDPOINT` — it won't be set.

**Missing CDN-tier visibility.** A "slow page load" debug session that has only browser and backend spans, no CDN tier, is missing the most likely culprit (cache miss, edge-to-origin latency). Materialize CDN logs as spans early — retrofit is painful.

## Quick checklist

```markdown
## Edge & RUM observability review

- [ ] Browser SDK propagates traceparent only to first-party origins (CORS allowed)
- [ ] Backend CORS middleware allows traceparent, tracestate, baggage headers
- [ ] OTLP endpoint exposed to browser is your own gateway, not a vendor endpoint
- [ ] Vendor API keys never reach browser code
- [ ] Gateway redacts PII (emails, card numbers) from RUM spans
- [ ] sendBeacon fallback configured for unload-time spans
- [ ] session.id set on every browser span and propagated to backend
- [ ] Edge SDK uses ctx.waitUntil (Workers) or equivalent for flush
- [ ] Edge cold-start cost measured with OTel enabled vs disabled
- [ ] CDN logs materialized as spans (Cloudflare Logpush / CloudFront real-time / GCP Cloud Logging sink)
- [ ] Sampling: 1-5% RUM, 5-10% edge, 100% errors at gateway tail sampling
- [ ] service.name distinguishes edge tier from backend ("vqms-edge" vs "vqms-api")
- [ ] cloud.platform attribute set for edge runtime identification
```

## Sources

- W3C Trace Context — w3.org/TR/trace-context
- `@microlabs/otel-cf-workers` — github.com/evanderkoogh/otel-cf-workers
- `@vercel/otel` — vercel.com/docs/observability/otel-overview
- OpenTelemetry JS Web SDK — github.com/open-telemetry/opentelemetry-js/tree/main/packages/opentelemetry-sdk-trace-web
- Cloudflare Logpush — developers.cloudflare.com/logs/about/
- CloudFront real-time logs — docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/real-time-logs.html
- Lambda@Edge restrictions — docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/edge-functions-restrictions.html
- OTel browser semantic conventions (development) — opentelemetry.io/docs/specs/semconv/registry/attributes/browser/