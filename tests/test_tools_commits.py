"""Integration tests for the Phase 3 Commits script
(cexp_list_branches, cexp_list_tags, cexp_list_commits, cexp_show_commit, cexp_compare_commits).

All tests operate on a local `git://` repository with real git history
(branches, tags, multiple commits, a diverging branch), served by the
`git daemon` fixture (conftest.py): `file://` is blocked by the clone-URL
allow-list, so the daemon provides a real
network-agnostic origin.
"""

import inspect
import json
import types
import uuid
from pathlib import Path

import pytest

from common import git_args, run_allowed
from conftest import DaemonSource, daemon_source
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
async def history_repo(git_daemon):
    return await init_history_repo(daemon_source(git_daemon, f"src-{uuid.uuid4().hex[:8]}"))


@pytest.fixture
def repos_path(tmp_path):
    return tmp_path / "repos"


def make_tools(repos_path: Path) -> Tools:
    tools = Tools()
    tools.valves.repos_path = str(repos_path)
    return tools


def parse_json(out: str) -> dict:
    return json.loads(out)


async def clone_source(repos_path: Path, source: DaemonSource, name: str = "testowner/testrepo") -> Tools:
    repos_tools = ReposTools()
    repos_tools.valves.repos_path = str(repos_path)
    out = await repos_tools.cexp_clone_repo(name, url=source.url)
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
        await repos_tools.cexp_clone_repo("testowner/testrepo", url=history_repo.url, ref="v1.0.0")
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

    async def test_prerelease_sorts_below_release(self, repos_path, git_daemon):
        """A prerelease tag (v2.0.0-rc2) must sort BELOW the pure release
        (v2.0.0), matching cexp_clone_repo(ref="release") and
        cexp_fetch_repo's reported release - unlike git's
        --sort=-version:refname, which treats -rcX as an extra component and
        would put the prerelease first (DESIGN.md §10)."""
        src = daemon_source(git_daemon, f"src-rc-{uuid.uuid4().hex[:8]}")
        src.mkdir()
        res = await run_git(src, "init", "-b", "main")
        assert res.returncode == 0
        await commit_file(src, "a.txt", "a\n", "init")
        await run_git(src, "tag", "v2.0.0-rc2")
        await run_git(src, "tag", "v2.0.0")
        await run_git(src, "tag", "v1.0.0")
        tools = await clone_source(repos_path, src)

        out = await tools.cexp_list_tags("testowner/testrepo")
        assert parse_json(out)["items"] == ["v2.0.0", "v2.0.0-rc2", "v1.0.0"]
        # fetch reports the same resolution (Repos script).
        repos_tools = ReposTools()
        repos_tools.valves.repos_path = str(repos_path)
        result = parse_json(await repos_tools.cexp_fetch_repo("testowner/testrepo"))
        assert result["release"] == "v2.0.0"

    async def test_no_tags(self, repos_path, git_daemon):
        src = daemon_source(git_daemon, f"src-notags-{uuid.uuid4().hex[:8]}")
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

    async def test_author_and_date_fields(self, repos_path, history_repo):
        """E4: items carry author (name) and date (commit date, YYYY-MM-DD)."""
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_list_commits("testowner/testrepo")
        items = parse_json(out)["items"]
        assert items, "expected some commits"
        for item in items:
            assert set(item) == {"hash", "subject", "author", "date"}
            assert item["author"] == "Test"  # IDENT user.name used by the fixture
            assert len(item["date"]) == 10  # YYYY-MM-DD
            y, m, d = item["date"].split("-")
            assert 2000 <= int(y) and 1 <= int(m) <= 12 and 1 <= int(d) <= 31

    async def test_date_is_the_commit_date(self, repos_path, history_repo):
        """E4: the date field equals git's own commit date (--date=short)."""
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_list_commits("testowner/testrepo")
        item = parse_json(out)["items"][0]
        root = repos_path / "testowner" / "testrepo"
        res = await run_git(root, "log", "-1", "--format=%cd", "--date=short", item["hash"])
        assert res.returncode == 0, res.stderr
        assert item["date"] == res.stdout.strip()

    async def test_author_date_in_range_mode(self, repos_path, history_repo):
        """E4: the new fields appear in every list mode (range here)."""
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_list_commits("testowner/testrepo", ref_a="v1.0.0", ref_b="main")
        items = parse_json(out)["items"]
        assert items
        assert all({"hash", "subject", "author", "date"} <= set(i) for i in items)

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

    async def _merge_repo(self, git_daemon) -> DaemonSource:
        """A repo with a real merge: main: init -> (feature branch commit) ->
        merge --no-ff. The feature commit is reachable only via the merged
        branch, so --first-parent must omit it."""
        src = daemon_source(git_daemon, f"src-merge-{uuid.uuid4().hex[:8]}")
        src.mkdir(parents=True, exist_ok=True)
        await run_git(src, "init", "-b", "main")
        await commit_file(src, "base.txt", "base\n", "init")
        await run_git(src, "checkout", "-b", "feature")
        await commit_file(src, "feature.txt", "feat\n", "feature work")
        await run_git(src, "checkout", "main")
        res = await run_git(
            src, *IDENT, "merge", "--no-ff", "-m", "merge feature", "feature"
        )
        assert res.returncode == 0, res.stderr
        return src

    async def test_first_parent_omits_side_branch_commits(self, repos_path, git_daemon):
        """E2: with a merge commit, first_parent=True keeps the merge and the
        main-line commits but hides the feature commit (reachable only via the
        merged branch); the default keeps everything."""
        src = await self._merge_repo(git_daemon)
        tools = await clone_source(repos_path, src)

        out = await tools.cexp_list_commits("testowner/testrepo")
        subjects = {i["subject"] for i in parse_json(out)["items"]}
        assert subjects == {"merge feature", "feature work", "init"}

        out = await tools.cexp_list_commits("testowner/testrepo", first_parent=True)
        subjects = {i["subject"] for i in parse_json(out)["items"]}
        assert subjects == {"merge feature", "init"}

    async def test_first_parent_composes_with_range_and_path(self, repos_path, git_daemon):
        """E2: first_parent must not break ref_a..ref_b ranges or path=."""
        src = await self._merge_repo(git_daemon)
        tools = await clone_source(repos_path, src)

        # Range: init..main with first_parent still works and is a subset.
        init_hash = (await run_git(src, "rev-parse", "HEAD~1")).stdout.strip()
        out = await tools.cexp_list_commits(
            "testowner/testrepo", ref_a=init_hash, ref_b="main", first_parent=True
        )
        result = parse_json(out)
        assert "merge feature" in [i["subject"] for i in result["items"]]

        # path= with first_parent: git follows ONLY the first-parent line, so
        # the feature commit (second parent) is not walked; the merge is where
        # feature.txt entered the mainline, so it is the (single) result.
        out = await tools.cexp_list_commits(
            "testowner/testrepo", path="feature.txt", first_parent=True
        )
        result = parse_json(out)
        assert [i["subject"] for i in result["items"]] == ["merge feature"]

    async def test_bad_ref_fails_cleanly(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_list_commits("testowner/testrepo", ref_b="nonexistent-ref")
        assert out.startswith("Error:")
        assert "cause:" in out

    async def test_path_traversal_rejected(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_list_commits("testowner/testrepo", path="../evil")
        assert out.startswith("Error:")

    async def test_malicious_refs_rejected(self, repos_path, history_repo):
        """Option-injection refs and revision expressions must be rejected by
        validate_ref before reaching git log."""
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_list_commits("testowner/testrepo", ref_b="--all")
        assert out.startswith("Error:")

    # ------------------------------------------------------------------
    # cexp_search_history
    # ------------------------------------------------------------------

    async def test_search_history_finds_introducing_commit(self, repos_path, history_repo):
        """E7: -S finds the commit that introduced the string; items carry the
        E4 shape (hash/subject/author/date)."""
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_search_history("testowner/testrepo", "return 2")
        items = parse_json(out)["items"]
        assert items, "expected a commit for return 2"
        assert all(set(i) == {"hash", "subject", "author", "date"} for i in items)
        assert any(i["subject"] == "fix: return 2" for i in items)

    async def test_search_history_empty_result(self, repos_path, history_repo):
        """E7: absent string -> empty items, not an error."""
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_search_history("testowner/testrepo", "zzz_nothing_zzz")
        result = parse_json(out)
        assert result["items"] == []

    async def test_search_history_path_narrows(self, repos_path, history_repo):
        """E7: path= restricts to that file."""
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_search_history("testowner/testrepo", "return 1", path="util.py")
        assert parse_json(out)["items"] == []  # return 1 lives in app.py
        out = await tools.cexp_search_history("testowner/testrepo", "return 1", path="app.py")
        assert any(i["subject"] == "feat: return 1" for i in parse_json(out)["items"])

    async def test_search_history_range_restricts_window(self, repos_path, history_repo):
        """E7: ref_a..ref_b limits the window."""
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_search_history(
            "testowner/testrepo", "return", ref_a="v1.0.0", ref_b="v1.1.0"
        )
        items = parse_json(out)["items"]
        assert any(i["subject"] == "feat: return 1" for i in items)
        assert not any(i["subject"] == "fix: return 2" for i in items)  # after v1.1.0

    async def test_search_history_refs_validated(self, repos_path, history_repo):
        """E7: refs go through validate_ref (revision expressions rejected)."""
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_search_history("testowner/testrepo", "return", ref_b="HEAD~1")
        assert out.startswith("Error:")
        assert "invalid ref" in out

    async def test_search_history_query_validation(self, repos_path, history_repo):
        """E7: empty/whitespace/NUL/too-long queries are rejected."""
        tools = await clone_source(repos_path, history_repo)
        for bad in ["", "   ", "a\x00b", "x" * 513]:
            out = await tools.cexp_search_history("testowner/testrepo", bad)
            assert out.startswith("Error:"), repr(bad)

    async def test_search_history_capped(self, repos_path, git_daemon):
        """E7: max_results caps the result with truncated metadata. Each
        commit APPENDS a token line (write_text would replace, keeping the
        -S count at 1 for every commit)."""
        src = daemon_source(git_daemon, f"src-hist-{uuid.uuid4().hex[:8]}")
        src.mkdir(parents=True, exist_ok=True)
        await run_git(src, "init", "-b", "main")
        lines = []
        for i in range(6):
            lines.append(f"token{i}")
            (src / "f.txt").write_text("\n".join(lines) + "\n")
            await run_git(src, "add", "-A")
            await run_git(src, *IDENT, "commit", "-m", f"add token{i}")
        tools = await clone_source(repos_path, src)
        tools.valves.max_results = 3
        out = await tools.cexp_search_history("testowner/testrepo", "token")
        result = parse_json(out)
        assert len(result["items"]) == 3
        assert result["truncated"]["shown"] == 3
        assert result["truncated"]["total"] == 6
        out = await tools.cexp_list_commits("testowner/testrepo", ref_a="a..b", ref_b="main")
        assert out.startswith("Error:")
        assert "invalid ref" in out


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

    async def test_stat_returns_summary_without_diff_body(self, repos_path, history_repo):
        """E1: stat=True shows commit metadata + file summary, never the diff
        body (the function source must not appear)."""
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_show_commit("testowner/testrepo", "v1.0.0", stat=True)
        assert "add app" in out  # subject is present
        assert "app.py" in out  # changed-file list is present
        assert "def main():" not in out  # no diff body

    async def test_stat_composes_with_path(self, repos_path, history_repo):
        """E1: path= narrows the stat summary to that file."""
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_show_commit(
            "testowner/testrepo", "main", path="app.py", stat=True
        )
        assert "app.py" in out
        assert "util.py" not in out

    async def test_stat_respects_byte_cap(self, repos_path, history_repo):
        """E1+E5: stat output is capped; the truncation marker (and hint) are
        present when capped."""
        tools = await clone_source(repos_path, history_repo)
        tools.valves.max_bytes = 200
        out = await tools.cexp_show_commit("testowner/testrepo", "v1.0.0", stat=True)
        assert "truncated" in out
        assert "hint:" in out  # E5: raw-text tools append a hint line
        assert len(out.encode("utf-8")) <= 200

    async def test_stat_invalid_commit_errors(self, repos_path, history_repo):
        """E1: invalid commit still errors with stat=True."""
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_show_commit("testowner/testrepo", "deadbeef", stat=True)
        assert out.startswith("Error:")
        assert "cause:" in out

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

    async def test_malicious_commit_rejected(self, repos_path, history_repo):
        """Option injection (--help) and revision expressions must be rejected
        by validate_ref before reaching git show."""
        tools = await clone_source(repos_path, history_repo)
        for bad in ["--help", "HEAD~1", "main^", "a..b"]:
            out = await tools.cexp_show_commit("testowner/testrepo", bad)
            assert out.startswith("Error:"), bad
            assert "invalid ref" in out, bad

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

    async def test_malicious_refs_rejected(self, repos_path, history_repo):
        """Option-injection refs and revision expressions must be rejected by
        validate_ref before reaching git diff."""
        tools = await clone_source(repos_path, history_repo)
        for bad in ["--stat", "HEAD~1", "a..b"]:
            out = await tools.cexp_compare_commits("testowner/testrepo", bad, "main")
            assert out.startswith("Error:"), bad
            assert "invalid ref" in out, bad

    async def test_bad_ref_fails_cleanly(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_compare_commits("testowner/testrepo", "v1.0.0", "nope")
        assert out.startswith("Error:")

    async def test_context_changes_unified_lines(self, repos_path, history_repo):
        """A larger -U N must include more surrounding context lines in the
        diff (the app.py change is a one-line edit, so -U 0 keeps only the
        changed line, -U 5 pulls in the neighbours)."""
        tools = await clone_source(repos_path, history_repo)
        out0 = await tools.cexp_compare_commits(
            "testowner/testrepo", "v1.0.0", "main", path="app.py", context=0
        )
        out5 = await tools.cexp_compare_commits(
            "testowner/testrepo", "v1.0.0", "main", path="app.py", context=5
        )
        assert "@@" in out0
        assert "@@" in out5
        # -U 0: a single hunk covering just the changed line; -U 5 pulls in
        # the neighbours, so the diff carries more lines.
        assert out0.count("@@") == 2  # "@@ -2 +2 @@" (both sides)
        assert out5.count("\n") > out0.count("\n")
        assert "def main():" in out5

    async def test_context_with_stat_ignored(self, repos_path, history_repo):
        """--stat output is unaffected by -U N (no hunks); both must still
        work together without error."""
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_compare_commits(
            "testowner/testrepo", "v1.0.0", "main", stat=True, context=2
        )
        assert "app.py" in out
        assert "file changed" in out or "files changed" in out

    async def test_negative_context_rejected(self, repos_path, history_repo):
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_compare_commits(
            "testowner/testrepo", "v1.0.0", "main", context=-1
        )
        assert out.startswith("Error:")
        assert "context" in out

    async def test_single_file_diff_narrows_to_the_file(self, repos_path, history_repo):
        """The large-diff gap: a single-file diff narrows to the file of
        interest (path=app.py) while the whole-tree diff would include
        util.py too."""
        tools = await clone_source(repos_path, history_repo)
        out = await tools.cexp_compare_commits(
            "testowner/testrepo", "v1.0.0", "main", path="app.py"
        )
        assert "app.py" in out
        assert "return 2" in out
        assert "util.py" not in out


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
            "cexp_search_history",
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
