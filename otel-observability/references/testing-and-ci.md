# Testing Instrumentation and CI Validation

Instrumentation that's never tested is instrumentation that breaks silently. The unique pain of observability bugs: a service compiles, deploys, and serves traffic correctly while emitting wrong or no telemetry, and you only discover it during an incident when the data you need was never captured.

This file is the CI-friendly testing patterns: in-memory exporters for unit/integration tests, snapshot tests for instrumentation, Collector config validation, and convention drift detection.

## What to test

Three layers, each catches different bugs:

| Layer | What it verifies | Cost to set up |
|-------|-----------------|----------------|
| **Unit — span shape** | A specific code path emits the spans/attributes you expect | Low; in-memory exporter |
| **Integration — propagation** | Trace context survives across async/IPC boundaries | Medium; in-memory + a fake downstream service |
| **CI — config validity** | Collector config parses, references no missing processors/exporters | Low; `otelcol validate` command |
| **CI — convention drift** | Custom attributes follow your namespace; no PII keys leaked | Low; lint script over codebase |

## Unit testing — InMemorySpanExporter

Every OTel SDK ships an in-memory exporter for tests. The pattern: replace the OTLP exporter with an in-memory one, run the code under test, assert on captured spans.

### Python

```python
# conftest.py
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

@pytest.fixture
def span_exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter
    exporter.clear()


# test_order_processing.py
def test_process_order_emits_expected_span(span_exporter):
    # The system under test
    process_order(order_id="ord-42", tenant_id="acme")

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1

    span = spans[0]
    assert span.name == "process_order"
    assert span.attributes["order.id"] == "ord-42"
    assert span.attributes["tenant.id"] == "acme"
    assert span.status.status_code.name == "OK"


def test_failed_order_records_error(span_exporter):
    with pytest.raises(InvalidOrderError):
        process_order(order_id="ord-bad")

    spans = span_exporter.get_finished_spans()
    assert spans[0].status.status_code.name == "ERROR"
    assert spans[0].events[0].name == "exception"
    assert spans[0].events[0].attributes["exception.type"] == "InvalidOrderError"
```

The `SimpleSpanProcessor` (synchronous) instead of `BatchSpanProcessor` is critical — batch is async and tests would have to wait.

### Node.js

```typescript
import { InMemorySpanExporter, SimpleSpanProcessor } from '@opentelemetry/sdk-trace-base';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { trace } from '@opentelemetry/api';

let exporter: InMemorySpanExporter;
let provider: NodeTracerProvider;

beforeEach(() => {
  exporter = new InMemorySpanExporter();
  provider = new NodeTracerProvider();
  provider.addSpanProcessor(new SimpleSpanProcessor(exporter));
  provider.register();
});

afterEach(() => {
  exporter.reset();
});

test('processOrder emits expected span', async () => {
  await processOrder({ orderId: 'ord-42', tenantId: 'acme' });

  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(1);
  expect(spans[0].name).toBe('process_order');
  expect(spans[0].attributes['order.id']).toBe('ord-42');
  expect(spans[0].attributes['tenant.id']).toBe('acme');
  expect(spans[0].status.code).toBe(SpanStatusCode.OK);
});
```

### Go

```go
package order_test

import (
    "context"
    "testing"

    "github.com/stretchr/testify/assert"
    "go.opentelemetry.io/otel"
    sdktrace "go.opentelemetry.io/otel/sdk/trace"
    "go.opentelemetry.io/otel/sdk/trace/tracetest"
)

func setupTracer(t *testing.T) *tracetest.SpanRecorder {
    t.Helper()
    rec := tracetest.NewSpanRecorder()
    tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(rec))
    otel.SetTracerProvider(tp)
    t.Cleanup(func() { tp.Shutdown(context.Background()) })
    return rec
}

func TestProcessOrder(t *testing.T) {
    rec := setupTracer(t)

    ProcessOrder(context.Background(), "ord-42", "acme")

    spans := rec.Ended()
    assert.Len(t, spans, 1)
    assert.Equal(t, "process_order", spans[0].Name())

    attrs := spans[0].Attributes()
    assert.Equal(t, "ord-42", findStringAttr(attrs, "order.id"))
    assert.Equal(t, "acme", findStringAttr(attrs, "tenant.id"))
}
```

### Java

```java
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.export.SimpleSpanProcessor;
import org.junit.jupiter.api.*;

import static org.assertj.core.api.Assertions.assertThat;

class OrderProcessorTest {
    static InMemorySpanExporter exporter;
    static SdkTracerProvider provider;

    @BeforeAll
    static void setup() {
        exporter = InMemorySpanExporter.create();
        provider = SdkTracerProvider.builder()
            .addSpanProcessor(SimpleSpanProcessor.create(exporter))
            .build();
    }

    @AfterEach
    void clear() { exporter.reset(); }

    @Test
    void processOrder_emitsExpectedSpan() {
        new OrderProcessor(provider.get("test")).process("ord-42", "acme");

        assertThat(exporter.getFinishedSpanItems())
            .singleElement()
            .satisfies(span -> {
                assertThat(span.getName()).isEqualTo("process_order");
                assertThat(span.getAttributes().get(stringKey("order.id"))).isEqualTo("ord-42");
            });
    }
}
```

## Snapshot testing — guard against silent attribute drift

Asserting on every attribute by hand is brittle. A snapshot test serializes the captured spans and compares against a stored fixture; changes show up as a clear diff in CI.

```python
import json
from pathlib import Path

def test_order_flow_snapshot(span_exporter, snapshot):
    process_order(order_id="ord-42", tenant_id="acme")

    spans = [
        {
            "name": s.name,
            "kind": s.kind.name,
            "status": s.status.status_code.name,
            "attributes": dict(sorted(s.attributes.items())),
            "events": [{"name": e.name} for e in s.events],
        }
        for s in span_exporter.get_finished_spans()
    ]
    # Compare against tests/snapshots/order_flow.json
    snapshot.assert_match(json.dumps(spans, indent=2, sort_keys=True), "order_flow.json")
```

When you add a new attribute, the snapshot diff makes it visible in code review. When you accidentally remove or rename one, the test fails. Snapshot files belong in version control.

Don't snapshot trace IDs, timestamps, or duration — those vary per run. Strip them before comparison.

## Integration testing — propagation across boundaries

Unit tests verify a single function's spans. Propagation tests verify trace context survives the boundary that breaks most often: async tasks, message queues, HTTP calls.

### HTTP propagation test

```python
import httpx
from opentelemetry.propagate import inject, extract

def test_http_propagation(span_exporter):
    """Verify that traceparent in request headers reaches the downstream service span."""
    with tracer.start_as_current_span("client") as client_span:
        # Make request to a fake server that captures headers
        with run_fake_server() as server:
            httpx.get(f"{server.url}/echo")  # auto-instrumented

        received_headers = server.last_request_headers
        assert "traceparent" in received_headers

        # Verify the downstream span (recorded server-side) is a child of the client span
        spans = span_exporter.get_finished_spans()
        client = next(s for s in spans if s.name == "client")
        server_span = next(s for s in spans if s.name == "GET /echo")
        assert server_span.parent.span_id == client.context.span_id
```

### Async propagation test (Python)

```python
import asyncio

async def child_work():
    with tracer.start_as_current_span("child") as span:
        span.set_attribute("worked", True)

async def parent_work():
    with tracer.start_as_current_span("parent"):
        await asyncio.gather(child_work(), child_work())

def test_async_context_propagation(span_exporter):
    asyncio.run(parent_work())

    spans = span_exporter.get_finished_spans()
    parent = next(s for s in spans if s.name == "parent")
    children = [s for s in spans if s.name == "child"]

    assert len(children) == 2
    for child in children:
        assert child.parent.trace_id == parent.context.trace_id
        assert child.parent.span_id == parent.context.span_id
```

If this test fails — children appear as separate traces with no parent — your async path is broken. The fix is usually a `contextvars`-aware executor or explicit `context.attach()` around the async submission.

### Pub/Sub / SQS propagation test

```python
def test_pubsub_propagation(span_exporter, fake_pubsub):
    with tracer.start_as_current_span("publisher"):
        publish_event(fake_pubsub, "event-1", payload={"x": 1})

    # Consumer pulls the message and processes it
    consume_one_message(fake_pubsub)

    spans = span_exporter.get_finished_spans()
    pub = next(s for s in spans if s.name == "publisher")
    consume = next(s for s in spans if s.name == "pubsub.process")

    # Same trace ID = propagation worked
    assert consume.context.trace_id == pub.context.trace_id
```

Run integration tests in CI on every PR. They catch the bugs that unit tests can't.

## Collector config validation

Every Collector config change should be validated in CI before deploy. Collector ships a config validator:

```bash
otelcol validate --config=config.yaml
otelcol-contrib validate --config=config.yaml          # for the contrib distro

# AWS distro
aws-otel-collector validate --config=config.yaml
```

Exit code 0 = valid, non-zero = invalid (with line-numbered errors). Wire into CI:

```yaml
# .github/workflows/validate-collector-config.yml
name: Validate Collector configs
on: [push, pull_request]
jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - name: Validate
        run: |
          docker run --rm \
            -v "$PWD/configs:/etc/otelcol" \
            otel/opentelemetry-collector-contrib:0.121.0 \
            validate --config=/etc/otelcol/gateway.yaml
```

The validator catches:
- Typos in processor/exporter names (`memory_limmiter`)
- References to undefined extensions (e.g., `auth: oidc` without an `oidc:` extension)
- Type mismatches in YAML
- Pipeline references to undefined receivers/processors/exporters

It does NOT catch:
- Wrong endpoint URLs
- Bad TLS certificate paths
- IAM permission issues at runtime
- Logical errors (sampling rate of 200%)

For end-to-end validation, run a containerized Collector against the config in CI with a fake OTLP source and assert that traces flow:

```bash
# Start the Collector with the production config
docker run -d --name otelcol-test \
    -v "$PWD/configs/gateway.yaml:/etc/otelcol/config.yaml" \
    -p 4317:4317 \
    otel/opentelemetry-collector-contrib:0.121.0

# Send a test span via grpcurl
grpcurl -plaintext -d @ localhost:4317 \
    opentelemetry.proto.collector.trace.v1.TraceService/Export \
    < test-span.json

# Verify it reached the (mocked) backend
docker logs otelcol-test | grep "spans"
```

## Schema and convention drift detection

A recurring enterprise problem: services drift from your team's attribute conventions over time. Service A uses `tenant_id`, service B uses `tenant.id`, service C uses `customerId`. By the time someone notices, querying across services is impossible.

### Lint custom attributes

A simple grep-based linter catches the most common drift in CI:

```bash
#!/usr/bin/env bash
# scripts/lint-otel-attributes.sh
# Fail CI if any code uses non-namespaced or PII-shaped attribute keys

PROHIBITED_KEYS=(
    "user_id"            # use vqms.user.id (and hash it)
    "tenant_id"          # use tenant.id
    "customerId"         # use vqms.customer.id
    "email"              # never; PII
    "password"           # never
    "credit_card"        # never
    "ssn"                # never
)

for key in "${PROHIBITED_KEYS[@]}"; do
    matches=$(rg --no-heading "set_attribute\(['\"]${key}['\"]" -- src/ tests/ || true)
    if [ -n "$matches" ]; then
        echo "ERROR: prohibited attribute key '${key}' used:"
        echo "$matches"
        exit 1
    fi
done

echo "Attribute lint passed."
```

For more sophisticated checks (allow-list of permitted keys per package), use OpenTelemetry's **Weaver** tool (`semantic-conventions.md`) — it can generate language-specific constants from a YAML schema and lint code against the schema.

### Weaver schema check

```yaml
# semconv/vqms.yaml
groups:
  - id: vqms.attributes
    type: attribute_group
    brief: VQMS-specific span attributes
    attributes:
      - id: tenant.id
        type: string
        brief: Logical tenant identifier
        examples: ["acme", "beta-corp"]
      - id: vqms.queue.id
        type: string
        brief: Queue identifier
      - id: vqms.ticket.id
        type: string
        brief: Ticket identifier
```

```bash
# Validate any code references match the schema
weaver registry check --registry=./semconv

# Generate Python constants from the schema
weaver registry generate \
    --registry=./semconv \
    --templates=python \
    --output=./vqms/otel_attributes.py
```

The generated constants prevent runtime typos:

```python
from vqms.otel_attributes import VqmsAttributes

# Typo here is a Python error at import time, not a silent telemetry bug
span.set_attribute(VqmsAttributes.TENANT_ID, tenant_id)
```

### PII detection in CI

A complementary check: scan emitted attribute values during integration tests for PII patterns. If a test produces a span with an email-shaped value, fail the build:

```python
import re

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
CARD_RE = re.compile(r"\b\d{16}\b")

def assert_no_pii(span_exporter):
    for span in span_exporter.get_finished_spans():
        for key, value in span.attributes.items():
            if not isinstance(value, str):
                continue
            assert not EMAIL_RE.search(value), \
                f"Email-shaped value in attribute {key}: {value}"
            assert not CARD_RE.search(value), \
                f"Card-shaped value in attribute {key}: {value}"
```

Run this as the last step of every integration test. PII bugs caught at PR time are dramatically cheaper than caught in production.

## Versioning and SDK upgrade testing

OTel SDK upgrades occasionally introduce breaking changes (renamed attributes, deprecated APIs). The pattern: pin SDK versions, upgrade in a dedicated branch, run the snapshot tests, review diffs.

```bash
# Pin in pyproject.toml / package.json / go.mod
# Upgrade quarterly with explicit testing
pip install --upgrade opentelemetry-api opentelemetry-sdk
pytest tests/instrumentation/  # snapshot diffs surface any change
```

For Collector upgrades, run the new version against the existing config in a staging environment for at least a week before promoting to production. Schema changes in processors (e.g., a renamed config key) often manifest only on hot paths.

## CI integration — a complete pipeline

```yaml
# .github/workflows/observability.yml
name: Observability checks
on: [push, pull_request]

jobs:
  test-instrumentation:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - name: Run unit tests
        run: pytest tests/instrumentation/ -v
      - name: Run integration tests
        run: pytest tests/propagation/ -v

  validate-collector-config:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        config: [gateway.yaml, agent.yaml, sidecar-cloud-run.yaml]
    steps:
      - uses: actions/checkout@v6
      - name: Validate ${{ matrix.config }}
        run: |
          docker run --rm \
            -v "$PWD/configs/${{ matrix.config }}:/etc/otelcol/config.yaml" \
            otel/opentelemetry-collector-contrib:0.121.0 \
            validate --config=/etc/otelcol/config.yaml

  lint-attributes:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - name: Run attribute linter
        run: ./scripts/lint-otel-attributes.sh

  weaver-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - name: Install Weaver
        run: |
          curl -L -o weaver.tar.xz https://github.com/open-telemetry/weaver/releases/latest/download/weaver-linux.tar.xz
          tar -xJf weaver.tar.xz
          chmod +x weaver
      - name: Validate schema
        run: ./weaver registry check --registry=./semconv
```

## Common pitfalls

**Using `BatchSpanProcessor` in tests.** Spans are flushed asynchronously; tests pass on fast machines and fail on slow CI. Always `SimpleSpanProcessor` in tests.

**Globally registered TracerProvider leaking between tests.** First test sets up the global provider; second test inherits it. Use `setUp/tearDown` (or fixtures) that recreate the provider per test, or scope per-test.

**Asserting on duration / timestamp.** Tests become flaky. Strip these from snapshots and assert structurally.

**Validator passing but config still broken at runtime.** `otelcol validate` is a static check. End-to-end tests with a real Collector + fake backend catch runtime issues.

**Weaver schema not enforced.** Schema exists in `semconv/` but no CI step runs `weaver registry check`. Add the step or the schema decays.

**Lint rules with too many false positives.** A linter that fails on `user_id` for a function variable named `user_id` (not an attribute) gets disabled. Scope the linter to known attribute-setting calls (`set_attribute`, `setAttribute`, etc.).

**Snapshot drift from non-deterministic attributes.** Random IDs, timestamps, environment-specific values. Either strip before snapshotting or use placeholder substitution.

**Tests don't cover error paths.** A span's status code, exception event, and error attributes only fire on failure. If your tests only exercise the happy path, you're not actually validating the error span shape.

## Quick checklist

```markdown
## Instrumentation testing review

- [ ] InMemorySpanExporter (or equivalent) used in unit tests
- [ ] SimpleSpanProcessor (not Batch) in test setup
- [ ] Test setup creates fresh provider per test (no global state leak)
- [ ] Snapshot tests exist for representative request flows
- [ ] Snapshots strip duration / timestamp / random IDs
- [ ] Integration tests verify trace context propagation across async, HTTP, queue boundaries
- [ ] Error path coverage: tests assert span.status, exception event, error attributes
- [ ] Collector configs validated in CI (otelcol validate per config file)
- [ ] End-to-end Collector test with fake source + sink
- [ ] Attribute linter (Weaver schema check OR custom grep) runs in CI
- [ ] PII scanner runs over test-emitted attribute values
- [ ] OTel SDK and Collector versions pinned; upgrades tested before merge
```

## Sources

- OpenTelemetry SDK testing utilities — opentelemetry.io/docs/languages/python/testing/
- Collector config validation — opentelemetry.io/docs/collector/configuration/#validation
- Weaver — github.com/open-telemetry/weaver
- W3C Trace Context — w3.org/TR/trace-context (used for propagation tests)