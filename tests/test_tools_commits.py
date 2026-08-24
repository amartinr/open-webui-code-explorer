"""Integration tests for the Phase 3 Commits script
(cexp_list_branches, cexp_list_tags, cexp_list_commits, cexp_show_commit, cexp_compare_commits).

All tests operate on a local file:// repository with real git history
(branches, tags, multiple commits, a diverging branch), so no network is
needed.
"""

import inspect
import json
import types
from pathlib import Path

import pytest

from common import git_args, run_allowed
from dist.commits import Tools
from dist.repos import Tools as ReposTools

IDENT = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]


async def run_git(cwd: Path, *args: str):
    return await run_allowed(git_args("-C", str(cwd), *args), 60)


async def commit_file(cwd: Path, name: str, content: str, message: str) -> None:
    (cwd / name).write_text(content)
    res = await run_git(cwd, "add", name)
    assert res.returncode == 0, res.stderr
    res = await run_git(cwd, *IDENT, "commit", "-m", message)
    assert res.returncode == 0, res.stderr


async def init_history_repo(path: Path) -> Path:
    """A repo with main and dev branches, semver tags, and a file change."""
    path.mkdir(parents=True, exist_ok=True)
    res = await run_git(path, "init", "-b", "main")
    assert res.returncode == 0, res.stderr

    await commit_file(path, "app.py", "def main():\n    pass\n", "add app")
    await run_git(path, "tag", "v1.0.0")

    await commit_file(path, "app.py", "def main():\n    return 1\n", "feat: return 1")
    await commit_file(path, "util.py", "def util():\n    pass\n", "add util")
    await run_git(path, "tag", "v1.1.0")

    await run_git(path, "checkout", "-b", "dev")
    await commit_file(path, "dev.py", "x = 1\n", "dev: add dev.py")

    await run_git(path, "checkout", "main")
    await commit_file(path, "app.py", "def main():\n    return 2\n", "fix: return 2")
    return path


@pytest.fixture
async def history_repo(tmp_path):
    return await init_history_repo(tmp_path / "src")


@pytest.fixture
def repos_path(tmp_path):
    return tmp_path / "repos"


def make_tools(repos_path: Path) -> Tools:
    tools = Tools()
    tools.valves.repos_path = str(repos_path)
    return tools


def parse_json(out: str) -> dict:
    return json.loads(out)


async def clone_source(repos_path: Path, source: Path, name: str = "testowner/testrepo") -> Tools:
    repos_tools = ReposTools()
    repos_tools.valves.repos_path = str(repos_path)
    out = await repos_tools.cexp_clone_repo(name, url=f"file://{source}")
    assert not out.startswith("Error:"), out
    return make_tools(repos_path)


# ---------------------------------------------------------------------------
# cexp_list_branches
# ---------------------------------------------------------------------------


class TestListBranches:
    async def test_local_branches_with_current_marker(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_list_branches("testowner/testrepo")
        result = parse_json(out)
        by_name = {i["branch"]: i["current"] for i in result["items"]}
        # After a clone, only the default branch is local; dev is origin/dev.
        assert by_name["main"] is True
        assert "dev" not in by_name
        assert "origin/main" not in by_name  # remote=False by default

    async def test_remote_true_includes_remotes(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_list_branches("testowner/testrepo", remote=True)
        result = parse_json(out)
        names = {i["branch"] for i in result["items"]}
        assert "main" in names
        assert "origin/main" in names
        assert "origin/dev" in names
        # The symbolic origin/HEAD pseudo-ref must be filtered out.
        assert not any("->" in n for n in names)

    async def test_detached_head_marks_current(self, repos_path, history_repo):
        repos_tools = ReposTools()
        repos_tools.valves.repos_path = str(repos_path)
        await repos_tools.cexp_clone_repo("testowner/testrepo", url=f"file://{history_repo}", ref="v1.0.0")
        tools = make_tools(repos_path)
        out = await tools.cexp_list_branches("testowner/testrepo")
        result = parse_json(out)
        # Detached HEAD: none marked current.
        assert all(not i["current"] for i in result["items"])

    async def test_repo_not_cloned(self, repos_path):
        tools = make_tools(repos_path)
        out = await tools.cexp_list_branches("o/r")
        assert out.startswith("Not found:")


# ---------------------------------------------------------------------------
# cexp_list_tags
# ---------------------------------------------------------------------------


class TestListTags:
    async def test_lists_tags_newest_first(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_list_tags("testowner/testrepo")
        result = parse_json(out)
        assert result["items"] == ["v1.1.0", "v1.0.0"]

    async def test_no_tags(self, repos_path, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        res = await run_git(src, "init", "-b", "main")
        assert res.returncode == 0
        await commit_file(src, "a.txt", "a\n", "init")
        tools = await clone_source(repos_path, src)
        out = await tools.cexp_list_tags("testowner/testrepo")
        assert parse_json(out)["items"] == []


# ---------------------------------------------------------------------------
# cexp_list_commits
# ---------------------------------------------------------------------------


class TestListCommits:
    async def test_default_head_history(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_list_commits("testowner/testrepo")
        result = parse_json(out)
        subjects = [i["subject"] for i in result["items"]]
        assert "fix: return 2" in subjects
        assert "add util" in subjects
        assert "add app" in subjects
        # newest first (git log default)
        assert subjects[0] == "fix: return 2"
        assert all(i["hash"] for i in result["items"])

    async def test_range(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_list_commits("testowner/testrepo", ref_a="v1.0.0", ref_b="main")
        result = parse_json(out)
        subjects = [i["subject"] for i in result["items"]]
        assert "feat: return 1" in subjects
        assert "fix: return 2" in subjects
        assert "add app" not in subjects  # before v1.0.0

    async def test_path_narrowing(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_list_commits("testowner/testrepo", path="util.py")
        result = parse_json(out)
        subjects = [i["subject"] for i in result["items"]]
        assert subjects == ["add util"]

    async def test_bad_ref_fails_cleanly(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_list_commits("testowner/testrepo", ref_b="nonexistent-ref")
        assert out.startswith("Error:")
        assert "cause:" in out

    async def test_path_traversal_rejected(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_list_commits("testowner/testrepo", path="../evil")
        assert out.startswith("Error:")


# ---------------------------------------------------------------------------
# cexp_show_commit
# ---------------------------------------------------------------------------


class TestShowCommit:
    async def test_shows_commit(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_show_commit("testowner/testrepo", "v1.0.0")
        assert "add app" in out
        assert "app.py" in out
        assert "def main():" in out

    async def test_show_by_branch_and_path(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_show_commit("testowner/testrepo", "main", path="app.py")
        assert "return 2" in out
        assert "util.py" not in out

    async def test_bad_commit_fails_cleanly(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_show_commit("testowner/testrepo", "deadbeef")
        assert out.startswith("Error:")
        assert "cause:" in out

    async def test_empty_commit_rejected(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_show_commit("testowner/testrepo", "   ")
        assert out.startswith("Error:")

    async def test_line_cap(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        tools.valves.max_lines = 5
        out = await tools.cexp_show_commit("testowner/testrepo", "v1.0.0")
        assert "truncated" in out


# ---------------------------------------------------------------------------
# cexp_compare_commits
# ---------------------------------------------------------------------------


class TestCompareCommits:
    async def test_three_dot_diff(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_compare_commits("testowner/testrepo", "v1.0.0", "main")
        assert "feat: return 1" in out or "diff" in out or "app.py" in out

    async def test_stat_summary(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_compare_commits("testowner/testrepo", "v1.0.0", "main", stat=True)
        assert "app.py" in out
        assert "file changed" in out or "files changed" in out

    async def test_path_narrowing(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_compare_commits("testowner/testrepo", "v1.0.0", "main", path="util.py")
        assert "util.py" in out
        assert "app.py" not in out

    async def test_missing_refs_rejected(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_compare_commits("testowner/testrepo", "", "main")
        assert out.startswith("Error:")

    async def test_bad_ref_fails_cleanly(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_compare_commits("testowner/testrepo", "v1.0.0", "nope")
        assert out.startswith("Error:")


# ---------------------------------------------------------------------------
# Open WebUI loading contract (DESIGN.md §9.1, §9.6)
# ---------------------------------------------------------------------------


class TestOpenWebUILoading:
    def test_dist_script_loads_via_exec_and_discovers_tools(self):
        dist_file = Path(__file__).resolve().parent.parent / "dist" / "commits.py"
        source = dist_file.read_text(encoding="utf-8")
        module = types.ModuleType("commits_script")
        exec(compile(source, dist_file.name, "exec"), module.__dict__)

        tools = module.Tools()
        discovered = [
            func
            for func in dir(tools)
            if callable(getattr(tools, func))
            and not func.startswith("_")
            and not inspect.isclass(getattr(tools, func))
        ]
        assert sorted(discovered) == [
            "cexp_compare_commits",
            "cexp_list_branches",
            "cexp_list_commits",
            "cexp_list_tags",
            "cexp_show_commit",
        ]
        for name in discovered:
            assert getattr(tools, name).__doc__

    def test_docstring_params_match_signature_and_are_single_line(self):
        import re

        param_pattern = re.compile(r":param (\w+):\s*(.+)")
        dist_file = Path(__file__).resolve().parent.parent / "dist" / "commits.py"
        module = types.ModuleType("commits_script")
        exec(compile(dist_file.read_text(encoding="utf-8"), dist_file.name, "exec"), module.__dict__)
        tools = module.Tools()

        for name in ["cexp_list_branches", "cexp_list_tags", "cexp_list_commits", "cexp_show_commit", "cexp_compare_commits"]:
            func = getattr(tools, name)
            doc = func.__doc__ or ""
            sig_params = set(inspect.signature(func).parameters)
            parsed = {}
            for line in doc.splitlines():
                m = param_pattern.match(line.strip())
                if m:
                    parsed[m.group(1)] = m.group(2)
            assert set(parsed) == sig_params, f"{name}: :param lines {set(parsed)} != signature {sig_params}"
            for pname, desc in parsed.items():
                assert desc[-1:] in ".)!", f"{name}:{pname} description truncated or unterminated: {desc!r}"

    def test_dist_script_is_self_contained(self):
        dist_file = Path(__file__).resolve().parent.parent / "dist" / "commits.py"
        source = dist_file.read_text(encoding="utf-8")
        assert "import common" not in source
        assert "{{COMMON_CODE}}" not in source
