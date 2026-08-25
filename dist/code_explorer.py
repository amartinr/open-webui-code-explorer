"""
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

_REPO_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_REF_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/+-]*$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_RELEASE_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)([-+].*)?$")


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
    env.pop("GIT_ASKPASS", None)  # no askpass helper may launch a prompt
    return env


async def run_allowed(argv: List[str], timeout: int, *, text: bool = True) -> CommandResult:
    """Run an allow-listed binary with arguments, capturing both pipes.

    - argv[0] MUST be one of ALLOWED_BINARIES (no arbitrary commands).
    - No shell: argument arrays only (shell=False).
    - Runs in a worker thread so the blocking call never stalls Open WebUI's
      event loop.
    - Uses the fixed headless environment (§9.7) so git can never prompt,
      page, localize, or read user/global config.
    - On timeout raises ToolError(kind="timed_out").
    - `text=False` captures raw bytes (stdout/stderr are `bytes`) so callers
      that need byte-exact output (e.g. blob reads for binary/UTF-8
      detection) can decode explicitly; `text=True` (default) decodes with
      the process locale and is only for human-facing git output.
    """
    if not argv:
        raise ToolError("empty command")
    if argv[0] not in ALLOWED_BINARIES:
        raise ToolError(f"disallowed command: {argv[0]!r} (allow-list: {sorted(ALLOWED_BINARIES)})")
    exe = shutil.which(argv[0])
    if exe is None:
        raise ToolError(f"required binary not found in PATH: {argv[0]}")
    full = [exe, *argv[1:]]
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            full,
            shell=False,
            capture_output=True,
            text=text,
            timeout=timeout,
            env=_headless_env(),
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"timed out after {timeout}s", kind="timed_out")
    stdout = proc.stdout or (b"" if not text else "")
    stderr = proc.stderr or (b"" if not text else "")
    return CommandResult(stdout=stdout, stderr=stderr, returncode=proc.returncode)


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


def truncate_output(text: str, max_lines: int, max_bytes: int) -> str:
    """Cap `text` by lines and bytes; append a truncation marker when cut.

    Whichever cap (lines or bytes) is hit first truncates. The marker always
    tells the agent the output is incomplete: line caps report `showing N of
    M lines`; byte-only caps report bytes (a bare `showing N of M` would be
    misleading when the line cap did not bind).
    """
    text = text or ""
    total_lines = len(text.splitlines())
    total_bytes = len(text.encode("utf-8"))
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return text
    if total_lines > max_lines:
        cut = "\n".join(text.splitlines()[:max_lines])
        marker = "... (truncated: showing {} of {} lines)".format(max_lines, total_lines)
        candidate = cut + "\n" + marker
        if len(candidate.encode("utf-8")) <= max_bytes:
            return candidate
        budget = max_bytes - len(marker.encode("utf-8")) - 1
        return _trim_bytes(cut, max(0, budget)) + "\n" + marker
    # Only the byte cap binds.
    marker = "... (truncated: byte cap of {} reached; showing first {} of {} bytes)".format(
        max_bytes, max_bytes, total_bytes
    )
    budget = max_bytes - len(marker.encode("utf-8")) - 1
    return _trim_bytes(text, max(0, budget)) + "\n" + marker


def _trim_bytes(text: str, budget: int) -> str:
    """Trim `text` to fit within `budget` UTF-8 bytes (character boundary)."""
    if budget <= 0:
        return ""
    while text and len(text.encode("utf-8")) > budget:
        text = text[:-1]
    return text


def json_output(data: dict, max_bytes: int) -> str:
    """Serialize `data` as a single valid JSON object, byte-capped.

    Structured tools return JSON (DESIGN.md §6, §9.3). The item cap
    (max_results) is applied by the tool before calling; this helper enforces
    the hard byte cap while keeping the JSON valid: it tries indented output
    first, then compact, then drops trailing `items` entries (updating
    `truncated` metadata) until it fits. The result is always valid JSON.
    """
    def _encode(indent: Optional[int]) -> str:
        return json.dumps(data, ensure_ascii=False, indent=indent)

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


class Tools:
    def __init__(self):
        self.valves = self.Valves()
        check_binaries("git")

    class Valves(BaseModel):
        repos_path: str = Field(
            "",
            description="Base directory for repository clones. Empty -> $OWUI_REPOS_PATH -> /usr/local/src. A dedicated volume must be mounted there and the process needs read/write permission; this Valve is a logical override only.",
        )
        max_results: int = Field(
            50, description="Cap on item counts (files, matches, commits, branches, tags)."
        )
        max_lines: int = Field(
            200, description="Cap on output lines. Whichever cap is hit first truncates."
        )
        max_bytes: int = Field(
            20480, description="Hard byte cap on tool output (20 KB default)."
        )

    async def cexp_clone_repo(
        self,
        repo: str,
        url: Optional[str] = None,
        ref: Optional[str] = None,
    ) -> str:
        """Clone a repository into the storage area.

        Use when a repository is not yet present locally. The repository is
        cloned to <repos_path>/<owner>/<name>. If the target already exists,
        nothing is modified - use cexp_fetch_repo, cexp_pull_repo, or cexp_list_repos instead.
        After cloning, the requested ref is checked out. Returns a JSON object
        with repo, path, default_branch, ref, and status. Only plain
        branch/tag/commit refs are accepted; revision expressions (HEAD~1) are
        rejected.

        :param repo: "<owner>/<name>" of the repository to clone (required).
        :param url: Optional full clone URL; overrides the default https://github.com/<owner>/<name>.git.
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
        if root.exists():
            raise ToolError(
                f"{repo} already exists at {root}; use cexp_fetch_repo, cexp_pull_repo, "
                "or cexp_list_repos instead (no destructive overwrite)"
            )
        if ref is not None:
            # Validated BEFORE cloning so a bad ref never triggers an
            # (unnecessary) clone; the special value "release" also passes.
            ref = validate_ref(ref)
        if url is not None:
            url = url.strip()
            if not url or url.startswith("-"):
                raise ToolError(f"invalid clone url: {url!r}")
            remote = url
        else:
            remote = f"https://github.com/{repo}.git"

        root.parent.mkdir(parents=True, exist_ok=True)
        res = await run_allowed(git_args("clone", "--no-progress", remote, str(root)), TIMEOUT_CLONE)
        if res.returncode != 0:
            self._cleanup_failed_clone(root)
            raise ToolError(f"clone failed: {repo}", cause=trim_cause(res.stderr))

        resolved_ref = None
        if ref:
            if ref == "release":
                tag = await self._resolve_release_tag(str(root))
                if tag is None:
                    raise ToolError(
                        f"ref='release' requested but {repo} has no tags; "
                        "clone without ref or specify a branch/tag explicitly"
                    )
                ref_arg = tag
                resolved_ref = f"{tag} (release tag)"
            else:
                ref_arg = ref
                resolved_ref = ref
            res = await run_allowed(
                git_args("-C", str(root), "-c", "advice.detachedHead=false", "checkout", "--quiet", ref_arg),
                TIMEOUT_SEARCH,
            )
            if res.returncode != 0:
                raise ToolError(
                    f"clone succeeded but checkout of ref {ref_arg!r} failed",
                    cause=trim_cause(res.stderr),
                )

        default_branch = await self._default_branch(str(root))
        status = await self._short_status(str(root))
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

    async def cexp_fetch_repo(self, repo: str) -> str:
        """Fetch new branches and tags from all remotes.

        Use to bring newly published branches/tags into an existing clone
        without touching the working tree (safe on a detached HEAD). Does not
        merge or move any local branch. Returns a JSON object with repo,
        up_to_date, and items (refs with their change).

        :param repo: "<owner>/<name>" of an already-cloned repository.
        """
        try:
            return await self._fetch_repo(repo)
        except Exception as e:
            return error_string(e)

    async def _fetch_repo(self, repo: str) -> str:
        root = resolve_repo_root(repo, resolve_repos_path(self.valves.repos_path))
        self._ensure_repo_exists(root, repo)
        before = await self._list_refs(str(root))
        res = await run_allowed(
            git_args("-C", str(root), "fetch", "--all", "--tags", "--prune", "--no-progress"),
            TIMEOUT_FETCH,
        )
        if res.returncode != 0:
            raise ToolError(f"fetch failed: {repo}", cause=trim_cause(res.stderr))
        after = await self._list_refs(str(root))

        added = sorted(set(after) - set(before))
        updated = sorted(r for r in before if r in after and before[r] != after[r])
        if not added and not updated:
            return json_output(
                {"repo": repo, "up_to_date": True, "items": []}, self.valves.max_bytes
            )
        items = [{"ref": r, "change": "new"} for r in added]
        items += [
            {"ref": r, "change": "updated", "from": before[r][:7], "to": after[r][:7]}
            for r in updated
        ]
        data: dict = {"repo": repo, "up_to_date": False, "items": items}
        if len(items) > self.valves.max_results:
            data["truncated"] = {"shown": self.valves.max_results, "total": len(items)}
            data["items"] = items[: self.valves.max_results]
        return json_output(data, self.valves.max_bytes)

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
        res = await run_allowed(git_args("-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"), TIMEOUT_SEARCH)
        branch = res.stdout.strip()
        if branch == "HEAD":
            raise ToolError(
                f"{repo} is on a detached HEAD; cexp_pull_repo only moves branches. "
                "Use cexp_fetch_repo to update refs without touching the working tree."
            )
        res = await run_allowed(
            git_args("-C", str(root), "pull", "--ff-only", "--no-progress"),
            TIMEOUT_PULL,
        )
        if res.returncode != 0:
            raise ToolError(
                f"pull failed (fast-forward only): {repo}",
                cause=trim_cause(res.stderr or res.stdout),
            )
        out = (res.stdout + "\n" + res.stderr).strip()
        if "Already up to date" in out:
            return json_output({"repo": repo, "result": "up_to_date"}, self.valves.max_bytes)
        m = re.search(r"Updating\s+([0-9a-f]+)\.\.([0-9a-f]+)", out)
        if m:
            return json_output(
                {
                    "repo": repo,
                    "result": "fast_forwarded",
                    "from": m.group(1)[:7],
                    "to": m.group(2)[:7],
                },
                self.valves.max_bytes,
            )
        return json_output(
            {"repo": repo, "result": "ok", "output": truncate_output(out, self.valves.max_lines, self.valves.max_bytes)},
            self.valves.max_bytes,
        )

    async def cexp_list_repos(self) -> str:
        """List all cloned repositories under the storage area.

        Use to see what is already cloned (owner/name and current branch)
        before deciding whether to clone, fetch, or pull. Returns a JSON
        object with an items array of {"repo", "branch"} entries. No parameters.

        :return: a JSON object with an `items` array of {"repo", "branch"} entries, sorted.
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
                    }
                )
        if not entries:
            raise ToolError(f"no repositories under {repos_path} (nothing cloned yet)", kind="not_found")
        data: dict = {"items": entries}
        if len(entries) > self.valves.max_results:
            data["truncated"] = {"shown": self.valves.max_results, "total": len(entries)}
            data["items"] = entries[: self.valves.max_results]
        return json_output(data, self.valves.max_bytes)

    async def _current_branch(self, root: str) -> str:
        res = await run_allowed(git_args("-C", root, "symbolic-ref", "--short", "-q", "HEAD"), TIMEOUT_SEARCH)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
        res = await run_allowed(git_args("-C", root, "rev-parse", "--short", "HEAD"), TIMEOUT_SEARCH)
        return res.stdout.strip() or "?"

    def _ensure_repo_exists(self, root: Path, repo: str) -> None:
        if not root.is_dir() or not (root / ".git").exists():
            raise ToolError(
                f"repository not cloned yet: {repo} (use cexp_clone_repo first)",
                kind="not_found",
            )

    async def cexp_list_files(
        self,
        repo: str,
        path: Optional[str] = None,
        max_depth: Optional[int] = None,
        filter: Optional[str] = None,
        type: Optional[str] = "all",
        ref: Optional[str] = None,
    ) -> str:
        """List files and directories under a path in a repository.

        Use to explore repository structure before reading files. Returns a
        JSON object with an items array of {"path", "kind"} entries, relative
        to the repository root and sorted. Respects .gitignore (via the
        pathspec package when available); hidden files are not shown by
        default. With ref, lists the files present at that branch/tag/commit
        from the local git object store (no working-tree changes, no network);
        directories are implied from file paths since git tracks no empty
        directories.

        :param repo: "<owner>/<name>" of an already-cloned repository.
        :param path: Optional subdirectory or file, relative to the repository root; do NOT include the "<owner>/<name>" prefix (that belongs in `repo`); always use "/" as separator (e.g. "src/main.py"); defaults to the repository root.
        :param ref: Optional git ref (branch, tag, or commit hash) to list files at; None (default) lists the working tree. Only plain refs are accepted; revision expressions like HEAD~1 are rejected.
        :param max_depth: Optional maximum directory depth (0 = only the given path).
        :param filter: Optional space-separated glob patterns; a leading "!" excludes (e.g. "*.py !*.md").
        :param type: "file", "dir", or "all" (default "all").
        """
        try:
            return await self._list_files(repo, path, max_depth, filter, type, ref)
        except Exception as e:
            return error_string(e)

    async def _list_files(
        self,
        repo: str,
        path: Optional[str],
        max_depth: Optional[int],
        filter: Optional[str],
        type: Optional[str],
        ref: Optional[str],
    ) -> str:
        repos_path = resolve_repos_path(self.valves.repos_path)
        root = resolve_repo_root(repo, repos_path)
        self._ensure_repo_exists(root, repo)
        if ref is not None:
            # Ref listing reads the tree, not the working tree: the path is
            # validated for traversal safety but need not exist on disk.
            return await self._list_files_at_ref(root, repo, path, max_depth, filter, type, ref)
        base = resolve_path(repo, path, repos_path)
        if not base.exists():
            raise ToolError(f"path not found: {path or '.'} in {repo}", kind="not_found")
        if max_depth is not None and max_depth < 0:
            raise ToolError(f"max_depth must be >= 0, got {max_depth}")
        if type not in ("file", "dir", "all"):
            raise ToolError(f"type must be 'file', 'dir', or 'all', got {type!r}")
        includes, excludes = parse_filter(filter)

        if base.is_file():
            rel = str(base.relative_to(root))
            ok = type != "dir" and glob_match(rel, includes, excludes)
            items = [{"path": rel, "kind": "file"}] if ok else []
            return json_output({"items": items}, self.valves.max_bytes)

        items = []
        if base.is_dir():
            for rel, is_dir in _walk_repo(root, base, max_depth):
                if is_dir and type == "file":
                    continue
                if not is_dir and type == "dir":
                    continue
                if not glob_match(rel, includes, excludes):
                    continue
                if not is_dir and rel.startswith("."):
                    continue
                items.append({"path": rel, "kind": "dir" if is_dir else "file"})
        items.sort(key=lambda i: i["path"])
        data: dict = {"items": items}
        if len(items) > self.valves.max_results:
            data["truncated"] = {"shown": self.valves.max_results, "total": len(items)}
            data["items"] = items[: self.valves.max_results]
        return json_output(data, self.valves.max_bytes)

    async def _list_files_at_ref(
        self,
        root: Path,
        repo: str,
        path: Optional[str],
        max_depth: Optional[int],
        filter: Optional[str],
        type: Optional[str],
        ref: str,
    ) -> str:
        """List the files present at a git ref (branch/tag/commit) via
        `git ls-tree -r --name-only`, without touching the working tree
        (DESIGN.md §12.1). Directories are derived from file paths (git tracks
        no empty directories), so type="dir" reflects implied directories.
        filter/type/max_depth/max_results apply exactly like the working-tree
        listing; an unknown ref is an Error naming the ref, a path absent at
        the ref is a Not found."""
        ref = validate_ref(ref)
        repos_path = resolve_repos_path(self.valves.repos_path)
        # Traversal-safety validation only; the path need not exist at the ref.
        base = resolve_path(repo, path, repos_path)
        if max_depth is not None and max_depth < 0:
            raise ToolError(f"max_depth must be >= 0, got {max_depth}")
        if type not in ("file", "dir", "all"):
            raise ToolError(f"type must be 'file', 'dir', or 'all', got {type!r}")
        includes, excludes = parse_filter(filter)

        res = await run_allowed(
            git_args("-C", str(root), "ls-tree", "-r", "--name-only", ref),
            TIMEOUT_SEARCH,
        )
        if res.returncode != 0:
            raise ToolError(
                f"unknown ref: {ref!r}",
                cause=trim_cause(res.stderr) or "ref does not exist in this repository",
            )
        files = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]

        # Narrow to the requested path (prefix semantics: a file matches
        # exactly, a dir matches everything under it). A path with no entries
        # at the ref is Not found.
        base_rel = ""
        if path:
            try:
                base_rel = base.relative_to(root).as_posix()
            except ValueError:
                raise ToolError(f"invalid path: {path!r}")
            if base_rel == ".":
                base_rel = ""
            if base_rel:
                files = [f for f in files if f == base_rel or f.startswith(base_rel + "/")]
                if not files:
                    raise ToolError(f"path not found at {ref}: {path}", kind="not_found")

        base_parts = len(base_rel.split("/")) if base_rel else 0

        def _depth(rel: str) -> int:
            """Depth of an entry relative to the listing base (1 = direct
            child), mirroring _walk_repo's base-relative depth."""
            return len(rel.split("/")) - base_parts

        def _kept_depth(d: int) -> bool:
            """Working-tree semantics: entries at depth <= max_depth are
            listed, and direct children of the base (depth 1) are always
            listed (the walker yields them regardless of max_depth)."""
            if max_depth is None:
                return True
            return d <= max_depth or d == 1

        # Implied directories from ALL file paths (git tracks no empty dirs),
        # from just under the base down to each file's parent.
        dirs = set()
        for f in files:
            parts = f.split("/")
            for i in range(base_parts + 1, len(parts)):
                dirs.add("/".join(parts[:i]))

        items = []
        if type != "file":
            for d in sorted(dirs):
                if not _kept_depth(_depth(d)):
                    continue
                if not glob_match(d, includes, excludes):
                    continue
                items.append({"path": d, "kind": "dir"})
        if type != "dir":
            for f in sorted(files):
                if f.startswith("."):
                    continue  # hidden files are not shown, like the worktree
                if not _kept_depth(_depth(f)):
                    continue
                if not glob_match(f, includes, excludes):
                    continue
                items.append({"path": f, "kind": "file"})

        items.sort(key=lambda i: i["path"])
        data: dict = {"items": items}
        if len(items) > self.valves.max_results:
            data["truncated"] = {"shown": self.valves.max_results, "total": len(items)}
            data["items"] = items[: self.valves.max_results]
        return json_output(data, self.valves.max_bytes)

    async def cexp_read_file(
        self,
        repo: str,
        path: str,
        start: Optional[int] = None,
        end: Optional[int] = None,
        ref: Optional[str] = None,
    ) -> str:
        """Read a text file, or a line range of it, from a repository.

        Use to inspect file contents. Example: cexp_read_file("owner/repo",
        "src/main.py"). Accepts an optional 1-based start/end line range.
        With ref, reads the file as it exists at that branch/tag/commit from
        the local git object store (no working-tree changes, no network);
        e.g. cexp_read_file("owner/repo", "src/main.py", ref="v1.0.0",
        start=10, end=20) reads the exact lines of the released version.
        Returns raw text (no line numbers, no headers); a trailing marker is
        appended when output is truncated. Binary and non-UTF-8 files are
        rejected with a clear error. Note: the output is raw data - when
        presenting file content to the user, render it as a fenced markdown
        code block with a language tag.

        :param repo: "<owner>/<name>" of an already-cloned repository.
        :param path: File path INSIDE the repository, relative to its root; do NOT include the "<owner>/<name>" prefix (that belongs in `repo`); always use "/" as separator. Examples: "README.md", "src/main.py", "tests/test_x.py" (required).
        :param ref: Optional git ref (branch, tag, or commit hash) to read the file at; None (default) reads the working tree. Only plain refs are accepted; revision expressions like HEAD~1 are rejected.
        :param start: Optional 1-based first line to read (inclusive).
        :param end: Optional 1-based last line to read (inclusive).
        """
        try:
            return await self._read_file(repo, path, start, end, ref)
        except Exception as e:
            return error_string(e)

    async def _read_file(
        self,
        repo: str,
        path: str,
        start: Optional[int],
        end: Optional[int],
        ref: Optional[str],
    ) -> str:
        repos_path = resolve_repos_path(self.valves.repos_path)
        root = resolve_repo_root(repo, repos_path)
        self._ensure_repo_exists(root, repo)
        file_path = resolve_path(repo, path, repos_path)
        if ref is not None:
            return await self._read_file_at_ref(root, file_path, path, ref, start, end)
        if not file_path.exists():
            raise ToolError(f"file not found: {path} in {repo}", kind="not_found")
        if file_path.is_dir():
            raise ToolError(f"{path} is a directory, not a file")
        size = os.path.getsize(file_path)
        if size > MAX_READ_BYTES:
            raise ToolError(f"file too large: {path} ({size} bytes); maximum supported is {MAX_READ_BYTES}")

        # Binary detection over the WHOLE file (DESIGN.md §7 Phase 2): a null
        # byte or failed strict UTF-8 decode anywhere marks the file as
        # binary / non-text. The old 8 KB sample missed bytes past the sample.
        bad = _scan_encoding(file_path)
        if bad == "binary":
            raise ToolError(f"binary file not supported: {path} (binary files are rejected)")
        if bad == "invalid_utf8":
            raise ToolError(f"not a UTF-8 text file: {path} (only UTF-8 text is supported)")

        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            total = sum(1 for _ in f)

        start_ = start if start is not None else 1
        if start_ < 1:
            raise ToolError(f"start must be >= 1, got {start_}")
        if total == 0:
            # Empty file: nothing to read. A default start (1) is fine; an
            # explicit start > 1 is out of range.
            if start is not None and start > 1:
                raise ToolError(f"start {start} is beyond the end of the file (0 lines)")
            return ""
        if start_ > total:
            raise ToolError(f"start {start_} is beyond the end of the file ({total} lines)")
        end_ = end if end is not None else total
        if end_ < start_:
            raise ToolError(f"end {end_} is before start {start_}")
        end_ = min(end_, total)
        range_total = end_ - start_ + 1

        if range_total <= MAX_INLINE_LINES:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                lines = list(itertools.islice(f, start_ - 1, end_))
            return truncate_output("".join(lines), self.valves.max_lines, self.valves.max_bytes)

        # Large range: read only the lines that will be shown, then cap.
        shown = min(range_total, self.valves.max_lines)
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            head = list(itertools.islice(f, start_ - 1, start_ - 1 + shown))
        text = "".join(head)
        marker = f"\n... (truncated: showing {shown} of {range_total} lines)"
        result = text + marker
        if len(result.encode("utf-8")) > self.valves.max_bytes:
            bmarker = f"\n... (truncated: byte cap of {self.valves.max_bytes} reached)"
            budget = max(0, self.valves.max_bytes - len(bmarker.encode("utf-8")) - 1)
            result = _trim_bytes(text, budget) + bmarker
        return result

    async def _read_file_at_ref(
        self,
        root: Path,
        file_path: Path,
        path: str,
        ref: str,
        start: Optional[int],
        end: Optional[int],
    ) -> str:
        """Read a file as it exists at a git ref (branch/tag/commit), from the
        local object store, without touching the working tree (DESIGN.md
        §12.1). Same binary/UTF-8 detection, line-range math, and truncation
        markers as the working-tree reader. A bad ref is an Error naming the
        ref; a path missing at that ref is a Not found."""
        ref = validate_ref(ref)
        try:
            rel = file_path.relative_to(root).as_posix()
        except ValueError:
            raise ToolError(f"invalid path: {path!r}")

        # 1. The ref must resolve to a commit (rejects worktree-path fallback
        #    and blob/tree refs).
        res = await run_allowed(
            git_args("-C", str(root), "rev-parse", "--verify", f"{ref}^{{commit}}"),
            TIMEOUT_SEARCH,
        )
        if res.returncode != 0:
            raise ToolError(
                f"unknown ref: {ref!r}",
                cause=trim_cause(res.stderr) or "ref does not exist in this repository",
            )

        # 2. Object type at <ref>:<path> -> blob, tree (directory), or missing.
        res = await run_allowed(
            git_args("-C", str(root), "cat-file", "-t", f"{ref}:{rel}"),
            TIMEOUT_SEARCH,
        )
        if res.returncode != 0:
            raise ToolError(
                f"path not found at {ref}: {path}",
                kind="not_found",
                cause=trim_cause(res.stderr),
            )
        objtype = res.stdout.strip()
        if objtype == "tree":
            raise ToolError(f"{path} is a directory, not a file")
        if objtype != "blob":
            raise ToolError(f"not a file at {ref}: {path}", kind="not_found")

        # 3. Size guard, mirroring the working-tree reader's MAX_READ_BYTES.
        res = await run_allowed(
            git_args("-C", str(root), "cat-file", "-s", f"{ref}:{rel}"),
            TIMEOUT_SEARCH,
        )
        if res.returncode != 0:
            raise ToolError(
                f"path not found at {ref}: {path}",
                kind="not_found",
                cause=trim_cause(res.stderr),
            )
        size = int(res.stdout.strip() or "0")
        if size > MAX_READ_BYTES:
            raise ToolError(
                f"file too large: {path} ({size} bytes); maximum supported is {MAX_READ_BYTES}"
            )

        # 4. Fetch the raw blob (bytes) and run the SAME binary/UTF-8 scan as
        #    the working-tree reader (text=False keeps the bytes intact).
        res = await run_allowed(
            git_args("-C", str(root), "cat-file", "blob", f"{ref}:{rel}"),
            TIMEOUT_SEARCH,
            text=False,
        )
        if res.returncode != 0:
            raise ToolError(
                f"cannot read {path} at {ref}",
                cause=trim_cause(res.stderr),
            )
        data = res.stdout
        bad = _scan_encoding_bytes(data)
        if bad == "binary":
            raise ToolError(f"binary file not supported: {path} (binary files are rejected)")
        if bad == "invalid_utf8":
            raise ToolError(f"not a UTF-8 text file: {path} (only UTF-8 text is supported)")

        return self._render_lines(_decode_text(data), start, end)

    def _render_lines(self, text: str, start: Optional[int], end: Optional[int]) -> str:
        """Shared 1-based line-range math and truncation markers over an
        in-memory text (ref reads). Mirrors the working-tree reader's range
        behaviour exactly; universal-newline handling is applied first so line
        counts match open()'s newline=None semantics."""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        total = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        start_ = start if start is not None else 1
        if start_ < 1:
            raise ToolError(f"start must be >= 1, got {start_}")
        if total == 0:
            # Empty file: nothing to read. A default start (1) is fine; an
            # explicit start > 1 is out of range.
            if start is not None and start > 1:
                raise ToolError(f"start {start} is beyond the end of the file (0 lines)")
            return ""
        if start_ > total:
            raise ToolError(f"start {start_} is beyond the end of the file ({total} lines)")
        end_ = end if end is not None else total
        if end_ < start_:
            raise ToolError(f"end {end_} is before start {start_}")
        end_ = min(end_, total)
        range_total = end_ - start_ + 1

        lines = text.splitlines(keepends=True)
        if range_total <= MAX_INLINE_LINES:
            return truncate_output(
                "".join(lines[start_ - 1 : end_]),
                self.valves.max_lines,
                self.valves.max_bytes,
            )

        # Large range: show the first max_lines lines, then a marker.
        shown = min(range_total, self.valves.max_lines)
        head = "".join(lines[start_ - 1 : start_ - 1 + shown])
        marker = f"\n... (truncated: showing {shown} of {range_total} lines)"
        result = head + marker
        if len(result.encode("utf-8")) > self.valves.max_bytes:
            bmarker = f"\n... (truncated: byte cap of {self.valves.max_bytes} reached)"
            budget = max(0, self.valves.max_bytes - len(bmarker.encode("utf-8")) - 1)
            result = _trim_bytes(head, budget) + bmarker
        return result

    async def cexp_search_text(
        self,
        repo: str,
        query: str,
        path: Optional[str] = None,
        filter: Optional[str] = None,
        context: Optional[int] = None,
        case_sensitive: bool = False,
    ) -> str:
        """Search repository contents with a pure-Python regex engine.

        Use to find where text or symbols appear. The query is a regular
        expression. Returns one item per match with path, line, and matched
        text (plus optional context lines). Honors .gitignore.

        :param repo: "<owner>/<name>" of an already-cloned repository.
        :param query: Regular expression to search for (required).
        :param path: Optional subdirectory or file to narrow the search, relative to the repository root; do NOT include the "<owner>/<name>" prefix; always use "/" as separator (e.g. "src/").
        :param filter: Optional space-separated glob patterns; a leading "!" excludes.
        :param context: Optional number of context lines around each match.
        :param case_sensitive: Optional; default False (case-insensitive).
        """
        try:
            return await self._search_text(repo, query, path, filter, context, case_sensitive)
        except Exception as e:
            return error_string(e)

    async def _search_text(
        self,
        repo: str,
        query: str,
        path: Optional[str],
        filter: Optional[str],
        context: Optional[int],
        case_sensitive: bool,
    ) -> str:
        repos_path = resolve_repos_path(self.valves.repos_path)
        root = resolve_repo_root(repo, repos_path)
        self._ensure_repo_exists(root, repo)
        if not query or not query.strip():
            raise ToolError("query must not be empty")
        if context is not None and context < 0:
            raise ToolError(f"context must be >= 0, got {context}")
        base = resolve_path(repo, path, repos_path) if path else root
        if not base.exists():
            raise ToolError(f"path not found: {path} in {repo}", kind="not_found")
        includes, excludes = parse_filter(filter)
        pattern = _compile_pattern(query, case_sensitive)

        items = []
        for rel_f, fp in self._iter_text_files(root, base, includes, excludes):
            _, text = _binary_or_readable(fp)
            _search_in_text(pattern, text, 0, context or 0, rel_f, items)

        items.sort(key=lambda i: (i["path"], i.get("line") or 0))
        data: dict = {"items": items}
        if len(items) > self.valves.max_results:
            data["truncated"] = {"shown": self.valves.max_results, "total": len(items)}
            data["items"] = items[: self.valves.max_results]
        return json_output(data, self.valves.max_bytes)

    async def cexp_search_symbol(
        self,
        repo: str,
        query: str,
        path: Optional[str] = None,
        filter: Optional[str] = None,
    ) -> str:
        """Find definitions of a symbol (function, class, method, constant).

        Use to locate where a symbol is DEFINED rather than mentioned. Searches
        with language-aware patterns for definitions (def/class/fn/func/type/
        struct/enum/impl/interface/module/const/var/let... plus top-level
        `NAME =` assignments). Returns a JSON object with an items array of
        {"path", "line", "text"}. Case-sensitive by default, since
        identifiers are case-sensitive in virtually every language. Heuristic,
        not a full parser: expect occasional false positives/negatives on
        exotic syntax.

        :param repo: "<owner>/<name>" of an already-cloned repository.
        :param query: Symbol name or partial (required).
        :param path: Optional subdirectory or file to narrow the search, relative to the repository root; do NOT include the "<owner>/<name>" prefix; always use "/" as separator (e.g. "src/").
        :param filter: Optional space-separated glob patterns; a leading "!" excludes.
        """
        try:
            return await self._search_symbol(repo, query, path, filter)
        except Exception as e:
            return error_string(e)

    async def _search_symbol(
        self,
        repo: str,
        query: str,
        path: Optional[str],
        filter: Optional[str],
    ) -> str:
        repos_path = resolve_repos_path(self.valves.repos_path)
        root = resolve_repo_root(repo, repos_path)
        self._ensure_repo_exists(root, repo)
        if not query or not query.strip():
            raise ToolError("query must not be empty")
        base = resolve_path(repo, path, repos_path) if path else root
        if not base.exists():
            raise ToolError(f"path not found: {path} in {repo}", kind="not_found")
        includes, excludes = parse_filter(filter)

        # Definition patterns: a keyword followed by the symbol name, or a
        # top-level `NAME =` assignment (constants). Case-sensitive (symbols).
        q = re.escape(query.strip())
        _def_keywords = (
            "def|class|fn|func|function|type|struct|enum|trait|impl|interface|"
            "module|sub|procedure|macro|const|var|let|public|private|protected"
        )
        _modifiers = (
            "async|export|default|pub|static|suspend|extern|inline|final|"
            "abstract|override|virtual|sealed|readonly"
        )
        # Optional, repeatable visibility/async modifiers before the keyword
        # (async def, pub fn, export default class, public static void), then
        # an optional receiver (Go: func (r *R) Method()), then the name.
        pattern = _compile_pattern(
            rf"^\s*(?:(?:{_modifiers}|{_def_keywords})\s+)*"
            rf"(?:\([^)]*\)\s*)?{q}\w*(?:\s*[(:={{]|\b)",
            case_sensitive=True,
            multiline=True,
        )

        items = []
        for rel_f, fp in self._iter_text_files(root, base, includes, excludes):
            _, text = _binary_or_readable(fp)
            _search_in_text(pattern, text, 0, 0, rel_f, items)

        items.sort(key=lambda i: (i["path"], i.get("line") or 0))
        data: dict = {"items": items}
        if len(items) > self.valves.max_results:
            data["truncated"] = {"shown": self.valves.max_results, "total": len(items)}
            data["items"] = items[: self.valves.max_results]
        return json_output(data, self.valves.max_bytes)

    def _iter_text_files(self, root: Path, base: Path, includes: List[str], excludes: List[str]):
        """Yield (rel_path, Path) for every text file under `base` that passes
        the .gitignore and filter globs. Shared by cexp_search_text/cexp_search_symbol."""
        spec = _load_ignore_spec(root)
        if base.is_file():
            rel_f = _try_decode_rel(base, root)
            if not glob_match(rel_f, includes, excludes):
                return
            if _is_binary_path(base):
                return
            yield rel_f, base
            return
        for dirpath, dirnames, filenames in os.walk(base):
            dp = Path(dirpath)
            dirnames[:] = [d for d in dirnames if d not in (".git", ".hg", ".svn")]
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
            kept = []
            for d in dirnames:
                rel_d = rel_dp + "/" + d if rel_dp != "." else d
                if not _ignore_match(spec, rel_d, True):
                    kept.append(d)
            dirnames[:] = kept
            for fname in filenames:
                rel_f = rel_dp + "/" + fname if rel_dp != "." else fname
                if _ignore_match(spec, rel_f, False):
                    continue
                if not glob_match(rel_f, includes, excludes):
                    continue
                fp = dp / fname
                if _is_binary_path(fp):
                    continue
                yield rel_f, fp

    async def cexp_list_branches(
        self,
        repo: str,
        remote: bool = False,
    ) -> str:
        """List branches of a repository.

        Use to discover which branches exist before pointing cexp_clone_repo,
        cexp_list_commits, cexp_show_commit, or cexp_compare_commits at a branch name.
        Local branches by default; with remote=True, remote-tracking branches
        appear as origin/<name> (reflecting the last fetch, never the live
        network state). Returns a JSON object with an items array of
        {"branch", "current"} entries (current marks the checked-out branch).

        :param repo: "<owner>/<name>" of an already-cloned repository.
        :param remote: Optional; if True, also include remote-tracking branches.
        """
        try:
            return await self._list_branches(repo, remote)
        except Exception as e:
            return error_string(e)

    async def _list_branches(self, repo: str, remote: bool) -> str:
        root = resolve_repo_root(repo, resolve_repos_path(self.valves.repos_path))
        self._ensure_repo_exists(root, repo)
        args = git_args("-C", str(root), "branch", "--no-color")
        if remote:
            args.append("-a")
        res = await run_allowed(args, TIMEOUT_SEARCH)
        if res.returncode != 0:
            raise ToolError(f"list branches failed: {repo}", cause=trim_cause(res.stderr))
        items = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            current = line.startswith("*")
            name = line[1:].strip() if current else line
            # Detached HEAD shows a pseudo-entry like "* (HEAD detached at v1.0.0)".
            if "HEAD detached" in name:
                continue
            if name.startswith("remotes/"):
                name = name[len("remotes/"):]  # remotes/origin/main -> origin/main
            # Skip the symbolic remote HEAD pseudo-ref (origin/HEAD -> ...).
            if "->" in name:
                continue
            items.append({"branch": name, "current": current})
        data: dict = {"items": items}
        if len(items) > self.valves.max_results:
            data["truncated"] = {"shown": self.valves.max_results, "total": len(items)}
            data["items"] = items[: self.valves.max_results]
        return json_output(data, self.valves.max_bytes)

    async def cexp_list_tags(self, repo: str) -> str:
        """List tags of a repository, newest first.

        Use to see which release tags exist before cexp_clone_repo(ref=\"release\"),
        cexp_compare_commits, or cexp_show_commit on a tag. Returns a JSON object with
        an items array of tag names.

        :param repo: "<owner>/<name>" of an already-cloned repository.
        """
        try:
            return await self._list_tags(repo)
        except Exception as e:
            return error_string(e)

    async def _list_tags(self, repo: str) -> str:
        root = resolve_repo_root(repo, resolve_repos_path(self.valves.repos_path))
        self._ensure_repo_exists(root, repo)
        # Newest-first by version (semver-aware). --sort=-creatordate is
        # unreliable when tags share a timestamp, and would leave ties in
        # arbitrary/alphabetical order.
        res = await run_allowed(
            git_args("-C", str(root), "tag", "-l", "--sort=-version:refname"),
            TIMEOUT_SEARCH,
        )
        if res.returncode != 0:
            raise ToolError(f"list tags failed: {repo}", cause=trim_cause(res.stderr))
        tags = [t.strip() for t in res.stdout.splitlines() if t.strip()]
        data: dict = {"items": tags}
        if len(tags) > self.valves.max_results:
            data["truncated"] = {"shown": self.valves.max_results, "total": len(tags)}
            data["items"] = tags[: self.valves.max_results]
        return json_output(data, self.valves.max_bytes)

    async def cexp_list_commits(
        self,
        repo: str,
        ref_a: Optional[str] = None,
        ref_b: Optional[str] = None,
        path: Optional[str] = None,
    ) -> str:
        """List commits, optionally between two refs, optionally for a path.

        Use to explore commit history. With no refs, shows the current HEAD
        history. With ref_a and ref_b, shows commits reachable from ref_b but
        not from ref_a (git's ref_a..ref_b range). Capable of narrowing to a
        single file or directory. Returns a JSON object with an items array
        of {"hash", "subject"} entries (newest first). Only plain
        branch/tag/commit refs are accepted; revision expressions (HEAD~1) are
        rejected.

        :param repo: "<owner>/<name>" of an already-cloned repository.
        :param ref_a: Optional start ref (branch, tag, or commit).
        :param ref_b: Optional end ref (branch, tag, or commit).
        :param path: Optional path to narrow the history to.
        """
        try:
            return await self._list_commits(repo, ref_a, ref_b, path)
        except Exception as e:
            return error_string(e)

    async def _list_commits(
        self,
        repo: str,
        ref_a: Optional[str],
        ref_b: Optional[str],
        path: Optional[str],
    ) -> str:
        root = resolve_repo_root(repo, resolve_repos_path(self.valves.repos_path))
        self._ensure_repo_exists(root, repo)
        if path is not None:
            resolve_path(repo, path, resolve_repos_path(self.valves.repos_path))
        if ref_a:
            ref_a = validate_ref(ref_a)
        if ref_b:
            ref_b = validate_ref(ref_b)
        args = git_args("-C", str(root), "log", "--oneline", "--no-decorate")
        if ref_a and ref_b:
            args.append(f"{ref_a}..{ref_b}")
        elif ref_b:
            args.append(ref_b)
        if path:
            args += ["--", path]
        res = await run_allowed(args, TIMEOUT_SEARCH)
        if res.returncode != 0:
            raise ToolError(f"list commits failed: {repo}", cause=trim_cause(res.stderr))
        items = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(" ", 1)
            items.append({"hash": parts[0], "subject": parts[1] if len(parts) > 1 else ""})
        data: dict = {"items": items}
        if len(items) > self.valves.max_results:
            data["truncated"] = {"shown": self.valves.max_results, "total": len(items)}
            data["items"] = items[: self.valves.max_results]
        return json_output(data, self.valves.max_bytes)

    async def cexp_show_commit(
        self,
        repo: str,
        commit: str,
        path: Optional[str] = None,
    ) -> str:
        """Show a single commit (metadata + diff).

        Use to inspect what a specific commit changed. Returns raw git show
        output (commit message, author, date, and the diff), capped by the
        max_lines/max_bytes Valves with a truncation marker. Only plain
        branch/tag/commit refs are accepted; revision expressions (HEAD~1) are
        rejected.

        :param repo: "<owner>/<name>" of an already-cloned repository.
        :param commit: Commit hash or ref (branch, tag) to show (required).
        :param path: Optional path to narrow the shown diff to.
        """
        try:
            return await self._show_commit(repo, commit, path)
        except Exception as e:
            return error_string(e)

    async def _show_commit(self, repo: str, commit: str, path: Optional[str]) -> str:
        root = resolve_repo_root(repo, resolve_repos_path(self.valves.repos_path))
        self._ensure_repo_exists(root, repo)
        commit = validate_ref(commit)
        if path is not None:
            resolve_path(repo, path, resolve_repos_path(self.valves.repos_path))
        args = git_args("-C", str(root), "show", commit)
        if path:
            args += ["--", path]
        res = await run_allowed(args, TIMEOUT_SEARCH)
        if res.returncode != 0:
            raise ToolError(f"show commit failed: {commit!r}", cause=trim_cause(res.stderr))
        return truncate_output(res.stdout, self.valves.max_lines, self.valves.max_bytes)

    async def cexp_compare_commits(
        self,
        repo: str,
        ref_a: str,
        ref_b: str,
        path: Optional[str] = None,
        stat: bool = False,
    ) -> str:
        """Compare changes between two refs (three-dot diff by default).

        Use to see what changed between two branches, tags, or commits. Uses
        the three-dot (merge-base) diff: shows changes on ref_b since its
        divergence from ref_a. With stat=True, returns the --stat summary
        instead of the full diff. Returns raw git diff output, capped by the
        max_lines/max_bytes Valves with a truncation marker. Only plain
        branch/tag/commit refs are accepted; revision expressions (HEAD~1) are
        rejected.

        :param repo: "<owner>/<name>" of an already-cloned repository.
        :param ref_a: First ref (branch, tag, or commit) (required).
        :param ref_b: Second ref (branch, tag, or commit) (required).
        :param path: Optional path to narrow the diff to.
        :param stat: Optional; if True, return the --stat summary only.
        """
        try:
            return await self._compare_commits(repo, ref_a, ref_b, path, stat)
        except Exception as e:
            return error_string(e)

    async def _compare_commits(
        self,
        repo: str,
        ref_a: str,
        ref_b: str,
        path: Optional[str],
        stat: bool,
    ) -> str:
        root = resolve_repo_root(repo, resolve_repos_path(self.valves.repos_path))
        self._ensure_repo_exists(root, repo)
        ref_a = validate_ref(ref_a)
        ref_b = validate_ref(ref_b)
        if path is not None:
            resolve_path(repo, path, resolve_repos_path(self.valves.repos_path))
        args = git_args("-C", str(root), "diff")
        if stat:
            args.append("--stat")
        args.append(f"{ref_a}...{ref_b}")
        if path:
            args += ["--", path]
        res = await run_allowed(args, TIMEOUT_SEARCH)
        if res.returncode != 0:
            raise ToolError(
                f"compare failed: {ref_a}...{ref_b}", cause=trim_cause(res.stderr)
            )
        return truncate_output(res.stdout, self.valves.max_lines, self.valves.max_bytes)
