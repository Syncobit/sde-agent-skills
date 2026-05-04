# Multi-Tenancy in Observability

Multi-tenant SaaS has observability requirements that single-tenant systems don't: a noisy tenant must not drown out a quiet one in sampling decisions, support engineers must be able to scope queries to one tenant without scrolling through everyone else's traffic, finance must be able to attribute observability cost back to tenants, and a tenant deletion request must purge that tenant's telemetry within compliance windows.

This file is the patterns. Most can be enforced at the Collector tier without per-app changes; some require app-side context propagation.

## The required attribute: `tenant.id`

Every span, log record, and metric data point that crosses a tenant boundary should carry `tenant.id`. Set it once at request entry (auth middleware) and propagate via OTel context — every child span inherits it.

```python
# Middleware sets tenant.id on the entry span
def tenant_middleware(request, handler):
    span = trace.get_current_span()
    span.set_attribute("tenant.id", request.tenant_id)
    span.set_attribute("tenant.tier", request.tenant_tier)   # enterprise, standard, free
    span.set_attribute("tenant.region", request.tenant_region)
    return handler(request)
```

For metrics — emit `tenant.id` as an attribute on counters/histograms only when cardinality is bounded (typically yes for B2B SaaS with hundreds-thousands of tenants; usually no for B2C with millions of users).

For logs — `tenant.id` should be in the MDC baseline (see `log-levels.md`).

`tenant.id` is the join key for everything below. Without it, none of the patterns work.

## Isolation strategies

Three viable models for isolating tenant telemetry. Pick by tenant-tier and compliance needs.

### Model 1 — Shared backend, attribute-scoped queries

All tenants' telemetry goes to one backend; queries filter by `tenant.id`.

| Pros | Cons |
|------|------|
| Cheapest infrastructure | Engineering team can see all tenants' data |
| Simplest to operate | Query mistakes leak cross-tenant info |
| Works for all backends | No regulatory isolation |

Default for most SaaS. Combine with strict access controls in the vendor backend (RBAC limiting which tenants each engineer can query).

### Model 2 — Shared backend, per-tenant project/workspace/dataset

Telemetry is partitioned at the backend level. Each tenant gets a separate project (GCP), account/log group prefix (AWS), workspace (Datadog), or dataset (Honeycomb).

```yaml
# Routing in the gateway Collector
processors:
  routing/by_tenant:
    from_attribute: tenant.id
    table:
      - value: acme
        exporters: [googlecloud/acme]
      - value: beta
        exporters: [googlecloud/beta]
    default_exporters: [googlecloud/shared]   # for tenants without dedicated workspace

exporters:
  googlecloud/acme:
    project: vqms-tenant-acme
  googlecloud/beta:
    project: vqms-tenant-beta
  googlecloud/shared:
    project: vqms-prod
```

Reserve this for **enterprise tenants with isolation requirements** (regulated industries, dedicated tier customers). Operating one project per of 1000 tenants is unmanageable; one per 10-50 enterprise tenants is fine.

| Pros | Cons |
|------|------|
| Cleaner access control (vendor-level IAM per project) | More expensive (per-project base costs) |
| Cross-tenant query is impossible (good for compliance) | Gateway routing logic must stay in sync with tenant list |
| Per-tenant retention/CMEK policies | Operational overhead per tenant |

### Model 3 — Per-tenant Collector + per-tenant backend

A dedicated Collector instance and backend per tenant. Used only for highest-tier customers (regulated industries, dedicated cloud).

| Pros | Cons |
|------|------|
| Strongest isolation | High infra cost |
| Tenant can BYO backend | Operational burden per tenant |
| Independent failure domains | Gateway tier becomes complex |

Reserve for "white-glove" enterprise tier where the customer specifically requires it.

### Picking a model — the decision matrix

| Tenant tier | Volume | Compliance | Recommended model |
|-------------|--------|------------|-------------------|
| Free / standard SaaS | Any | None | Model 1 (shared, attribute-scoped) |
| Enterprise standard | Moderate | SOC 2 | Model 1 + per-tenant access controls |
| Enterprise regulated | Any | HIPAA / PCI / GDPR | Model 2 (per-tenant project/workspace) |
| Sovereign / dedicated | Any | National data residency | Model 3 (dedicated Collector + backend) |

## Per-tenant sampling — the noisy-neighbor problem

A naive 10% probabilistic sampler treats all traffic the same. If one tenant produces 90% of your traffic, they get 90% of the sampled traces and the small tenants are nearly invisible. Tenant-aware sampling fixes this.

### Pattern A — per-tenant sampling rates

```yaml
processors:
  tail_sampling:
    decision_wait: 30s
    policies:
      # Always keep errors for everyone
      - name: errors
        type: status_code
        status_code: { status_codes: [ERROR] }

      # Always keep slow requests for everyone
      - name: slow
        type: latency
        latency: { threshold_ms: 1000 }

      # Enterprise tenants: sample 50%
      - name: enterprise_traces
        type: and
        and:
          and_sub_policy:
            - name: enterprise
              type: string_attribute
              string_attribute: { key: tenant.tier, values: [enterprise] }
            - name: prob_50
              type: probabilistic
              probabilistic: { sampling_percentage: 50 }

      # Standard tenants: sample 10%
      - name: standard_traces
        type: and
        and:
          and_sub_policy:
            - name: standard
              type: string_attribute
              string_attribute: { key: tenant.tier, values: [standard] }
            - name: prob_10
              type: probabilistic
              probabilistic: { sampling_percentage: 10 }

      # Free tenants: sample 1%
      - name: free_traces
        type: and
        and:
          and_sub_policy:
            - name: free
              type: string_attribute
              string_attribute: { key: tenant.tier, values: [free] }
            - name: prob_1
              type: probabilistic
              probabilistic: { sampling_percentage: 1 }
```

Now an enterprise tenant's traces are 50x more represented than a free tier tenant's, regardless of relative traffic volume.

### Pattern B — per-tenant rate limiting at the SDK

Limit how many spans any one tenant can produce per minute. Prevents one runaway tenant from saturating the pipeline:

```python
# Per-tenant token bucket sampler — pseudocode
class PerTenantSampler(Sampler):
    def __init__(self, default_rate: float, per_tenant_max_spans_per_min: int):
        self.default_rate = default_rate
        self.tenant_buckets = {}  # tenant_id → token bucket

    def should_sample(self, parent_context, trace_id, name, kind, attributes, ...):
        tenant_id = attributes.get("tenant.id", "unknown")
        bucket = self.tenant_buckets.setdefault(tenant_id, TokenBucket(...))
        if not bucket.try_consume():
            return SamplingResult(decision=Decision.DROP)
        # Otherwise apply normal probabilistic sampling
        return ProbabilitySampler(self.default_rate).should_sample(...)
```

Implementations exist in some OTel community packages; for production, the Collector tail sampler with `rate_limiting` policy is usually sufficient:

```yaml
- name: rate_limit_per_tenant
  type: rate_limiting
  rate_limiting: { spans_per_second: 100 }
  # Combined with attribute filters above to scope per-tenant
```

### Pattern C — drop traffic from terminated tenants

A tenant whose contract ended shouldn't generate billing-impacting telemetry while their account is being decommissioned:

```yaml
processors:
  filter/drop_terminated:
    error_mode: ignore
    traces:
      span:
        - 'IsMatch(attributes["tenant.id"], "^(acme-deleted|beta-suspended)$")'
    metrics:
      datapoint:
        - 'IsMatch(attributes["tenant.id"], "^(acme-deleted|beta-suspended)$")'
    logs:
      log_record:
        - 'IsMatch(attributes["tenant.id"], "^(acme-deleted|beta-suspended)$")'
```

Update the regex from your tenant-state database (CRD, ConfigMap, or external API) on a schedule.

## Cost attribution / chargeback

To attribute observability cost back to tenants, you need volume per tenant. The Collector emits this naturally if you set up the right pipeline.

### Volume metrics per tenant

```yaml
processors:
  groupbyattrs/tenant:
    keys: [tenant.id]
  count/per_tenant:
    spans:
      tenant_span_count:
        description: "Spans by tenant"
        attributes:
          - key: tenant.id
        conditions:
          - "true"

exporters:
  prometheus/internal:                  # internal metrics for chargeback
    endpoint: 0.0.0.0:8889

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, count/per_tenant, batch]
      exporters: [otlphttp/backend, prometheus/internal]
```

The Collector now exposes `tenant_span_count{tenant_id="acme"}` etc. Scrape into your finance/FinOps dashboard.

### Chargeback math

```
monthly_cost(tenant) = vendor_cost_per_span × spans(tenant) +
                       vendor_cost_per_metric_dp × metrics(tenant) +
                       vendor_cost_per_log_gb × log_volume(tenant)
```

For Datadog's flat APM pricing, divide flat cost by tenant-share-of-total-spans for proportional attribution. For pay-per-span backends (Honeycomb, X-Ray), it's direct.

Surface this back to product teams: "tenant Acme costs us $1,200/month in observability". Surfaces tenants whose pricing tier doesn't cover their telemetry footprint.

## Per-tenant retention

Different tenants may have different retention requirements (regulation, contract). With Model 2 (per-tenant project), this is straightforward — each project has its own retention policy. With Model 1 (shared backend), most vendors don't support per-record retention; the workaround is per-tenant log buckets:

```bash
# GCP — log sink filtered by tenant.id
gcloud logging sinks create tenant-acme-7y \
    bigquery.googleapis.com/projects/PROJECT/datasets/logs_acme \
    --log-filter='jsonPayload."tenant.id"="acme" AND severity >= "INFO"'

bq update --time_partitioning_expiration 220752000 PROJECT:logs_acme  # 7 years
```

The default bucket retains 30 days; the tenant-specific BigQuery dataset retains 7 years. Standard tenants pay default cost; enterprise tenants pay their own retention cost.

## Tenant deletion / right-to-be-forgotten

When a tenant terminates and requests data deletion, telemetry must be purged. The pattern depends on retention model:

### Bounded retention (default — easiest)

Operational telemetry retained ≤ 30 days. After tenant termination, wait 30 days and natural expiry handles purge. Document the timeline in your data deletion SLA.

### Long retention (audit logs, regulated industries)

Maintain a deletion queue keyed on `tenant.id`. Run a daily job that issues vendor-specific delete-by-attribute queries:

```bash
# GCP — delete logs for a specific tenant
gcloud logging logs delete --log-filter='jsonPayload."tenant.id"="acme-terminated"'

# Datadog — delete events
curl -X POST "https://api.datadoghq.com/api/v1/logs/delete-job" \
  -H "DD-API-KEY: $DD_API_KEY" \
  -d '{
    "filter": {
      "query": "@tenant.id:acme-terminated",
      "from": "2025-01-01T00:00:00Z",
      "to": "now"
    }
  }'

# CloudWatch Logs — by querying and deleting matching streams (no native filter delete)
# In practice: log group per tenant + delete the group
aws logs delete-log-group --log-group-name /aws/ecs/vqms-api-acme
```

Audit each deletion in a separate immutable log: when, who triggered, what tenant_id, vendor confirmation. The audit trail is the proof you handled the request.

### Hashed identifiers — make deletion easier

If you've used hashed user IDs (per `security-and-compliance.md`), tenant-level deletion still uses the unhashed `tenant.id` (which is your own identifier, not PII). Hashing is for *user* IDs within a tenant; the tenant itself is not anonymized.

## Per-tenant query access

A tenant's support team or the customer themselves may need access to their own observability data. Patterns:

### Vendor-side IAM — Model 2 only

With per-tenant project/workspace, vendor IAM policies grant tenant users read access to their own project. Datadog organizations, GCP project-level IAM, AWS account-level IAM all support this naturally.

### Custom query proxy — Model 1

When everyone shares a backend, build a thin proxy that:
1. Accepts authenticated tenant user requests
2. Auto-injects `tenant.id == "acme"` into every query
3. Forwards to the vendor backend

```
[ Tenant user ]
       │ login (tenant=acme)
       ▼
[ Query proxy ]
       │ injects: tenant.id == "acme"
       ▼
[ Vendor backend ]  → returns only acme's data
```

Without the proxy, exposing the vendor UI directly to tenants is a security disaster — they'd see all customers.

### Customer-facing dashboards

Embedded dashboards (Grafana, Datadog dashboards via embedding API) work with the proxy pattern: render the dashboard inside an iframe, with the tenant scope pre-applied via signed URL parameters. Customer sees only their data; UI looks branded.

## Multi-tenancy and tail sampling — the locality problem

Tail sampling requires all spans of one trace at the same Collector instance. With multi-tenancy + multiple gateway Collectors, this still applies — but you don't need *per-tenant* locality, just per-trace locality. The `loadbalancing` exporter with `routing_key: traceID` handles this correctly regardless of tenant.

What does *not* work: routing by `tenant.id` to dedicated per-tenant Collectors and then tail-sampling within each. If a request from tenant A makes a downstream call to a service handling tenant B's data (rare but happens in admin/cross-tenant flows), the spans split across Collectors and the trace is incomplete.

The defensible pattern: route by `traceID` to a shared Collector pool, then *export* per-tenant from there. Locality is at the trace level; isolation is at the export level.

## Common pitfalls

**`tenant.id` set on some spans, not others.** A child span created on a worker thread loses context if propagation isn't wired. Set `tenant.id` on the entry span and rely on OTel context propagation; verify with a test trace that crosses an async boundary.

**Per-tenant exporter list growing unboundedly.** Static `routing` config in the Collector becomes unmaintainable with hundreds of tenants. For Model 2 (per-tenant project), use exporter templating or generate the config from a tenant database via your CI/CD; don't edit YAML by hand.

**Free-tier tenants with no `tenant.tier` attribute.** The sampling rules above only work if every span has `tenant.tier` set. If middleware misses it for unauth'd traffic, the `default` policy catches them — make sure you have one.

**Cross-tenant query in a vendor UI.** An engineer types a query without scoping by `tenant.id` and sees data from all tenants. Solution: vendor-side custom views/folders pre-scoped to specific tenants for support, or the proxy pattern for tenant access.

**Tenant cost report missing telemetry cost.** Product teams see "tenant Acme uses $X of compute". The observability cost is often comparable — surface it. Otherwise tenants who generate massive trace volume look profitable on paper.

**Hard-coded tenant routing logic.** "If tenant.id starts with 'enterprise-', route to project X" — this couples your Collector config to your tenant naming scheme. The first tenant rename (from M&A or rebrand) breaks routing silently. Use an explicit list, refresh from the source-of-truth tenant database.

**Tenant deletion without audit trail.** A "we deleted Acme's data" claim that cannot be proven post-fact. Always log deletions to an immutable audit channel.

**Forgetting metric cardinality.** `request_count{tenant_id="acme"}` is fine for 100 tenants; for a B2C product with 100M users where you accidentally use `user.id`, the metric explodes. Tenant-level cardinality is usually safe; user-level cardinality is not.

## Quick checklist

```markdown
## Multi-tenancy review

- [ ] tenant.id set in middleware on every entry span
- [ ] tenant.id propagates to all child spans (verify with test trace)
- [ ] tenant.tier (or equivalent) attribute set for sampling decisions
- [ ] Isolation model documented per tenant tier (Model 1 / 2 / 3)
- [ ] Per-tenant sampling rates defined; not all tenants treated identically
- [ ] Per-tenant rate limiting in place (prevents noisy-neighbor explosion)
- [ ] Terminated tenants' traffic dropped at Collector
- [ ] Volume metrics per tenant exposed for chargeback
- [ ] Telemetry cost attributed in tenant economics review
- [ ] Per-tenant retention configured if regulation/contract demands
- [ ] Tenant deletion process documented and tested (with audit trail)
- [ ] Cross-tenant query prevented in customer-facing UIs
- [ ] Engineering trace UI access reviewed; cross-tenant access logged
- [ ] tail_sampling routes by traceID, not tenant.id (preserves trace locality)
```

## Sources

- OpenTelemetry semantic conventions for service.namespace and tenant attributes — opentelemetry.io/docs/specs/semconv
- Collector routing connector — github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/connector/routingconnector
- Datadog multi-org / multi-account — docs.datadoghq.com/account_management/multi_organization/
- GCP project organization — cloud.google.com/resource-manager/docs/cloud-platform-resource-hierarchy
- GDPR right to erasure (Article 17) — gdpr-info.eu/art-17-gdpr/