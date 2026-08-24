"""Integration tests for the Phase 1 Repos script (clone/fetch/pull/list).

All tests operate on local file:// repositories created on the fly, so no
network access is needed and the full surface is exercised end to end.
"""

import asyncio
import inspect
import types
from pathlib import Path

import pytest

from common import CommandResult, _release_sort_key, git_args, run_allowed
from dist.repos import Tools

IDENT = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]


async def run_git(cwd: Path, *args: str) -> CommandResult:
    return await run_allowed(git_args("-C", str(cwd), *args), 60)


async def commit_file(cwd: Path, name: str, content: str, message: str) -> None:
    (cwd / name).write_text(content)
    res = await run_git(cwd, "add", name)
    assert res.returncode == 0, res.stderr
    res = await run_git(cwd, *IDENT, "commit", "-m", message)
    assert res.returncode == 0, res.stderr


async def init_source_repo(path: Path, *, with_release_tags: bool = True) -> Path:
    """Create a local git repo with two commits and optional semver tags."""
    path.mkdir(parents=True, exist_ok=True)
    res = await run_git(path, "init", "-b", "main")
    assert res.returncode == 0, res.stderr
    await commit_file(path, "hello.txt", "hello world\n", "init")
    await commit_file(path, "feature.txt", "feature\n", "add feature")
    if with_release_tags:
        res = await run_git(path, "tag", "v1.0.0", "HEAD~1")
        assert res.returncode == 0
        res = await run_git(path, "tag", "v1.1.0")
        assert res.returncode == 0
    return path


@pytest.fixture
async def source_repo(tmp_path):
    return await init_source_repo(tmp_path / "src")


@pytest.fixture
async def source_repo_no_tags(tmp_path):
    return await init_source_repo(tmp_path / "src", with_release_tags=False)


@pytest.fixture
def repos_path(tmp_path):
    return tmp_path / "repos"


def make_tools(repos_path: Path) -> Tools:
    tools = Tools()
    tools.valves.repos_path = str(repos_path)
    return tools


def src_url(source: Path) -> str:
    return f"file://{source}"


# ---------------------------------------------------------------------------
# clone_repo
# ---------------------------------------------------------------------------


class TestCloneRepo:
    async def test_clone_basic(self, repos_path, source_repo):
        tools = make_tools(repos_path)
        out = await tools.clone_repo("testowner/testrepo", url=src_url(source_repo))

        assert out.startswith("cloned: testowner/testrepo")
        root = repos_path / "testowner" / "testrepo"
        assert (root / ".git").exists()
        assert (root / "hello.txt").read_text() == "hello world\n"
        assert (root / "feature.txt").read_text() == "feature\n"
        assert "default branch: main" in out
        assert "ref: main" in out
        assert "status: clean" in out

    async def test_clone_existing_fails_without_overwrite(self, repos_path, source_repo):
        tools = make_tools(repos_path)
        await tools.clone_repo("testowner/testrepo", url=src_url(source_repo))
        root = repos_path / "testowner" / "testrepo"
        marker = root / "keep.txt"
        marker.write_text("do not delete")

        out = await tools.clone_repo("testowner/testrepo", url=src_url(source_repo))

        assert "already exists" in out
        assert out.startswith("Error:")
        assert marker.read_text() == "do not delete"

    async def test_clone_with_ref_branch(self, repos_path, source_repo):
        # Add a dev branch to the source.
        await run_git(source_repo, "checkout", "-b", "dev")
        await commit_file(source_repo, "dev.txt", "dev\n", "dev work")

        tools = make_tools(repos_path)
        out = await tools.clone_repo("testowner/testrepo", url=src_url(source_repo), ref="dev")

        assert "ref: dev" in out
        root = repos_path / "testowner" / "testrepo"
        branch = await run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
        assert branch.stdout.strip() == "dev"
        assert (root / "dev.txt").exists()

    async def test_clone_ref_release_resolves_highest_semver(self, repos_path, source_repo):
        tools = make_tools(repos_path)
        out = await tools.clone_repo(
            "testowner/testrepo", url=src_url(source_repo), ref="release"
        )

        root = repos_path / "testowner" / "testrepo"
        head = await run_git(root, "rev-parse", "HEAD")
        tag = await run_git(root, "rev-parse", "v1.1.0")
        assert head.stdout.strip() == tag.stdout.strip()
        assert "ref: v1.1.0 (release tag)" in out
        # Detached at the tag: working tree must still be clean.
        assert "status: clean" in out

    async def test_clone_ref_release_no_tags(self, repos_path, source_repo_no_tags):
        tools = make_tools(repos_path)
        out = await tools.clone_repo(
            "testowner/testrepo", url=src_url(source_repo_no_tags), ref="release"
        )
        assert out.startswith("Error:")
        assert "no tags" in out

    async def test_release_fallback_newest_tag_by_date(self, repos_path, tmp_path, monkeypatch):
        src = await init_source_repo(tmp_path / "src", with_release_tags=False)
        # Annotated tags with explicit tagger dates: newest by creatordate wins.
        monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "2020-01-01T00:00:00Z")
        await run_git(src, "tag", "-a", "alpha", "-m", "alpha", "HEAD~1")
        monkeypatch.setenv("GIT_COMMITTER_DATE", "2024-06-15T00:00:00Z")
        await run_git(src, "tag", "-a", "beta", "-m", "beta")
        tools = make_tools(repos_path)
        out = await tools.clone_repo("o/r", url=src_url(src), ref="release")
        assert "ref: beta (release tag)" in out

    async def test_clone_release_prefers_semver_over_newer_non_semver(self, repos_path, tmp_path):
        src = await init_source_repo(tmp_path / "src")  # v1.0.0, v1.1.0
        await run_git(src, "tag", "zzz")  # newest by date, but not semver
        tools = make_tools(repos_path)
        out = await tools.clone_repo("o/r", url=src_url(src), ref="release")
        assert "ref: v1.1.0 (release tag)" in out

    async def test_clone_release_rejects_bad_ref(self, repos_path, source_repo):
        tools = make_tools(repos_path)
        out = await tools.clone_repo("o/r", url=src_url(source_repo), ref="release")
        assert not out.startswith("Error:")

    async def test_clone_bad_ref_fails_cleanly(self, repos_path, source_repo):
        tools = make_tools(repos_path)
        out = await tools.clone_repo(
            "testowner/testrepo", url=src_url(source_repo), ref="nonexistent-ref"
        )
        assert out.startswith("Error:")
        assert "cause:" in out

    async def test_repo_traversal_rejected(self, repos_path, source_repo):
        tools = make_tools(repos_path)
        for bad in ["../evil", "a/../b", "a/b/c", "repo"]:
            out = await tools.clone_repo(bad, url=src_url(source_repo))
            assert out.startswith("Error:"), bad
            assert not (repos_path / "evil").exists()

    async def test_invalid_url_rejected(self, repos_path):
        tools = make_tools(repos_path)
        out = await tools.clone_repo("o/r", url="-o")
        assert out.startswith("Error:")
        assert "invalid clone url" in out

    async def test_failed_clone_cleans_partial_dir(self, repos_path, tmp_path):
        # A clone from a nonexistent remote must fail and leave no junk behind.
        tools = make_tools(repos_path)
        out = await tools.clone_repo("o/r", url=src_url(tmp_path / "does-not-exist"))
        assert out.startswith("Error:")
        assert not (repos_path / "o" / "r").exists()


# ---------------------------------------------------------------------------
# fetch_repo
# ---------------------------------------------------------------------------


class TestFetchRepo:
    async def test_fetch_updates_refs_without_touching_worktree(self, repos_path, source_repo):
        tools = make_tools(repos_path)
        await tools.clone_repo("o/r", url=src_url(source_repo))
        root = repos_path / "o" / "r"

        # Source advances: new commit + new tag.
        await commit_file(source_repo, "late.txt", "late\n", "late work")
        await run_git(source_repo, "tag", "v2.0.0")
        head_before = (await run_git(root, "rev-parse", "HEAD")).stdout.strip()
        content_before = (root / "hello.txt").read_text()

        out = await tools.fetch_repo("o/r")

        assert out.startswith("fetched: o/r")
        assert "v2.0.0 (new)" in out
        assert "origin/main" in out
        # Working tree untouched.
        assert (await run_git(root, "rev-parse", "HEAD")).stdout.strip() == head_before
        assert (root / "hello.txt").read_text() == content_before
        assert not (root / "late.txt").exists()
        # But the new refs are available locally now.
        tag_oid = (await run_git(root, "rev-parse", "v2.0.0")).stdout.strip()
        assert len(tag_oid) == 40

    async def test_fetch_up_to_date(self, repos_path, source_repo):
        tools = make_tools(repos_path)
        await tools.clone_repo("o/r", url=src_url(source_repo))
        out = await tools.fetch_repo("o/r")
        assert "up to date" in out
        assert "no new branches or tags" in out

    async def test_fetch_not_cloned(self, repos_path):
        tools = make_tools(repos_path)
        out = await tools.fetch_repo("o/r")
        assert out.startswith("Not found:")
        assert "not cloned yet" in out


# ---------------------------------------------------------------------------
# pull_repo
# ---------------------------------------------------------------------------


class TestPullRepo:
    async def test_pull_fast_forwards(self, repos_path, source_repo):
        tools = make_tools(repos_path)
        await tools.clone_repo("o/r", url=src_url(source_repo))
        root = repos_path / "o" / "r"

        await commit_file(source_repo, "late.txt", "late\n", "late work")
        out = await tools.pull_repo("o/r")

        assert "fast-forwarded" in out
        # Working tree advanced to the source HEAD.
        head = (await run_git(root, "rev-parse", "HEAD")).stdout.strip()
        src_head = (await run_git(source_repo, "rev-parse", "HEAD")).stdout.strip()
        assert head == src_head
        assert (root / "late.txt").read_text() == "late\n"

    async def test_pull_already_up_to_date(self, repos_path, source_repo):
        tools = make_tools(repos_path)
        await tools.clone_repo("o/r", url=src_url(source_repo))
        out = await tools.pull_repo("o/r")
        assert "Already up to date" in out

    async def test_pull_detached_head_fails_cleanly(self, repos_path, source_repo):
        tools = make_tools(repos_path)
        await tools.clone_repo("o/r", url=src_url(source_repo), ref="v1.0.0")
        out = await tools.pull_repo("o/r")
        assert out.startswith("Error:")
        assert "detached HEAD" in out
        assert "fetch_repo" in out

    async def test_pull_diverged_fails_without_merge(self, repos_path, source_repo):
        tools = make_tools(repos_path)
        await tools.clone_repo("o/r", url=src_url(source_repo))
        root = repos_path / "o" / "r"

        # Local commit diverges...
        await commit_file(root, "local.txt", "local\n", "local change")
        # ...while the source also advances.
        await commit_file(source_repo, "late.txt", "late\n", "late work")

        out = await tools.pull_repo("o/r")

        assert out.startswith("Error:")
        assert "cause:" in out
        # No merge commit created: HEAD is still the local commit.
        head = (await run_git(root, "rev-parse", "HEAD")).stdout.strip()
        local = (await run_git(root, "rev-parse", "HEAD~0")).stdout.strip()
        assert head == local
        log = (await run_git(root, "log", "--oneline", "-1")).stdout
        assert "local change" in log
        # The fetched commit is available but not merged.
        src_head = (await run_git(source_repo, "rev-parse", "HEAD")).stdout.strip()
        origin = (await run_git(root, "rev-parse", "origin/main")).stdout.strip()
        assert origin == src_head

    async def test_pull_not_cloned(self, repos_path):
        tools = make_tools(repos_path)
        out = await tools.pull_repo("o/r")
        assert out.startswith("Not found:")


# ---------------------------------------------------------------------------
# list_repos
# ---------------------------------------------------------------------------


class TestListRepos:
    async def test_lists_clones_with_branch(self, repos_path, source_repo, tmp_path):
        src2 = await init_source_repo(tmp_path / "src2")
        tools = make_tools(repos_path)
        await tools.clone_repo("owner1/repo1", url=src_url(source_repo))
        await tools.clone_repo("owner2/repo2", url=src_url(src2))

        out = await tools.list_repos()

        assert "owner1/repo1  [main]" in out
        assert "owner2/repo2  [main]" in out

    async def test_detached_clone_shows_commit(self, repos_path, source_repo):
        tools = make_tools(repos_path)
        await tools.clone_repo("o/r", url=src_url(source_repo), ref="v1.0.0")
        out = await tools.list_repos()
        assert "o/r  [" in out
        assert "[main]" not in out

    async def test_empty_reports_not_found(self, repos_path):
        tools = make_tools(repos_path)
        out = await tools.list_repos()
        assert out.startswith("Not found:")
        assert "nothing cloned yet" in out

    async def test_ignores_non_git_dirs(self, repos_path, source_repo):
        tools = make_tools(repos_path)
        await tools.clone_repo("o/r", url=src_url(source_repo))
        (repos_path / "o" / "not-a-repo").mkdir(parents=True)
        (repos_path / "not-a-repo").mkdir(parents=True)
        (repos_path / "o" / "r" / "sub").mkdir()  # nested dirs are not repos
        out = await tools.list_repos()
        assert "o/r  [" in out
        assert "not-a-repo" not in out

    async def test_max_results_cap(self, repos_path, source_repo, tmp_path):
        tools = make_tools(repos_path)
        for i in range(5):
            src = await init_source_repo(tmp_path / f"src{i}")
            await tools.clone_repo(f"owner/repo{i}", url=src_url(src))
        tools.valves.max_results = 2
        out = await tools.list_repos()
        assert "truncated: showing 2 of 5" in out


class TestReleaseSortKey:
    def test_highest_semver_wins(self):
        tags = ["v1.0.0-rc1", "v1.0.0", "v0.9.9", "2.0.0", "v1.10.0", "v1.9.0"]
        assert max(tags, key=_release_sort_key) == "2.0.0"

    def test_pure_release_beats_prerelease(self):
        assert max(["v1.0.0-rc1", "v1.0.0"], key=_release_sort_key) == "v1.0.0"

    def test_major_minor_patch_numeric(self):
        assert max(["v2.1.0", "v10.0.0", "v2.9.9"], key=_release_sort_key) == "v10.0.0"

    def test_build_suffix_does_not_beat_release(self):
        # Semver: build metadata carries no precedence, so both orders are equal;
        # the chosen tag must still be version 1.0.0.
        best = max(["v1.0.0+build5", "v1.0.0"], key=_release_sort_key)
        assert best.startswith("v1.0.0")


# ---------------------------------------------------------------------------
# Open WebUI loading contract (DESIGN.md §9.1, §9.6)
# ---------------------------------------------------------------------------


class TestOpenWebUILoading:
    def test_dist_script_loads_via_exec_like_open_webui(self):
        """Open WebUI stores the tool as one source string, exec's it into an
        isolated module, and discovers tools from public methods of `Tools`."""
        dist_file = Path(__file__).resolve().parent.parent / "dist" / "repos.py"
        source = dist_file.read_text(encoding="utf-8")
        module = types.ModuleType("repos_script")
        exec(compile(source, dist_file.name, "exec"), module.__dict__)

        tools = module.Tools()
        assert isinstance(tools.valves, module.Tools.Valves)

        # Mirror of open_webui.utils.tools.get_functions_from_tool.
        discovered = [
            func
            for func in dir(tools)
            if callable(getattr(tools, func))
            and not func.startswith("_")  # noqa: SIM102
            and not inspect.isclass(getattr(tools, func))
        ]
        assert sorted(discovered) == ["clone_repo", "fetch_repo", "list_repos", "pull_repo"]
        for name in discovered:
            assert asyncio.iscoroutinefunction(getattr(tools, name))
            assert getattr(tools, name).__doc__

    def test_dist_script_is_self_contained(self):
        dist_file = Path(__file__).resolve().parent.parent / "dist" / "repos.py"
        source = dist_file.read_text(encoding="utf-8")
        assert "import common" not in source
        assert "{{COMMON_CODE}}" not in source

    def test_docstring_params_match_signature_and_are_single_line(self):
        """Open WebUI parses docstrings line-by-line with
        `:param (\w+):\s*(.+)` (parse_docstring in utils/tools.py), so a
        continuation line indented under a `:param` is silently dropped and
        truncates the description the model sees. Regression: every parameter
        must be documented on ONE self-contained line (DESIGN.md §9.6), the
        parsed `:param` set must equal the signature parameters, and each
        description must end with terminal punctuation (a trailing comma or
        cut-off sentence is a truncation bug)."""
        import inspect
        import re

        param_pattern = re.compile(r":param (\w+):\s*(.+)")
        dist_file = Path(__file__).resolve().parent.parent / "dist" / "repos.py"
        module = types.ModuleType("repos_script")
        exec(compile(dist_file.read_text(encoding="utf-8"), dist_file.name, "exec"), module.__dict__)
        tools = module.Tools()

        for name in ["clone_repo", "fetch_repo", "pull_repo", "list_repos"]:
            func = getattr(tools, name)
            doc = func.__doc__ or ""
            sig_params = set(inspect.signature(func).parameters)
            parsed = {}
            for line in doc.splitlines():
                m = param_pattern.match(line.strip())
                if m:
                    parsed[m.group(1)] = m.group(2)
            # Every signature parameter documented exactly once, nothing extra.
            assert set(parsed) == sig_params, f"{name}: :param lines {set(parsed)} != signature {sig_params}"
            # No truncation: each description is complete on its line.
            for pname, desc in parsed.items():
                assert desc[-1:] in ".)!", f"{name}:{pname} description truncated or unterminated: {desc!r}"


# ---------------------------------------------------------------------------
# Configuration precedence (§5.2): Valve > env > default
# ---------------------------------------------------------------------------


class TestConfigPrecedence:
    async def test_env_repos_path_used(self, tmp_path, monkeypatch, source_repo):
        env_repos = tmp_path / "envrepos"
        monkeypatch.setenv("OWUI_REPOS_PATH", str(env_repos))
        tools = Tools()  # valves.repos_path == "" -> env
        out = await tools.clone_repo("o/r", url=src_url(source_repo))
        assert out.startswith("cloned:")
        assert (env_repos / "o" / "r").exists()

    async def test_valve_overrides_env(self, tmp_path, monkeypatch, source_repo):
        env_repos = tmp_path / "envrepos"
        valve_repos = tmp_path / "valverepos"
        monkeypatch.setenv("OWUI_REPOS_PATH", str(env_repos))
        tools = Tools()
        tools.valves.repos_path = str(valve_repos)
        await tools.clone_repo("o/r", url=src_url(source_repo))
        assert (valve_repos / "o" / "r").exists()
        assert not (env_repos / "o" / "r").exists()

    async def test_list_repos_uses_valve(self, repos_path, source_repo):
        tools = make_tools(repos_path)
        await tools.clone_repo("o/r", url=src_url(source_repo))
        out = await tools.list_repos()
        assert "o/r  [" in out
