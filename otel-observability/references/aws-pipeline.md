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
