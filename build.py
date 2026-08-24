#!/usr/bin/env python3
"""Build the self-contained Open WebUI tool scripts.

Inlines the body of common.py (the single source of truth, DESIGN.md §5.6)
into each templates/*.py.tpl at the `# {{COMMON_CODE}}` marker and writes the
result to dist/. The generated scripts are self-contained (no `import common`)
so each one can be pasted into the Open WebUI admin Tools UI.

Usage:
    python build.py

The frontmatter docstring (title/description/required_open_webui_version) is
per-template and is NOT taken from common.py (§9.2).
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMMON = ROOT / "common.py"
TEMPLATES = ROOT / "templates"
DIST = ROOT / "dist"
MARKER = "# {{COMMON_CODE}}"


def build() -> None:
    common_code = COMMON.read_text(encoding="utf-8").rstrip("\n")
    DIST.mkdir(exist_ok=True)
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
    print(f"done ({built} script(s) written to {DIST})")


if __name__ == "__main__":
    build()
