# sde-agent-skills

Production-grade [Agent Skills](https://agentskills.io/) for designing and reviewing REST APIs. Each skill packages opinionated guidance, real worked examples, and review checklists grounded in current IETF standards (RFC 9110, 9457, draft-ietf-httpapi-idempotency-key-header-07).

These skills are written to the open Agent Skills standard and work with any compatible runtime — Claude Code, Codex CLI, Cursor, Gemini CLI, GitHub Copilot, Goose, and others. The format is the same across all of them.

## What's in here

| Skill | What it does |
|-------|--------------|
| [`api-idempotency`](./api-idempotency/) | Apply idempotency patterns when designing or reviewing REST endpoints, webhooks, and any operation with side effects. Covers Idempotency-Key headers, natural-key dedupe, storage schemas, concurrency handling, and a 16-item anti-pattern catalog. |
| [`api-error-responses`](./api-error-responses/) | Design HTTP error responses across the four major formats (RFC 9457 Problem Details, JSON:API, `google.rpc.Status`, custom). Covers status code selection, validation errors, security, and OpenAPI integration. |
| [`api-http-caching`](./api-http-caching/) | Design HTTP caching and conditional requests for REST APIs and CDNs. Covers Cache-Control directives, freshness vs validation, ETag generation (strong vs weak), `If-Match` / `If-None-Match` (304/412/428), optimistic concurrency control / lost-update prevention, private vs shared caches, stale-while-revalidate, Vary, and CDN invalidation strategies. |
| [`otel-observability`](./otel-observability/) | Apply OpenTelemetry across backend AND edge tiers — traces, metrics, logs — with enterprise-grade coverage of GCP (Cloud Run, GKE, Cloud Functions, Apigee), AWS (ECS, EKS, Lambda, API Gateway, Step Functions, EventBridge), edge (Cloudflare Workers, Vercel Edge, Lambda@Edge), browser RUM, and non-cloud OTLP backends (Datadog, Splunk, New Relic, Grafana Cloud, Honeycomb, Dynatrace, Elastic). Covers Collector production hardening (memory limiter, persistent queue, gateway pattern, mTLS, redaction, CMEK), enterprise log discipline (audit vs operational channels, retention tiers, SIEM, MDC baseline), multi-tenancy (isolation, sampling, chargeback), migration from X-Ray/Jaeger/Zipkin/StatsD/Prometheus/Datadog APM, CI testing, and 22 anti-patterns with severity-tagged review checklist. |

More skills will be added over time. Each is self-contained — install only the ones you need.

## Installing into Claude

Two ways:

**Option 1 — pre-built `.skill` files (easiest).** Grab the latest release from the [Releases](../../releases) page, download the `.skill` file for the skill you want, and upload it via Claude.ai → Settings → Capabilities → Skills.

**Option 2 — build from source.**

```bash
git clone https://github.com/<your-username>/sde-agent-skills.git
cd sde-agent-skills
python scripts/build.py
# .skill files are now in dist/
```

Then upload the `.skill` files from `dist/` via the same Claude.ai settings page.

## Installing into other runtimes

Each runtime has its own install path; the source folder is the same.

- **Claude Code** — drop the skill folder into your project's `.claude/skills/` directory or `~/.claude/skills/` for global use. See [Claude Code Skills docs](https://docs.claude.com).
- **Codex CLI** — see [OpenAI's Codex CLI documentation](https://github.com/openai/codex-cli) for the current install path.
- **Cursor, Gemini CLI, Goose, etc.** — most read skills from a configured directory; consult each runtime's docs.

The `SKILL.md` format and folder structure are identical across runtimes per the [Agent Skills spec](https://agentskills.io/).

## Repo layout

```
.
├── api-idempotency/                    # one folder per skill
│   ├── SKILL.md                        # required: frontmatter + instructions
│   └── references/                     # progressive-disclosure references
│       ├── core-pattern.md
│       ├── implementation.md
│       └── ...
├── api-error-responses/
│   ├── SKILL.md
│   └── references/...
├── api-http-caching/
│   ├── SKILL.md
│   └── references/...
├── otel-observability/
│   ├── SKILL.md
│   └── references/...
├── scripts/
│   ├── build.py                        # package every skill into dist/*.skill
│   └── validate.py                     # check frontmatter and references
├── .github/workflows/validate.yml      # CI: validates every skill on push
├── LICENSE                             # Apache 2.0
├── NOTICE                              # required by Apache 2.0
└── README.md
```

## Development

```bash
# Validate all skills (run before committing)
python scripts/validate.py

# Build all skills into dist/*.skill
python scripts/build.py

# Build just one
python scripts/build.py api-idempotency
```

Validation runs automatically on every push and pull request via GitHub Actions.

## Contributing

PRs welcome. A new skill should:

1. Live in its own top-level folder named exactly the same as the `name:` field in its `SKILL.md` frontmatter.
2. Have a description in the frontmatter that's specific enough to trigger reliably (~50+ characters, includes both what it does and when to use it).
3. Pass `python scripts/validate.py`.
4. Cite primary sources (RFCs, official docs) rather than blog posts where possible.
5. Include a "Sources" section at the bottom of `SKILL.md` and any reference files.

The [`skill-creator` skill](https://github.com/anthropics/skills/tree/main/skill-creator) (in Anthropic's official skills repo) is useful for drafting and iterating on new skills.

## License

Apache 2.0. See [LICENSE](./LICENSE) and [NOTICE](./NOTICE).

Copyright 2026 Syncobit.

## See also

- [Agent Skills specification](https://agentskills.io/) — the open standard
- [Anthropic's `skills` repository](https://github.com/anthropics/skills) — reference implementation and example skills
- [skill-creator](https://github.com/anthropics/skills/tree/main/skill-creator) — meta-skill for creating new skills
