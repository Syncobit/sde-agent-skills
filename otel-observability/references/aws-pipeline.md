# AWS Pipeline — ADOT, X-Ray, CloudWatch, ECS/EKS/Lambda

How to ship OTel telemetry to AWS observability backends: X-Ray for traces, CloudWatch Metrics, CloudWatch Logs. Covers the three main compute platforms (ECS, EKS, Lambda).

## AWS Distro for OpenTelemetry (ADOT)

ADOT is AWS's curated build of the OTel Collector and language SDKs, with AWS exporters and resource detectors pre-configured. Use it instead of the upstream Collector when shipping to AWS — fewer surprises, AWS-supported, and the X-Ray exporter is bundled.

Collector image: `public.ecr.aws/aws-observability/aws-otel-collector:<version>`

ADOT also publishes language-specific distributions (e.g., `aws-otel-python-instrumentation`) that pre-configure the SDK with AWS-friendly defaults including the X-Ray ID generator and propagator. For new projects, **prefer the upstream OTel SDK + ADOT Collector** over the ADOT SDK distributions — keeps your application code vendor-neutral and ports cleanly to GCP if needed.

## X-Ray vs CloudWatch — what goes where

| OTel signal | AWS backend | How |
|-------------|-------------|-----|
| Traces | X-Ray | `awsxray` exporter in the Collector |
| Metrics | CloudWatch Metrics | `awsemf` exporter (writes EMF logs that CloudWatch indexes as metrics) |
| Logs | CloudWatch Logs | `awscloudwatchlogs` exporter |

The `awsemf` (Embedded Metric Format) approach is AWS-idiomatic but has cardinality cost implications: each unique combination of dimensions creates a new metric in CloudWatch, and CloudWatch charges per metric. Cardinality control at the Collector is even more important on AWS than on GCP.

## X-Ray trace context — the propagator gotcha

X-Ray uses its own trace ID format and a non-W3C header (`X-Amzn-Trace-Id`). If you're integrating with AWS-native services that emit X-Ray traces (API Gateway, ALB, Lambda triggers), you need:

1. **X-Ray ID generator** in the SDK — produces IDs in X-Ray's format (which is also valid W3C format, but not vice versa).
2. **X-Ray propagator** registered alongside or instead of W3C — reads/writes `X-Amzn-Trace-Id`.

Without these, your application's traces don't link up with the AWS-managed segments. With them, you see end-to-end traces from API Gateway through Lambda or ECS into your service.

In Python:
```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
from opentelemetry.propagators.aws import AwsXRayPropagator
from opentelemetry import propagate

propagate.set_global_textmap(AwsXRayPropagator())
tracer_provider = TracerProvider(id_generator=AwsXRayIdGenerator())
```

In Node:
```javascript
const { AWSXRayIdGenerator } = require('@opentelemetry/id-generator-aws-xray');
const { AWSXRayPropagator } = require('@opentelemetry/propagator-aws-xray');

const sdk = new NodeSDK({
  idGenerator: new AWSXRayIdGenerator(),
  textMapPropagator: new AWSXRayPropagator(),
  // ...
});
```

In Java (the agent supports this via system property):
```bash
-Dotel.propagators=xray
-Dotel.aws.imds.endpoint=...   # if using EC2 metadata
```

If you're not integrating with AWS-managed services and only your own services use OTel, stick with W3C propagation. Mixed-mode (W3C inside your services, X-Ray for cross-AWS-service correlation) is also viable — register both propagators.

## ECS pattern: ADOT as a sidecar container

Equivalent to the Cloud Run sidecar pattern. ADOT runs as a sidecar in the ECS task definition.

### Task definition (excerpt)

```json
{
  "family": "vqms-api",
  "containerDefinitions": [
    {
      "name": "app",
      "image": "ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/vqms/api:1.4.7",
      "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
      "environment": [
        {"name": "OTEL_SERVICE_NAME", "value": "vqms-api"},
        {"name": "OTEL_EXPORTER_OTLP_ENDPOINT", "value": "http://localhost:4317"},
        {"name": "OTEL_RESOURCE_ATTRIBUTES", "value": "deployment.environment.name=production,service.namespace=vqms"},
        {"name": "OTEL_PROPAGATORS", "value": "tracecontext,baggage,xray"},
        {"name": "OTEL_TRACES_SAMPLER", "value": "parentbased_traceidratio"},
        {"name": "OTEL_TRACES_SAMPLER_ARG", "value": "0.1"}
      ],
      "dependsOn": [
        {"containerName": "otel-collector", "condition": "START"}
      ]
    },
    {
      "name": "otel-collector",
      "image": "public.ecr.aws/aws-observability/aws-otel-collector:latest",
      "command": ["--config=/etc/ecs/ecs-default-config.yaml"],
      "essential": true,
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/vqms-api/collector",
          "awslogs-region": "us-east-1",
          "awslogs-stream-prefix": "ecs"
        }
      }
    }
  ],
  "taskRoleArn": "arn:aws:iam::ACCOUNT:role/vqms-api-task-role",
  "executionRoleArn": "arn:aws:iam::ACCOUNT:role/ecsTaskExecutionRole"
}
```

ADOT ships with several built-in configs, including `ecs-default-config.yaml` which auto-detects ECS resource attributes and exports to X-Ray + CloudWatch. For custom config, mount your own YAML via `command: ["--config=/path/to/config.yaml"]` and provide it through Parameter Store or a config volume.

### Custom Collector config (`config.yaml`)

```yaml
receivers:
  otlp:
    protocols:
      grpc: { endpoint: 0.0.0.0:4317 }
      http: { endpoint: 0.0.0.0:4318 }

processors:
  resourcedetection:
    detectors: [env, ecs, ec2]
    timeout: 2s

  batch:
    timeout: 10s

  tail_sampling:
    decision_wait: 30s
    policies:
      - name: errors
        type: status_code
        status_code: { status_codes: [ERROR] }
      - name: probabilistic
        type: probabilistic
        probabilistic: { sampling_percentage: 10 }

exporters:
  awsxray:
    region: us-east-1

  awsemf:
    region: us-east-1
    namespace: VqmsApi
    log_group_name: '/aws/ecs/vqms-api'
    log_stream_name: 'metrics'
    dimension_rollup_option: NoDimensionRollup
    metric_declarations:
      - dimensions: [[service.name, deployment.environment.name]]
        metric_name_selectors:
          - "^http\\.server\\."

  awscloudwatchlogs:
    region: us-east-1
    log_group_name: '/aws/ecs/vqms-api'
    log_stream_name: 'app'

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [resourcedetection, tail_sampling, batch]
      exporters: [awsxray]
    metrics:
      receivers: [otlp]
      processors: [resourcedetection, batch]
      exporters: [awsemf]
    logs:
      receivers: [otlp]
      processors: [resourcedetection, batch]
      exporters: [awscloudwatchlogs]
```

### IAM (task role)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "xray:GetSamplingRules",
        "xray:GetSamplingTargets",
        "xray:GetSamplingStatisticSummaries"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams",
        "logs:DescribeLogGroups"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "cloudwatch:PutMetricData"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {"cloudwatch:namespace": "VqmsApi"}
      }
    }
  ]
}
```

The `AWSXRayDaemonWriteAccess` managed policy covers the X-Ray actions. CloudWatch and Logs need separate statements.

## EKS pattern: ADOT via the OpenTelemetry Operator

Same operator-driven pattern as GKE. Deploy ADOT as a DaemonSet via the operator.

### Install operator + cert-manager

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml
kubectl wait --for=condition=Available deployment --all -n cert-manager --timeout=300s
kubectl apply -f https://github.com/aws-observability/aws-otel-operator/releases/latest/download/opentelemetry-operator.yaml
```

(Or use the upstream OTel Operator with ADOT collector image — both work.)

### Deploy ADOT Collector

```yaml
apiVersion: opentelemetry.io/v1beta1
kind: OpenTelemetryCollector
metadata:
  name: gateway
  namespace: observability
spec:
  mode: daemonset
  image: public.ecr.aws/aws-observability/aws-otel-collector:latest
  serviceAccount: adot-collector
  config:
    receivers:
      otlp:
        protocols:
          grpc: { endpoint: 0.0.0.0:4317 }
          http: { endpoint: 0.0.0.0:4318 }
    processors:
      k8sattributes:
        auth_type: serviceAccount
        extract:
          metadata: [k8s.pod.name, k8s.deployment.name, k8s.namespace.name, k8s.node.name]
      resourcedetection:
        detectors: [env, eks, ec2]
      batch: {}
    exporters:
      awsxray:
        region: us-east-1
      awsemf:
        region: us-east-1
        namespace: VqmsApi
        log_group_name: '/aws/eks/vqms-api'
      awscloudwatchlogs:
        region: us-east-1
        log_group_name: '/aws/eks/vqms-api'
    service:
      pipelines:
        traces:
          receivers: [otlp]
          processors: [k8sattributes, resourcedetection, batch]
          exporters: [awsxray]
        metrics:
          receivers: [otlp]
          processors: [k8sattributes, resourcedetection, batch]
          exporters: [awsemf]
        logs:
          receivers: [otlp]
          processors: [k8sattributes, resourcedetection, batch]
          exporters: [awscloudwatchlogs]
```

### IAM Roles for Service Accounts (IRSA)

```bash
eksctl create iamserviceaccount \
    --cluster=vqms-prod \
    --namespace=observability \
    --name=adot-collector \
    --attach-policy-arn=arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess \
    --attach-policy-arn=arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy \
    --approve
```

## Lambda pattern: ADOT as a Lambda Layer

Lambda is special: cold starts matter, processes are short-lived, and you can't run a sidecar. ADOT solves this with a Lambda Layer that bundles a minimal Collector + auto-instrumentation.

```bash
# Add the layer ARN to your function (region-specific; check aws-otel.github.io for current ARNs)
aws lambda update-function-configuration \
    --function-name vqms-callback \
    --layers arn:aws:lambda:us-east-1:901920570463:layer:aws-otel-python-amd64-ver-1-29-0:1 \
    --environment "Variables={
        AWS_LAMBDA_EXEC_WRAPPER=/opt/otel-instrument,
        OTEL_SERVICE_NAME=vqms-callback,
        OTEL_PROPAGATORS=tracecontext\\,xray,
        OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=production
    }"
```

Cold start cost: ADOT layer adds ~1-3 seconds to cold starts. For latency-sensitive Lambdas, the alternatives:

- **Use AWS-native X-Ray SDK only** — already in the runtime, no cold start cost, but doesn't support OTel-style spans across non-AWS services.
- **Use the OTel SDK without a layer** — bundle the SDK into the deployment package directly, configure manually. Reduces cold start vs. ADOT layer (no extra Collector), but more setup work.
- **Provisioned concurrency** — eliminates cold starts at the cost of always-on billing.

For high-throughput Lambdas, ADOT's overhead amortizes to nothing once warm. For low-throughput, latency-sensitive Lambdas, weigh the cold-start tax against the value of unified observability.

## Gateway pattern on AWS

For >50 services or any tail sampling, run a centralized ADOT gateway cluster instead of per-task sidecars. General hardening lives in `collector-production.md`; this section is the AWS-specific deployment.

### EKS-hosted gateway

```yaml
apiVersion: opentelemetry.io/v1beta1
kind: OpenTelemetryCollector
metadata:
  name: gateway
  namespace: observability
spec:
  mode: statefulset
  replicas: 3
  image: public.ecr.aws/aws-observability/aws-otel-collector:v0.42.0
  serviceAccount: adot-gateway
  volumeClaimTemplates:
    - metadata: { name: queue }
      spec:
        accessModes: [ReadWriteOnce]
        resources: { requests: { storage: "20Gi" } }
        storageClassName: gp3
  volumeMounts:
    - { name: queue, mountPath: /var/lib/otelcol }
```

Expose via an internal Network Load Balancer (NLB):

```yaml
apiVersion: v1
kind: Service
metadata:
  name: gateway-collector
  namespace: observability
  annotations:
    service.beta.kubernetes.io/aws-load-balancer-type: nlb
    service.beta.kubernetes.io/aws-load-balancer-internal: "true"
    service.beta.kubernetes.io/aws-load-balancer-scheme: internal
spec:
  type: LoadBalancer
  ports:
    - { name: otlp-grpc, port: 4317, targetPort: 4317 }
    - { name: otlp-http, port: 4318, targetPort: 4318 }
  selector:
    app.kubernetes.io/name: gateway-collector
```

Apps in ECS, Lambda, EC2, and other VPCs reach the gateway via the NLB's internal DNS. For cross-account or cross-VPC access, expose the NLB via PrivateLink (see "Private networking" below).

### Two-tier (DaemonSet agent + StatefulSet gateway)

For ECS, the equivalent is the sidecar-tier ADOT pointing to the gateway with `loadbalancing` exporter and `routing_key: traceID`. See `collector-production.md` for the routing rationale and full config.

For EKS:

```yaml
# Agent DaemonSet
apiVersion: opentelemetry.io/v1beta1
kind: OpenTelemetryCollector
metadata:
  name: agent
  namespace: observability
spec:
  mode: daemonset
  image: public.ecr.aws/aws-observability/aws-otel-collector:v0.42.0
  config:
    receivers:
      otlp:
        protocols:
          grpc: { endpoint: 0.0.0.0:4317 }
          http: { endpoint: 0.0.0.0:4318 }
    processors:
      memory_limiter: { check_interval: 1s, limit_mib: 800, spike_limit_mib: 200 }
      k8sattributes: { auth_type: serviceAccount }
      resourcedetection: { detectors: [env, eks, ec2] }
      batch: { timeout: 5s }
    exporters:
      loadbalancing:
        routing_key: traceID
        protocol:
          otlp:
            tls: { insecure: false }
            sending_queue: { enabled: true, queue_size: 5000 }
        resolver:
          k8s:
            service: gateway-collector.observability
            ports: [4317]
    service:
      pipelines:
        traces:
          receivers: [otlp]
          processors: [memory_limiter, k8sattributes, resourcedetection, batch]
          exporters: [loadbalancing]
```

Apps target the node IP via Downward API (per `gcp-pipeline.md` GKE example); the agent forwards trace-routed traffic to the gateway pool.

## API Gateway

API Gateway (REST and HTTP APIs) supports X-Ray tracing natively. Enable per-stage:

```bash
aws apigateway update-stage \
    --rest-api-id $API_ID \
    --stage-name prod \
    --patch-operations op=replace,path=/tracingEnabled,value=true
```

API Gateway emits an X-Ray segment per request. Downstream Lambda integrations propagate via `X-Amzn-Trace-Id`. To see API Gateway segments alongside OTel spans from your services:

1. Configure your service SDKs with the X-Ray ID generator and propagator (see "X-Ray trace context — the propagator gotcha" above)
2. Ship X-Ray segments and OTel spans to a backend that accepts both (X-Ray itself, or an OTLP backend via the AWS X-Ray exporter in the Collector)

For HTTP APIs, native X-Ray support is more limited. Use Lambda authorizer / proxy integration to inject `traceparent` headers in addition to `X-Amzn-Trace-Id`:

```python
# Custom Lambda authorizer
def lambda_handler(event, context):
    # X-Amzn-Trace-Id is set by API Gateway
    trace_id_header = event["headers"].get("x-amzn-trace-id", "")
    # Convert to W3C traceparent for downstream OTel services
    # Format: Root=1-5e988ba5-deadbeef → 4bf92f3577b34da6deadbeef00000000
    w3c_trace_id = parse_xray_to_w3c(trace_id_header)
    event["headers"]["traceparent"] = f"00-{w3c_trace_id}-...-01"
    return event
```

In practice the cleaner pattern is a single OTel SDK with both propagators registered (W3C + X-Ray), no header rewriting.

### Custom domain and access logs

API Gateway access logs can be JSON-formatted with `$context.xrayTraceId` — pipe to CloudWatch Logs and pick up via Log subscription → Lambda → OTLP gateway to materialize as spans (similar to the CloudFront pattern in `edge-and-rum.md`).

## AppSync (GraphQL)

AppSync supports X-Ray tracing for resolvers. Enable per-API:

```bash
aws appsync update-graphql-api \
    --api-id $API_ID \
    --xray-enabled \
    --name "vqms-graphql"
```

Each resolver invocation emits an X-Ray subsegment. Combined with `X-Amzn-Trace-Id` propagation to downstream Lambda/RDS/ES integrations, traces span the full GraphQL request.

For OTel correlation, register the X-Ray propagator in your Lambda resolvers and your downstream services. AppSync resolver spans appear as siblings of your OTel-instrumented spans in the same trace.

## Step Functions

Step Functions integrates with X-Ray when tracing is enabled at the state machine level:

```json
{
  "stateMachineArn": "arn:aws:states:...",
  "tracingConfiguration": { "enabled": true }
}
```

Each execution becomes an X-Ray trace; each state becomes a subsegment. Tasks invoking Lambda, ECS, etc. propagate `X-Amzn-Trace-Id` automatically.

For OTel-instrumented downstream services, register both W3C and X-Ray propagators so Step Functions traces extend through. The X-Ray subsegment for the state appears as the parent of your OTel span.

Custom states that call external HTTP services (HTTP Tasks, third-party APIs): inject `traceparent` explicitly via the state's parameter mapping:

```json
{
  "Type": "Task",
  "Resource": "arn:aws:states:::http:invoke",
  "Parameters": {
    "ApiEndpoint": "https://api.partner.com/v1/notify",
    "Method": "POST",
    "Headers": {
      "traceparent.$": "$.traceparent"
    }
  }
}
```

The `$.traceparent` variable comes from a prior state that built it from the execution's X-Ray context.

## EventBridge

EventBridge does not propagate trace context by default. Pattern: include `traceparent` in the event detail or in CloudEvents-format extensions, extract on the consumer side.

### Publishing

```python
import boto3
from opentelemetry import trace, propagate

eventbridge = boto3.client("events")
tracer = trace.get_tracer(__name__)

def publish_order_created(order):
    with tracer.start_as_current_span("eventbridge.publish") as span:
        span.set_attribute("messaging.system", "aws_eventbridge")
        span.set_attribute("messaging.destination.name", "vqms-events")

        # Inject trace context into the event detail
        trace_carrier = {}
        propagate.inject(trace_carrier)

        eventbridge.put_events(Entries=[{
            "Source": "vqms.api",
            "DetailType": "OrderCreated",
            "Detail": json.dumps({
                "_trace": trace_carrier,        # convention: nest under _trace
                "order": order.to_dict(),
            }),
            "EventBusName": "vqms-events",
        }])
```

### Consuming (Lambda target)

```python
def handler(event, context):
    # EventBridge wraps the original detail; extract trace context from it
    detail = event["detail"]
    trace_carrier = detail.get("_trace", {})
    parent_ctx = propagate.extract(trace_carrier)

    with tracer.start_as_current_span(
        "eventbridge.consume",
        context=parent_ctx,
        kind=SpanKind.CONSUMER,
    ) as span:
        span.set_attribute("messaging.message.id", event["id"])
        # ... process the event ...
```

The `_trace` key is a convention — choose what fits your event schema, but be consistent across all event publishers.

## SNS and SQS

### SNS

Publish with message attributes carrying trace context:

```python
sns.publish(
    TopicArn=topic_arn,
    Message=json.dumps(payload),
    MessageAttributes={
        "traceparent": {"DataType": "String", "StringValue": current_traceparent()},
        "tracestate":  {"DataType": "String", "StringValue": current_tracestate()},
    },
)
```

The OTel auto-instrumentation packages for boto3 (`opentelemetry-instrumentation-botocore`) handle this automatically — the package injects `AWSTraceHeader` and propagates AWS X-Ray trace context. For W3C, add it explicitly.

### SQS

Receive and extract:

```python
def process_messages(messages):
    for msg in messages:
        attrs = {k: v["StringValue"] for k, v in msg["MessageAttributes"].items()}
        ctx = propagate.extract(attrs)
        with tracer.start_as_current_span(
            "sqs.process",
            context=ctx,
            kind=SpanKind.CONSUMER,
        ) as span:
            span.set_attribute("messaging.system", "aws_sqs")
            span.set_attribute("messaging.message.id", msg["MessageId"])
            # ... process body ...
```

### Lambda + SQS event source mapping

When SQS triggers Lambda directly (event source mapping), Lambda's runtime extracts `AWSTraceHeader` automatically. To preserve W3C `traceparent` on this path, use the OTel propagator that handles both, and ensure `traceparent` was set as a message attribute when publishing.

## SNS → SQS fan-out

SNS to SQS subscription: SNS includes the original `MessageAttributes` in the SQS message body if you enable raw message delivery. Otherwise, attributes are nested under the SNS envelope:

```json
// SQS message body when raw delivery is OFF
{
  "Type": "Notification",
  "MessageId": "...",
  "TopicArn": "...",
  "Message": "...original payload...",
  "MessageAttributes": {
    "traceparent": { "Type": "String", "Value": "00-..." }
  }
}
```

Enable raw message delivery on the subscription so `MessageAttributes` appear at the top level of the SQS message — simpler trace context extraction:

```bash
aws sns set-subscription-attributes \
    --subscription-arn $SUB_ARN \
    --attribute-name RawMessageDelivery \
    --attribute-value true
```

## Private networking — VPC endpoints (PrivateLink)

Production AWS workloads usually run in private subnets. Telemetry must route over the private network. Interface VPC endpoints for X-Ray, CloudWatch, and CloudWatch Logs:

```bash
aws ec2 create-vpc-endpoint \
    --vpc-id vpc-xxx \
    --service-name com.amazonaws.us-east-1.xray \
    --vpc-endpoint-type Interface \
    --subnet-ids subnet-aaa subnet-bbb \
    --security-group-ids sg-otel-endpoints \
    --private-dns-enabled

aws ec2 create-vpc-endpoint \
    --vpc-id vpc-xxx \
    --service-name com.amazonaws.us-east-1.logs \
    --vpc-endpoint-type Interface \
    --subnet-ids subnet-aaa subnet-bbb \
    --security-group-ids sg-otel-endpoints \
    --private-dns-enabled

aws ec2 create-vpc-endpoint \
    --vpc-id vpc-xxx \
    --service-name com.amazonaws.us-east-1.monitoring \
    --vpc-endpoint-type Interface \
    --subnet-ids subnet-aaa subnet-bbb \
    --security-group-ids sg-otel-endpoints \
    --private-dns-enabled
```

The security group `sg-otel-endpoints` should accept traffic on 443 only from the security group used by the Collectors. Combined with private subnets and no NAT, telemetry never crosses the public internet.

### Cross-account / cross-VPC gateway access

To let workloads in account B reach a gateway Collector in account A's VPC:

```bash
# Account A — expose the gateway's NLB via PrivateLink
aws ec2 create-vpc-endpoint-service-configuration \
    --network-load-balancer-arns $NLB_ARN \
    --acceptance-required

# Get the service name
aws ec2 describe-vpc-endpoint-service-configurations \
    --query 'ServiceConfigurations[0].ServiceName' --output text
# → "com.amazonaws.vpce.us-east-1.vpce-svc-0123456789abcdef0"

# Account A — allow account B
aws ec2 modify-vpc-endpoint-service-permissions \
    --service-id $SERVICE_ID \
    --add-allowed-principals arn:aws:iam::ACCOUNT_B:root

# Account B — create the endpoint that connects to account A's service
aws ec2 create-vpc-endpoint \
    --vpc-id vpc-yyy \
    --service-name com.amazonaws.vpce.us-east-1.vpce-svc-0123456789abcdef0 \
    --vpc-endpoint-type Interface \
    --subnet-ids subnet-ccc subnet-ddd \
    --security-group-ids sg-otel-client
```

Account B's apps OTLP-export to the endpoint's DNS name; PrivateLink tunnels traffic to account A's NLB → gateway. No internet, no cross-account VPC peering.

For VPC endpoint policies (limiting which IAM principals can use the endpoint) and PrivateLink ingress ACLs, see `security-and-compliance.md`.

## Common pitfalls on AWS

**X-Ray trace IDs incompatible with W3C tools.** X-Ray IDs encode a timestamp in the high bits. Tools that assume W3C random IDs (e.g., trace ID generators in tests, third-party tracing UIs) may reject them. If your traces span both AWS-managed services and external systems, accept this and configure tools to handle X-Ray IDs.

**X-Ray sampling rules override SDK sampling.** X-Ray has its own sampling rules configured in the X-Ray console. If you set both ("X-Ray sample 5%" + "SDK sample 10%"), the more restrictive wins, and which one wins depends on subtle order-of-operations. Pick one place to control sampling — recommendation: Collector (tail-based) for full control, disable X-Ray's sampling rules.

**CloudWatch Metrics cost from EMF cardinality.** Every unique dimension combination creates a new metric, billed monthly. A metric with `user_id` dimension creates one metric per user — quickly six figures of metrics. Drop high-cardinality dimensions in the Collector's transform processor before they reach EMF.

**ADOT Collector image versioning.** `:latest` works for development but pin to a specific version (`:v0.42.0` or similar) in production. ADOT releases sometimes change config schema; an automatic image refresh can break a working pipeline.

**Lambda log groups created at runtime.** ADOT in Lambda creates log groups on first invocation if they don't exist. The execution role needs `logs:CreateLogGroup`. A common omission — works in dev, fails on first deploy to a new account/region.

## Sources

- AWS Distro for OpenTelemetry — aws-otel.github.io
- ADOT Collector GitHub — github.com/aws-observability/aws-otel-collector
- ADOT Lambda — aws-otel.github.io/docs/getting-started/lambda
- ADOT EKS — aws-otel.github.io/docs/getting-started/adot-eks-add-on
- X-Ray + OTel migration guide — aws.amazon.com/blogs/mt/migrating-x-ray-tracing-to-aws-distro-for-opentelemetry/
