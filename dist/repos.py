"""
title: Code Explorer - Repos
author: A. Martin
author_url: https://github.com/amartinr
version: 1.2.5
icon_url: https://github.com/amartinr/open-webui-code-explorer/raw/main/docs/icon.svg
description: Clone, fetch, pull, and list code repositories for the meta model. Read-only with respect to source code; writes happen only inside the allow-listed repositories directory, and only via git.
required_open_webui_version: 0.9.6
"""
from typing import Optional

from pydantic import BaseModel, Field

"""Shared helpers for the Code Explorer tools.

This module is the SINGLE SOURCE OF TRUTH for the security-critical logic
(see DESIGN.md §5.6). It is inlined verbatim into each generated tool script
by build.py, so it MUST stay inline-safe:

- stdlib imports only
- no relative imports
- no module-level side effects (no I/O, no logging, no printing)
- no `if __name__ == "__main__"` guard

Every tool goes through these helpers for repo/path resolution, subprocess
execution, and output capping. Do not re-implement any of this per tool.
"""

import asyncio
import codecs
import fnmatch
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constants (DESIGN.md §5.2, §9.4, §9.7)
# ---------------------------------------------------------------------------

DEFAULT_REPOS_PATH = "/usr/local/src"
ENV_REPOS_PATH = "OWUI_REPOS_PATH"

ALLOWED_BINARIES = {"git"}

# Timeout policy (DESIGN.md §9.4), in seconds.
TIMEOUT_CLONE = 600
TIMEOUT_FETCH = 120
TIMEOUT_PULL = 120
TIMEOUT_READ = 10
TIMEOUT_SEARCH = 30
TIMEOUT_COMMIT = 30

# cexp_read_file safety limits: files larger than MAX_READ_BYTES are rejected;
# ranges larger than MAX_INLINE_LINES are streamed (only the shown lines are
# read) instead of being read fully into memory.
MAX_READ_BYTES = 50 * 1024 * 1024
MAX_INLINE_LINES = 5000

# Headless, non-interactive environment for every subprocess (DESIGN.md §9.7).
# GIT_ASKPASS is deliberately removed (unset) rather than set to "".
HEADLESS_ENV: Dict[str, str] = {
    "GIT_TERMINAL_PROMPT": "0",  # fail instead of prompting for credentials
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",  # never prompt for SSH passphrases
    "GIT_PAGER": "cat",  # no pager, ever
    "GIT_CONFIG_NOSYSTEM": "1",  # ignore system gitconfig
    "GIT_CONFIG_GLOBAL": "/dev/null",  # ignore user global gitconfig
    "GIT_OPTIONAL_LOCKS": "0",  # read-only commands skip optional locks
    "LC_ALL": "C",  # stable, English, non-localized output
}

# GIT_* vars that can point git at a DIFFERENT repository, object store, ssh
# binary, index, or namespace than the one the tool resolved. They are popped
# from the subprocess environment in _headless_env (S2): the overrides above
# set the policy; these remove the hijack surface.
PURGED_GIT_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_SSH",
    "GIT_SSH_VARIANT",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_COMMON_DIR",
)

_REPO_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_REF_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/+-]*$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_RELEASE_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)([-+].*)?$")

# Clone-URL allow-list. Only these
# schemes may appear in a cexp_clone_repo `url` override. Everything else
# (ext::/sh:: command URLs, file://, ftp, rsync, ...) is rejected.
ALLOWED_CLONE_SCHEMES = {"https", "http", "git", "ssh"}
# git's scp-like syntax: user@host:path (no scheme). No port is possible in
# this form (use ssh://host:port/path), matching git's own semantics.
_SCPLIKE_RE = re.compile(r"^([^/@:]+)@([^/:]+):(.+)$")


def _release_sort_key(tag: str) -> tuple:
    """Sort key for release tags: numeric semver, preferring pure X.Y.Z over
    pre-release/build suffixed tags (v1.0.0 > v1.0.0-rc1)."""
    m = _RELEASE_TAG_RE.match(tag)
    assert m is not None
    nums = tuple(int(g) for g in m.group(1, 2, 3))
    suffix = m.group(4) or ""
    is_release = 1 if not suffix.startswith("-") else 0
    return nums + (is_release, suffix)

TRUNCATION_MARKER = "... (truncated: showing {shown} of {total})"

# ---------------------------------------------------------------------------
# Errors (DESIGN.md §9.3)
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """Raised for expected, agent-facing failures.

    kind selects the error prefix: "error" -> `Error:`, "not_found" ->
    `Not found:`, "timed_out" -> `Timed out:`. `cause` is an optional,
    concise, noise-stripped reason line included only when it helps the
    agent correct its input.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "error",
        cause: Optional[str] = None,
    ) -> None:
        self.message = message
        self.kind = kind
        self.cause = cause
        super().__init__(message)


_PREFIXES = {"error": "Error", "not_found": "Not found", "timed_out": "Timed out"}


def format_tool_error(exc: ToolError) -> str:
    """Render a ToolError as the stable agent-readable error shape (§9.3)."""
    prefix = _PREFIXES.get(exc.kind, "Error")
    lines = [f"{prefix}: {exc.message}"]
    if exc.cause:
        lines.append(f"cause: {exc.cause}")
    return "\n".join(lines)


def error_string(exc: Exception) -> str:
    """Convert any exception raised inside a tool into an agent-facing string.

    Tools NEVER raise exceptions to the model: ToolError becomes the
    structured `Error:`/`Not found:`/`Timed out:` shape; anything else becomes
    a generic unexpected-failure line (no raw traceback).
    """
    if isinstance(exc, ToolError):
        return format_tool_error(exc)
    return f"Error: unexpected failure: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Pure-Python filesystem/search helpers (DESIGN.md §5.6, §7 Phase 2)
#
# The Files & Search tools are implemented without the fd/rg binaries: the
# deployment environment does not provide them, but it does provide the
# `pathspec` package (fd/rg's own .gitignore engine) and the `regex` package
# (backtracking regexes, matching ripgrep's syntax). The fallbacks below keep
# the tools functional on any stdlib-only environment. Because these helpers
# are inlined into the self-contained scripts, the imports are guarded and
# the stdlib-only path must work.
# ---------------------------------------------------------------------------

_IGNORE_PATTERNS = (
    ".git/",
    ".hg/",
    ".svn/",
    ".DS_Store",
    "*.swp",
    "*~",
)


def _load_ignore_spec(root: Path, base: Optional[Path] = None):
    """Build a pathspec for the repo's .gitignore files (root + nested),
    honoring the gitignore semantics of git/fd/ripgrep. Returns None if
    pathspec is unavailable (stdlib-only fallback).

    Nested .gitignore files are honored: each subdirectory's .gitignore is
    read and its patterns are prefixed with the subdir path so they apply
    relative to that subdir (git semantics). A .gitignore also applies to
    the directory it lives in, so all of them are merged.
    """
    try:
        import pathspec  # type: ignore
    except ImportError:
        return None
    lines = list(_IGNORE_PATTERNS)
    for gi in sorted(root.rglob(".gitignore")):
        if not gi.is_file():
            continue
        try:
            rel = gi.relative_to(root).as_posix()
        except ValueError:
            continue
        gi_dir = "" if rel == ".gitignore" else rel[: -len(".gitignore")].rstrip("/")
        try:
            gi_lines = gi.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in gi_lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if gi_dir:
                # gitignore semantics: a pattern in subdir applies relative to
                # that subdir. If it does not contain a slash, it matches at
                # any depth below that subdir, so prefixing the dir works.
                # A leading "!" (negation) must stay at the very front.
                neg = line.startswith("!")
                body = line[1:] if neg else line
                if body.startswith("/"):
                    body = "/" + gi_dir + body  # anchored to the subdir
                else:
                    body = gi_dir + "/" + body
                line = ("!" if neg else "") + body
            lines.append(line)
    try:
        # pathspec >= 0.10 names the gitignore engine "gitignore" ("gitwildmatch"
        # is deprecated in 1.x). Fall back for older versions.
        try:
            return pathspec.PathSpec.from_lines("gitignore", lines)
        except Exception:
            return pathspec.PathSpec.from_lines("gitwildmatch", lines)
    except Exception:
        return None


def _ignore_match(spec, rel: str, is_dir: bool, base: Optional[Path] = None) -> bool:
    """True iff `rel` (relative to repo root) is ignored."""
    if spec is None:
        return False
    key = rel + "/" if is_dir else rel
    try:
        return spec.match_file(key)
    except Exception:
        return False


def _walk_repo(root: Path, base: Path, max_depth: Optional[int] = None):
    """Yield (rel_path, is_dir) for every entry under `base`, respecting
    .gitignore (via pathspec when available) and skipping .git. When
    `max_depth` is given, deeper directories are pruned (not descended).
    Depth is measured from `base` (0 = base itself), matching fd."""
    spec = _load_ignore_spec(root)
    try:
        base_rel = base.relative_to(root).as_posix()
    except ValueError:
        base_rel = "."
    base_depth = 0 if base_rel == "." else base_rel.count("/") + 1
    for dirpath, dirnames, filenames in os.walk(base):
        dp = Path(dirpath)
        try:
            rel_dp = dp.relative_to(root).as_posix()
        except ValueError:
            rel_dp = dp.as_posix()
        if rel_dp != ".":
            parts = rel_dp.split("/")
            if any(p in (".git", ".hg", ".svn") for p in parts):
                dirnames[:] = []
                continue
            if _ignore_match(spec, rel_dp, True):
                dirnames[:] = []
                continue
        descend = []
        for d in dirnames:
            if d in (".git", ".hg", ".svn"):
                continue
            rel_d = rel_dp + "/" + d if rel_dp != "." else d
            # Depth relative to base: number of path components below base.
            rel_d_depth = max(0, len(Path(rel_d).parts) - base_depth)
            if _ignore_match(spec, rel_d, True):
                continue
            yield rel_d, True  # always list the dir itself
            if max_depth is not None and rel_d_depth >= max_depth:
                continue  # at the depth limit: do NOT descend into it
            descend.append(d)
        dirnames[:] = descend
        for f in filenames:
            rel_f = rel_dp + "/" + f if rel_dp != "." else f
            if not _ignore_match(spec, rel_f, False):
                yield rel_f, False


def _read_binary_sample(path: Path, size: int = 8192) -> bytes:
    """First `size` bytes of a file (empty bytes if unreadable)."""
    try:
        with open(path, "rb") as f:
            return f.read(size)
    except OSError:
        return b""


def _scan_encoding(path: Path, chunk: int = 65536) -> Optional[str]:
    """Scan the WHOLE file for binary content: "binary" if a null byte is
    found anywhere, "invalid_utf8" if strict UTF-8 decoding fails anywhere,
    None if the file is valid UTF-8 text without null bytes.

    Uses an incremental decoder so chunk boundaries never split a multibyte
    character (a naive per-chunk decode would false-positive on a character
    cut in half). This replaces the 8 KB sample check, which missed binary
    bytes past the sample and silently returned corrupted text.
    """
    dec = codecs.getincrementaldecoder("utf-8")()
    try:
        with open(path, "rb") as f:
            while True:
                block = f.read(chunk)
                if not block:
                    break
                if b"\x00" in block:
                    return "binary"
                try:
                    dec.decode(block)
                except UnicodeDecodeError:
                    return "invalid_utf8"
        try:
            dec.decode(b"", final=True)
        except UnicodeDecodeError:
            return "invalid_utf8"
    except OSError:
        return "binary"
    return None


def _scan_encoding_bytes(data: bytes, chunk: int = 65536) -> Optional[str]:
    """Binary/UTF-8 scan of an in-memory blob (DESIGN.md §12.1 read-at-ref):
    "binary" if a null byte appears anywhere, "invalid_utf8" if strict UTF-8
    decoding fails anywhere, None if the data is valid UTF-8 text without null
    bytes. Mirrors `_scan_encoding` (same incremental-decoder semantics, same
    boundaries) for blob content fetched from git instead of the filesystem.
    """
    if b"\x00" in data:
        return "binary"
    dec = codecs.getincrementaldecoder("utf-8")()
    try:
        for i in range(0, len(data), chunk):
            dec.decode(data[i : i + chunk])
        dec.decode(b"", final=True)
    except UnicodeDecodeError:
        return "invalid_utf8"
    return None


def _is_binary(sample: bytes) -> bool:
    """True iff the sample looks binary: NUL bytes or mostly non-text."""
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    text = sample[:512].decode("utf-8", errors="replace")
    control = sum(1 for c in text if ord(c) < 32 and c not in "\n\r\t")
    return control > 2


def _decode_text(data: bytes) -> str:
    """Decode bytes as UTF-8 (replacing invalid sequences), BOM-tolerant."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", errors="replace")


def _binary_or_readable(path: Path) -> tuple:
    """(is_binary, decoded_text) - decodes only when text."""
    sample = _read_binary_sample(path)
    if _is_binary(sample):
        return True, ""
    try:
        return False, _decode_text(open(path, "rb").read())
    except OSError:
        return True, ""


def _line_is_binary(line: str) -> bool:
    """Fast per-line binary heuristic used while scanning big files."""
    return "\x00" in line or (len(line) > 0 and sum(1 for c in line if ord(c) < 8) > 2)


def _is_binary_path(p: Path) -> bool:
    """Binary check on a path, used by cexp_search_text (scans many files)."""
    return _is_binary(_read_binary_sample(p))


def _search_in_text(pattern, text: str, base: int, context: int, path: str, out: list) -> None:
    """Find all matches of `pattern` in `text` starting at line offset `base`."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if pattern.search(line):
            item = {"path": path, "line": base + i + 1, "text": line}
            if context:
                ctx = []
                for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                    if j == i:
                        continue
                    ctx.append(f"{base + j + 1}: {lines[j]}")
                item["context"] = ctx
            out.append(item)


def _iter_big_file_lines(path: Path, max_line_len: int = 1024 * 1024):
    """Yield (line, is_binary) lazily for very large files, skipping
    pathological single-line files (cap at max_line_len bytes per line)."""
    try:
        with open(path, "rb") as f:
            remaining = b""
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                chunk = remaining + chunk
                *lines, remaining = chunk.split(b"\n")
                for ln in lines:
                    if len(ln) > max_line_len:
                        continue
                    yield ln, _line_is_binary(ln)
            if remaining and len(remaining) <= max_line_len:
                yield remaining, _line_is_binary(remaining)
    except OSError:
        return


def _compile_pattern(
    query: str, case_sensitive: bool, multiline: bool = False
):
    """Compile the user regex with the `regex` package when available (matching
    ripgrep's backtracking engine), falling back to `re` (cached) otherwise.
    Query errors raise ToolError."""
    flags = 0
    if not case_sensitive:
        flags |= re.IGNORECASE
    if multiline:
        flags |= re.MULTILINE
    try:
        import regex as _regex  # type: ignore

        return _regex.compile(query, flags)
    except ImportError:
        try:
            return re.compile(query, flags)
        except re.error as e:
            raise ToolError(f"invalid regex: {query!r}: {e}")
    except Exception as e:
        raise ToolError(f"invalid regex: {query!r}: {e}")


def _has_regex_package() -> bool:
    try:
        import regex  # type: ignore

        return True
    except ImportError:
        return False


def _rglob_repo(base: Path):
    """rglob equivalent that works without pathspec: walks all files under
    `base`, skipping .git and VCS dirs."""
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in (".git", ".hg", ".svn")]
        for f in filenames:
            yield Path(dirpath) / f


def _match_glob(rel: str, globs: List[str]) -> bool:
    """Match a path against a glob set (fnmatch, fd-style: match against the
    full relative path)."""
    return any(fnmatch.fnmatch(rel, g) for g in globs)


def _try_decode_rel(p: Path, root: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()


# ---------------------------------------------------------------------------
# Subprocess execution (DESIGN.md §4, §5.6, §9.7)
# ---------------------------------------------------------------------------


@dataclass
class CommandResult:
    """Captured pipes of a finished subprocess. Both streams are DATA.

    With `run_allowed(text=True)` (default) `stdout`/`stderr` are `str`;
    with `text=False` they are `bytes` (raw output, decoded by the caller).
    """

    stdout: str
    stderr: str
    returncode: int


def _headless_env() -> Dict[str, str]:
    env = dict(os.environ)
    for key, value in HEADLESS_ENV.items():
        env[key] = value
    # No askpass helper may launch a prompt (must be unset, not "").
    env.pop("GIT_ASKPASS", None)
    # No GIT_* var may redirect git to a different repo/ssh/index/namespace.
    for key in PURGED_GIT_ENV_VARS:
        env.pop(key, None)
    return env


async def _kill_process(proc: asyncio.subprocess.Process) -> None:
    """Kill a subprocess in stages so no zombie is left (S4).

    Graceful terminate -> short grace period -> hard kill -> reap. Safe to
    call on an already-exited process (each step absorbs the failure). Used
    by run_allowed on both timeout and cancellation.
    """
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), 1.0)
        return
    except (asyncio.TimeoutError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await proc.wait()
    except ProcessLookupError:
        pass


async def run_allowed(
    argv: List[str], timeout: int, *, text: bool = True, input: Optional[bytes] = None
) -> CommandResult:
    """Run an allow-listed binary with arguments, capturing both pipes.

    - argv[0] MUST be one of ALLOWED_BINARIES (no arbitrary commands).
    - No shell: argument arrays only (shell=False).
    - Runs the process asynchronously (create_subprocess_exec + communicate)
      so BOTH the timeout and a task cancellation act on the real process
      (S4): on timeout the process is killed and `Timed out:` is returned;
      on CancelledError the process is killed and the exception is
      re-raised (never swallowed).
    - Uses the fixed headless environment (§9.7) so git can never prompt,
      page, localize, or read user/global config.
    - On timeout raises ToolError(kind="timed_out").
    - `text=False` captures raw bytes (stdout/stderr are `bytes`) so callers
      that need byte-exact output (e.g. blob reads for binary/UTF-8
      detection) can decode explicitly; `text=True` (default) decodes with
      the process locale and is only for human-facing git output.
    - `input` (bytes) is written to the child's stdin (e.g. `git cat-file
      --batch` fed a list of rev:path lines); use it with `text=False`.
    """
    if not argv:
        raise ToolError("empty command")
    if argv[0] not in ALLOWED_BINARIES:
        raise ToolError(f"disallowed command: {argv[0]!r} (allow-list: {sorted(ALLOWED_BINARIES)})")
    exe = shutil.which(argv[0])
    if exe is None:
        # Fail-closed environment event: git missing at runtime. Unlike the
        # load-time check_binaries warning, this is emitted unconditionally
        # (zero volume, high value, logger-only) - it has no per-instance
        # Valve to opt out of, and a broken environment must leave a trace.
        audit_event(
            True,
            logging.ERROR,
            "git",
            "",
            "failed",
            error=f"required binary not found in PATH: {argv[0]}",
        )
        raise ToolError(f"required binary not found in PATH: {argv[0]}")
    full = [exe, *argv[1:]]
    env = _headless_env()
    try:
        proc = await asyncio.create_subprocess_exec(
            *full,
            stdin=asyncio.subprocess.PIPE if input is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=input), timeout
            )
        except asyncio.TimeoutError:
            await _kill_process(proc)
            raise ToolError(f"timed out after {timeout}s", kind="timed_out")
        except asyncio.CancelledError:
            await _kill_process(proc)
            raise  # cancellation must propagate as BaseException
    except asyncio.CancelledError:
        raise  # the spawn itself was cancelled before a process existed
    if text:
        return CommandResult(
            stdout=(stdout_b or b"").decode("utf-8", errors="replace"),
            stderr=(stderr_b or b"").decode("utf-8", errors="replace"),
            returncode=proc.returncode,
        )
    return CommandResult(
        stdout=stdout_b or b"",
        stderr=stderr_b or b"",
        returncode=proc.returncode,
    )


def git_args(*args: str) -> List[str]:
    """Base git invocation with headless-safe global flags (DESIGN.md §9.7).

    Compatible with git >= 2.39 (the minimum supported version). Deliberately
    does NOT use `--no-advice`, which only exists since git 2.45: advice hints
    go to stderr, never pollute stdout, and are stripped from error output by
    trim_cause(). Where a specific advice must be suppressed, pass the config
    form explicitly (e.g. `-c advice.detachedHead=false`).
    """
    return ["git", "-c", "color.ui=never", *args]


def check_binaries(*names: str) -> None:
    """Log a clear warning at tool load time for missing required binaries."""
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        logging.getLogger("code_explorer").warning(
            "required binaries not found in PATH: %s", ", ".join(missing)
        )


# ---------------------------------------------------------------------------
# Repo / path resolution (DESIGN.md §5.2, §5.6)
# ---------------------------------------------------------------------------


def resolve_repos_path(valve_path: str) -> str:
    """Priority: Valve repos_path -> env OWUI_REPOS_PATH -> /usr/local/src."""
    if valve_path and valve_path.strip():
        return valve_path.strip()
    env = os.environ.get(ENV_REPOS_PATH)
    if env and env.strip():
        return env.strip()
    return DEFAULT_REPOS_PATH


def parse_filter(filter_str: Optional[str]) -> tuple:
    """Split a `filter` value into (includes, excludes) glob patterns.

    A filter string may contain space-separated glob patterns; a leading "!"
    marks an exclusion (DESIGN.md §6): "*.py !*.md" -> includes ["*.py"],
    excludes ["*.md"].
    """
    if not filter_str or not filter_str.strip():
        return [], []
    includes: List[str] = []
    excludes: List[str] = []
    for pat in filter_str.split():
        if pat.startswith("!"):
            excludes.append(pat[1:])
        else:
            includes.append(pat)
    return includes, excludes


def glob_match(relpath: str, includes: List[str], excludes: List[str]) -> bool:
    """True iff `relpath` matches the include/exclude glob sets.

    Used for the single-file case of cexp_list_files (fd-style semantics: globs are
    matched against the full relative path).
    """
    if excludes and any(fnmatch.fnmatch(relpath, g) for g in excludes):
        return False
    if includes and not any(fnmatch.fnmatch(relpath, g) for g in includes):
        return False
    return True


def host_allowed(host: str, allowed_hosts: str) -> bool:
    """True iff `host` is allowed by the comma-separated `allowed_hosts`
    Valve (S3). Exact host match or suffix match on a dot boundary: listing
    "github.com" allows "github.com" and "raw.githubusercontent.com" but not
    "evilgithub.com" or "notgithub.com". An empty/whitespace-only list means
    no restriction (backward compatible). Host matching is case-insensitive.
    """
    host = (host or "").strip().lower()
    if not host:
        return False
    allowed = [h.strip().lower() for h in (allowed_hosts or "").split(",") if h.strip()]
    if not allowed:
        return True
    return any(host == entry or host.endswith("." + entry) for entry in allowed)


def repo_component_ok(component: str) -> bool:
    """True iff component matches ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ and is not "." or "..".

    NOTE: do NOT use ^[\\w.-]+/... - that accepts "..", enabling path traversal.
    """
    return bool(_REPO_COMPONENT_RE.match(component)) and component not in (".", "..")


def validate_ref(ref: str) -> str:
    """Validate a git ref (branch, tag, or commit hash) before it is
    interpolated into any git argument (DESIGN.md §5.6, §6).

    Accepts plain refs: branch names (including slash-containing ones like
    "release/v1.0.0"), tags (including "v1.0.0-rc.1+build.5"), short/full
    commit hashes, and "HEAD". Returns the ref unchanged.

    Rejects (raising ToolError with a cause naming the offending ref): empty
    strings, whitespace, a leading dash (option injection), ":" (revision:path
    or protocol syntax), ".." (revision ranges), and anything outside
    [A-Za-z0-9_./+-] (shell/revision metacharacters such as ~ ^ * ? [ ] { }
    @ \\, and a leading "."). Revision expressions like "HEAD~1" or "main^"
    are deliberately NOT supported: only plain branch/tag/commit refs.
    """
    if not ref or any(c.isspace() for c in ref):
        raise ToolError(
            f"invalid ref: {ref!r}", cause="refs may not be empty or contain whitespace"
        )
    if ref.startswith("-"):
        raise ToolError(f"invalid ref: {ref!r}", cause="refs may not start with '-'")
    if ":" in ref:
        raise ToolError(f"invalid ref: {ref!r}", cause="refs may not contain ':'")
    if ".." in ref:
        raise ToolError(f"invalid ref: {ref!r}", cause="refs may not contain '..'")
    if not _REF_RE.match(ref):
        raise ToolError(
            f"invalid ref: {ref!r}",
            cause="refs may only contain letters, digits, '_', '.', '/', '+' and '-'",
        )
    return ref


def validate_clone_url(url: str) -> str:
    """Validate and normalize a clone `url` override before it reaches git.

    Protocol allow-list (https/http/git/ssh). Everything else is rejected,
    which closes the RCE gap (`ext::`/`sh::` command URLs) and the local
    exfiltration gap (`file://`), and drops stray protocols (ftp, rsync, ...).
    scp-like "user@host:path" is normalized to ssh://user@host/path exactly
    like git does (no port syntax in scp-like form; use ssh://host:port/path).
    Credentials in the URL are rejected (they would be persisted in
    <repo>/.git/config), except the ssh username (git@github.com is the
    normal ssh form). Returns the normalized URL.
    """
    url = (url or "").strip()
    if not url:
        raise ToolError("invalid clone url: empty")
    if url.startswith("-"):
        raise ToolError(f"invalid clone url: {url!r}", cause="urls may not start with '-'")
    if any(c.isspace() or ord(c) < 32 for c in url):
        raise ToolError(
            f"invalid clone url: {url!r}",
            cause="urls may not contain whitespace or control characters",
        )
    m = _SCPLIKE_RE.match(url)
    if m and "://" not in url:
        user, host, path = m.group(1), m.group(2), m.group(3)
        url = f"ssh://{user}@{host}/{path}"
    parts = urlparse(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_CLONE_SCHEMES:
        raise ToolError(
            f"invalid clone url: {url!r}",
            cause=f"protocol must be one of {sorted(ALLOWED_CLONE_SCHEMES)}; got {scheme or '(none)'}",
        )
    if parts.query or parts.fragment:
        raise ToolError(
            f"invalid clone url: {url!r}", cause="query strings and fragments are not allowed"
        )
    if scheme == "ssh":
        if parts.password is not None:
            raise ToolError(
                f"invalid clone url: {url!r}", cause="ssh passwords in URLs are not allowed"
            )
    elif parts.username is not None or parts.password is not None:
        raise ToolError(
            f"invalid clone url: {url!r}",
            cause="credentials in URLs are not allowed (they would be persisted in the repo config); use preconfigured credentials instead",
        )
    return url


def _normalize_remote(url: str) -> str:
    """Canonical form for COMPARING remotes (collision detection only).

    Compares the logical origin: lowercases the host (without any userinfo),
    keeps the path case-sensitive (git hosting paths are), and strips a
    trailing ".git" and trailing "/". Scheme and ssh user are intentionally
    NOT part of the comparison, so https://github.com/o/r,
    ssh://git@github.com/o/r and https://github.com/o/r.git all compare
    equal: the same logical repo on the same provider, just a different
    transport.
    """
    url = (url or "").strip()
    if "://" not in url:
        return url
    _, rest = url.split("://", 1)
    host, _, path = rest.partition("/")
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{host.lower()}/{path}"


def list_cloned_repos(repos_path: str, limit: int = 10) -> List[str]:
    """Names of currently cloned repos under `repos_path` ("owner/name",
    sorted), capped at `limit`. Used as a hint in "repo not cloned yet"
    errors so the agent sees what is already available instead of guessing.
    """
    base = Path(repos_path)
    if not base.is_dir():
        return []
    out: List[str] = []
    for owner_dir in sorted(base.iterdir()):
        if not owner_dir.is_dir() or owner_dir.name.startswith("."):
            continue
        for repo_dir in sorted(owner_dir.iterdir()):
            if repo_dir.is_dir() and (repo_dir / ".git").exists():
                out.append(f"{owner_dir.name}/{repo_dir.name}")
                if len(out) >= limit:
                    return out
    return out


def resolve_repo_root(repo: str, repos_path: str) -> Path:
    """Validate `<owner>/<name>` (exactly two components) and return its root.

    Splits on the FIRST "/" and rejects any input whose components fail
    repo_component_ok (which rejects "", ".", "..", slashes, spaces, leading
    dashes, and absolute paths). Existence is NOT checked here.
    """
    if not repo:
        raise ToolError("repo must be '<owner>/<name>', got empty string")
    if "/" not in repo:
        raise ToolError(f"repo must be '<owner>/<name>', got {repo!r}")
    owner, name = repo.split("/", 1)
    if not repo_component_ok(owner):
        raise ToolError(f"invalid owner component {owner!r} in repo {repo!r}")
    if not repo_component_ok(name):
        raise ToolError(f"invalid name component {name!r} in repo {repo!r}")
    return Path(repos_path) / owner / name


async def _remote_origin(root: str) -> str:
    """The repo's remote origin URL (git -C <root> remote get-url origin), or
    "" when the clone has no origin. Used by the clone-collision message and
    by cexp_list_repos' `origin` field."""
    res = await run_allowed(
        git_args("-C", root, "remote", "get-url", "origin"), TIMEOUT_SEARCH
    )
    if res.returncode != 0:
        return ""  # "No such remote 'origin'" -> rc != 0
    return res.stdout.strip()


def resolve_path(repo: str, path: Optional[str], repos_path: str) -> Path:
    """Resolve `path` relative to the repo root, strictly inside it.

    Raises ToolError if: `path` is absolute; any segment is ".."; the empty
    string (as a segment) appears; or the resolved path, after .resolve(),
    escapes the repo root (symlink escape). None -> repo root.
    """
    root = resolve_repo_root(repo, repos_path)
    if path is None or path == "":
        return root
    if path.startswith("/") or path.startswith("\\") or _WIN_ABS_RE.match(path):
        raise ToolError(f"absolute paths are not allowed: {path!r}")
    # Trailing-slash tolerance (fix): strip a trailing "/" (like
    # _normalize_remote does for clone URLs) so "backend/" resolves exactly
    # like "backend". Done AFTER the absolute check so "/" and "//" are
    # still rejected below, and BEFORE the segment check so the empty-segment
    # rule (which blocks "..", "a//b", "./") stays intact.
    path = path.rstrip("/")
    if not path:
        raise ToolError(f"invalid path segment in {path!r}")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ToolError(f"invalid path segment in {path!r}")
    candidate = root.joinpath(*parts)
    try:
        resolved = candidate.resolve()
    except OSError as e:
        raise ToolError(f"cannot resolve path {path!r}: {e}")
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ToolError(f"path escapes repository root: {path!r}")
    return candidate


# ---------------------------------------------------------------------------
# Output capping (DESIGN.md §4.4, §5.5, §9.3)
# ---------------------------------------------------------------------------


def truncate_output(text: str, max_lines: int, max_bytes: int, hint: Optional[str] = None) -> str:
    """Cap `text` by lines and bytes; append a truncation marker when cut.

    Whichever cap (lines or bytes) is hit first truncates. The marker always
    tells the agent the output is incomplete: line caps report `showing N of
    M lines`; byte-only caps report bytes (a bare `showing N of M` would be
    misleading when the line cap did not bind). When `hint` is given (a
    tool-specific "how to narrow" suggestion) it is appended after the marker
    on its own line; default None keeps the output unchanged.
    """
    text = text or ""
    total_lines = len(text.splitlines())
    total_bytes = len(text.encode("utf-8"))
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return text
    if total_lines > max_lines:
        cut = "\n".join(text.splitlines()[:max_lines])
        marker = "... (truncated: showing {} of {} lines)".format(max_lines, total_lines)
        if hint:
            marker += "\nhint: " + hint
        candidate = cut + "\n" + marker
        if len(candidate.encode("utf-8")) <= max_bytes:
            return candidate
        # The hint itself must survive the byte cap: cut the content first.
        budget = max_bytes - len(marker.encode("utf-8")) - 1
        return _trim_bytes(cut, max(0, budget)) + "\n" + marker
    # Only the byte cap binds.
    marker = "... (truncated: byte cap of {} reached; showing first {} of {} bytes)".format(
        max_bytes, max_bytes, total_bytes
    )
    if hint:
        marker += "\nhint: " + hint
    budget = max_bytes - len(marker.encode("utf-8")) - 1
    return _trim_bytes(text, max(0, budget)) + "\n" + marker


def _trim_bytes(text: str, budget: int) -> str:
    """Trim `text` to fit within `budget` UTF-8 bytes (character boundary)."""
    if budget <= 0:
        return ""
    while text and len(text.encode("utf-8")) > budget:
        text = text[:-1]
    return text


def json_output(data: dict, max_bytes: int, hint: Optional[str] = None) -> str:
    """Serialize `data` as a single valid JSON object, byte-capped.

    Structured tools return JSON (DESIGN.md §6, §9.3). The item cap
    (max_results) is applied by the tool before calling; this helper enforces
    the hard byte cap while keeping the JSON valid: it tries indented output
    first, then compact, then drops trailing `items` entries (updating
    `truncated` metadata) until it fits. The result is always valid JSON.
    When `hint` is given it is added inside the `truncated` object (whether
    capped by max_results or by bytes), a tool-specific "how to narrow"
    suggestion; default None keeps the output shape unchanged.
    """
    def _encode(indent: Optional[int]) -> str:
        return json.dumps(data, ensure_ascii=False, indent=indent)

    if hint and isinstance(data.get("truncated"), dict) and "hint" not in data["truncated"]:
        data["truncated"]["hint"] = hint

    text = _encode(2)
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    compact = _encode(None)
    if len(compact.encode("utf-8")) <= max_bytes:
        return compact
    items = data.get("items")
    if isinstance(items, list) and items:
        total = (data.get("truncated") or {}).get("total", len(items))
        data["truncated"] = {"shown": len(items), "total": total, "reason": "bytes"}
        if hint:
            data["truncated"]["hint"] = hint
        while items and len(_encode(None).encode("utf-8")) > max_bytes:
            items.pop()
            data["truncated"]["shown"] = len(items)
        return _encode(None)
    return compact


def trim_cause(text: str, limit: int = 300) -> str:
    """Strip ANSI/progress/noise from a stderr snippet for the `cause:` line."""
    text = _ANSI_RE.sub("", text or "")
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(("remote:", "Receiving objects:", "Resolving deltas:", "Updating files:")):
            continue
        lines.append(line)
    text = " | ".join(lines)
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


def audit_event(
    enabled: bool,
    level: int,
    action: str,
    repo: str,
    outcome: str,
    detail: str = "",
    error: str = "",
) -> None:
    """Emit an audit event to the "code_explorer" logger (S5).

    Opt-in via the audit_log Valve (`enabled`): when off this is a complete
    no-op (zero cost, zero logs). When on, logs one structured line with the
    fields ts/level/action/repo/outcome/detail/error. NEVER writes to stdout:
    the tool's stdout is what the model sees; the logger goes to the server
    logs. `error` must be pre-trimmed by the caller (trim_cause); raw stderr
    is never logged. ts is ISO-8601 UTC; level is the numeric logging level.
    """
    if not enabled:
        return
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "action": action,
        "repo": repo,
        "outcome": outcome,
        "detail": detail,
        "error": error,
    }
    logger = logging.getLogger("code_explorer")
    if level >= logging.ERROR:
        logger.error("audit %s", json.dumps(event, ensure_ascii=False))
    elif level >= logging.WARNING:
        logger.warning("audit %s", json.dumps(event, ensure_ascii=False))
    else:
        logger.info("audit %s", json.dumps(event, ensure_ascii=False))


class Tools:
    def __init__(self):
        self.valves = self.Valves()
        check_binaries("git")

    class Valves(BaseModel):
        repos_path: str = Field(
            "",
            description="Base directory for repository clones. Empty -> $OWUI_REPOS_PATH -> /usr/local/src. A dedicated volume must be mounted there and the process needs read/write permission; this Valve is a logical override only.",
        )
        allowed_hosts: str = Field(
            "",
            description="Comma-separated host allow-list for cexp_clone_repo. Empty (default): no restriction. When set, only origins whose host matches exactly or is a subdomain of a listed host may be cloned (e.g. github.com also allows api.github.com).",
        )
        min_free_bytes: int = Field(
            2147483648,
            description="Minimum free disk space (bytes) required on the repos volume before a clone starts (2 GiB default). 0 disables the check; a clone that could exhaust the disk is rejected with an Error before any network work.",
        )
        max_repo_bytes: int = Field(
            2147483648,
            description="Maximum .git size (bytes) allowed for a new clone, measured via a two-phase clone: the object store is fetched first and the working tree is checked out only if it is under this limit (2 GiB default, a \".git budget\" - the checkout roughly doubles the footprint). 0 disables the check.",
        )
        max_results: int = Field(
            50, description="Cap on item counts (repositories, refs)."
        )
        max_lines: int = Field(
            200, description="Cap on output lines. Whichever cap is hit first truncates."
        )
        max_bytes: int = Field(
            20480, description="Hard byte cap on tool output (20 KB default)."
        )
        audit_log: bool = Field(
            False,
            description="Enable audit logging of repo operations and security rejections to the code_explorer logger (opt-in; off by default).",
        )

    # ------------------------------------------------------------------
    # cexp_clone_repo
    # ------------------------------------------------------------------

    async def cexp_clone_repo(
        self,
        repo: str,
        url: Optional[str] = None,
        ref: Optional[str] = None,
    ) -> str:
        """Clone a repository into the storage area.

        Use when a repository is not yet present locally. The repository is
        cloned to <repos_path>/<owner>/<name>. If the target already exists,
        nothing is modified - the existing clone's origin is reported: use
        cexp_fetch_repo/cexp_pull_repo when it is the same logical repo, or
        cexp_list_repos to review existing clones (one clone per
        <owner>/<name> is supported). Protocols https, http, git, and ssh are
        allowed (scp-like git@host:path works); file:// and other protocols
        are rejected, and URLs must not contain credentials (ssh requires
        preconfigured credentials). After cloning, the requested ref is
        checked out. Returns a JSON object with repo, path, default_branch,
        ref, and status. Only plain branch/tag/commit refs are accepted;
        revision expressions (HEAD~1) are rejected.

        :param repo: "<owner>/<name>" of the repository to clone (required).
        :param url: Optional full clone URL; overrides the default https://github.com/<owner>/<name>.git; protocols https, http, git, ssh are allowed (scp-like git@host:path works); credentials in the URL are rejected; ssh requires preconfigured credentials.
        :param ref: Optional branch, tag, or the special value "release", which resolves to the most recent release tag.
        """
        try:
            return await self._clone_repo(repo, url, ref)
        except Exception as e:
            return error_string(e)

    async def _clone_repo(
        self, repo: str, url: Optional[str], ref: Optional[str]
    ) -> str:
        repos_path = resolve_repos_path(self.valves.repos_path)
        root = resolve_repo_root(repo, repos_path)
        if ref is not None:
            # Validated BEFORE cloning so a bad ref never triggers an
            # (unnecessary) clone; the special value "release" also passes.
            try:
                ref = validate_ref(ref)
            except ToolError as e:
                audit_event(
                    self.valves.audit_log,
                    logging.WARNING,
                    "clone",
                    repo,
                    "blocked",
                    detail=f"invalid ref: {ref!r}",
                    error=str(e),
                )
                raise
        if url is not None:
            # Protocol allow-list + scp-like normalization BEFORE the clone so
            # a malicious URL never reaches git.
            try:
                url = validate_clone_url(url)
            except ToolError as e:
                audit_event(
                    self.valves.audit_log,
                    logging.WARNING,
                    "clone",
                    repo,
                    "blocked",
                    detail=f"url rejected: {url!r}",
                    error=str(e),
                )
                raise
            remote = url
        else:
            remote = f"https://github.com/{repo}.git"

        # S3: optional host allow-list Valve (empty = unrestricted). Applied
        # after validate_clone_url, so only allow-listed protocols reach it.
        allowed_hosts = getattr(self.valves, "allowed_hosts", "")
        if allowed_hosts and allowed_hosts.strip():
            host = urlparse(remote).hostname or ""
            if not host_allowed(host, allowed_hosts):
                audit_event(
                    self.valves.audit_log,
                    logging.WARNING,
                    "clone",
                    repo,
                    "blocked",
                    detail=f"host not allowed: {host or '(none)'!r}",
                )
                raise ToolError(
                    f"host not allowed: {host or '(none)'!r} (allowed_hosts: {allowed_hosts.strip()!r})",
                    cause="the allowed_hosts Valve restricts which origins cexp_clone_repo may clone from",
                )

        if root.exists():
            existing = await _remote_origin(str(root))
            if existing and _normalize_remote(existing) == _normalize_remote(remote):
                audit_event(
                    self.valves.audit_log,
                    logging.WARNING,
                    "clone",
                    repo,
                    "blocked",
                    detail=f"already exists, same origin: {existing}",
                )
                raise ToolError(
                    f"repo already exists: {repo} at {root} (cloned from {existing}, same origin)",
                    cause="use cexp_fetch_repo or cexp_pull_repo to update it (no destructive overwrite)",
                )
            audit_event(
                self.valves.audit_log,
                logging.WARNING,
                "clone",
                repo,
                "blocked",
                detail=f"namespace collision: existing {existing or 'unknown origin'} vs requested {remote}",
            )
            raise ToolError(
                f"repo already exists: {repo} at {root} (cloned from {existing or 'unknown origin'})",
                cause=(
                    f"namespace collision: the existing clone comes from a different origin than the "
                    f"requested {remote}. One clone per <owner>/<name> is supported; use "
                    "cexp_fetch_repo/cexp_pull_repo if it is the same logical repo, or "
                    "cexp_list_repos to review existing clones"
                ),
            )

        try:
            root.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            audit_event(
                self.valves.audit_log,
                logging.ERROR,
                "clone",
                repo,
                "failed",
                error=f"cannot create storage directory: {e}",
            )
            raise ToolError(f"cannot create storage directory for {repo}", cause=str(e))
        # S6-A: pre-flight free-space check (ENOSPC prevention). A cheap
        # disk_usage syscall on the (now existing) storage area, before any
        # network work; min_free_bytes is operator policy, 0 = disabled.
        min_free_bytes = getattr(self.valves, "min_free_bytes", 0) or 0
        if min_free_bytes > 0:
            usage = shutil.disk_usage(repos_path)
            if usage.free < min_free_bytes:
                audit_event(
                    self.valves.audit_log,
                    logging.WARNING,
                    "clone",
                    repo,
                    "blocked",
                    detail=f"free space {usage.free} < min_free_bytes {min_free_bytes}",
                )
                raise ToolError(
                    "clone rejected: not enough free disk space for a new clone",
                    cause=(
                        f"free space on the repos volume is {usage.free} bytes, below the "
                        f"min_free_bytes limit of {min_free_bytes}; free disk with "
                        "cexp_remove_repo (preview with dry_run=True) or raise the Valve"
                    ),
                )
        try:
            # S6-B phase 1: fetch the object store WITHOUT a working tree so
            # the .git size can be measured (and a giant repo rejected) before
            # any checkout writes the worktree.
            res = await run_allowed(
                git_args("clone", "--no-progress", "--no-checkout", remote, str(root)),
                TIMEOUT_CLONE,
            )
        except ToolError as e:
            # Timeout (600 s): worse than fetch - left something half-done.
            self._cleanup_failed_clone(root)
            audit_event(
                self.valves.audit_log,
                logging.ERROR,
                "clone",
                repo,
                "timeout",
                detail=remote,
                error=str(e),
            )
            raise
        except asyncio.CancelledError:
            # S4+S5: a cancelled clone must not leave a partial directory
            # blocking <owner>/<name>; the cancellation is the most valuable
            # audit event (explains why a namespace is blocked). Reuse the
            # failed-clone cleanup, then re-raise.
            self._cleanup_failed_clone(root)
            audit_event(
                self.valves.audit_log,
                logging.ERROR,
                "clone",
                repo,
                "cancelled",
                detail=remote,
                error="clone cancelled while git was running",
            )
            raise
        if res.returncode != 0:
            self._cleanup_failed_clone(root)
            audit_event(
                self.valves.audit_log,
                logging.ERROR,
                "clone",
                repo,
                "failed",
                detail=remote,
                error=trim_cause(res.stderr),
            )
            raise ToolError(f"clone failed: {repo}", cause=trim_cause(res.stderr))

        # S6-B: size gate on the fetched object store. Because the clone ran
        # with --no-checkout, the worktree is still empty and _dir_size
        # measures .git alone. max_repo_bytes is a ".git budget" (the
        # checkout roughly doubles the on-disk footprint for text-heavy
        # repos); when the limit is exceeded the fetch is discarded and the
        # namespace stays free for a retry. 0 = disabled.
        max_repo_bytes = getattr(self.valves, "max_repo_bytes", 0) or 0
        if max_repo_bytes > 0:
            measured = await self._dir_size(root)
            if measured > max_repo_bytes:
                await self._discard_new_clone(root)
                audit_event(
                    self.valves.audit_log,
                    logging.WARNING,
                    "clone",
                    repo,
                    "blocked",
                    detail=f"measured .git size {measured} exceeds max_repo_bytes {max_repo_bytes}",
                )
                raise ToolError(
                    "clone rejected: repository too large",
                    cause=(
                        f"measured .git size {measured} bytes exceeds the max_repo_bytes "
                        f"limit of {max_repo_bytes}; raise the Valve for a genuinely large "
                        "repository or clone a smaller one"
                    ),
                )

        default_branch = await self._default_branch(str(root))
        # S6-B phase 2: populate the working tree (--no-checkout leaves it
        # empty). With ref the checkout is the requested branch/tag (release
        # resolves from the fetched tags); without ref the clone's default
        # branch is checked out, matching pre-S6 `git clone` behaviour.
        resolved_ref = None
        if ref:
            if ref == "release":
                tag = await self._resolve_release_tag(str(root))
                if tag is None:
                    # A .git-only clone is useless to the read tools (they
                    # read the working tree), so discard it and keep the
                    # namespace free for a retry without ref.
                    await self._discard_new_clone(root)
                    audit_event(
                        self.valves.audit_log,
                        logging.WARNING,
                        "clone",
                        repo,
                        "blocked",
                        detail="ref='release' requested but the repo has no tags",
                    )
                    raise ToolError(
                        f"ref='release' requested but {repo} has no tags; "
                        "clone without ref or specify a branch/tag explicitly"
                    )
                checkout_ref = tag
                resolved_ref = f"{tag} (release tag)"
            else:
                checkout_ref = ref
                resolved_ref = ref
        else:
            checkout_ref = default_branch
        try:
            res = await run_allowed(
                git_args("-C", str(root), "-c", "advice.detachedHead=false", "checkout", "--quiet", checkout_ref),
                TIMEOUT_SEARCH,
            )
        except ToolError as e:
            # Phase-2 timeout: discard the fetched .git so the namespace is
            # not left blocked by a useless object store (S4+S5 discipline).
            await self._discard_new_clone(root)
            audit_event(
                self.valves.audit_log,
                logging.ERROR,
                "clone",
                repo,
                "timeout",
                detail=f"checkout of {checkout_ref!r}",
                error=str(e),
            )
            raise
        except asyncio.CancelledError:
            # S4+S5: a cancelled post-clone checkout must not leave a
            # worktree-less .git blocking <owner>/<name>.
            await self._discard_new_clone(root)
            audit_event(
                self.valves.audit_log,
                logging.ERROR,
                "clone",
                repo,
                "cancelled",
                detail=f"checkout of {checkout_ref!r} cancelled",
            )
            raise
        if res.returncode != 0:
            # The ref does not exist in the fetched repo: no worktree was
            # ever written, so discard rather than leave a useless .git-only
            # clone blocking the namespace.
            await self._discard_new_clone(root)
            audit_event(
                self.valves.audit_log,
                logging.ERROR,
                "clone",
                repo,
                "failed",
                detail=f"checkout of {checkout_ref!r} failed",
                error=trim_cause(res.stderr),
            )
            raise ToolError(
                f"clone succeeded but checkout of {checkout_ref!r} failed",
                cause=trim_cause(res.stderr),
            )

        status = await self._short_status(str(root))
        audit_event(
            self.valves.audit_log,
            logging.INFO,
            "clone",
            repo,
            "success",
            detail=remote,
        )
        return json_output(
            {
                "repo": repo,
                "path": str(root),
                "default_branch": default_branch,
                "ref": resolved_ref or default_branch,
                "status": status,
            },
            self.valves.max_bytes,
        )

    async def _default_branch(self, root: str) -> str:
        res = await run_allowed(
            git_args("-C", root, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"),
            TIMEOUT_SEARCH,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip().replace("origin/", "", 1)
        # Fallback: whatever HEAD is on right now.
        res = await run_allowed(git_args("-C", root, "rev-parse", "--abbrev-ref", "HEAD"), TIMEOUT_SEARCH)
        return res.stdout.strip() or "unknown"

    async def _short_status(self, root: str) -> str:
        res = await run_allowed(git_args("-C", root, "status", "--porcelain"), TIMEOUT_SEARCH)
        count = len([l for l in res.stdout.splitlines() if l.strip()])
        return "clean" if count == 0 else f"{count} changed entr{'y' if count == 1 else 'ies'}"

    async def _resolve_release_tag(self, root: str) -> Optional[str]:
        """Most recent release tag: highest semver (v?X.Y.Z), else newest by date."""
        res = await run_allowed(git_args("-C", root, "tag", "-l"), TIMEOUT_SEARCH)
        tags = [t.strip() for t in res.stdout.splitlines() if t.strip()]
        if not tags:
            return None
        semver_tags = [t for t in tags if _RELEASE_TAG_RE.match(t)]
        if semver_tags:
            return max(semver_tags, key=_release_sort_key)
        res = await run_allowed(
            git_args("-C", root, "tag", "--sort=-creatordate"), TIMEOUT_SEARCH
        )
        newest = [t.strip() for t in res.stdout.splitlines() if t.strip()]
        return newest[0] if newest else None

    def _cleanup_failed_clone(self, root: Path) -> None:
        """Best-effort removal of a partial clone directory left by a failed
        `git clone`. The directory was created by this tool's own clone call,
        so removal is confined to the allow-listed repos_path and cannot touch
        an existing repository (that case is rejected before cloning)."""
        if root.exists() and not (root / ".git").exists():
            shutil.rmtree(root, ignore_errors=True)

    async def _discard_new_clone(self, root: Path) -> None:
        """Remove a clone directory created by THIS clone call that must not
        be kept: an oversized .git (S6-B size gate) or a failed phase-2
        checkout leaves only a useless worktree-less object store. The
        namespace was verified free before cloning (collision checks), so
        removal is confined to the allow-listed repos_path and cannot touch
        a pre-existing repository. Unlike _cleanup_failed_clone, the .git
        directory exists here and must be removed too."""
        if root.exists():
            await asyncio.to_thread(shutil.rmtree, root, ignore_errors=True)

    # ------------------------------------------------------------------
    # cexp_fetch_repo
    # ------------------------------------------------------------------

    async def cexp_fetch_repo(self, repo: str) -> str:
        """Fetch new branches and tags from all remotes.

        Use to bring newly published branches/tags into an existing clone
        without touching the working tree (safe on a detached HEAD). Does not
        merge or move any local branch. Reports the most recent release tag
        (same resolution as cexp_clone_repo ref="release") so you know which
        tag to point cexp_read_file/cexp_compare_commits at without running
        cexp_list_tags. Returns a JSON object with repo, up_to_date, items
        (refs with their change), and release.

        :param repo: "<owner>/<name>" of an already-cloned repository.
        """
        try:
            return await self._fetch_repo(repo)
        except Exception as e:
            return error_string(e)

    async def _fetch_repo(self, repo: str) -> str:
        root = resolve_repo_root(repo, resolve_repos_path(self.valves.repos_path))
        self._ensure_repo_exists(root, repo)
        await self._validate_origin(str(root), repo, "fetch")
        before = await self._list_refs(str(root))
        try:
            res = await run_allowed(
                git_args("-C", str(root), "fetch", "--all", "--tags", "--prune", "--no-progress"),
                TIMEOUT_FETCH,
            )
        except ToolError as e:
            # Timeout (120 s): non-destructive, retryable -> WARNING.
            audit_event(
                self.valves.audit_log,
                logging.WARNING,
                "fetch",
                repo,
                "timeout",
                error=str(e),
            )
            raise
        if res.returncode != 0:
            audit_event(
                self.valves.audit_log,
                logging.ERROR,
                "fetch",
                repo,
                "failed",
                error=trim_cause(res.stderr),
            )
            raise ToolError(f"fetch failed: {repo}", cause=trim_cause(res.stderr))
        after = await self._list_refs(str(root))
        audit_event(
            self.valves.audit_log,
            logging.INFO,
            "fetch",
            repo,
            "success",
        )

        added = sorted(set(after) - set(before))
        updated = sorted(r for r in before if r in after and before[r] != after[r])
        release = await self._resolve_release_tag(str(root))
        if not added and not updated:
            return json_output(
                {"repo": repo, "up_to_date": True, "items": [], "release": release},
                self.valves.max_bytes,
            )
        items = [{"ref": r, "change": "new"} for r in added]
        items += [
            {"ref": r, "change": "updated", "from": before[r][:7], "to": after[r][:7]}
            for r in updated
        ]
        data: dict = {
            "repo": repo,
            "up_to_date": False,
            "items": items,
            "release": release,
        }
        if len(items) > self.valves.max_results:
            data["truncated"] = {"shown": self.valves.max_results, "total": len(items)}
            data["items"] = items[: self.valves.max_results]
        return json_output(
            data, self.valves.max_bytes, hint="refs with changes are capped; the clone is up to date otherwise"
        )

    async def _list_refs(self, root: str) -> Dict[str, str]:
        res = await run_allowed(
            git_args("-C", root, "for-each-ref", "--format=%(refname:short) %(objectname)"),
            TIMEOUT_SEARCH,
        )
        refs: Dict[str, str] = {}
        for line in res.stdout.splitlines():
            if not line.strip():
                continue
            name, oid = line.rsplit(" ", 1)
            if name.endswith("/HEAD"):
                continue  # symbolic ref (origin/HEAD): moves with its target
            refs[name] = oid
        return refs

    # ------------------------------------------------------------------
    # cexp_pull_repo
    # ------------------------------------------------------------------

    async def cexp_pull_repo(self, repo: str) -> str:
        """Fast-forward the current branch of an existing clone.

        Use to keep a moving branch (e.g. dev) up to date. Only fast-forward
        updates are allowed: never creates a merge commit, never leaves the
        repo conflicted. Fails cleanly on a detached HEAD (use cexp_fetch_repo
        there) and when the local branch has diverged. Returns a JSON object
        with repo and result (up_to_date or fast_forwarded).

        :param repo: "<owner>/<name>" of an already-cloned repository.
        """
        try:
            return await self._pull_repo(repo)
        except Exception as e:
            return error_string(e)

    async def _pull_repo(self, repo: str) -> str:
        root = resolve_repo_root(repo, resolve_repos_path(self.valves.repos_path))
        self._ensure_repo_exists(root, repo)
        await self._validate_origin(str(root), repo, "pull")
        res = await run_allowed(git_args("-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"), TIMEOUT_SEARCH)
        branch = res.stdout.strip()
        if branch == "HEAD":
            audit_event(
                self.valves.audit_log,
                logging.WARNING,
                "pull",
                repo,
                "blocked",
                detail="detached HEAD; pull only moves branches",
            )
            raise ToolError(
                f"{repo} is on a detached HEAD; cexp_pull_repo only moves branches. "
                "Use cexp_fetch_repo to update refs without touching the working tree."
            )
        try:
            res = await run_allowed(
                git_args("-C", str(root), "pull", "--ff-only", "--no-progress"),
                TIMEOUT_PULL,
            )
        except ToolError as e:
            # Timeout (120 s): non-destructive, retryable -> WARNING.
            audit_event(
                self.valves.audit_log,
                logging.WARNING,
                "pull",
                repo,
                "timeout",
                error=str(e),
            )
            raise
        if res.returncode != 0:
            # Not fast-forwardable (diverged) or network error -> ERROR.
            audit_event(
                self.valves.audit_log,
                logging.ERROR,
                "pull",
                repo,
                "failed",
                error=trim_cause(res.stderr or res.stdout),
            )
            raise ToolError(
                f"pull failed (fast-forward only): {repo}",
                cause=trim_cause(res.stderr or res.stdout),
            )
        out = (res.stdout + "\n" + res.stderr).strip()
        if "Already up to date" in out:
            audit_event(self.valves.audit_log, logging.INFO, "pull", repo, "success", detail="up to date")
            return json_output({"repo": repo, "result": "up_to_date"}, self.valves.max_bytes)
        m = re.search(r"Updating\s+([0-9a-f]+)\.\.([0-9a-f]+)", out)
        if m:
            audit_event(
                self.valves.audit_log,
                logging.INFO,
                "pull",
                repo,
                "success",
                detail=f"fast-forwarded {m.group(1)[:7]}..{m.group(2)[:7]}",
            )
            return json_output(
                {
                    "repo": repo,
                    "result": "fast_forwarded",
                    "from": m.group(1)[:7],
                    "to": m.group(2)[:7],
                },
                self.valves.max_bytes,
            )
        audit_event(self.valves.audit_log, logging.INFO, "pull", repo, "success")
        return json_output(
            {"repo": repo, "result": "ok", "output": truncate_output(out, self.valves.max_lines, self.valves.max_bytes, hint="pull output was large; the repo is updated regardless")},
            self.valves.max_bytes,
        )

    # ------------------------------------------------------------------
    # cexp_list_repos
    # ------------------------------------------------------------------

    async def cexp_list_repos(self) -> str:
        """List all cloned repositories under the storage area.

        Use to see what is already cloned (owner/name, current branch, the
        origin URL each clone was made from, and its on-disk size in bytes)
        before deciding whether to clone, fetch, pull, or remove. Returns a
        JSON object with an items array of {"repo", "branch", "origin",
        "size"} entries. No parameters.

        :return: a JSON object with an `items` array of {"repo", "branch", "origin", "size"} entries, sorted.
        """
        try:
            return await self._list_repos()
        except Exception as e:
            return error_string(e)

    async def _list_repos(self) -> str:
        repos_path = Path(resolve_repos_path(self.valves.repos_path))
        if not repos_path.is_dir():
            raise ToolError(
                f"no repositories under {repos_path} (nothing cloned yet)", kind="not_found"
            )
        entries: List[Dict[str, str]] = []
        for owner_dir in sorted(repos_path.iterdir()):
            if not owner_dir.is_dir() or owner_dir.name.startswith("."):
                continue
            for repo_dir in sorted(owner_dir.iterdir()):
                if not repo_dir.is_dir() or not (repo_dir / ".git").exists():
                    continue
                entries.append(
                    {
                        "repo": f"{owner_dir.name}/{repo_dir.name}",
                        "branch": await self._current_branch(str(repo_dir)),
                        "origin": await _remote_origin(str(repo_dir)),
                        "size": await self._dir_size(repo_dir),
                    }
                )
        if not entries:
            raise ToolError(f"no repositories under {repos_path} (nothing cloned yet)", kind="not_found")
        data: dict = {"items": entries}
        if len(entries) > self.valves.max_results:
            data["truncated"] = {"shown": self.valves.max_results, "total": len(entries)}
            data["items"] = entries[: self.valves.max_results]
        return json_output(
            data, self.valves.max_bytes, hint="use cexp_remove_repo to free disk space, or raise the max_results Valve"
        )

    async def _current_branch(self, root: str) -> str:
        res = await run_allowed(git_args("-C", root, "symbolic-ref", "--short", "-q", "HEAD"), TIMEOUT_SEARCH)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
        res = await run_allowed(git_args("-C", root, "rev-parse", "--short", "HEAD"), TIMEOUT_SEARCH)
        return res.stdout.strip() or "?"

    # ------------------------------------------------------------------
    # cexp_remove_repo
    # ------------------------------------------------------------------

    async def cexp_remove_repo(self, repo: str, dry_run: bool = False) -> str:
        """Delete a cloned repository (or preview the deletion).

        Use to free disk space or remove a clone you no longer need. Resolves
        <repos_path>/<owner>/<name>; the target must exist and must resolve
        strictly inside <repos_path> (symlinked roots are refused). With
        dry_run=True, returns the path and its on-disk size in bytes without
        deleting. With dry_run=False, deletes the directory tree and returns a
        confirmation. Returns a JSON object with {"repo", "path", "dry_run",
        "size", "removed"}.

        :param repo: "<owner>/<name>" of the repository to remove (required).
        :param dry_run: Optional; if True, only report the path and size, do not delete.
        """
        try:
            return await self._remove_repo(repo, dry_run)
        except Exception as e:
            return error_string(e)

    async def _remove_repo(self, repo: str, dry_run: bool) -> str:
        repos_path = resolve_repos_path(self.valves.repos_path)
        root = resolve_repo_root(repo, repos_path)
        if not root.is_dir():
            raise ToolError(f"repository not found: {repo}", kind="not_found")
        # The target must resolve strictly inside the resolved repos_path and
        # must not be a symlink (same guard style as resolve_path).
        base = Path(repos_path).resolve()
        resolved = root.resolve()
        if resolved != base and base not in resolved.parents:
            raise ToolError(f"refusing to remove path outside repos_path: {root}")
        if root.is_symlink():
            raise ToolError(f"refusing to remove symlinked root: {root}")

        size = await self._dir_size(root)
        if dry_run:
            return json_output(
                {"repo": repo, "path": str(root), "dry_run": True, "size": size, "removed": False},
                self.valves.max_bytes,
            )
        await asyncio.to_thread(shutil.rmtree, root, ignore_errors=False)
        return json_output(
            {"repo": repo, "path": str(root), "dry_run": False, "size": size, "removed": True},
            self.valves.max_bytes,
        )

    async def _dir_size(self, root: Path) -> int:
        """Total on-disk size in bytes of a clone (Python walk, offloaded)."""
        def _walk(p: Path) -> int:
            total = 0
            for dp, _, files in os.walk(p):
                for f in files:
                    try:
                        total += (Path(dp) / f).stat().st_size
                    except OSError:
                        pass
            return total

        return await asyncio.to_thread(_walk, root)

    # ------------------------------------------------------------------
    # shared internals
    # ------------------------------------------------------------------

    async def _validate_origin(self, root: str, repo: str, action: str) -> str:
        """Re-validate the remote origin through the clone-URL allow-list
        before any network git command runs (fetch/pull). A tampered
        .git/config (e.g. remote.origin.url = ext::sh -c '...') must never
        reach git: the strict allow-list currently only guards clone."""
        origin = await _remote_origin(root)
        try:
            return validate_clone_url(origin)
        except ToolError as e:
            audit_event(
                self.valves.audit_log,
                logging.WARNING,
                action,
                repo,
                "blocked",
                detail=f"origin not allow-listed: {origin or '(none)'!r}",
                error=str(e),
            )
            raise ToolError(
                f"cannot {action}: remote origin is not allow-listed",
                cause=f"origin {origin or '(none)'!r}: {e.message}",
            )

    def _ensure_repo_exists(self, root: Path, repo: str) -> None:
        if not root.is_dir() or not (root / ".git").exists():
            existing = list_cloned_repos(
                resolve_repos_path(self.valves.repos_path), limit=10
            )
            cause = None
            if existing:
                cause = f"currently cloned: {', '.join(existing)}"
            raise ToolError(
                f"repository not cloned yet: {repo} (use cexp_clone_repo first)",
                kind="not_found",
                cause=cause,
            )
