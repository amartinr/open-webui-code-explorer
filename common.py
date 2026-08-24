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

ALLOWED_BINARIES = {"git", "rg", "fd"}

# Timeout policy (DESIGN.md §9.4), in seconds.
TIMEOUT_CLONE = 600
TIMEOUT_FETCH = 120
TIMEOUT_PULL = 120
TIMEOUT_READ = 10
TIMEOUT_SEARCH = 30
TIMEOUT_COMMIT = 30

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
# Subprocess execution (DESIGN.md §4, §5.6, §9.7)
# ---------------------------------------------------------------------------


@dataclass
class CommandResult:
    """Captured pipes of a finished subprocess. Both streams are DATA."""

    stdout: str
    stderr: str
    returncode: int


def _headless_env() -> Dict[str, str]:
    env = dict(os.environ)
    for key, value in HEADLESS_ENV.items():
        env[key] = value
    env.pop("GIT_ASKPASS", None)  # no askpass helper may launch a prompt
    return env


async def run_allowed(argv: List[str], timeout: int) -> CommandResult:
    """Run an allow-listed binary with arguments, capturing both pipes.

    - argv[0] MUST be one of ALLOWED_BINARIES (no arbitrary commands).
    - No shell: argument arrays only (shell=False).
    - Runs in a worker thread so the blocking call never stalls Open WebUI's
      event loop.
    - Uses the fixed headless environment (§9.7) so git can never prompt,
      page, localize, or read user/global config.
    - On timeout raises ToolError(kind="timed_out").
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
            text=True,
            timeout=timeout,
            env=_headless_env(),
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"timed out after {timeout}s", kind="timed_out")
    return CommandResult(stdout=proc.stdout or "", stderr=proc.stderr or "", returncode=proc.returncode)


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


def repo_component_ok(component: str) -> bool:
    """True iff component matches ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ and is not "." or "..".

    NOTE: do NOT use ^[\\w.-]+/... - that accepts "..", enabling path traversal.
    """
    return bool(_REPO_COMPONENT_RE.match(component)) and component not in (".", "..")


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
