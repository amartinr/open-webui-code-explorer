"""Valves contract tests.

The design (DESIGN.md §5.5) requires every tool script to declare the SAME
Valves contract (same fields, same names, same types, same defaults) so the
per-script Valves cannot drift. This test enforces that contract across all
generated scripts in dist/.
"""

import asyncio
import json
import types
from pathlib import Path

import pytest

from common import git_args, run_allowed
from conftest import daemon_source
from dist.files_search import Tools as FilesSearchTools
from dist.repos import Tools as ReposTools

DIST = Path(__file__).resolve().parent.parent / "dist"

# The canonical shared contract: name -> (type, default).
REQUIRED_VALVES = {
    "repos_path": (str, ""),
    "max_results": (int, 50),
    "max_lines": (int, 200),
    "max_bytes": (int, 20480),
}

# Which scripts exist and what max_results is capped on (per-script wording,
# allowed to differ; the field itself must still be identical).
SCRIPTS = [
    "code_explorer.py",
    "commits.py",
    "files_search.py",
    "repos.py",
]


def load_script(name: str):
    source = (DIST / name).read_text(encoding="utf-8")
    module = types.ModuleType(name.replace(".py", ""))
    exec(compile(source, name, "exec"), module.__dict__)
    return module


@pytest.mark.parametrize("script", SCRIPTS)
def test_valves_contract_identical(script):
    module = load_script(script)
    valves_cls = module.Tools.Valves
    fields = valves_cls.model_fields

    assert set(fields) == set(REQUIRED_VALVES), (
        f"{script}: valves {set(fields)} != required {set(REQUIRED_VALVES)}"
    )
    for name, (expected_type, expected_default) in REQUIRED_VALVES.items():
        field = fields[name]
        assert field.annotation is expected_type, (
            f"{script}.{name}: type {field.annotation} != {expected_type}"
        )
        assert field.default == expected_default, (
            f"{script}.{name}: default {field.default!r} != {expected_default!r}"
        )


def test_no_extra_or_missing_scripts():
    """Every script in dist/ must declare the contract; no script may omit it."""
    scripts = sorted(p.name for p in DIST.glob("*.py"))
    assert scripts == SCRIPTS, f"unexpected scripts in dist/: {scripts}"


async def _init_source_repo(path: Path) -> Path:
    """Tiny local repo with 3 files for the injection test."""
    path.mkdir(parents=True, exist_ok=True)
    ident = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]
    res = await run_allowed(git_args("-C", str(path), "init", "-b", "main"), 30)
    assert res.returncode == 0, res.stderr
    for i in range(3):
        (path / f"f{i}.py").write_text(f"x = {i}\n")
    res = await run_allowed(git_args("-C", str(path), "add", "-A"), 30)
    assert res.returncode == 0
    res = await run_allowed(git_args("-C", str(path), *ident, "commit", "-m", "init"), 30)
    assert res.returncode == 0
    return path


async def test_admin_valves_take_effect_at_runtime(tmp_path, git_daemon):
    """Simulate Open WebUI's injection exactly: load_tool_module_by_id returns
    a Tools instance, then `module.valves = module.Valves(**admin_vals)`
    replaces the defaults (backend/open_webui/utils/tools.py, plugin.py).
    The tools must honor the ADMIN values, not the __init__ defaults."""
    src = await _init_source_repo(daemon_source(git_daemon, "valves-src"))

    # Clone with the Repos script.
    repos_tools = ReposTools()
    repos_tools.valves = repos_tools.Valves(repos_path=str(tmp_path / "repos"))
    out = await repos_tools.cexp_clone_repo("o/r", url=src.url)
    assert not out.startswith("Error:"), out

    # Open WebUI injects the admin-saved valves onto a fresh instance.
    tools = FilesSearchTools()
    tools.valves = tools.Valves(
        repos_path=str(tmp_path / "repos"),
        max_results=2,
        max_lines=200,
        max_bytes=20480,
    )
    out = await tools.cexp_list_files("o/r")
    result = json.loads(out)
    assert len(result["items"]) == 2  # max_results=2 injected by the admin
    assert result["truncated"] == {"shown": 2, "total": 3}

    # The same injection flow for the Repos script: clone a second repo so
    # max_results=1 actually truncates.
    out = await repos_tools.cexp_clone_repo("o/r2", url=src.url)
    assert not out.startswith("Error:"), out
    repos_tools2 = ReposTools()
    repos_tools2.valves = repos_tools2.Valves(
        repos_path=str(tmp_path / "repos"), max_results=1
    )
    out = await repos_tools2.cexp_list_repos()
    result = json.loads(out)
    assert len(result["items"]) == 1
    assert result["truncated"] == {"shown": 1, "total": 2}


def test_combined_script_loads_and_discovers_all_tools():
    """dist/code_explorer.py is the single-script build artifact: it must load
    via exec like Open WebUI does and expose ALL tools (no duplicates), one
    Valves class, and no missing/extra members (DESIGN.md §5.4, §9.2)."""
    import inspect

    module = load_script("code_explorer.py")
    tools = module.Tools()
    discovered = sorted(
        func
        for func in dir(tools)
        if callable(getattr(tools, func))
        and not func.startswith("_")  # noqa: SIM102
        and not inspect.isclass(getattr(tools, func))
    )
    assert discovered == [
        "cexp_clone_repo",
        "cexp_compare_commits",
        "cexp_fetch_repo",
        "cexp_list_branches",
        "cexp_list_commits",
        "cexp_list_files",
        "cexp_list_repos",
        "cexp_list_tags",
        "cexp_pull_repo",
        "cexp_read_file",
        "cexp_search_symbol",
        "cexp_search_text",
        "cexp_show_commit",
    ]
    for name in discovered:
        assert getattr(tools, name).__doc__
    # One Valves class only; contract enforced by the parametrized test.
    assert isinstance(tools.valves, module.Tools.Valves)
    # Self-contained: no import common, no leftover marker.
    source = (DIST / "code_explorer.py").read_text(encoding="utf-8")
    assert "import common" not in source
    assert "{{COMMON_CODE}}" not in source
