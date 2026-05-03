# Sampling and Cost Control

Telemetry costs scale with volume. Without sampling, a service handling 10K req/sec produces 36M traces per hour, billions per month. Sampling reduces volume; the question is which strategy balances coverage against cost.

## The fundamental tradeoff

Every sampling decision is a tradeoff between:

- **Cost** — fewer spans/metrics/logs ingested means lower vendor bills (Cloud Trace charges per span, X-Ray charges per trace, CloudWatch per metric).
- **Coverage** — when an incident happens, you want the relevant traces. Aggressive sampling means the failing request was probably the one dropped.
- **Statistical validity** — for aggregate analysis (latency percentiles, error rates), you need a representative sample. Random sampling preserves this; biased sampling can distort.

The right strategy depends on the signal:

- **Traces**: sample heavily but keep all errors and slow requests
- **Metrics**: don't sample (they're already aggregated), but limit cardinality
- **Logs**: sample by severity (keep all errors and warnings; sample info/debug)

## Trace sampling — head vs tail

### Head-based sampling

The decision is made at the start of the trace, propagated to all child spans via the `sampled` flag in the W3C `traceparent` header. Cheap and simple.

OTel SDK config (parent-based + ratio):

```yaml
OTEL_TRACES_SAMPLER: parentbased_traceidratio
OTEL_TRACES_SAMPLER_ARG: "0.1"        # 10% of root traces
```

Behavior:
- Root span flips a weighted coin (10% sampled).
- Child spans inherit the parent's decision via `traceparent`.
- A trace is either fully sampled or fully not — no half-sampled traces.

**Limitations:**
- Cannot upsample errors. If a request errors but the trace wasn't sampled at start, you don't have the trace.
- Cannot upsample slow requests for the same reason.
- Total volume is predictable but coverage of interesting events is not.

### Tail-based sampling (in the Collector)

The decision is made after the trace completes. Spans are buffered in memory until the trace finishes (or a timeout fires), then sampled based on the full trace's properties.

Collector config:

```yaml
processors:
  tail_sampling:
    decision_wait: 30s              # how long to wait before deciding
    num_traces: 50000               # in-memory buffer size
    expected_new_traces_per_sec: 1000
    policies:
      # Keep all errors
      - name: errors
        type: status_code
        status_code: { status_codes: [ERROR] }

      # Keep all slow requests
      - name: slow
        type: latency
        latency: { threshold_ms: 1000 }

      # Keep all traces from canary deployments
      - name: canary
        type: string_attribute
        string_attribute:
          key: deployment.canary
          values: ["true"]

      # Keep 10% of everything else (random)
      - name: probabilistic
        type: probabilistic
        probabilistic: { sampling_percentage: 10 }
```

Policies are evaluated in order; the first match wins. The "OR-of-policies" semantic means a trace is kept if it matches any policy.

**Tradeoffs:**
- Coverage of important events is excellent — every error is captured.
- Memory cost on the Collector scales with `num_traces × decision_wait × spans_per_trace`. For 1000 traces/sec with 30s wait and 20 spans/trace average, that's 600,000 spans buffered — significant memory.
- Adds 30s latency between when the trace happens and when it's queryable in the backend.
- Requires the Collector pattern (Pattern B from the SKILL.md). Direct exporters can't do tail sampling.

### Recommendation

For Syncobit:
- **Default**: head-based sampling at 10% in the SDK + tail sampling in the Collector for "always keep errors and slow requests"
- **High-traffic services**: drop SDK sampling to 1% or 5%, rely entirely on the Collector's tail sampling for coverage
- **Low-traffic services** (less than 10 req/sec): no sampling, ship everything

The combination of the two is more reliable than either alone — the SDK sampling cuts volume cheaply, the Collector ensures the important traces survive.

## Probabilistic sampling math

If you sample at rate `p`, your effective coverage of a class of events with frequency `f` is:

```
expected_samples_per_hour = f × p × 3600
```

Some examples for a service with 1000 req/sec and 0.5% error rate:

- **All requests, sampled at 1%**: 36,000 traces/hour. Plenty for aggregate analysis.
- **Errors at 1%**: 5 errors traced/min. Marginal for debugging — when an incident happens, you may not catch it.
- **Errors at 100% (via tail sampling)**: 18,000 error traces/hour. Excellent coverage; cost is bounded because errors are rare.

The asymmetry: errors are usually rare relative to total traffic, so 100% error sampling adds modest cost while massively improving incident response. This is why tail-based "100% errors + 1% normal" is the sweet spot.

## Per-service sampling strategy

Different services have different needs. A useful pattern: scope sampling decisions per-service via Collector pipelines.

```yaml
processors:
  tail_sampling/critical:
    decision_wait: 30s
    policies:
      - { name: errors, type: status_code, status_code: { status_codes: [ERROR] } }
      - { name: slow,   type: latency, latency: { threshold_ms: 500 } }
      - { name: prob,   type: probabilistic, probabilistic: { sampling_percentage: 50 } }   # 50%

  tail_sampling/standard:
    decision_wait: 30s
    policies:
      - { name: errors, type: status_code, status_code: { status_codes: [ERROR] } }
      - { name: slow,   type: latency, latency: { threshold_ms: 1000 } }
      - { name: prob,   type: probabilistic, probabilistic: { sampling_percentage: 10 } }   # 10%

service:
  pipelines:
    traces/critical:
      receivers: [otlp]
      processors: [tail_sampling/critical, batch]
      exporters: [googlecloud]
    traces/standard:
      receivers: [otlp]
      processors: [tail_sampling/standard, batch]
      exporters: [googlecloud]
```

Use the `routing_connector` (or the simpler `filter_processor` + multiple pipelines) to route traces to the right pipeline based on `service.name`.

For Syncobit specifically: payments and queue-state-transition services should use the "critical" pipeline; logging/notification services can use "standard".

## Metrics — cardinality, not sampling

Metrics in OTel are aggregated at the SDK or Collector level before export. You don't sample metrics; you control cardinality.

The cardinality killer: a metric tagged with a high-uniqueness attribute. A `http.requests` counter tagged with `user.id` produces a separate time series per user — millions of time series for a B2C service. CloudWatch and Cloud Monitoring both bill per active time series.

**Cardinality budget: aim for under 100 unique attribute combinations per metric, max 10,000 in extreme cases.**

Mitigation:
- Drop high-cardinality attributes at the Collector before they reach the backend
- Bucket numeric attributes (e.g., `latency_bucket: "lt_100ms"` instead of raw latency as a label)
- Use OTel views (in the SDK) to drop attributes from specific instruments
- Aggregate user-specific metrics at the application layer; emit the aggregate metric only

Collector example:

```yaml
processors:
  transform/limit_cardinality:
    metric_statements:
      - context: datapoint
        statements:
          # Drop user_id from all metrics
          - delete_key(attributes, "user.id")
          # Drop session_id
          - delete_key(attributes, "session.id")
          # For http metrics, drop the route's path parameters
          - replace_pattern(attributes["http.route"], "/[0-9a-f-]+", "/{id}") where attributes["http.route"] != nil
```

The `replace_pattern` example is critical for HTTP metrics — without normalizing routes, you get a separate metric for every user ID in the URL path.

## Logs — sampling by severity

Logs are typically not sampled probabilistically; instead, sampled by severity level.

```yaml
processors:
  filter/log_level:
    logs:
      log_record:
        - severity_number < SEVERITY_NUMBER_INFO    # drop debug and below
```

For high-volume info logs, a probabilistic sample on top:

```yaml
processors:
  probabilistic_sampler:
    sampling_percentage: 25
    mode: hash_seed
    hash_seed: 22                                 # deterministic per-trace
```

The `hash_seed` mode samples based on `trace_id`, so all logs for a sampled trace are kept together — preserving the trace-log correlation.

Production posture for most services:
- Keep all `WARN` and above
- Sample 25% of `INFO`
- Drop `DEBUG` (or never emit it in prod)

## Vendor-specific cost notes

### Google Cloud
- **Cloud Trace**: charged per span ingested. Sampling at the Collector caps cost.
- **Managed Service for Prometheus**: charged per sample. ~10,000 active series at 60s scrape = ~14M samples/month — usually cheap.
- **Cloud Logging**: charged per GB ingested. Logs are the biggest cost driver typically.
- Free tier: 50 GB logs, basic Cloud Trace, basic Cloud Monitoring per project per month.

### AWS
- **X-Ray**: $5 per million traces ingested + $0.50 per million scanned in queries. The query cost is the surprise — wide scans across millions of traces add up.
- **CloudWatch Metrics**: $0.30/metric/month for the first 10K metrics, declining tiers after. Cardinality control is critical.
- **CloudWatch Logs**: $0.50/GB ingested + $0.03/GB stored monthly + $0.005 per query GB scanned. Logs are usually the biggest line item.
- **Embedded Metric Format (EMF)** writes to CloudWatch Logs first, which CloudWatch indexes as metrics. You're billed for both the log ingestion AND the metric.

### Cost reality check

For a service handling 100 req/sec, sampled at 10% with 100% error coverage, with logs sampled at 25%, ballpark monthly observability costs:

- GCP: ~$50-200/month for traces + metrics + logs
- AWS: ~$100-400/month (X-Ray + CloudWatch tend to be more expensive at moderate volume)

Get this right early; retrofitting sampling on a high-volume service that's already shipping everything is painful.

## Sources

- OpenTelemetry sampler specification — opentelemetry.io/docs/specs/otel/trace/sdk/#sampling
- Tail sampling processor — github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/processor/tailsamplingprocessor
- Cloud Trace pricing — cloud.google.com/trace/pricing
- X-Ray pricing — aws.amazon.com/xray/pricing
