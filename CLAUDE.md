# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A collection of [Agent Skills](https://agentskills.io/) (open standard) for designing and reviewing REST APIs. Each top-level folder is one skill — a self-contained bundle of opinionated guidance grounded in IETF RFCs and current industry practice. The skills are content artifacts, not runnable code; the only "code" in the repo is two stdlib Python scripts that validate and package the skills.

Current skills: `api-idempotency`, `api-error-responses`, `api-http-caching`, `otel-observability`.

## Common commands

```bash
# Validate every skill — run before committing. CI runs this on every push/PR.
python scripts/validate.py

# Package every skill into dist/<name>.skill (a zip with the skill folder at top level)
python scripts/build.py

# Package a single skill
python scripts/build.py api-idempotency
```

No package manager, no virtualenv, no third-party deps. Anything Python ≥ 3.12 will run the scripts.

## Skill anatomy (the architectural pattern)

Every skill lives in its own top-level folder and follows the same shape:

```
<skill-name>/
├── SKILL.md            # required. YAML frontmatter + body.
└── references/         # progressive-disclosure deep dives, linked from SKILL.md
    ├── <topic>.md
    └── ...
```

`SKILL.md` frontmatter has two required fields: `name` (must equal the folder name) and `description` (≥ 50 chars, must include both *what* the skill does and *when* to use it — the description is what makes the skill trigger reliably in an agent runtime).

`scripts/validate.py` enforces, in addition to YAML well-formedness:
1. `name` == folder name
2. `description` length ≥ 50
3. Every `references/X.md` path mentioned in `SKILL.md` actually exists on disk.

`scripts/build.py` is just `zipfile` — it walks every directory containing a `SKILL.md` (skipping `.git`, `.github`, `scripts`, `dist`, `node_modules`, `__pycache__`, and any dotfiles), and emits `dist/<folder>.skill` with the folder preserved as the top-level entry inside the archive. That layout is what Claude.ai, Codex CLI, etc. expect on upload.

## Authoring conventions for skills

These are repo-wide expectations — they're not in code but they govern review:

- **Cite primary sources.** RFCs, IETF drafts, and official vendor docs over blog posts. Each `SKILL.md` and reference file should end with a `Sources` section.
- **Progressive disclosure.** Keep `SKILL.md` focused on the decision flow ("when to use what"). Push storage schemas, full algorithms, code samples, and edge-case catalogues into `references/*.md` files that the agent loads on demand.
- **The description field is load-bearing.** It's how the runtime decides to invoke the skill. It must name concrete trigger phrases and adjacent vocabulary the user might use ("retries", "double-charging", "exactly-once", etc. for `api-idempotency`).
- **Folder name == frontmatter name == intended skill identifier.** Renaming requires updating both.

## CI and release

- `.github/workflows/validate.yml` — runs validate + build on every push/PR to `main`, uploads built `.skill` files as an artifact.
- `.github/workflows/release.yml` — on a `v*` tag push, validates, builds, and creates a GitHub Release with the `dist/*.skill` files attached. Releases are the consumer-facing distribution channel (per the README).

## When adding or modifying a skill

1. Edit/create the folder with `SKILL.md` and any `references/*.md`.
2. Run `python scripts/validate.py` locally — fix any reported errors before committing. Pay attention to the "references X.md missing" check, which catches dead links to deep-dive files.
3. (Optional) Run `python scripts/build.py <name>` and inspect the resulting zip to make sure nothing dotfile-related leaked in.
4. Update `README.md`'s skill table if you've added a new top-level skill.