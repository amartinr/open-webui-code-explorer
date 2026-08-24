#!/usr/bin/env python3
"""Build the self-contained Open WebUI tool scripts.

Inlines the body of common.py (the single source of truth, DESIGN.md §5.6)
into each templates/*.py.tpl at the `# {{COMMON_CODE}}` marker and writes the
result to dist/. The generated scripts are self-contained (no `import common`)
so each one can be pasted into the Open WebUI admin Tools UI.

Usage:
    python build.py

Outputs (dist/):
- repos.py, files_search.py, commits.py: the three per-group scripts
  (granular tool access + per-script Valves, DESIGN.md §5.4).
- code_explorer.py: a single combined script exposing ALL tools in one
  Tools class with one Valves class (simpler deploy; operator accepts giving
  every capability, including the repo-management write tools).

The frontmatter docstring (title/description/required_open_webui_version) is
per-template and is NOT taken from common.py (§9.2).
"""

import ast
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent
COMMON = ROOT / "common.py"
TEMPLATES = ROOT / "templates"
DIST = ROOT / "dist"
MARKER = "# {{COMMON_CODE}}"

# Order matters: repos -> files & search -> commits.
TPL_ORDER = ["repos.py.tpl", "files_search.py.tpl", "commits.py.tpl"]

# The combined script's frontmatter and imports (union of the three templates).
ALL_FRONTMATTER = '''"""
title: Code Explorer - All
author: A. Martin
author_url: https://github.com/amartinr
version: 1.0.0
icon_url: https://github.com/amartinr/open-webui-code-explorer/raw/main/docs/icon.svg
description: All Code Explorer tools in one script, prefixed cexp_: clone/fetch/pull/list repos, list/read/search files, find symbols, and inspect branches, tags, and commits. Read-only with respect to source code; only clone/fetch/pull write inside the allow-listed repositories directory, and only via git.
required_open_webui_version: 0.9.6
"""
import itertools
import os
from typing import Optional

from pydantic import BaseModel, Field

'''


def _extract_methods(tpl_path: Path) -> List[Tuple[str, str]]:
    """Extract (name, source) for every method of the template's Tools class,
    excluding __init__ (each script defines its own) and the nested Valves."""
    src = tpl_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    tools = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Tools")
    lines = src.splitlines()
    out = []
    for node in tools.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name != "__init__":
            body = "\n".join(lines[node.lineno - 1 : node.end_lineno])
            out.append((node.name, body))
    return out


def build_combined(common_code: str, methods: List[Tuple[str, str]]) -> str:
    """Assemble the single-script artifact: frontmatter + imports + common +
    one Tools class with one Valves and all methods (deduplicated by name)."""
    seen = set()
    uniq = []
    for name, body in methods:
        if name in seen:
            continue  # _ensure_repo_exists is identical across scripts
        seen.add(name)
        uniq.append(body)

    class_body = "\n\n".join(uniq)
    return (
        ALL_FRONTMATTER
        + common_code
        + "\n\n\nclass Tools:\n"
        + "    def __init__(self):\n"
        + "        self.valves = self.Valves()\n"
        + "        check_binaries(\"git\")\n"
        + "\n"
        + "    class Valves(BaseModel):\n"
        + '        repos_path: str = Field(\n            "",\n            description="Base directory for repository clones. Empty -> $OWUI_REPOS_PATH -> /usr/local/src. A dedicated volume must be mounted there and the process needs read/write permission; this Valve is a logical override only.",\n        )\n'
        + "        max_results: int = Field(\n            50, description=\"Cap on item counts (files, matches, commits, branches, tags).\"\n        )\n"
        + "        max_lines: int = Field(\n            200, description=\"Cap on output lines. Whichever cap is hit first truncates.\"\n        )\n"
        + "        max_bytes: int = Field(\n            20480, description=\"Hard byte cap on tool output (20 KB default).\"\n        )\n"
        + "\n"
        + class_body
        + "\n"
    )


def build() -> None:
    common_code = COMMON.read_text(encoding="utf-8").rstrip("\n")
    DIST.mkdir(exist_ok=True)

    # Per-group scripts (existing behaviour).
    built = 0
    for tpl in sorted(TEMPLATES.glob("*.py.tpl")):
        text = tpl.read_text(encoding="utf-8")
        if MARKER not in text:
            raise SystemExit(f"{tpl.name}: missing marker {MARKER!r}")
        text = text.replace(MARKER, common_code)
        out = DIST / tpl.name[: -len(".tpl")]
        out.write_text(text, encoding="utf-8")
        built += 1
        print(f"built {out.name}")

    # Combined script: all tools in one Tools class.
    methods: List[Tuple[str, str]] = []
    for tpl_name in TPL_ORDER:
        methods.extend(_extract_methods(TEMPLATES / tpl_name))
    combined = build_combined(common_code, methods)
    out = DIST / "code_explorer.py"
    out.write_text(combined, encoding="utf-8")
    built += 1
    print(f"built {out.name}")

    print(f"done ({built} script(s) written to {DIST})")


if __name__ == "__main__":
    build()
