"""Unit tests for common.py: the shared, security-critical helpers (§5.6)."""

import json
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
    json_output,
    repo_component_ok,
    resolve_path,
    resolve_repo_root,
    resolve_repos_path,
    run_allowed,
    trim_cause,
    truncate_output,
    validate_clone_url,
    validate_ref,
    _normalize_remote,
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
# validate_ref (DESIGN.md §5.6, §6 - the ref-validation guard)
# ---------------------------------------------------------------------------


class TestValidateRef:
    @pytest.mark.parametrize(
        "good",
        [
            "main",
            "origin/main",
            "release/v1.0.0",
            "v1.0.0",
            "v1.0.0-rc.1+build.5",
            "a1b2c3d",  # short hash
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",  # full hash
            "HEAD",
            "open_webui-dev",
            "feature_x.y-z",
        ],
    )
    def test_valid(self, good):
        assert validate_ref(good) == good

    @pytest.mark.parametrize(
        "bad",
        [
            "",  # empty
            "   ",  # whitespace only
            "a b",  # whitespace inside
            "-x",  # leading dash -> option injection
            "--all",  # option injection
            "a:b",  # revision:path / protocol syntax
            "a..b",  # two-dot revision range
            "a...b",  # three-dot revision range
            "HEAD~1",  # revision expression
            "main^",  # revision expression
            "a*b",  # glob metachar
            "a?b",  # glob metachar
            "a[b]",  # glob metachar
            "a{b}",  # revision metachar
            "a@b",  # reflog metachar
            r"a\b",  # backslash
            ".hidden",  # leading dot
        ],
    )
    def test_invalid(self, bad):
        with pytest.raises(ToolError) as excinfo:
            validate_ref(bad)
        assert "invalid ref" in str(excinfo.value)
        # The error must name the offending ref so the agent can correct it.
        assert repr(bad) in str(excinfo.value)

    def test_valid_does_not_mutate(self):
        ref = "release/v2.3.4"
        assert validate_ref(ref) is ref


# ---------------------------------------------------------------------------
# validate_clone_url / _normalize_remote (Enhancement B, ENHANCEMENT_B.md §2)
# ---------------------------------------------------------------------------


class TestValidateCloneUrl:
    @pytest.mark.parametrize(
        "good,expected",
        [
            ("https://github.com/o/r.git", "https://github.com/o/r.git"),
            ("http://git.local/o/r", "http://git.local/o/r"),
            ("git://127.0.0.1:9418/src", "git://127.0.0.1:9418/src"),
            ("ssh://git@github.com/o/r.git", "ssh://git@github.com/o/r.git"),
            # scp-like is normalized to ssh:// (git's own semantics).
            ("git@github.com:o/r.git", "ssh://git@github.com/o/r.git"),
            ("git@host:1234/repo.git", "ssh://git@host/1234/repo.git"),
            # Scheme matching is case-insensitive.
            ("HTTPS://GITHUB.COM/o/r.git", "HTTPS://GITHUB.COM/o/r.git"),
            # ssh with an explicit port is fine.
            ("ssh://git@host:2222/o/r.git", "ssh://git@host:2222/o/r.git"),
        ],
    )
    def test_valid(self, good, expected):
        assert validate_clone_url(good) == expected

    @pytest.mark.parametrize(
        "bad",
        [
            "",  # empty
            "   ",  # whitespace only
            "-o",  # leading dash -> option injection
            "--upload-pack=x",  # option injection
            "ext::sh -c 'id'",  # git command-execution URL (RCE vector)
            "sh::anything",  # command execution
            "file:///etc/passwd",  # local exfiltration
            "file:///tmp/repo",  # local exfiltration
            "ftp://host/o/r.git",  # stray protocol
            "rsync://host/o/r",  # stray protocol
            "https://TOKEN@github.com/o/r.git",  # credentials would persist
            "https://user:pass@github.com/o/r.git",  # credentials would persist
            "ssh://git:pass@github.com/o/r.git",  # ssh password
            "https://github.com/o/r.git?x=1",  # query string
            "https://github.com/o/r.git#frag",  # fragment
            "git@host",  # scp-like without a path
            "host:repo.git",  # scp-like without a user
            "hello world",  # whitespace, no scheme
        ],
    )
    def test_invalid(self, bad):
        with pytest.raises(ToolError) as excinfo:
            validate_clone_url(bad)
        assert "invalid clone url" in str(excinfo.value)

    def test_error_names_the_url(self):
        with pytest.raises(ToolError) as excinfo:
            validate_clone_url("ftp://host/x")
        assert "ftp://host/x" in str(excinfo.value)


class TestNormalizeRemote:
    @pytest.mark.parametrize(
        "a,b",
        [
            # Same logical repo: trailing .git and case are stripped.
            ("https://github.com/o/r", "https://github.com/o/r.git"),
            ("HTTPS://GitHub.com/o/r.git", "https://github.com/o/r"),
            # Same repo across transports (scheme/ssh user ignored).
            ("https://github.com/o/r", "ssh://git@github.com/o/r"),
            ("git://127.0.0.1:9418/src", "ssh://git@127.0.0.1:9418/src"),
        ],
    )
    def test_equal(self, a, b):
        assert _normalize_remote(a) == _normalize_remote(b)

    @pytest.mark.parametrize(
        "a,b",
        [
            ("https://github.com/o/r", "https://gitlab.com/o/r"),
            ("https://github.com/o/r", "https://github.com/o/other"),
            ("https://github.com/o/r", "git://127.0.0.1:9418/src"),
        ],
    )
    def test_different(self, a, b):
        assert _normalize_remote(a) != _normalize_remote(b)

    def test_no_scheme_passthrough(self):
        assert _normalize_remote("o/r") == "o/r"


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


class TestJsonOutput:
    def test_indented_by_default(self):
        out = json_output({"repo": "o/r", "status": "clean"}, 20480)
        assert "\n" in out  # indented, human-readable
        assert json.loads(out) == {"repo": "o/r", "status": "clean"}

    def test_never_invalid_json(self):
        out = json_output({"items": [{"path": "x" * 500} for _ in range(50)]}, 500)
        parsed = json.loads(out)  # must not raise
        assert parsed["truncated"]["reason"] == "bytes"
        assert parsed["truncated"]["shown"] <= parsed["truncated"]["total"]

    def test_byte_cap_updates_truncated_and_drops_items(self):
        data = {"items": [{"repo": f"owner/repo{i}", "branch": "main"} for i in range(50)]}
        out = json_output(data, 800)
        parsed = json.loads(out)
        assert len(out.encode("utf-8")) <= 800
        assert parsed["truncated"]["reason"] == "bytes"
        assert parsed["truncated"]["total"] == 50
        assert len(parsed["items"]) == parsed["truncated"]["shown"]

    def test_compact_fallback_keeps_all_items_if_possible(self):
        data = {"items": [{"repo": "o/r", "branch": "main"} for _ in range(50)]}
        out = json_output(data, 4096)
        parsed = json.loads(out)
        assert len(parsed["items"]) == 50
        assert "truncated" not in parsed

    def test_preexisting_truncated_kept(self):
        data = {"items": ["x" * 100 for _ in range(30)], "truncated": {"shown": 30, "total": 200}}
        out = json_output(data, 20480)
        parsed = json.loads(out)
        assert parsed["truncated"] == {"shown": 30, "total": 200}

    def test_utf8_preserved(self):
        out = json_output({"repo": "café", "items": ["mañana"]}, 20480)
        assert "café" in out
        assert json.loads(out)["repo"] == "café"


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
        break on older installs (regression: cexp_clone_repo failed with `unknown
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
