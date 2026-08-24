# Open WebUI Code Explorer — Design Document

> **Status:** Draft for iterative implementation
> **Audience:** AI programming agents and human maintainers iterating on this project.
> **Language:** All user-facing strings, tool names, descriptions, schemas, and code comments SHALL be written in English.

---

## 1. Purpose

Build a set of Open WebUI **Tools** that give a "meta model" (an LLM configured
inside Open WebUI) first-class, read-only access to the source code of Open
WebUI itself and to community tool repositories.

The meta model uses these tools to:

- **Understand** how Open WebUI works internally (source code).
- **Inspect and evaluate** community tool repositories before integration.
- **Compare** versions, branches, and releases to reason about changes.

This is distinct from the documentation side, which is handled separately
(optional Phase 5) via Open WebUI's native Knowledge Base features.

---

## 2. Goals & Non-Goals

### Goals

- Let the meta model **clone, fetch, pull, list, search, explore, read, and
  compare** code repositories through tools, never through its own shell or
  network.
- Keep every tool **read-only** with respect to source code, except for the
  `clone_repo`, `fetch_repo`, and `pull_repo` tools, which may only write
  inside a dedicated, allow-listed repository directory.
- Provide a **secure-by-default** surface: path sanitization, output size
  limits, allow-listed subprocesses, no arbitrary command execution.
- Make the repository storage location **configurable** at two levels:
  - Process-wide via environment variable.
  - Runtime-adjustable via an admin-facing Valve.
- Structure the work in **phases** so implementation can proceed and be
  validated incrementally.

### Non-Goals

- Serving documentation (that is the Knowledge Base / oikb concern).
- Running, building, or testing the code (no execution).
- Modifying, committing, or pushing code.
- General-purpose shell access.

---

## 3. Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     Open WebUI                               │
│                                                              │
│  ┌──────────────┐      tool calls       ┌─────────────────┐  │
│  │  Meta model   │ ─────────────────────▶ │   Tools          │  │
│  │  (LLM)        │ ◀───────────────────── │  (this project)  │  │
│  └──────────────┘     results/observations└────────┬────────┘  │
│                                                    │            │
│                                        allow-listed subprocesses│
│                                        (git, rg, fd, cat/sed)   │
│                                                    │            │
│  ┌──────────────────┐   (optional)   ┌─────────────▼─────────┐  │
│  │ Knowledge Base    │◀──────────────▶│ Repo storage            │  │
│  │ (docs, via oikb)  │                │ <repos_path>/<owner>/<repo>│
│  └──────────────────┘                └────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

Key principle: **the tools are the entire attack surface.** The model only ever
sees the tools. Therefore every safeguard lives inside the tools themselves.

---

## 4. Security Model

These rules are non-negotiable and MUST be enforced by every tool.

1. **Allow-listed subprocesses only.** No shell execution with user-controlled
   strings. Each tool invokes a fixed set of binaries (`git`, `rg`, `fd`) using
   argument arrays (no `shell=True`).
2. **Read-only for code.** No tool may create, modify, or delete files inside
   repositories. Exception: the `clone_repo`, `fetch_repo`, and `pull_repo`
   tools may write *only* into `<repos_path>`, and only via `git`.
3. **Path sanitization.** Every `repo` and `path` parameter MUST be validated
   against path traversal (`..`, absolute paths, symlink escapes) and resolved
   strictly inside the repository root. Reject anything that escapes.
4. **Bounded output.** Every tool MUST enforce maximum result, line, and byte
   limits (admin Valves, §5.5), truncating with an explicit "truncated" marker so
   the model knows results are incomplete.
5. **No network from the model.** The model cannot reach the network. Only the
   `clone_repo`, `fetch_repo`, and `pull_repo` tools may talk to remotes, and
   only through `git` (clone/fetch/pull).
6. **No execution of code.** Nothing is run, imported, or evaluated. Tools only
   read bytes and run Git/ripgrep/fd.

---

## 5. Repository Storage & Configuration

### 5.1 Layout

Repositories are stored under a configurable base path using the
`<owner>/<repo>` convention:

```
<repos_path>/
  open-webui/
    open-webui/          # from github.com/open-webui/open-webui
  <community-owner>/
    <community-repo>/
```

### 5.2 Path resolution (priority order)

1. **Valve** `repos_path` (admin-overridable at runtime), if set.
2. **Environment variable** `OWUI_REPOS_PATH`, if set.
3. **Default** `/usr/local/src`.

### 5.3 Persistence & permissions

- A **dedicated volume** SHOULD be mounted at the resolved default path so
  clones survive container recreation.
- The process running the tools MUST have read/write permission on that
  directory.
- The Valve is a *logical* override; actual write permission is still granted
  by the mounted volume. Document this clearly in the Valve description.

### 5.4 Deployment: three tool scripts, three `Valves` classes

The project is deployed to Open WebUI as **three tool scripts** (one per Tool
in the admin UI), each exposing multiple tools. Open WebUI auto-discovers a
tool's functions from the public methods of its `Tools` class (§9.6); there is
no `as_tools()` in the current Open WebUI API. The grouping is by object, not
by phase:

| Script | Tools exposed | Phase(s) |
|---|---|---|
| **Repos** | `clone_repo`, `fetch_repo`, `pull_repo`, `list_repos` | Phase 1 |
| **Files & Search** | `list_files`, `read_file`, `search_text`, `search_symbol` | Phase 2 + `search_symbol` in Phase 3 |
| **Commits** | `list_commits`, `show_commit`, `compare_commits` | Phase 3 |

Rationale for this split:
- **Per-script tool access in Open WebUI**: an operator can attach only the
  "Files & Search" script to a model, withholding repo management and commit
  analysis.
- **Per-script Valves**: caps can be tuned per group (e.g. wider `max_results`
  for search than for file listing).

Note: the "Files & Search" script is created in Phase 2 with `list_files`,
`read_file`, and `search_text`. `search_symbol` is added to the same script in
Phase 3 (not stubbed earlier).

### 5.5 Shared Valves (identical contract across the three scripts)

Each script defines its **own** `Valves` class (Open WebUI Valves are
per-script). To avoid drift, the same fields with the same names and defaults
MUST be declared consistently in all three scripts.

Admin-facing Valves (not exposed to the agent):

| Valve | Type | Default | Purpose |
|---|---|---|---|
| `repos_path` | str | `""` (→ env → `/usr/local/src`) | Base directory for clones (§5.2). |
| `max_results` | int | `50` | Cap on item counts (files, matches, commits). |
| `max_lines` | int | `200` | Cap on output lines (file reads, diffs). |
| `max_bytes` | int | `20480` | Hard byte cap (20 KB); whichever of `max_lines`/`max_bytes` is hit first truncates. |

All three scripts need `repos_path` (even read/search/commit tools must locate
the repositories). The **`OWUI_REPOS_PATH` env var is the recommended global
single source of truth**: set it once at the container level and leave the
`repos_path` Valve empty, so the per-script Valves rarely need to diverge.

These caps are **infrastructure/safety policy** (protect against huge outputs,
timeouts, and context exhaustion), so they belong to the operator, not the
agent. The agent never passes them as parameters.

When a cap truncates output, the tool MUST append a marker like
`... (truncated: showing N of M)` so the agent knows results are incomplete and
can refine its query instead of assuming it saw everything.

### 5.6 Shared helper contract

`common.py` is the **single source of truth** for the security-critical logic.
Because Open WebUI loads each tool as a self-contained script (stored in the DB
and `exec`'d in an isolated namespace, §9.1), the three scripts cannot `import
common` at runtime. Instead, a **build script inlines the body of `common.py`
into each of the three scripts** (§9.2). The following MUST be the single
implementation for every tool (no per-tool reimplementation) and MUST be safe to
inline: stdlib imports only, no relative imports, no module-level side effects,
no `if __name__ == "__main__"` guard.

```python
def resolve_repos_path(valve_path: str) -> str
# Priority: Valve repos_path → env OWUI_REPOS_PATH → default "/usr/local/src".

def repo_component_ok(component: str) -> bool
# True iff component matches ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ and not in {".", ".."}.

def resolve_repo_root(repo: str, repos_path: str) -> Path
# Split repo on the FIRST "/" (exactly two components). Reject if either component
# fails repo_component_ok. Return repos_path / owner / name (existence NOT checked).

def resolve_path(repo: str, path: str | None, repos_path: str) -> Path
# Resolve `path` relative to the repo root. Raise ToolError if: path is absolute;
# any segment is ".."; or the resolved path, after .resolve(), escapes the repo
# root (symlink escape). None → repo root.

async def run_allowed(argv: list[str], timeout: int) -> subprocess.CompletedProcess
# await asyncio.to_thread(subprocess.run, argv, shell=False, capture_output=True,
#   text=True, timeout=timeout). Offload to a worker thread so the blocking call
#   does not stall Open WebUI's event loop (backend is fully async since 0.9.0).
# argv[0] MUST be one of the allow-listed binaries {"git", "rg", "fd"}.
```

`ToolError` is a shared exception mapped to a user-facing message (never a raw
traceback). All tools go through these helpers for repo/path resolution and
subprocess execution.

---

## 6. Tool Contract Conventions

All tools share these conventions. Implement them consistently.

- **Naming:** Open WebUI convention `verb_noun` (action + object). Tools are
  **granular**: one tool = one operation, and the name states what it does.
  There are no dispatch parameters (`action`/`mode`) anywhere in the project;
  every operation is a distinct tool.
- **Selector vocabulary** (one meaning per name, project-wide):
  - `type` → file-type filter in `list_files` (`file | dir | all`).
  - `filter` → see below.
- **`filter` parameter:** named `filter` for model readability, but its value is a
  **glob pattern** (e.g. `*.py`, `!*.md`). A `filter` string may contain
  space-separated patterns; a leading `!` marks an exclusion. Mapping:
  - `rg` (search): include → `--glob P`, exclude → `--glob '!P'`.
  - `fd` (list): include → `--glob P`, exclude → `--exclude P`.
  Used by `list_files`, `search_text`, and `search_symbol`.
- **Parameter style:** snake_case. Required vs. optional is stated per schema.
- **`repo` scoping:** every tool (except `list_repos`) REQUIRES a `repo`
  (`<owner>/<repo>`) parameter. Tools never operate over the entire
  `<repos_path>` blindly.
- **Output caps are Valves, not parameters.** `max_results`, `max_lines`, and
  `max_bytes` are admin Valves (§5.5) and MUST NOT appear in tool schemas.
  Agent-facing parameters are limited to *semantic* inputs (paths, queries,
  filters, ranges, flags) that express intent.
- **Output format:** plain text/markdown-friendly. Errors are returned as
  structured messages, not raised exceptions that crash the tool.
- **Determinism:** prefer stable ordering (e.g., `rg --sort path`, `fd`
  default order, `git log` default order).

---

## 7. Tools by Phase

Implementation is split into phases with clear dependencies. Each phase MUST be
validated (see §8) before moving to the next.

### Phase 1 — Foundation: `clone_repo`, `fetch_repo`, `pull_repo`, `list_repos`

> Goal: establish the storage layer and enable bringing code into the system.
> These are the only tools that write to disk (inside the allow-listed repo dir).

#### `clone_repo`

```
clone_repo(
  repo:  str      # required: "<owner>/<name>"
  url:   str      # optional: full clone URL (overrides repo)
  ref:   str      # optional: branch | tag | "release"
)
```

- Resolve target `<repos_path>/<owner>/<name>`.
- If it already exists, return an error/notice telling the model to use
  `fetch_repo`, `pull_repo`, or `list_repos` (no destructive overwrite).
- Run `git clone` (full clone; no shallow option).
- After clone, if `ref` is given, checkout that ref.
- `ref="release"` is a special value resolving to the most recent **release tag**:
  prefer the highest tag matching a semver pattern (`v?X.Y.Z`) ordered by version;
  fall back to the latest tag by commit date (`git tag --sort=-creatordate`, first
  line); error if the repo has no tags. This requires a full clone (tags included).
- Return: target path, default branch, resolved `ref`, and short status.

#### `fetch_repo`

```
fetch_repo(
  repo:  str      # required: "<owner>/<name>"
)
```

- Requires the repo to already exist.
- Run `git fetch --all --tags --prune`.
- Return list of updated branches/tags, or a notice if up to date.
- Does NOT touch the working tree. This is the only way to bring in newly
  published tags while the checkout is on a detached HEAD (e.g. cloned at a
  specific tag) or on a branch you do not want to move.

#### `pull_repo`

```
pull_repo(
  repo:  str      # required: "<owner>/<name>"
)
```

- Requires the repo to already exist and the checkout to be on a branch
  (fails with a clear message if on a detached HEAD — use `fetch_repo` there).
- Run `git pull --ff-only`.
- `--ff-only` ensures the working tree only advances via fast-forward: it never
  creates merge commits and never leaves the repo in a conflicted state. If the
  local branch diverged, it fails and reports the situation instead of merging.
- Implicitly performs a fetch, so it also brings in new tags. It is the tool to
  use for keeping a moving branch (e.g. `dev`) up to date.

#### `list_repos`

```
list_repos()
```

- Enumerate existing clones under `<repos_path>` (owner/repo and, optionally,
  current checked-out branch). No parameters.
- Helpful so the model does not clone duplicates.

#### Phase 1 safety notes

- Only `clone_repo` / `fetch_repo` / `pull_repo` write; all restrict writes to
  `<repos_path>`.
- `repo` is validated via the shared helper `resolve_repo_root` (§5.6). The
  format is `<owner>/<name>`, each component matching
  `^[A-Za-z0-9_][A-Za-z0-9_.-]*$` and neither component equal to `.` or `..`.
  **Do NOT use `^[\w.-]+/[\w.-]+$`: it accepts `..`, enabling path traversal.**
- No `--recurse-submodules` unless explicitly designed and safe.

---

### Phase 2 — Reading & searching: `list_files`, `read_file`, `search_text`

> Goal: give the model the ability to navigate structure, read files, and find
> code/text. These map to `fd` (find), direct read (`cat`/`sed -n`), and `rg`
> (ripgrep).

#### `list_files`

```
list_files(
  repo:      str                  # required: "<owner>/<name>"
  path:      str                  # optional (default repo root)
  max_depth: int                  # optional
  filter:    str                  # optional (glob pattern, e.g. "*.py", "!*.md")
  type:      "file" | "dir" | "all"   # optional
)
```

- Uses `fd` with `--max-depth`, `--type`, glob patterns (from `filter`). Returns
  relative paths, sorted, capped by the `max_results` Valve.

#### `read_file`

```
read_file(
  repo:  str      # required: "<owner>/<name>"
  path:  str      # required
  start: int      # optional (1-based line)
  end:   int      # optional (1-based line)
)
```

- Direct file read (or `sed -n 'start,endp'`). Binary files MUST be detected and
  rejected with a clear message. Output capped by the `max_lines`/`max_bytes`
  Valves.

#### `search_text`

```
search_text(
  repo:          str      # required: "<owner>/<name>"
  query:         str      # required
  path:          str      # optional: subdirectory/file to narrow scope
  filter:        str      # optional: file filter, glob pattern (e.g. "*.py")
  context:       int      # optional: lines of context (rg -C)
  case_sensitive:bool     # optional, default false
)
```

- Runs `rg -n --sort path [--context N] [--glob <filter>] [--case-sensitive] <query> <path>`.
- Capped by the `max_results` Valve.
- Never pass raw query via shell; use argument array. Escaping is handled by
  subprocess args.

---

### Phase 3 — Comparative & symbolic: `search_symbol`, `list_commits`, `show_commit`, `compare_commits`

> Goal: reasoning about changes and navigating code by symbol, not just raw text.

#### `search_symbol`

```
search_symbol(
  repo:   str      # required: "<owner>/<name>"
  query:  str      # required (symbol name or partial)
  path:   str      # optional: narrow scope
  filter: str      # optional: file filter, glob pattern
)
```

- Uses `rg` with language-aware patterns for definitions (functions, classes,
  methods, constants). Implementation detail: derive patterns per file extension
  or use `rg` with a curated set of regexes; do NOT run `ctags` unless
  explicitly added to the allow-list.
- Capped by the `max_results` Valve.

#### `list_commits`

```
list_commits(
  repo:  str      # required: "<owner>/<name>"
  ref_a: str      # optional (branch|tag|commit)
  ref_b: str      # optional (branch|tag|commit)
  path:  str      # optional: narrow scope
)
```

- `git log --oneline [ref_a..ref_b] -- <path>`, capped by `max_results`. Defaults
  to current HEAD history when no refs are given.

#### `show_commit`

```
show_commit(
  repo:   str      # required: "<owner>/<name>"
  commit: str      # required (commit hash or ref)
  path:   str      # optional: narrow scope
)
```

- `git show <commit> -- <path>`, capped by `max_lines`/`max_bytes`.

#### `compare_commits`

```
compare_commits(
  repo:  str      # required: "<owner>/<name>"
  ref_a: str      # required (branch|tag|commit)
  ref_b: str      # required (branch|tag|commit)
  path:  str      # optional: narrow scope
  stat:  bool     # optional: return --stat summary instead of full diff
)
```

- `git diff ref_a...ref_b -- <path>` (three-dot / merge-base; the decided default,
  see §10). A two-dot (`..`) variant is a possible future addition.
- `stat=True` → `git diff --stat ref_a...ref_b -- <path>` for an overview.
- Capped by `max_lines`/`max_bytes`.

---

### Phase 4 — Meta model integration

- Write **high-quality tool descriptions** (the text the model reads to decide
  when and how to use each tool). Each description MUST state: purpose, when to
  use it, and parameter meanings.
- (Optional) Provide a **system prompt / skill** with usage rules, e.g.:
  - Always scope with `repo` + narrow `path`/`filter` before broad searches.
  - Prefer `list_files` to understand structure before reading.
  - Use `list_commits` / `show_commit` / `compare_commits` when asked about changes/releases.
  - Treat truncated results as incomplete; refine the query.

### Phase 5 — (Optional) Documentation via Knowledge Base

- Distinct from code. Deploy **oikb** as a sidecar (requires Open WebUI 0.9.6+),
  configured via service name (`http://openwebui:8080`) and API keys.
- Sync `open-webui/docs` into a Knowledge Base with incremental sync.
- Attach that KB to the meta model so it can answer "how to use/configure"
  questions, while the code tools answer "how it works" questions.

---

## 8. Acceptance Criteria (per phase)

Each phase is "done" only when all its criteria pass.

### Phase 1

- [ ] `clone_repo` clones a public repo into `<repos_path>/<owner>/<name>`.
- [ ] `clone_repo` on an existing repo fails gracefully (no overwrite).
- [ ] `fetch_repo` updates branches/tags and reports changes, without touching
      the working tree.
- [ ] `pull_repo` fast-forwards the working tree on a branch; it fails cleanly
      on a detached HEAD and never creates a merge commit.
- [ ] `list_repos` shows all clones with their owner/repo.
- [ ] Path traversal and malformed `repo` values are rejected.
- [ ] Env var `OWUI_REPOS_PATH` and Valve `repos_path` both affect location,
      with Valve taking precedence.

### Phase 2

- [ ] `list_files` returns sorted relative paths, honoring max_depth/filter/type.
- [ ] `read_file` reads a range; binary files are rejected cleanly.
- [ ] `search_text` finds matches with line numbers and context.
- [ ] All outputs respect the `max_results`/`max_lines`/`max_bytes` Valves and
      include truncation markers.
- [ ] No path escapes the repo root.

### Phase 3

- [ ] `search_symbol` locates definitions with reasonable precision.
- [ ] `list_commits` lists history, capped.
- [ ] `show_commit` displays a single commit.
- [ ] `compare_commits` shows changes between two refs, with `--stat` support.

### Phase 4

- [ ] Tool descriptions are written in English and usable by an LLM.
- [ ] A meta model configured with these tools can answer a sample
      "how does X work in Open WebUI?" question using the tools.

---

## 9. Implementation Notes (Open WebUI specifics)

### 9.1 References & environment

- **Tool API reference** (docs.openwebui.com):
  - `https://docs.openwebui.com/features/extensibility/plugin/tools`
  - `https://docs.openwebui.com/features/extensibility/plugin/tools/development`
  - `https://docs.openwebui.com/features/extensibility/plugin/development/valves`
- **How tools are loaded (source of truth):** `backend/open_webui/utils/plugin.py`
  (`load_tool_module_by_id`) and `backend/open_webui/utils/tools.py`
  (`get_tool_specs`, `get_functions_from_tool`). A tool is stored in the DB as a
  single Python source string, `exec`'d into an isolated `types.ModuleType`, and
  its functions are auto-discovered from the public methods of a class named
  `Tools`. There is **no `as_tools()`** and **no filesystem tools directory** for
  user tools in the current API.
- **Built-in tool examples:** `backend/open_webui/tools/builtin.py` (plain
  functions, not a `Tools` class) and the community site `openwebui.com` for
  importable user tools. Note: the `open-webui/tools` GitHub repo referenced in
  earlier drafts does not exist.
- **Required binaries** (must be in `PATH`): `git`, `rg` (ripgrep), `fd`.
  Each script SHOULD check for them at startup and log a clear error if missing.
  All subprocess calls use the resolved absolute path of the binary.

### 9.2 File structure (proposed)

```
open-webui-code-explorer/
  common.py              # shared helpers (§5.6), ToolError, allow-list, caps
  build.py               # inlines common.py into each template → dist/
  templates/
    repos.py.tpl         # script "Repos": clone_repo, fetch_repo, pull_repo, list_repos
    files_search.py.tpl  # script "Files & Search": list_files, read_file, search_text, search_symbol
    commits.py.tpl       # script "Commits": list_commits, show_commit, compare_commits
  dist/                  # GENERATED, self-contained scripts (paste into admin UI)
    repos.py
    files_search.py
    commits.py
  tests/
    test_common.py       # path sanitization, repo validation, caps
    test_tools_*.py
  README.md              # build + deploy/configure instructions
```

**Build model.** `common.py` is the single source of truth; the three scripts
are generated. Each `templates/*.py.tpl` is a full tool script containing
a marker line (e.g. `# {{COMMON_CODE}}`); `build.py` replaces that marker with
the verbatim body of `common.py` and writes the result to `dist/`. The `dist/`
files are self-contained (no `import common`), so each is pasted into the Open
WebUI admin UI as its own Tool.

- `common.py` must be inline-safe (§5.6).
- The frontmatter docstring (`title`/`description`/`required_open_webui_version`)
  is per-template, not in `common.py`, so each generated script keeps its own
  metadata (§9.6).
- `dist/` is a build artifact; commit or gitignore it per project preference.

### 9.3 Output format examples

Every tool returns a single string, parseable and markdown-friendly.

- `list_files`: one relative path per line, sorted. Marker on its own line when
  truncated.
  ```
  src/open_webui/app.py
  src/open_webui/config.py
  ... (truncated: showing 50 of 128)
  ```
- `read_file`: raw file content (or the requested range), no added headers. If a
  range is truncated, append the marker.
- `search_text` / `search_symbol`: one match per line, `path:line: matched text`,
  plus context lines indented, then the marker if truncated.
  ```
  src/open_webui/app.py:42: def create_app():
  ```
- `list_repos`: one line per clone, `owner/name  [branch]`.
- `list_commits`: one commit per line (`hash subject`), capped.
- `show_commit` / `compare_commits`: raw `git show` / `git diff` output (or the
  `--stat` summary when `stat=True`), capped.

Errors: return a plain string beginning with `Error:` (or `Not found:` / `Timed
out:`), never a raised exception.

### 9.4 Timeout policy

| Operation | Default timeout |
|---|---|
| `clone_repo` | 600 s |
| `fetch_repo` | 120 s |
| `pull_repo` | 120 s |
| `list_files`, `search_text`, `search_symbol` | 30 s |
| `read_file` | 10 s |
| `list_commits`, `show_commit`, `compare_commits` | 30 s |

On timeout, return `Error: timed out after Ns`. These are implementation defaults;
they may be exposed later as admin Valves if needed.

### 9.5 Deployment (per script)

1. Edit `common.py` and/or the templates; run `python build.py` to regenerate
   `dist/`.
2. Unit-test `common.py` and the generated scripts locally.
3. In Open WebUI admin → Tools, add a new tool; paste the corresponding
   `dist/*.py` content. (No filesystem tools directory exists for user
   tools; the source lives in the DB.)
4. In the tool's Valves, confirm the defaults (§5.5); set `repos_path` only if
   you are NOT using `OWUI_REPOS_PATH`.
5. In a model config, attach the desired tool script(s).
6. Test against a small public repo before wiring the meta model.

### 9.6 Tool script skeleton (canonical, per current Open WebUI API)

Each generated script follows this shape. The `# {{COMMON_CODE}}` marker is
replaced by `build.py` with the inlined `common.py` body.

```python
"""
title: Code Explorer — Repos
description: Clone, fetch, pull, and list code repositories for the meta model.
required_open_webui_version: 0.9.6
"""
from typing import Optional

from pydantic import BaseModel, Field

# {{COMMON_CODE}}
# (build.py inlines common.py here; no `import common` at runtime.)


class Tools:
    def __init__(self):
        self.valves = self.Valves()

    class Valves(BaseModel):
        repos_path: str = Field("", description="Base directory for clones (§5.2).")
        max_results: int = Field(50, description="Cap on item counts.")
        max_lines: int = Field(200, description="Cap on output lines.")
        max_bytes: int = Field(20480, description="Hard byte cap on output.")

    async def clone_repo(self, repo: str, ref: Optional[str] = None) -> str:
        """
        Clone a repository into the storage area.

        :param repo: "<owner>/<name>" of the repository to clone.
        :param ref: Optional branch, tag, or "release".
        """
        root = resolve_repo_root(repo, resolve_repos_path(self.valves.repos_path))
        ...
        return "cloned: ..."
```

Key points (confirmed against current Open WebUI source):

- **One file, one `Tools` class, one `Valves` class.** Open WebUI auto-discovers
  every public (non-`_`) method of `Tools` as a tool: the tool's *name* is the
  method name, its *description* is the method docstring, and its *parameter
  schema* is derived from type hints + docstring `:param` entries.
- **Methods must be `async def`.** The backend is fully async since 0.9.0;
  blocking calls (subprocess) must be offloaded via `asyncio.to_thread` (§5.6).
- **Return the result as a string.** The tool's return value is what the model
  sees. Errors are returned as `Error: ...` strings, never raised.
- **`Valves` is a nested `BaseModel`** instantiated in `__init__` as
  `self.valves = self.Valves()`; Open WebUI overwrites it with admin values at
  load time. `UserValves` (optional) is exposed as `__user__["valves"]`.
- **Docstring conventions:** a top-level frontmatter docstring (`title:`,
  `description:`, `required_open_webui_version:`, optional `requirements:`) plus
  `:param name: description` lines. Do not rely on `self.citation` (deprecated;
  it is read but nothing acts on it).
- **Injected params are optional.** `__user__`, `__request__`, `__event_emitter__`,
  `__event_call__`, `__metadata__`, `__messages__`, `__files__`, `__model__`,
  `__oauth_token__` are injected only if declared in the signature. These tools
  are read-only and self-contained, so they typically need none of them.

---

## 10. Open Questions / Decisions Pending

Decisions made (recorded for the record):
- `compare_commits` uses the **three-dot** (`...`, merge-base) diff by default —
  for "what changed between X and Y" this shows changes on `ref_b` since its
  divergence from `ref_a`. A two-dot (`..`) option may be added later if needed.
- `clone_repo` `ref="release"` resolves to the latest release tag (§7 Phase 1).

Still open:
- [ ] Exact symbol-search strategy (regex set vs. tree-sitter vs. ctags).
- [ ] Default cap values: propose `max_results=50`, `max_lines=200`,
      `max_bytes=20480`; adjust after real-world testing.
- [ ] Whether `fetch_repo` should also resolve `ref="release"` (currently only
      `clone_repo` does).

---

## 11. Iteration Workflow for Implementing Agents

1. Read this document fully before starting.
2. Implement **Phase 1** and satisfy its acceptance criteria.
3. Commit (the repo's `commit-msg` hook adds Pi co-authorship automatically).
4. Proceed to Phase 2, then 3, validating each before the next.
5. When a phase reveals a flaw in this design, update this document in the same
   commit and note the rationale.
