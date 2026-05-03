#!/usr/bin/env python3
"""Package every skill in this repo into a .skill file in dist/.

A .skill file is just a zip archive of the skill folder. The folder name
is preserved as the top-level directory inside the archive — this is what
runtimes (Claude.ai, Codex CLI, etc.) expect when installing.

Usage:
    python scripts/build.py              # build all skills
    python scripts/build.py <name>       # build only the named skill

Pure stdlib, no dependencies.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = REPO_ROOT / "dist"
SKIP_DIRS = {".git", ".github", "scripts", "dist", "node_modules", "__pycache__"}


def find_skills(root: Path):
    for skill_md in root.rglob("SKILL.md"):
        if any(part in SKIP_DIRS or part.startswith(".") for part in skill_md.relative_to(root).parts):
            continue
        yield skill_md.parent


def package_skill(skill_dir: Path, out_dir: Path) -> Path:
    out_path = out_dir / f"{skill_dir.name}.skill"
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(skill_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(skill_dir)
            # Skip hidden files / build artifacts
            if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
                continue
            arcname = f"{skill_dir.name}/{rel}"
            zf.write(path, arcname)
    return out_path


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else None

    skills = sorted(find_skills(REPO_ROOT))
    if target:
        skills = [s for s in skills if s.name == target]
        if not skills:
            print(f"No skill named '{target}' found.", file=sys.stderr)
            return 1

    if not skills:
        print("No skills found in repo.")
        return 0

    DIST_DIR.mkdir(exist_ok=True)
    print(f"Packaging {len(skills)} skill(s) -> {DIST_DIR}/\n")

    for skill_dir in skills:
        out = package_skill(skill_dir, DIST_DIR)
        size_kb = out.stat().st_size / 1024
        print(f"  {out.relative_to(REPO_ROOT)}  ({size_kb:.1f} KB)")

    print(f"\nDone. {len(skills)} skill(s) packaged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
