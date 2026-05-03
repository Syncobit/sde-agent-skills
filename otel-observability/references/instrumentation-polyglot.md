# Polyglot Instrumentation — Python, Node.js, Go, Java

The application-side patterns for each major language. The recipes look superficially similar — same API names, same span model — but the idiomatic setup, the autoinstrumentation maturity, and the log bridging differ enough to be worth treating per-language.

## Common ground (all languages)

Every OTel SDK has the same four moving parts:

1. **TracerProvider, MeterProvider, LoggerProvider** — factories that produce tracers/meters/loggers and own the export pipeline.
2. **SDK configuration via environment variables** — `OTEL_SERVICE_NAME`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_TRACES_SAMPLER`, etc. Configuration via env vars is the canonical pattern; code-based configuration is a fallback.
3. **Propagators** — handle the `traceparent`/`tracestate` headers (W3C Trace Context). Default in modern SDKs; AWS X-Ray uses a different format and requires `X-Amzn-Trace-Id` propagator if you want X-Ray correlation.
4. **Exporters** — push telemetry to OTLP (gRPC or HTTP) by default. The Collector receives OTLP and re-routes; vendor-specific exporters skip the Collector but bind the app to the vendor.

The recommended pattern: use OTLP exporters in the app, run a Collector to do vendor-specific work. Per-language code below assumes this.

## Python

Reference stack: FastAPI / Django / Flask / generic.

```bash
pip install \
    opentelemetry-distro \
    opentelemetry-exporter-otlp \
    opentelemetry-instrumentation-fastapi \
    opentelemetry-instrumentation-requests \
    opentelemetry-instrumentation-sqlalchemy
```

### Auto-instrumentation (zero-code)

```bash
opentelemetry-bootstrap --action=install   # discovers and installs instrumentations
opentelemetry-instrument \
    --service_name vqms-api \
    --resource_attributes "deployment.environment.name=production" \
    --traces_exporter otlp \
    --metrics_exporter otlp \
    --logs_exporter otlp \
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

`opentelemetry-instrument` is a shim that initializes the SDK and registers all installed instrumentations before importing your app. Works with Gunicorn, Uvicorn, and most Python WSGI/ASGI servers.

### Manual spans + custom attributes

```python
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

tracer = trace.get_tracer(__name__)

async def call_next_ticket(queue_id: str, terminal_id: str):
    with tracer.start_as_current_span("queue.call_next") as span:
        span.set_attribute("queue.id", queue_id)
        span.set_attribute("terminal.id", terminal_id)
        try:
            ticket = await select_next_ticket(queue_id)
            span.set_attribute("ticket.id", ticket.id)
            return ticket
        except NoTicketsAvailable:
            span.set_status(Status(StatusCode.OK, "no tickets"))   # not an error
            return None
        except Exception as e:
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.record_exception(e)
            raise
```

### Log bridging (existing Python `logging` → OTel logs with trace correlation)

```python
import logging
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

logger_provider = LoggerProvider()
logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))

# Attach OTel handler to root logger
handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
logging.getLogger().addHandler(handler)

# Now any standard logger.info() call gets shipped via OTLP
# AND automatically tagged with the current trace_id and span_id
log = logging.getLogger(__name__)
log.info("ticket called", extra={"ticket.id": ticket.id, "queue.id": queue_id})
```

### Async context propagation

The `contextvars`-based propagation in modern Python SDKs handles `asyncio` correctly. For older async patterns or threadpools, you may need to wrap callables with `opentelemetry.context.attach()` manually. Trace context loss across async boundaries is the most common silent bug — verify by checking that a downstream call's spans appear under the upstream parent in your trace UI.

## Node.js / TypeScript

Reference stack: Express / Fastify / NestJS / generic.

```bash
npm install \
    @opentelemetry/api \
    @opentelemetry/auto-instrumentations-node \
    @opentelemetry/exporter-trace-otlp-grpc \
    @opentelemetry/exporter-metrics-otlp-grpc \
    @opentelemetry/exporter-logs-otlp-grpc \
    @opentelemetry/sdk-node
```

### Auto-instrumentation

```bash
node --require @opentelemetry/auto-instrumentations-node/register app.js
```

Or in a `tracing.js` that you load before your main file:

```javascript
// tracing.js
const { NodeSDK } = require('@opentelemetry/sdk-node');
const { getNodeAutoInstrumentations } = require('@opentelemetry/auto-instrumentations-node');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-grpc');
const { OTLPMetricExporter } = require('@opentelemetry/exporter-metrics-otlp-grpc');
const { PeriodicExportingMetricReader } = require('@opentelemetry/sdk-metrics');

const sdk = new NodeSDK({
  serviceName: process.env.OTEL_SERVICE_NAME ?? 'vqms-api',
  traceExporter: new OTLPTraceExporter(),
  metricReader: new PeriodicExportingMetricReader({
    exporter: new OTLPMetricExporter(),
    exportIntervalMillis: 60_000,
  }),
  instrumentations: [getNodeAutoInstrumentations()],
});

sdk.start();

// Graceful shutdown — IMPORTANT for Cloud Run / Lambda where SIGTERM is delivered on shutdown
process.on('SIGTERM', () => sdk.shutdown().then(() => process.exit(0)));
```

### Manual spans

```typescript
import { trace, SpanStatusCode } from '@opentelemetry/api';

const tracer = trace.getTracer('vqms-api');

async function callNextTicket(queueId: string, terminalId: string) {
  return tracer.startActiveSpan('queue.call_next', async (span) => {
    span.setAttributes({
      'queue.id': queueId,
      'terminal.id': terminalId,
    });
    try {
      const ticket = await selectNextTicket(queueId);
      span.setAttribute('ticket.id', ticket.id);
      return ticket;
    } catch (err) {
      span.setStatus({ code: SpanStatusCode.ERROR, message: err.message });
      span.recordException(err);
      throw err;
    } finally {
      span.end();
    }
  });
}
```

### Log bridging (Pino, Winston, Bunyan → OTel logs)

The OTel logs SDK provides a `LoggerProvider`. For trace correlation, use the Pino/Winston instrumentation packages which auto-inject `trace_id` and `span_id` into log records:

```bash
npm install @opentelemetry/instrumentation-pino
```

The auto-instrumentation registration above includes Pino instrumentation by default. Logs through Pino now carry trace context automatically.

## Go

Reference stack: net/http / gRPC / Gin / Echo.

```bash
go get \
    go.opentelemetry.io/otel \
    go.opentelemetry.io/otel/sdk \
    go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc \
    go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc \
    go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploggrpc \
    go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp
```

Go has no auto-instrumentation (no runtime monkey-patching). Manual setup, but compact:

```go
package main

import (
    "context"
    "net/http"

    "go.opentelemetry.io/otel"
    "go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
    "go.opentelemetry.io/otel/sdk/resource"
    sdktrace "go.opentelemetry.io/otel/sdk/trace"
    semconv "go.opentelemetry.io/otel/semconv/v1.27.0"
    "go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
)

func initTracer(ctx context.Context) (*sdktrace.TracerProvider, error) {
    exp, err := otlptracegrpc.New(ctx)
    if err != nil {
        return nil, err
    }

    res, err := resource.New(ctx,
        resource.WithFromEnv(),                          // OTEL_RESOURCE_ATTRIBUTES env var
        resource.WithProcess(),
        resource.WithHost(),
        resource.WithAttributes(
            semconv.ServiceName("vqms-api"),
            semconv.ServiceVersion("1.4.7"),
        ),
    )
    if err != nil {
        return nil, err
    }

    tp := sdktrace.NewTracerProvider(
        sdktrace.WithBatcher(exp),
        sdktrace.WithResource(res),
    )
    otel.SetTracerProvider(tp)
    return tp, nil
}

func main() {
    ctx := context.Background()
    tp, _ := initTracer(ctx)
    defer tp.Shutdown(ctx)

    // Wrap handlers with otelhttp middleware
    mux := http.NewServeMux()
    mux.Handle("/v1/tickets/", otelhttp.NewHandler(http.HandlerFunc(ticketHandler), "tickets"))
    http.ListenAndServe(":8080", mux)
}

// Manual span inside a handler
func ticketHandler(w http.ResponseWriter, r *http.Request) {
    ctx, span := otel.Tracer("vqms-api").Start(r.Context(), "queue.call_next")
    defer span.End()

    span.SetAttributes(
        attribute.String("queue.id", queueID),
        attribute.String("terminal.id", terminalID),
    )
    // ... business logic ...
}
```

### Logs in Go

The Go SDK reached log GA in 2025. Use `otelslog` to bridge `log/slog` into OTel:

```go
import (
    "log/slog"
    "go.opentelemetry.io/contrib/bridges/otelslog"
)

logger := otelslog.NewLogger("vqms-api")
logger.InfoContext(ctx, "ticket called", "ticket.id", ticket.ID, "queue.id", queueID)
```

The `slog` calls that take a `context.Context` automatically propagate trace correlation.

## Java / Kotlin

Java has the most polished auto-instrumentation in the OTel ecosystem.

### Auto-instrumentation (recommended)

Download the agent JAR:
```bash
curl -L -o opentelemetry-javaagent.jar \
    https://github.com/open-telemetry/opentelemetry-java-instrumentation/releases/latest/download/opentelemetry-javaagent.jar
```

Run with:
```bash
java -javaagent:./opentelemetry-javaagent.jar \
     -Dotel.service.name=vqms-api \
     -Dotel.exporter.otlp.endpoint=http://localhost:4317 \
     -Dotel.resource.attributes=deployment.environment.name=production \
     -jar app.jar
```

The agent instruments hundreds of libraries (servlet containers, JDBC, Kafka, Redis, gRPC, etc.) without code changes. For most Java services, this is enough — no code changes needed for full distributed tracing.

### Manual spans (when needed)

```java
import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.context.Scope;

private static final Tracer tracer = GlobalOpenTelemetry.getTracer("vqms-api");

public Ticket callNextTicket(String queueId, String terminalId) {
    Span span = tracer.spanBuilder("queue.call_next").startSpan();
    try (Scope scope = span.makeCurrent()) {
        span.setAttribute("queue.id", queueId);
        span.setAttribute("terminal.id", terminalId);
        return selectNextTicket(queueId);
    } catch (Exception e) {
        span.recordException(e);
        span.setStatus(StatusCode.ERROR, e.getMessage());
        throw e;
    } finally {
        span.end();
    }
}
```

### Logs

The Java agent auto-injects `trace_id` and `span_id` into MDC (Mapped Diagnostic Context). Existing log statements via Log4j/Logback/SLF4J carry trace context automatically — no code changes. To ship logs via OTel, configure the appropriate appender:

```xml
<!-- logback.xml -->
<appender name="OTEL" class="io.opentelemetry.instrumentation.logback.appender.v1_0.OpenTelemetryAppender"/>
<root level="INFO">
    <appender-ref ref="OTEL"/>
</root>
```

## Cross-language: graceful shutdown

The single most common bug across all SDKs: missing telemetry from the last few seconds of a process's life because spans buffered in memory weren't flushed before exit.

**Always handle SIGTERM/SIGINT** to call the SDK's shutdown method:

- Python: `tracer_provider.shutdown()`
- Node: `sdk.shutdown()`
- Go: `tracerProvider.Shutdown(ctx)`
- Java: handled by the agent automatically; for manual setup, register a JVM shutdown hook

This matters most on Cloud Run and Lambda, where SIGTERM is delivered on container stop and you have ~10 seconds to flush before forced kill.

## Cross-language: testing the instrumentation

Don't ship instrumentation without verifying it produces the data you expect. Two quick tests:

1. **Local Collector with debug exporter** — run a Collector with a `debug` exporter that prints every span/metric/log to stdout. Hit your service with a request; verify the expected spans appear with the expected attributes.
2. **End-to-end propagation test** — make a request that crosses a service boundary; verify the downstream service's spans appear as children of the upstream span in your tracing UI.

A 30-minute investment here saves days of debugging later.

## Sources

- OpenTelemetry specification — opentelemetry.io/docs/specs/otel/
- Per-language SDK status — opentelemetry.io/docs/languages/
- Auto-instrumentation lists — opentelemetry.io/ecosystem/registry/
