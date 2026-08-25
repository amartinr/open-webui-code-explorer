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
  `cexp_clone_repo`, `cexp_fetch_repo`, and `cexp_pull_repo` tools, which may only write
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
│                                        (git only)               │
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
   strings. Each tool invokes a fixed set of binaries (`git` only) using
   argument arrays (no `shell=True`). Filesystem listing, reading, and
   searching are implemented in pure Python (pathspec for `.gitignore`),
   so the deployment environment does NOT need fd/ripgrep.
2. **Read-only for code.** No tool may create, modify, or delete files inside
   repositories. Exception: the `cexp_clone_repo`, `cexp_fetch_repo`, and `cexp_pull_repo`
   tools may write *only* into `<repos_path>`, and only via `git`.
3. **Path sanitization.** Every `repo` and `path` parameter MUST be validated
   against path traversal (`..`, absolute paths, symlink escapes) and resolved
   strictly inside the repository root. Reject anything that escapes.
4. **Bounded output.** Every tool MUST enforce maximum result, line, and byte
   limits (admin Valves, §5.5), truncating with an explicit "truncated" marker so
   the model knows results are incomplete.
5. **No network from the model.** The model cannot reach the network. Only the
   `cexp_clone_repo`, `cexp_fetch_repo`, and `cexp_pull_repo` tools may talk to remotes, and
   only through `git` (clone/fetch/pull).
6. **No execution of code.** Nothing is run, imported, or evaluated. Tools only
   read bytes and run Git.

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
| **Repos** | `cexp_clone_repo`, `cexp_fetch_repo`, `cexp_pull_repo`, `cexp_list_repos` | Phase 1 |
| **Files & Search** | `cexp_list_files`, `cexp_read_file`, `cexp_search_text`, `cexp_search_symbol` | Phase 2 + `cexp_search_symbol` in Phase 3 |
| **Commits** | `cexp_list_branches`, `cexp_list_tags`, `cexp_list_commits`, `cexp_show_commit`, `cexp_compare_commits` | Phase 3 |

Rationale for this split:
- **Per-script tool access in Open WebUI**: an operator can attach only the
  "Files & Search" script to a model, withholding repo management and commit
  analysis.
- **Per-script Valves**: caps can be tuned per group (e.g. wider `max_results`
  for search than for file listing).

Note: the "Files & Search" script is created in Phase 2 with `cexp_list_files`,
`cexp_read_file`, and `cexp_search_text`. `cexp_search_symbol` is added to the same script in
Phase 3 (not stubbed earlier). The "Commits" script is created in Phase 3
complete with all five tools (not stubbed earlier).

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

When a cap truncates output, the tool MUST make that explicit so the agent
knows results are incomplete and can refine its query instead of assuming it
saw everything. Structured (JSON) tools carry a `truncated` field
(`{"shown": N, "total": M}`, plus `"reason"` when the binding cap is not the
item count); raw-text tools (`cexp_read_file`, `cexp_show_commit`, `cexp_compare_commits`)
append the trailing marker `... (truncated: showing N of M)`.

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

async def run_allowed(argv: list[str], timeout: int) -> CommandResult
# await asyncio.to_thread(subprocess.run, argv, shell=False, capture_output=True,
#   text=True, timeout=timeout, env=<headless env, §9.7>). Offload to a worker
#   thread so the blocking call does not stall Open WebUI's event loop (backend
#   is fully async since 0.9.0). argv[0] MUST be one of the allow-listed binaries
#   {"git"}. Returns CommandResult(stdout, stderr, returncode) so
#   tools capture BOTH pipes as data. TimeoutExpired → ToolError("timed out after Ns").

def validate_ref(ref: str) -> str
# Validate a git ref (branch, tag, or commit hash) before it is interpolated
# into any git argument. Accept plain refs (including slash-containing branch
# names); reject empty strings, leading dashes, ":", "..", whitespace, and
# shell metacharacters (~ ^ * ? [ ] { } @ \). Raise ToolError naming the
# offending ref. Deliberately rejects revision expressions (HEAD~1, main^).

def validate_clone_url(url: str) -> str
# Validate and normalize a clone `url` override before it reaches git
# (Enhancement B, ENHANCEMENT_B.md). Protocol allow-list: https/http/git/ssh;
# everything else (ext::/sh:: command URLs, file://, ftp, rsync, ...) is
# rejected with a cause explaining why. scp-like "user@host:path" is
# normalized to ssh://user@host/path (git's own scp-like semantics; no port
# syntax in that form). Credentials in the URL are rejected (they would be
# persisted in <repo>/.git/config), except the ssh username (the normal ssh
# form). Returns the normalized URL.

def _normalize_remote(url: str) -> str
# Canonical form for COMPARING remotes (collision detection only): lowercase
# host (without userinfo), strip trailing ".git" and "/", keep path case.
# Scheme and ssh user are NOT part of the comparison, so https://github.com/o/r,
# ssh://git@github.com/o/r and https://github.com/o/r.git compare equal (same
# logical repo, different transport).

async def _remote_origin(root: str) -> str
# The repo's remote origin URL (git -C <root> remote get-url origin), or ""
# when there is no origin. Used by the clone-collision message and by
# cexp_list_repos (new `origin` field, Enhancement B).
```

`ToolError` is a shared exception mapped to a user-facing message (never a raw
traceback). All tools go through these helpers for repo/path/ref resolution and
subprocess execution.

The **capture-and-translate contract** is central to the design (§6, §9.3):
`run_allowed` returns the raw pipes as *data* (never as the tool's return
value); each tool then interprets that data and returns an agent-facing string.
No tool ever returns raw `stdout`/`stderr` directly to the model.

---

## 6. Tool Contract Conventions

All tools share these conventions. Implement them consistently.

- **Naming:** Open WebUI convention `verb_noun` (action + object). Tools are
  **granular**: one tool = one operation, and the name states what it does.
  There are no dispatch parameters (`action`/`mode`) anywhere in the project;
  every operation is a distinct tool.
- **Prefix:** every public tool name is prefixed with `cexp_`
  (`cexp_read_file`, `cexp_list_files`, ...). The `verb_noun` suffix is
  unchanged; the prefix is an anti-collision measure: the generic names
  (read_file, list_files, ...) could collide with third-party tools in shared
  Open WebUI instances. If a collision still happens, Open WebUI's first-wins
  mechanism (`utils/tools.py`) appends the tool_id (e.g.
  `code_explorer_cexp_read_file`).
- **Selector vocabulary** (one meaning per name, project-wide):
  - `type` → file-type filter in `cexp_list_files` (`file | dir | all`).
  - `filter` → see below.
  - `ref` → a git ref (branch, tag, or commit hash) selecting which snapshot of
    the repository to read; `None`/omitted means the working tree. Always
    validated by `validate_ref` before reaching git.
  - `context` → number of unified-context lines around a diff (maps to
    `git diff -U N`).
- **`filter` parameter:** named `filter` for model readability, but its value is a
  **glob pattern** (e.g. `*.py`, `!*.md`). A `filter` string may contain
  space-separated patterns; a leading `!` marks an exclusion. Applied with
  Python's `fnmatch` (fd/ripgrep-style: matched against the full relative path):
  - search: include → keep, exclude → drop.
  - list: include → keep, exclude → drop.
  Used by `cexp_list_files`, `cexp_search_text`, and `cexp_search_symbol`.
- **Parameter style:** snake_case. Required vs. optional is stated per schema.
- **`repo` scoping:** every tool (except `cexp_list_repos`) REQUIRES a `repo`
  (`<owner>/<repo>`) parameter. Tools never operate over the entire
  `<repos_path>` blindly.
- **Ref validation:** every parameter that accepts a git ref (branch, tag, or
  commit hash) MUST be validated by the shared `validate_ref` helper before it
  is interpolated into any git argument. No ref may reach git unvalidated; this
  applies to both new and existing ref-accepting tools.
- **Output caps are Valves, not parameters.** `max_results`, `max_lines`, and
  `max_bytes` are admin Valves (§5.5) and MUST NOT appear in tool schemas.
  Agent-facing parameters are limited to *semantic* inputs (paths, queries,
  filters, ranges, flags) that express intent.
- **Output format:** JSON for structured results; raw text for content.
  Structured tools (clone/fetch/pull/list/search/commit enumerations) return a
  single JSON object: indented, UTF-8, always valid, with named fields and a
  structured `truncated` metadata field (§5.5, §9.3). Content tools
  (`cexp_read_file`) and diff tools (`cexp_show_commit`, `cexp_compare_commits`) return raw
  text: JSON-escaping code or diffs would obscure the very thing the model
  wants to read. Errors keep the stable `Error:` prefix contract (§9.3),
  never JSON.
- **Error handling:** a tool NEVER returns raw `stdout`/`stderr`. It captures
  both pipes (§5.6), interprets the result, and returns an agent-facing
  message. On success it returns the transformed output (sorted, capped,
  marker-terminated). On failure it returns a structured error string of the
  form `Error: <summary>` with an optional `cause:` line (§9.3) — never a
  raised exception, never a raw traceback, never an uninterpreted stream dump.
- **Determinism:** prefer stable ordering (e.g., items sorted in Python by
  path, `git log` default order).

---

## 7. Tools by Phase

Implementation is split into phases with clear dependencies. Each phase MUST be
validated (see §8) before moving to the next.

### Phase 1 — Foundation: `cexp_clone_repo`, `cexp_fetch_repo`, `cexp_pull_repo`, `cexp_list_repos`

> Goal: establish the storage layer and enable bringing code into the system.
> These are the only tools that write to disk (inside the allow-listed repo dir).

#### `cexp_clone_repo`

```
cexp_clone_repo(
  repo:  str      # required: "<owner>/<name>"
  url:   str      # optional: full clone URL (overrides repo)
  ref:   str      # optional: branch | tag | "release"
)
```

- Resolve target `<repos_path>/<owner>/<name>`.
- If it already exists, return an error/notice telling the model to use
  `cexp_fetch_repo`, `cexp_pull_repo`, or `cexp_list_repos` (no destructive overwrite).
- Run `git clone` (full clone; no shallow option).
- After clone, if `ref` is given, checkout that ref.
- `ref="release"` is a special value resolving to the most recent **release tag**:
  prefer the highest tag matching a semver pattern (`v?X.Y.Z`) ordered by version;
  fall back to the latest tag by commit date (`git tag --sort=-creatordate`, first
  line); error if the repo has no tags. This requires a full clone (tags included).
- Return: target path, default branch, resolved `ref`, and short status.

#### `cexp_fetch_repo`

```
cexp_fetch_repo(
  repo:  str      # required: "<owner>/<name>"
)
```

- Requires the repo to already exist.
- Run `git fetch --all --tags --prune`.
- Return list of updated branches/tags, or a notice if up to date.
- Does NOT touch the working tree. This is the only way to bring in newly
  published tags while the checkout is on a detached HEAD (e.g. cloned at a
  specific tag) or on a branch you do not want to move.

#### `cexp_pull_repo`

```
cexp_pull_repo(
  repo:  str      # required: "<owner>/<name>"
)
```

- Requires the repo to already exist and the checkout to be on a branch
  (fails with a clear message if on a detached HEAD — use `cexp_fetch_repo` there).
- Run `git pull --ff-only`.
- `--ff-only` ensures the working tree only advances via fast-forward: it never
  creates merge commits and never leaves the repo in a conflicted state. If the
  local branch diverged, it fails and reports the situation instead of merging.
- Implicitly performs a fetch, so it also brings in new tags. It is the tool to
  use for keeping a moving branch (e.g. `dev`) up to date.

#### `cexp_list_repos`

```
cexp_list_repos()
```

- Enumerate existing clones under `<repos_path>` (owner/repo and, optionally,
  current checked-out branch). No parameters.
- Helpful so the model does not clone duplicates.

#### Phase 1 safety notes

- Only `cexp_clone_repo` / `cexp_fetch_repo` / `cexp_pull_repo` write; all restrict writes to
  `<repos_path>`.
- `repo` is validated via the shared helper `resolve_repo_root` (§5.6). The
  format is `<owner>/<name>`, each component matching
  `^[A-Za-z0-9_][A-Za-z0-9_.-]*$` and neither component equal to `.` or `..`.
  **Do NOT use `^[\w.-]+/[\w.-]+$`: it accepts `..`, enabling path traversal.**
- No `--recurse-submodules` unless explicitly designed and safe.

---

### Phase 2 - Reading & searching: `cexp_list_files`, `cexp_read_file`, `cexp_search_text`

> Goal: give the model the ability to navigate structure, read files, and find
> code/text. Pure-Python implementation: no `fd`/`rg` binaries required
> (pathspec for `.gitignore`, `regex` for searches, stdlib fallbacks).

#### `cexp_list_files`

```
cexp_list_files(
  repo:      str                  # required: "<owner>/<name>"
  path:      str                  # optional (default repo root)
  max_depth: int                  # optional
  filter:    str                  # optional (glob pattern, e.g. "*.py", "!*.md")
  type:      "file" | "dir" | "all"   # optional
  ref:       str                  # optional: git ref (branch/tag/hash); None = working tree
)
```

- Walks the repo with Python's `os.walk`, honoring `.gitignore` (via
  `pathspec` when available) and skipping `.git`/VCS dirs; returns relative
  paths (sorted), each with `{"path", "kind"}`, honoring `max_depth` (dirs at
  the depth limit are listed but not descended), `type`, and `filter` globs.
  Capped by the `max_results` Valve.
- When `ref` is given, the file list is produced from `git ls-tree -r
  --name-only <ref>` (validated by `validate_ref`) instead of the working tree,
  and the same `filter`/`type`/`max_depth`/cap logic is applied to the
  resulting paths in Python. Directories are derived from file paths (git does
  not track empty directories), so `type="dir"` reflects implied directories.

#### `cexp_read_file`

```
cexp_read_file(
  repo:  str      # required: "<owner>/<name>"
  path:  str      # required
  start: int      # optional (1-based line)
  end:   int      # optional (1-based line)
  ref:   str      # optional: git ref (branch/tag/hash); None = working tree
)
```

- Native Python I/O (open/read in `asyncio.to_thread`), no `cat`/`sed`
  subprocess. Binary files MUST be detected (null byte anywhere in the file or
  failed strict UTF-8 decode) and rejected with a clear message. Output capped
  by the `max_lines`/`max_bytes` Valves.
- When `ref` is given, the content is read from the local object store
  (`git cat-file blob <ref>:<path>`) without touching the working tree, and the
  SAME binary/UTF-8 detection, line-range math, and truncation behaviour apply
  as for the working-tree read. The `ref` is validated by `validate_ref`; a bad
  ref is an `Error:` (cause naming the ref) while a path missing at that ref is
  a `Not found:`.

#### `cexp_search_text`

```
cexp_search_text(
  repo:          str      # required: "<owner>/<name>"
  query:         str      # required
  path:          str      # optional: subdirectory/file to narrow scope
  filter:        str      # optional: file filter, glob pattern (e.g. "*.py")
  context:       int      # optional: lines of context (rg -C)
  case_sensitive:bool     # optional, default false
)
```

- Pure-Python regex search over the repo's text files (the `regex` package
  when available, else stdlib `re`), honoring `.gitignore`, `path` narrowing,
  and `filter` globs. Returns JSON items `{"path", "line", "text", "context"}`.
- Capped by the `max_results` Valve.

---

### Phase 3 — Comparative & symbolic: `cexp_search_symbol`, `cexp_list_branches`, `cexp_list_tags`, `cexp_list_commits`, `cexp_show_commit`, `cexp_compare_commits`

> Goal: reasoning about changes and navigating code by symbol, not just raw text —
> plus discovering the named refs (branches, tags) the history/comparison tools
> operate on.

#### `cexp_search_symbol`

```
cexp_search_symbol(
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

#### `cexp_list_branches`

```
cexp_list_branches(
  repo:   str      # required: "<owner>/<name>"
  remote: bool     # optional, default false: also include remote-tracking branches
)
```

- Runs `git branch --no-color` (local branches, current marked with `*`); with
  `remote=True`, `git branch --no-color -a`, adding remote-tracking refs as
  `origin/<name>`. Relative to the checked-out clone; never contacts the
  network (`origin/*` reflects the last fetch, not live state).
- Sorted (git's default alphabetic order), capped by the `max_results` Valve.
- Use before `cexp_clone_repo(ref=...)`, `cexp_list_commits`, or `cexp_compare_commits` to
  discover which branch names exist instead of guessing.

#### `cexp_list_tags`

```
cexp_list_tags(
  repo:   str      # required: "<owner>/<name>"
)
```

- Runs `git tag -l --sort=-creatordate` (newest first), so the most recent
  tags appear before the `max_results` cap bites.
- Use to see which release tags exist before `cexp_clone_repo(ref="release")`,
  `cexp_compare_commits`, or `cexp_show_commit` on a tag.
- Capped by the `max_results` Valve.

#### `cexp_list_commits`

```
cexp_list_commits(
  repo:  str      # required: "<owner>/<name>"
  ref_a: str      # optional (branch|tag|commit)
  ref_b: str      # optional (branch|tag|commit)
  path:  str      # optional: narrow scope
)
```

- `git log --oneline [ref_a..ref_b] -- <path>`, capped by `max_results`. Defaults
  to current HEAD history when no refs are given.

#### `cexp_show_commit`

```
cexp_show_commit(
  repo:   str      # required: "<owner>/<name>"
  commit: str      # required (commit hash or ref)
  path:   str      # optional: narrow scope
)
```

- `git show <commit> -- <path>`, capped by `max_lines`/`max_bytes`.

#### `cexp_compare_commits`

```
cexp_compare_commits(
  repo:    str      # required: "<owner>/<name>"
  ref_a:   str      # required (branch|tag|commit)
  ref_b:   str      # required (branch|tag|commit)
  path:    str      # optional: narrow scope
  stat:    bool     # optional: return --stat summary instead of full diff
  context: int      # optional: unified-context lines (-U N) around the diff
)
```

- `git diff [-U N] ref_a...ref_b -- <path>` (three-dot / merge-base; the decided
  default, see §10). A two-dot (`..`) variant is a possible future addition.
- `stat=True` → `git diff --stat ref_a...ref_b -- <path>` for an overview.
- `context=N` → `git diff -U N` (unified-context lines) for finer or coarser
  surrounding context; the default is git's own (3 lines).
- `path` narrows the diff to a single file/directory, so a whole-tree diff that
  would truncate can be inspected file by file; `ref_a`/`ref_b` are validated
  by `validate_ref`.
- Capped by `max_lines`/`max_bytes`.

---

### Phase 4 - Meta model integration

- Write **high-quality tool descriptions** (the text the model reads to decide
  when and how to use each tool). Each description MUST state: purpose, when to
  use it, and parameter meanings. DONE: every tool follows the
  "what it does + use when + return format" pattern; see the Polish commit.
- Provide a **system prompt / skill** with usage rules (see
  `META_MODEL_PROMPT.md`, which is the canonical artifact):
  - Always scope with `repo` + narrow `path`/`filter` before broad searches.
  - Prefer `cexp_list_files` to understand structure before reading.
  - Use `cexp_list_commits` / `cexp_show_commit` / `cexp_compare_commits` when asked about changes/releases.
  - Treat truncated results as incomplete; refine the query.
  - Tools return data, not presentation: quote code excerpts to the user as
    fenced markdown code blocks with a language tag (the model decides
    presentation; `cexp_read_file`/diff tools stay raw by design, §9.3).

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

- [ ] `cexp_clone_repo` clones a public repo into `<repos_path>/<owner>/<name>`.
- [ ] `cexp_clone_repo` on an existing repo fails gracefully (no overwrite).
- [ ] `cexp_fetch_repo` updates branches/tags and reports changes, without touching
      the working tree.
- [ ] `cexp_pull_repo` fast-forwards the working tree on a branch; it fails cleanly
      on a detached HEAD and never creates a merge commit.
- [ ] `cexp_list_repos` shows all clones with their owner/repo.
- [ ] Path traversal and malformed `repo` values are rejected.
- [ ] Env var `OWUI_REPOS_PATH` and Valve `repos_path` both affect location,
      with Valve taking precedence.

### Phase 2

- [ ] `cexp_list_files` returns sorted relative paths, honoring max_depth/filter/type.
- [ ] `cexp_read_file` reads a range; binary files are rejected cleanly.
- [ ] `cexp_search_text` finds matches with line numbers and context.
- [ ] All outputs respect the `max_results`/`max_lines`/`max_bytes` Valves and
      expose explicit truncation (a `truncated` field in JSON tools, a trailing
      marker in raw-text tools).
- [ ] No path escapes the repo root.

### Phase 3

- [ ] `cexp_search_symbol` locates definitions with reasonable precision.
- [ ] `cexp_list_branches` lists local branches (current marked with `*`),
      optionally including remote-tracking ones, capped.
- [ ] `cexp_list_tags` lists tags newest-first, capped.
- [ ] `cexp_list_commits` lists history, capped.
- [ ] `cexp_show_commit` displays a single commit.
- [ ] `cexp_compare_commits` shows changes between two refs, with `--stat` support.

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
- **Required binaries**: only `git` (>= 2.39). `fd`/ripgrep are NOT required:
  listing, reading, and searching are pure-Python (pathspec for `.gitignore`;
  the `regex` package when available, falling back to stdlib `re`). The
  script checks for the binaries it actually uses and logs a clear warning if
  missing; all subprocess calls use the resolved absolute path.

### 9.2 File structure (proposed)

```
open-webui-code-explorer/
  common.py              # shared helpers (§5.6), ToolError, allow-list, caps
  build.py               # inlines common.py into each template → dist/
  templates/
    repos.py.tpl         # script "Repos": cexp_clone_repo, cexp_fetch_repo, cexp_pull_repo, cexp_list_repos
    files_search.py.tpl  # script "Files & Search": cexp_list_files, cexp_read_file, cexp_search_text, cexp_search_symbol
    commits.py.tpl       # script "Commits": cexp_list_branches, cexp_list_tags, cexp_list_commits, cexp_show_commit, cexp_compare_commits
  dist/                  # GENERATED, self-contained scripts (paste into admin UI)
    repos.py
    files_search.py
    commits.py
    code_explorer.py     # OPTIONAL single-script: all 13 tools, one Tools + one Valves
  tests/
    test_common.py       # path sanitization, repo validation, caps
    test_tools_*.py
  README.md              # build + deploy/configure instructions
```

**Build model.** `common.py` is the single source of truth; the scripts are
generated. Each `templates/*.py.tpl` is a full tool script containing
a marker line (e.g. `# {{COMMON_CODE}}`); `build.py` replaces that marker with
the verbatim body of `common.py` and writes the result to `dist/`. The `dist/`
files are self-contained (no `import common`), so each is pasted into the Open
WebUI admin UI as its own Tool.

`build.py` ALSO generates `dist/code_explorer.py`: a single combined script
that exposes ALL tools in one `Tools` class with one `Valves` class, for
operators who prefer pasting one script. The methods are extracted from the
three templates' `Tools` classes via AST (no source duplication); the only
duplicated helper (`_ensure_repo_exists`, identical in all three) is
deduplicated by name. Trade-off (recorded in §10): the combined script gives
every capability at once (including the repo-management write tools), so the
per-group tool access of §5.4 is lost; keep the three per-group scripts when
that granularity matters.

- `common.py` must be inline-safe (§5.6).
- The frontmatter docstring (`title`/`description`/`required_open_webui_version`)
  is per-template, not in `common.py`, so each generated script keeps its own
  metadata (§9.6).
- `dist/` is a build artifact; commit or gitignore it per project preference.

### 9.3 Output format examples

Structured tools return a single JSON object (indented, UTF-8, always valid).
Content/diff tools return raw text. Errors keep the prefix shape below.

- `cexp_clone_repo` / `cexp_pull_repo` / `cexp_fetch_repo`: a JSON object with named fields.
  ```
  {"repo": "open-webui/open-webui", "path": "/usr/local/src/open-webui/open-webui",
   "default_branch": "main", "ref": "main", "status": "clean"}
  ```
- `cexp_list_files`: `{"items": ["src/open_webui/app.py", ...], "truncated": {"shown": 50, "total": 128}}`.
- `cexp_list_repos`: `{"items": [{"repo": "owner/name", "branch": "main"}, ...], "truncated": {...}}`.
- `cexp_list_branches`: `{"items": [{"branch": "main", "current": true}, ...], "truncated": {...}}`.
- `cexp_list_tags`: `{"items": ["v1.1.0", "v1.0.0", ...], "truncated": {...}}`.
- `cexp_list_commits`: `{"items": [{"hash": "a1b2c3d", "subject": "..."}, ...], "truncated": {...}}`.
- `cexp_search_text` / `cexp_search_symbol`: `{"items": [{"path": "...", "line": 42, "text": "..."}, ...], "truncated": {...}}`.
- `cexp_read_file`: raw file content (or the requested range), no added headers; a
  trailing marker when truncated.
- `cexp_show_commit` / `cexp_compare_commits`: raw `git show` / `git diff` output (or the
  `--stat` summary when `stat=True`), with a trailing marker when truncated.

Truncation must never break the JSON: if the serialized output exceeds
`max_bytes`, the tool re-serializes compact and then drops trailing `items`
until it fits, updating `truncated` (always valid JSON).

Errors: return a plain string, never a raised exception. Use a stable,
agent-readable shape:

```
Error: <one-line summary of what failed and why>
cause: <optional, concise extracted reason (e.g. trimmed git stderr, exit code)>
```

- `Error:` is the only mandatory line; `cause:` is optional and included only
  when it helps the agent correct its input (e.g. `fatal: repository
  '...' not found`). Strip ANSI/progress/noise before including it.
- Use `Not found:` for missing repos/files and `Timed out:` for timeouts
  (§9.4), each followed by the same summary shape.
- The tool captures stdout/stderr and *translates* them into this form; it
  never dumps the raw pipes to the model.

### 9.4 Timeout policy

| Operation | Default timeout |
|---|---|
| `cexp_clone_repo` | 600 s |
| `cexp_fetch_repo` | 120 s |
| `cexp_pull_repo` | 120 s |
| `cexp_list_files`, `cexp_search_text`, `cexp_search_symbol` | 30 s |
| `cexp_read_file` | 10 s |
| `cexp_list_branches`, `cexp_list_tags`, `cexp_list_commits`, `cexp_show_commit`, `cexp_compare_commits` | 30 s |

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

    async def cexp_clone_repo(self, repo: str, ref: Optional[str] = None) -> str:
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
  load time. Confirmed against source: `load_tool_module_by_id`
  (`backend/open_webui/utils/plugin.py`) returns `module.Tools()` (an
  *instance*, not the raw module), and `get_tools`
  (`backend/open_webui/utils/tools.py`) then runs
  `module.valves = module.Valves(**admin_values)`; tools are invoked as
  `getattr(module, fn)` (bound methods), so every cap read goes through the
  injected `self.valves`. `UserValves` (optional) is exposed as
  `__user__["valves"]`. The `valves` attribute is a non-callable instance, so
  `get_functions_from_tool` never exposes it as a tool. The tests in
  `tests/test_valves.py` enforce the identical contract across scripts and
  simulate the injection flow end to end.
- **Docstring conventions:** a top-level frontmatter docstring (`title:`,
  `description:`, `required_open_webui_version:`, optional `requirements:`) plus
  `:param name: description` lines. Do not rely on `self.citation` (deprecated;
  it is read but nothing acts on it).
- **`:param` lines MUST be single, self-contained lines.** Open WebUI parses
  docstrings line-by-line with `re.compile(r':param (\w+):\s*(.+)')`
  (`parse_docstring` in `backend/open_webui/utils/tools.py`): each line is
  matched independently, so a continuation line indented under a `:param` is
  silently dropped and the description the model sees is TRUNCATED at the end
  of the first line. Never wrap a parameter description across lines; write
  the complete description on the `:param` line itself. The tool's free-text
  description ends at the first `:param`/`:return` line (`parse_description`),
  so keep all prose before the first `:param`.
- **Every parameter must have a `:param` line.** The schema description is
  optional in Open WebUI (a missing one yields a param with no description),
  but this project requires every parameter to be documented; the regression
  test in `tests/test_tools_repos.py` asserts that the parsed `:param` set
  equals the signature parameters and that each description ends with terminal
  punctuation (a trailing `,` or a cut-off sentence is a truncation bug).
- **Injected params are optional.** `__user__`, `__request__`, `__event_emitter__`,
  `__event_call__`, `__metadata__`, `__messages__`, `__files__`, `__model__`,
  `__oauth_token__` are injected only if declared in the signature. These tools
  are read-only and self-contained, so they typically need none of them.

### 9.7 Headless, non-interactive subprocess policy

These tools run server-side with no TTY and no human to answer prompts. Every
subprocess (especially `git`) MUST run non-interactively so it can never block
on a prompt, emit progress spam, page output, or localize text. This is
enforced in exactly one place (`run_allowed`, §5.6) via a fixed environment and
command-line flags — not re-implemented per tool. Progress events are
intentionally not captured or surfaced: they are noise to the model.

**Environment (set on every `subprocess.run`):**

| Variable | Value | Effect |
|---|---|---|
| `GIT_TERMINAL_PROMPT` | `0` | Fail instead of prompting for credentials (HTTP/IMAP auth). The critical anti-hang guard. |
| `GIT_ASKPASS` | (unset/empty) | No askpass helper can launch a GUI/terminal prompt. |
| `GIT_SSH_COMMAND` | `ssh -o BatchMode=yes` | Disable SSH password/passphrase prompts for SSH remotes (fails fast instead of prompting). |
| `GIT_PAGER` | `cat` | No pager, ever. |
| `GIT_CONFIG_NOSYSTEM` | `1` | Ignore system gitconfig. |
| `GIT_CONFIG_GLOBAL` | `/dev/null` | Ignore user global gitconfig (aliases, credential helpers, hooksPath, color). |
| `GIT_OPTIONAL_LOCKS` | `0` | Read-only commands skip optional locks (no lock contention). |
| `LC_ALL` | `C` | Stable, English, non-localized output. |

**Command-line flags (per command):**

- `--no-progress` (or `-q`/`--quiet`) on `clone`/`fetch`/`pull` — suppress
  progress, which git otherwise writes to stderr.
- Do NOT pass `--no-advice` (a global git flag) to suppress advice hints: it
  only exists since git 2.45 and breaks on the supported minimum (git 2.39).
  Advice hints go to stderr, never pollute stdout, and are stripped from error
  output by `trim_cause()`. Suppress a specific advice with the config form
  (`-c advice.detachedHead=false`) where it actually matters.
- `-c color.ui=never` — no ANSI color codes in `diff`/`log`/`show`/`status`
  output, regardless of any config.

**Notes:**

- With `capture_output=True`, stdout/stderr are pipes (not a TTY), so git
  disables the pager and progress by default. The explicit settings above are
  defense in depth: they also guard against a *global* gitconfig forcing
  `color.ui=always`, a `core.pager`, or a `credential.helper` that would
  otherwise prompt or pollute output.
- Timeouts (§9.4) are the backstop: if a command somehow still blocks, it is
  killed and the tool returns `Timed out:` — it never hangs waiting for input.

---

## 10. Open Questions / Decisions Pending

Decisions made (recorded for the record):
- `cexp_compare_commits` uses the **three-dot** (`...`, merge-base) diff by default —
  for "what changed between X and Y" this shows changes on `ref_b` since its
  divergence from `ref_a`. A two-dot (`..`) option may be added later if needed.
- `cexp_clone_repo` `ref="release"` resolves to the latest release tag (§7 Phase 1).
- Structured tool results are returned as **JSON** (a single indented object,
  UTF-8, always valid) so the agent gets named fields and structured truncation
  metadata instead of ad-hoc text. Content/diff tools (`cexp_read_file`,
  `cexp_show_commit`, `cexp_compare_commits`) keep raw text: JSON-escaping code and
  diffs hurts readability. Errors keep the `Error:`/`Not found:`/`Timed out:`
  prefix contract (stable, parseable, instantly recognizable) rather than
  being JSON-encoded.
- `cexp_read_file` reads with native Python I/O (open/read in `asyncio.to_thread`)
  instead of `cat`/`sed` subprocesses: it does not widen the subprocess
  allow-list. Binary detection scans the WHOLE file with an incremental UTF-8
  decoder (a null byte or failed strict decode anywhere rejects the file) -
  an earlier 8 KB sample missed binary bytes past the sample and silently
  returned corrupted text (fixed). A multibyte character straddling a scan
  chunk boundary is handled by the incremental decoder (no false positives).
  Empty files (0 lines) return "" instead of erroring. Ranges larger than
  5000 lines are streamed (only the shown lines are read); files larger than
  50 MB are rejected.
- After the JSON migration (§10), the `max_lines` Valve applies only to
  line-based text output: `cexp_read_file` and the Phase 3 diff tools
  (`cexp_show_commit`, `cexp_compare_commits`) plus `cexp_pull_repo`'s raw fallback.
  Structured JSON tools are capped by `max_results` (item count) and
  `max_bytes` (via `json_output`). This is by design, not a lost valve.
- **fd/ripgrep are NOT used.** The Files & Search tools are pure Python:
  `cexp_list_files` walks with `os.walk` and honors `.gitignore` via the `pathspec`
  package (the same engine fd/ripgrep use; `pathspec` 1.x names it
  "gitignore", older 0.x "gitwildmatch" — both supported), with a stdlib-only
  fallback that skips `.git`/`.hg`/`.svn` and the common ignore patterns but
  cannot honor the repo's `.gitignore`. **Nested `.gitignore` files are
  honored** (git semantics): every `.gitignore` under the repo root is read
  and its patterns are prefixed with the subdir path so they apply relative
  to that subdir; negations (`!pattern`) keep the `!` at the front
  (`!sub/keep.gen`, never `sub/!keep.gen`). `cexp_search_text` uses the `regex`
  package (matching ripgrep's backtracking syntax) when available, else
  stdlib `re`; matches are returned as `{path, line, text, context}` items
  parsed from the file content in Python. This makes the tools runnable in
  environments that only have git + Python, which is the deployment target
  (§9.1). Performance is adequate for code repos: cexp_list_files ~16 ms and
  cexp_search_text ~50 ms on ~2000 files (measured).
- **`cexp_search_symbol` uses a curated definition pattern**, not ctags/tree-sitter:
  a case-sensitive regex that matches a leading definition keyword (`def`,
  `class`, `fn`, `func`, `function`, `type`, `struct`, `enum`, `trait`,
  `impl`, `interface`, `module`, `sub`, `procedure`, `macro`, `const`, `var`,
  `let`, `public`, `private`, `protected`) followed by the query (as a
  prefix), or a top-level `NAME =` assignment (constants). Case-sensitive
  because identifiers are case-sensitive in virtually every language. It is
  a heuristic, not a full parser; good precision for common languages, some
  false positives/negatives on exotic syntax (recorded as expected). The
  pattern allows optional, repeatable modifiers/visibility keywords before
  the definition keyword (`async def`, `pub fn`, `export default class`,
  `public static void`) and an optional Go receiver (`func (r *R) Method()`);
  a bug where `async def` definitions were silently missed (the keyword was
  anchored directly after `^\s*`, so any modifier broke the match) was fixed
  and covered by regression tests.
- `cexp_list_branches` normalizes `git branch -a` output: remote-tracking refs are
  shown as `origin/<name>` (stripping the `remotes/` prefix) and the detached
  HEAD pseudo-entry `(HEAD detached at ...)` is skipped.
- `cexp_list_tags` uses `--sort=-version:refname` (semver-aware, newest first)
  instead of `--sort=-creatordate`: the latter is unreliable when tags share
  a timestamp and leaves ties in alphabetical order.
- `cexp_show_commit` runs `git show <commit>` (full diff, matching §7), not
  `--stat`; the diff is capped by `max_lines`/`max_bytes`. `cexp_compare_commits`
  uses the three-dot `ref_a...ref_b` diff by default and `--stat` when
  `stat=True` (§7).
- `cexp_list_commits` validates the `path` argument with `resolve_path` (path
  traversal protection) before passing it to `git log -- <path>`.
- Content/diff tools (`cexp_read_file`, `cexp_show_commit`, `cexp_compare_commits`) return
  **raw text by design**, never fenced markdown: the tool output is data for
  the model to process, truncation would break an open fence, content may
  contain triple backticks, and concatenated ranges would duplicate fences.
  Code-excerpt presentation (fenced blocks with language tags) is the
  MODEL's responsibility, guided by the system prompt
  (`META_MODEL_PROMPT.md`) and a usage note in the `cexp_read_file` description.
- The `path` parameters of the Files & Search tools explicitly warn the model
  NOT to include the `<owner>/<name>` prefix (it belongs in `repo`) and give
  `"/"`-separated examples (e.g. `"src/main.py"`). Rationale: an agent using
  the tools passed `owner/repo/README.md` as the `path`, an ambiguity the
  earlier terse `:param path:` wording invited. The wording is kept on a
  single line per `:param` (Open WebUI parses line-by-line) and a regression
  test guards the warning + examples.
- `build.py` additionally generates `dist/code_explorer.py`, a single-script
  artifact with all 13 tools in one `Tools` class and one `Valves` class.
  Methods are extracted from the three templates via AST (no source
  duplication); `_ensure_repo_exists` (identical in all three) is
  deduplicated by name. It is an OPTION, never a replacement: the three
  per-group scripts remain the default because §5.4's per-group tool access
  is a security property (a model attached only to Files & Search cannot
  clone/write). The combined script is for operators who accept giving every
  capability in exchange for a one-paste deploy.
- All public tool names are prefixed `cexp_` (§6): `cexp_read_file`,
  `cexp_list_files`, etc. Private helpers keep their names (`_read_file`,
  `_clone_repo`, ...). The rename is names-only (no logic or docstring
  changes); user-facing strings that reference other tools were updated
  too, so the model is never told to call a stale name. Rationale: the
  generic names could collide with third-party tools in shared instances; a
  prefix removes the practical risk, and Open WebUI's first-wins fallback
  (tool_id prefix) remains as a backstop.
- After a `git clone`, only the default branch exists locally; other branches
  are `origin/<name>` until fetched/checked out (relevant for `cexp_list_branches`
  and the meta model's expectations).
- `cexp_list_branches` and `cexp_list_tags` were added to Phase 3 (Commits script): the
  model must be able to discover the named refs before pointing
  `cexp_list_commits`/`cexp_show_commit`/`cexp_compare_commits` or `cexp_clone_repo(ref=...)` at
  them. `cexp_list_tags` sorts newest-first by version (`--sort=-version:refname`)
  so the `max_results` cap shows the most recent releases first.
- `cexp_clone_repo` derives the default remote as `https://github.com/<owner>/<name>.git`
  when `url` is omitted; `url` overrides the remote, never the target directory
  (which always comes from the validated `repo`).
- On `git clone` failure, `cexp_clone_repo` best-effort removes the partial clone
  directory it just created (only when it is not a valid git repo). Without
  this, a failed clone would permanently block that `<owner>/<name>`. This is
  the only non-git write, and it is confined to `<repos_path>`.
- Truncation markers (§5.5): line caps append `... (truncated: showing N of M
  lines)`; when only the byte cap binds, a bare `showing N of M` would be
  misleading (N == M), so the marker becomes `... (truncated: byte cap of B
  reached; showing first B of T bytes)`.
- `Tools` methods are split public (discovered by Open WebUI, wraps the impl
  in the error contract) / private (`_impl`, raises `ToolError`); private
  methods are excluded from tool discovery by `get_functions_from_tool`
  (underscore filter).
- **Enhancement A (read-at-ref & large-diff) extends existing tools with
  optional selector parameters instead of adding new tools.** `cexp_read_file`
  and `cexp_list_files` gain an optional `ref` parameter (`None` = working
  tree); `cexp_compare_commits` gains an optional `context` parameter (it
  already had `path` and `stat`, which already close the "single-file diff"
  gap). Rationale: `ref`/`context` are *selectors* (like `start`/`end`/`path`/
  `filter`/`type`), not *dispatch* parameters, so they belong on the existing
  tools under the §6 "one tool = one operation" rule; separate `_at_ref` tools
  would duplicate the line-range/truncation/binary-detection logic and force
  the model to learn redundant tool names. The change stays backward-compatible
  (existing calls are unchanged) and additive.
- **`validate_ref` is the single ref-validation helper** (shared, inline-safe,
  unit-tested) and is applied to EVERY ref-accepting parameter, including the
  existing ones (`cexp_clone_repo.ref`, `cexp_show_commit.commit`,
  `cexp_list_commits.ref_a/ref_b`, `cexp_compare_commits.ref_a/ref_b`). This
  closes pre-existing option-injection gaps (e.g. a `--help` commit string)
  and prevents revision-range (`..`/`...`) or shell-metacharacter injection.
  Revision expressions (`HEAD~1`, `main^`) are deliberately NOT supported:
  only branch/tag/commit-hash refs are accepted.
- **`run_allowed` is extended with a keyword-only `text: bool = True` flag**
  rather than gaining a sibling function: `text=True` keeps the current
  locale-decoded `str` output (all existing call sites unchanged); `text=False`
  returns raw `bytes` for the read-at-ref path, which must preserve bytes for
  binary/UTF-8 detection before decoding explicitly as UTF-8.

Still open:
- [ ] Exact symbol-search strategy (regex set vs. tree-sitter vs. ctags).
- [ ] Default cap values: propose `max_results=50`, `max_lines=200`,
      `max_bytes=20480`; adjust after real-world testing.
- [ ] Whether `cexp_fetch_repo` should also resolve `ref="release"` (currently only
      `cexp_clone_repo` does).

---

## 11. Iteration Workflow for Implementing Agents

1. Read this document fully before starting.
2. Implement **Phase 1** and satisfy its acceptance criteria.
3. Commit (the repo's `commit-msg` hook adds Pi co-authorship automatically).
4. Proceed to Phase 2, then 3, validating each before the next.
5. When a phase reveals a flaw in this design, update this document in the same
   commit and note the rationale.

---

## 12. Post-1.0 Enhancements

Two enhancements are planned beyond the initial five phases. They are
independent of each other and can be implemented, reviewed, and released
separately. Both follow the same build discipline (§9.2): edit the templates
and `common.py`, regenerate `dist/`, and commit the regenerated output.

### 12.1 Enhancement A — Read-at-Ref and Large-Diff Gaps

**Status:** ready to implement (see `PLAN.md`).

**Motivation.** Two field-observed limitations: (1) the file reader could only
read the working tree, so inspecting a file at a released tag forced the model
to fetch it over the network despite a local clone; (2) a whole-tree diff that
truncated at the output caps left no way to page through a single heavily
changed file.

**Resolution.** Both are omissions, not security decisions, and are closed by
*extending* existing tools with optional selector parameters (§10 decisions):

- `cexp_read_file` gains `ref` (read a file as it exists at a branch/tag/commit,
  from the local object store, with identical line-range/binary/truncation
  behaviour; `None` = working tree).
- `cexp_list_files` gains `ref` (list the files present at a ref via
  `git ls-tree`).
- `cexp_compare_commits` gains `context` (unified-context lines); its existing
  `path` parameter already narrows a diff to a single file, closing the
  large-diff gap.

**New shared helper.** `validate_ref` (§5.6) validates any ref string before it
reaches git and is applied to every ref-accepting tool (new and existing).

**Security posture unchanged.** Only `git` is invoked (plus the `git cat-file` /
`git ls-tree` plumbing, already within the binary allow-list). No new binaries,
no subcommand allow-list in this phase, no execution, no network, no
working-tree mutation for reads.

**Acceptance criteria.**

- `cexp_read_file("o/r", "src/x.py", ref="v1.0.0", start=10, end=20)` returns
  the exact lines from that tag using only the local clone (no network, no
  checkout change).
- A previously-truncating whole-tree diff can be fully inspected via
  `cexp_compare_commits(repo, ref_a, ref_b, path="the/file.py")` (with optional
  `context`), and both sides read at their refs.
- Forbidden refs are rejected with the standard error shape in every tool and
  never reach git unvalidated.
- Binary/non-UTF-8 blobs at refs are rejected exactly like their working-tree
  counterparts.
- Build regenerates `dist/`, the full suite is green, and docs reflect the new
  surface.

### 12.2 Enhancement B — Git Provider & Protocol Selection (proposed)

**Status:** redesigned (was "multi-host directory restructure"); see
`ENHANCEMENT_B.md` for the full proposal. No hard ordering dependency on A.

The directory layout STAYS `<repos_path>/<owner>/<name>` (two levels): the
provider/protocol is NOT part of the path. It is metadata already persisted
by git itself (`remote.origin.url` in `<repo>/.git/config`, written by the
clone including any `url` override); the tools only read and expose it — in
the clone-collision message and in `cexp_list_repos`'s new `origin` field.
Namespace collisions are managed (informative `Error:` naming the existing
origin and the decision path: fetch/pull when the origin matches, review via
`cexp_list_repos` when it does not), not avoided by re-layout. The protocol
allow-list (https/http/git/ssh) on the clone `url` override closes the RCE/
exfiltration gap where only a leading dash was rejected; scp-like
`user@host:path` is normalized to `ssh://`; credentials in URLs are rejected
(they would persist in `.git/config`); `file://` and `ext::`/`sh::` are
blocked (local exfiltration / command execution). A three-level
`<host>/<owner>/<name>` layout is explicitly deferred: it would only be
justified if two same-namespace repos from different providers must coexist
simultaneously. See `ENHANCEMENT_B.md` for full scope, tests, and order.
