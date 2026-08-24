"""Unit tests for common.py: the shared, security-critical helpers (§5.6)."""

import subprocess
from pathlib import Path

import pytest

import common
from common import (
    DEFAULT_REPOS_PATH,
    CommandResult,
    ToolError,
    error_string,
    format_tool_error,
    git_args,
    repo_component_ok,
    resolve_path,
    resolve_repo_root,
    resolve_repos_path,
    run_allowed,
    trim_cause,
    truncate_output,
)


# ---------------------------------------------------------------------------
# resolve_repos_path (DESIGN.md §5.2)
# ---------------------------------------------------------------------------


class TestResolveReposPath:
    def test_valve_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("OWUI_REPOS_PATH", "/env/path")
        assert resolve_repos_path("/valve/path") == "/valve/path"

    def test_env_fallback(self, monkeypatch):
        monkeypatch.setenv("OWUI_REPOS_PATH", "/env/path")
        assert resolve_repos_path("") == "/env/path"
        assert resolve_repos_path("   ") == "/env/path"

    def test_default(self, monkeypatch):
        monkeypatch.delenv("OWUI_REPOS_PATH", raising=False)
        assert resolve_repos_path("") == DEFAULT_REPOS_PATH

    def test_whitespace_valve_ignored(self, monkeypatch):
        monkeypatch.setenv("OWUI_REPOS_PATH", "/env/path")
        assert resolve_repos_path(" \t ") == "/env/path"


# ---------------------------------------------------------------------------
# repo_component_ok (DESIGN.md §5.6 - the path-traversal guard)
# ---------------------------------------------------------------------------


class TestRepoComponentOk:
    @pytest.mark.parametrize(
        "good",
        [
            "open-webui",
            "open_webui",
            "OpenWebUI",
            "repo123",
            "a.b",
            "a-b",
            "A",
            "_x",
            "a1",
            "a-b_c.d",
        ],
    )
    def test_valid(self, good):
        assert repo_component_ok(good)

    @pytest.mark.parametrize(
        "bad",
        ["", ".", "..", "a b", "a/b", "-a", ".a", "..a", "é", "a b", "../x", "/a"],
    )
    def test_invalid(self, bad):
        assert not repo_component_ok(bad)


# ---------------------------------------------------------------------------
# resolve_repo_root (DESIGN.md §5.6)
# ---------------------------------------------------------------------------


class TestResolveRepoRoot:
    def test_valid(self):
        assert resolve_repo_root("open-webui/open-webui", "/base") == Path(
            "/base/open-webui/open-webui"
        )

    @pytest.mark.parametrize(
        "bad",
        [
            "../evil/repo",  # traversal via owner
            "a/../b",  # traversal via name
            "a/b/c",  # three components
            "repo",  # no slash
            "",  # empty
            "/abs/repo",  # leading slash -> empty owner
            "a//b",  # empty name component
            "a/",  # empty name
            "/a/b",  # absolute
        ],
    )
    def test_invalid_rejected(self, bad):
        with pytest.raises(ToolError):
            resolve_repo_root(bad, "/base")

    def test_does_not_check_existence(self):
        root = resolve_repo_root("owner/repo", "/nonexistent/base")
        assert root == Path("/nonexistent/base/owner/repo")


# ---------------------------------------------------------------------------
# resolve_path (DESIGN.md §5.6 - symlink escape guard)
# ---------------------------------------------------------------------------


class TestResolvePath:
    def test_none_returns_root(self, tmp_path):
        assert resolve_path("o/r", None, str(tmp_path)) == tmp_path / "o" / "r"

    def test_empty_returns_root(self, tmp_path):
        assert resolve_path("o/r", "", str(tmp_path)) == tmp_path / "o" / "r"

    def test_relative_ok(self, tmp_path):
        assert resolve_path("o/r", "src/x.py", str(tmp_path)) == tmp_path / "o" / "r" / "src" / "x.py"

    @pytest.mark.parametrize("bad", ["/etc/passwd", "\\etc", "C:\\x", "C:/x"])
    def test_absolute_rejected(self, bad, tmp_path):
        with pytest.raises(ToolError):
            resolve_path("o/r", bad, str(tmp_path))

    @pytest.mark.parametrize("bad", ["..", "src/../..", "a/../b"])
    def test_dotdot_rejected(self, bad, tmp_path):
        with pytest.raises(ToolError):
            resolve_path("o/r", bad, str(tmp_path))

    def test_symlink_escape_rejected(self, tmp_path):
        repos = tmp_path / "repos"
        root = repos / "o" / "r"
        root.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "link").symlink_to(outside)
        with pytest.raises(ToolError):
            resolve_path("o/r", "link/secret.txt", str(repos))

    def test_symlink_inside_is_allowed(self, tmp_path):
        repos = tmp_path / "repos"
        root = repos / "o" / "r"
        root.mkdir(parents=True)
        inner = root / "inner"
        inner.mkdir()
        (root / "link").symlink_to(inner)
        assert resolve_path("o/r", "link/file.txt", str(repos)) == root / "link" / "file.txt"


# ---------------------------------------------------------------------------
# run_allowed (DESIGN.md §4, §5.6, §9.7)
# ---------------------------------------------------------------------------


class TestRunAllowed:
    async def test_allowed_git_returns_pipes_as_data(self):
        res = await run_allowed(git_args("--version"), 10)
        assert isinstance(res, CommandResult)
        assert res.returncode == 0
        assert "git version" in res.stdout

    async def test_disallowed_binary_rejected(self):
        with pytest.raises(ToolError, match="disallowed command"):
            await run_allowed(["bash", "-c", "echo hi"], 10)

    async def test_missing_binary_rejected(self, monkeypatch):
        monkeypatch.setattr(common.shutil, "which", lambda name: None)
        with pytest.raises(ToolError, match="not found in PATH"):
            await run_allowed(git_args("--version"), 10)

    async def test_timeout_becomes_timed_out_error(self, monkeypatch):
        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

        monkeypatch.setattr(common.subprocess, "run", fake_run)
        with pytest.raises(ToolError) as excinfo:
            await run_allowed(git_args("--version"), 1)
        assert excinfo.value.kind == "timed_out"
        assert "timed out after 1s" in str(excinfo.value)

    async def test_git_never_reads_global_config(self, tmp_path, monkeypatch):
        # A user/global gitconfig must never be honored (DESIGN.md §9.7): the
        # fixed headless env forces GIT_CONFIG_GLOBAL=/dev/null, so a hostile
        # alias defined outside the tool cannot be seen or executed.
        bad_cfg = tmp_path / "bad-config"
        bad_cfg.write_text("[alias]\n  clone = !echo PWNED\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(bad_cfg))
        res = await run_allowed(["git", "config", "--global", "--get", "alias.clone"], 10)
        assert res.returncode == 1  # key not found: hostile config was ignored
        assert "PWNED" not in res.stdout

    async def test_git_failure_reports_nonzero_returncode(self, tmp_path):
        res = await run_allowed(git_args("-C", str(tmp_path), "rev-parse", "--git-dir"), 10)
        assert res.returncode != 0


# ---------------------------------------------------------------------------
# Output capping (DESIGN.md §4.4, §5.5)
# ---------------------------------------------------------------------------


class TestTruncateOutput:
    def test_no_truncation(self):
        text = "a\nb\nc"
        assert truncate_output(text, 100, 100000) == text

    def test_empty(self):
        assert truncate_output("", 10, 100) == ""

    def test_line_cap(self):
        text = "\n".join(f"line{i}" for i in range(10))
        out = truncate_output(text, 3, 100000)
        assert out == "line0\nline1\nline2\n... (truncated: showing 3 of 10 lines)"

    def test_line_cap_keeps_trailing_newline_semantics(self):
        text = "a\nb\n"
        out = truncate_output(text, 1, 100000)
        assert out.startswith("a\n")
        assert "showing 1 of 2 lines" in out

    def test_byte_cap(self):
        text = ("a" * 40 + "\n") * 10  # 410 bytes, 10 lines
        out = truncate_output(text, 200, 100)
        assert len(out.encode("utf-8")) <= 100
        assert "byte cap of 100 reached" in out
        assert "of 410 bytes" in out

    def test_both_caps_line_binds(self):
        text = "\n".join(f"line{i:04d}" for i in range(1000))
        out = truncate_output(text, 5, 100000)
        assert "showing 5 of 1000 lines" in out
        assert out.count("\n") == 5  # 4 separators + marker line

    def test_utf8_bytes_not_chars(self):
        text = ("é" * 30 + "\n") * 5  # é is 2 bytes in UTF-8
        out = truncate_output(text, 200, 100)
        assert len(out.encode("utf-8")) <= 100
        assert "byte cap" in out


class TestTrimCause:
    def test_strips_ansi_and_progress(self):
        raw = "\x1b[31mremote: Counting\x1b[0m\nReceiving objects: 100%\n fatal: repo 'x' not found\n"
        out = trim_cause(raw)
        assert "fatal" in out
        assert "\x1b" not in out
        assert "Receiving objects" not in out

    def test_empty(self):
        assert trim_cause("") == ""

    def test_limit(self):
        out = trim_cause("x" * 1000)
        assert len(out) <= 300 + 3  # limit + ellipsis


class TestGitArgs:
    def test_only_config_flags_and_command_args(self):
        """The ONLY global options git_args emits are the `-c key=value`
        config forms, which git 2.39 (the minimum supported version) accepts.
        Long global flags like --no-advice (git >= 2.45) are forbidden: they
        break on older installs (regression: clone_repo failed with `unknown
        option: --no-advice` on git 2.39.5)."""
        forbidden = {"--no-advice", "--no-pager", "--paginate", "--exec-path", "--html-path"}
        for args in [
            git_args("status", "--porcelain"),
            git_args("clone", "--no-progress", "url", "dir"),
            git_args("-C", "/x", "fetch", "--all"),
            git_args("-C", "/x", "-c", "advice.detachedHead=false", "checkout", "ref"),
        ]:
            # git_args always emits exactly: git, -c, key=value, then the
            # caller's args (which may include -C/-c passthroughs and
            # subcommand flags - none of which are long global flags).
            assert args[0] == "git"
            assert args[1] == "-c"
            assert "=" in args[2]
            assert not args[2].startswith("-")
            assert not (forbidden & set(args))

    def test_no_no_advice_anywhere(self):
        assert "--no-advice" not in git_args("version")


# ---------------------------------------------------------------------------
# Error rendering (DESIGN.md §9.3)
# ---------------------------------------------------------------------------


class TestErrorFormat:
    def test_error_shape(self):
        exc = ToolError("something failed")
        assert format_tool_error(exc) == "Error: something failed"

    def test_with_cause(self):
        exc = ToolError("clone failed", cause="fatal: repo not found")
        assert format_tool_error(exc) == "Error: clone failed\ncause: fatal: repo not found"

    def test_not_found_kind(self):
        exc = ToolError("no repos here", kind="not_found")
        assert format_tool_error(exc) == "Not found: no repos here"

    def test_timed_out_kind(self):
        exc = ToolError("timed out after 5s", kind="timed_out")
        assert format_tool_error(exc) == "Timed out: timed out after 5s"

    def test_error_string_unknown_exception_no_traceback(self):
        out = error_string(ValueError("boom"))
        assert out.startswith("Error: unexpected failure: ValueError: boom")
        assert "Traceback" not in out

    def test_error_string_toolerror(self):
        assert error_string(ToolError("nope")) == "Error: nope"
