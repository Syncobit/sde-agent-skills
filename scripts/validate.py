#!/usr/bin/env python3
"""Validate every skill in this repo.

A skill is any directory containing a SKILL.md file. We check:
  1. Frontmatter exists and is well-formed YAML.
  2. Required fields (name, description) are present.
  3. The frontmatter `name` matches the directory name.
  4. The description is long enough to trigger reliably (>=50 chars).
  5. References mentioned in SKILL.md actually exist on disk.

Exit code is 0 if everything passes, non-zero otherwise — suitable for CI.

This script has no third-party dependencies. Pure stdlib.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".github", "scripts", "dist", "node_modules", "__pycache__"}


def find_skills(root: Path):
    """Yield every directory containing a SKILL.md file, skipping infrastructure dirs."""
    for skill_md in root.rglob("SKILL.md"):
        if any(part in SKIP_DIRS or part.startswith(".") for part in skill_md.relative_to(root).parts):
            continue
        yield skill_md.parent


def parse_frontmatter(content: str) -> tuple[dict[str, str], str | None]:
    """Return (fields, error). fields is empty if error is set."""
    if not content.startswith("---\n") and not content.startswith("---\r\n"):
        return {}, "missing YAML frontmatter (file must start with '---')"

    # Find the closing '---' line
    lines = content.splitlines()
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, "frontmatter not properly closed (no terminating '---' line)"

    fields: dict[str, str] = {}
    current_key: str | None = None
    current_value: list[str] = []

    for line in lines[1:end_idx]:
        # Multi-line continuation (starts with whitespace)
        if line.startswith((" ", "\t")) and current_key is not None:
            current_value.append(line.strip())
            continue

        # Flush previous key
        if current_key is not None:
            fields[current_key] = " ".join(current_value).strip()
            current_key, current_value = None, []

        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$", line)
        if match:
            current_key = match.group(1)
            current_value = [match.group(2)]

    if current_key is not None:
        fields[current_key] = " ".join(current_value).strip()

    return fields, None


def validate_skill(skill_dir: Path) -> list[str]:
    """Return a list of validation error messages. Empty list means valid."""
    errors: list[str] = []
    skill_md = skill_dir / "SKILL.md"
    content = skill_md.read_text(encoding="utf-8")

    fields, parse_err = parse_frontmatter(content)
    if parse_err:
        errors.append(parse_err)
        return errors

    if "name" not in fields:
        errors.append("frontmatter missing required 'name' field")
    elif fields["name"] != skill_dir.name:
        errors.append(
            f"frontmatter name '{fields['name']}' does not match folder name '{skill_dir.name}'"
        )

    if "description" not in fields:
        errors.append("frontmatter missing required 'description' field")
    else:
        desc_len = len(fields["description"])
        if desc_len < 50:
            errors.append(
                f"description is too short ({desc_len} chars); needs ~50+ to trigger reliably"
            )
        # Claude.ai upload enforces a 1024-char hard cap on description.
        if desc_len > 1024:
            errors.append(
                f"description is too long ({desc_len} chars); Claude.ai rejects uploads over 1024 chars"
            )

    # Check that referenced files exist (heuristic: any references/X.md path mentioned in SKILL.md)
    body = content.split("---", 2)[-1] if content.startswith("---") else content
    referenced_paths = re.findall(r"`?(references/[A-Za-z0-9_\-./]+\.md)`?", body)
    for ref in set(referenced_paths):
        if not (skill_dir / ref).exists():
            errors.append(f"SKILL.md references '{ref}' but file does not exist")

    return errors


def main() -> int:
    skills = sorted(find_skills(REPO_ROOT))
    if not skills:
        print("No skills found in repo.")
        return 0

    total_errors = 0
    for skill_dir in skills:
        rel = skill_dir.relative_to(REPO_ROOT)
        errors = validate_skill(skill_dir)
        if errors:
            print(f"FAIL  {rel}")
            for err in errors:
                print(f"      - {err}")
            total_errors += len(errors)
        else:
            print(f"OK    {rel}")

    print()
    if total_errors:
        print(f"FAILED: {total_errors} error(s) across {len(skills)} skill(s)")
        return 1
    print(f"PASSED: {len(skills)} skill(s) validated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
