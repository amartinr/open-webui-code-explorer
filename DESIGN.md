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

- Let the meta model **clone, fetch, list, search, explore, read, and compare**
  code repositories through tools, never through its own shell or network.
- Keep every tool **read-only** with respect to source code, except for the
  `manage_repos` tool (its `clone`/`fetch` actions) which may only write inside a
  dedicated, allow-listed repository directory.
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
   repositories. Exception: the `manage_repos` tool (its `clone`/`fetch` actions) may
   write *only* into `<repos_path>`, and only via `git`.
3. **Path sanitization.** Every `repo` and `path` parameter MUST be validated
   against path traversal (`..`, absolute paths, symlink escapes) and resolved
   strictly inside the repository root. Reject anything that escapes.
4. **Bounded output.** Every tool MUST enforce a maximum number of results and
   a maximum byte/line output, truncating with an explicit "truncated" marker so
   the model knows results are incomplete.
5. **No network from the model.** The model cannot reach the network. Only the
   `manage_repos` tool may talk to remotes, and only through `git` (clone/fetch).
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

### 5.4 Configuration of the `manage_repos` tool

All three `manage_repos` actions (`clone`, `fetch`, `list`) read the same storage
location. The `manage_repos` tool MUST expose a single `repos_path` Valve (empty by
default) and resolve it against the env var and default as described in §5.2.
Implement the resolution logic once (shared helper) and reuse it across the
actions.

---

## 6. Tool Contract Conventions

All tools share these conventions. Implement them consistently.

- **Naming:** Open WebUI convention `verb_noun` (action + object). Each tool's
  name states what it does. A tool MAY use a dispatch parameter when several
  operations act on the same object and share configuration/safeguards:
  - `manage_repos` → `action: clone | fetch | list` (repo collection, shared storage/Valve).
  - `inspect_files` → `action: list | read` (files in a repo, shared path sanitization).
  - `review_commits` → `action: list | show | compare` (commit history, shared `git` handling).
  - `search_code` → `mode: text | symbol` (code search, shared `rg` handling).
- **Dispatch parameter vocabulary** (one meaning per name, project-wide):
  - `action` → values are **verbs** (operations): `clone|fetch|list`, `list|read`, `list|show|compare`.
  - `mode` → values are **categories** (nouns): `text|symbol`.
  - `type` → reserved for the file-type filter of `inspect_files` (`file|dir|all`).
- **Parameter style:** snake_case. Required vs. optional is stated per schema.
- **`repo` scoping:** every read/search/compare tool REQUIRES a `repo`
  (`<owner>/<repo>`) parameter. Tools never operate over the entire
  `<repos_path>` blindly.
- **Output format:** plain text/markdown-friendly. Errors are returned as
  structured messages, not raised exceptions that crash the tool.
- **Truncation marker:** when output exceeds limits, append a line like
  `... (truncated: showing N of M results)`.
- **Determinism:** prefer stable ordering (e.g., `rg --sort path`, `fd`
  default order, `git log` default order).

---

## 7. Tools by Phase

Implementation is split into phases with clear dependencies. Each phase MUST be
validated (see §8) before moving to the next.

### Phase 1 — Foundation: `manage_repos` (clone / fetch / list)

> Goal: establish the storage layer and enable bringing code into the system.
> This is the only tool that writes to disk (inside the allow-listed repo dir).

#### Schema

```
manage_repos(
  action: "clone" | "fetch" | "list"   # required
  repo:   str                          # required for clone/fetch: "<owner>/<name>"
  url:    str                          # optional for clone: full clone URL (overrides repo)
  ref:    str                          # optional for clone: branch | tag | "release"
  depth:  int                          # optional for clone: shallow clone depth
)
```

#### Behavior per action

- **`clone`**
  - Resolve target `<repos_path>/<owner>/<name>`.
  - If it already exists, return an error/notice telling the model to use
    `fetch` or `list` (no destructive overwrite).
  - Run `git clone` with `--depth <depth>` if provided; otherwise a full clone.
  - After clone, if `ref` is given, checkout that ref.
  - Return: target path, default branch, resolved `ref`, and short status.
- **`fetch`**
  - Requires the repo to already exist.
  - Run `git fetch --all --tags --prune`.
  - Return list of updated branches/tags, or a notice if up to date.
- **`list`**
  - Enumerate existing clones under `<repos_path>` (owner/repo and, optionally,
    current checked-out branch). No `repo` needed.
  - Helpful so the model does not clone duplicates.

#### Phase 1 safety notes

- Only `clone`/`fetch` write; both restrict writes to `<repos_path>`.
- `repo` is validated against `^[\w.-]+/[\w.-]+$` (or resolved from `url`).
- No `--recurse-submodules` unless explicitly designed and safe.

---

### Phase 2 — Reading & searching: `inspect_files`, `search_code`

> Goal: give the model the ability to navigate structure, read files, and find
> code/text. These map to `fd` (find), direct read (`cat`/`sed -n`), and `rg`
> (ripgrep).

#### `inspect_files`

```
inspect_files(
  action:    "list" | "read"          # required
  repo:      str                      # required: "<owner>/<name>"
  path:      str                      # optional for list (default repo root); required for read
  max_depth: int                      # optional, list only
  glob:      str                      # optional, list only (e.g. "*.py", "!*.md")
  type:      "file" | "dir" | "all"   # optional, list only
  start:     int                      # optional, read only (1-based line)
  end:       int                      # optional, read only (1-based line)
  max_lines: int                      # optional, read only (if no range)
)
```

- **`list`** → `fd` with `--max-depth`, `--type`, glob patterns. Returns relative
  paths, sorted, capped by `max_results`.
- **`read`** → direct file read (or `sed -n 'start,endp'`). Binary files MUST be
  detected and rejected with a clear message. Enforce `max_lines`/byte cap.

#### `search_code`

```
search_code(
  mode:          "text" | "symbol"    # required
  repo:          str                  # required: "<owner>/<name>"
  query:         str                  # required
  path:          str                  # optional: subdirectory/file to narrow scope
  glob:          str                  # optional: file filter (e.g. "*.py")
  context:       int                  # optional: lines of context (rg -C), text only
  case_sensitive:bool                 # optional, default false, text only
  max_results:   int                  # optional, default 50
)
```

- **`text`** → `rg -n --sort path [--context N] [--glob G] [--case-sensitive] <query> <path>`.
  (Delivered in Phase 2.)
- **`symbol`** → `rg` with language-aware definition patterns (functions, classes,
  methods, constants). (Delivered in Phase 3 — see §7 Phase 3.)
- Never pass raw query via shell; use argument array. Escaping is handled by
  subprocess args.

---

### Phase 3 — Comparative & symbolic: `review_commits`, `search_code` (symbol)

> Goal: reasoning about changes and navigating code by symbol, not just raw text.

#### `review_commits`

```
review_commits(
  action:  "list" | "show" | "compare"   # required
  repo:    str                           # required: "<owner>/<name>"

  # list:
  ref_a:   str                           # optional (branch|tag|commit)
  ref_b:   str                           # optional (branch|tag|commit)
  path:    str                           # optional: narrow scope
  max_results: int                       # optional

  # show:
  commit:  str                           # required for show (commit hash or ref)
  path:    str                           # optional: narrow scope
  max_lines: int                         # optional

  # compare:
  ref_a:   str                           # required for compare
  ref_b:   str                           # required for compare
  path:    str                           # optional: narrow scope
  stat:    bool                          # optional: return --stat summary instead of full diff
  max_lines: int                         # optional
)
```

- **`list`** → `git log --oneline [ref_a..ref_b] -- <path>`, capped. Defaults to
  current HEAD history when no refs are given.
- **`show`** → `git show <commit> -- <path>`, capped.
- **`compare`** → `git diff ref_a...ref_b -- <path>` (or `..` as a documented
  variant). `stat=True` → `git diff --stat ref_a...ref_b -- <path>` for an overview.

#### `search_code` — `symbol` mode

- Delivered in this phase (schema defined in Phase 2).
- Uses `rg` with language-aware patterns for definitions (functions, classes,
  methods, constants). Implementation detail: derive patterns per file extension
  or use `rg` with a curated set of regexes; do NOT run `ctags` unless
  explicitly added to the allow-list.

---

### Phase 4 — Meta model integration

- Write **high-quality tool descriptions** (the text the model reads to decide
  when and how to use each tool). Each description MUST state: purpose, when to
  use it, parameter meanings, and safety limits.
- (Optional) Provide a **system prompt / skill** with usage rules, e.g.:
  - Always scope with `repo` + narrow `path`/`glob` before broad searches.
  - Prefer `inspect_files(action="list")` to understand structure before reading.
  - Use `review_commits(action=...)` when asked about changes/releases.
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

- [ ] `manage_repos(action="clone")` clones a public repo into `<repos_path>/<owner>/<name>`.
- [ ] `manage_repos(action="clone")` on an existing repo fails gracefully (no overwrite).
- [ ] `manage_repos(action="fetch")` updates branches/tags and reports changes.
- [ ] `manage_repos(action="list")` shows all clones with their owner/repo.
- [ ] Path traversal and malformed `repo` values are rejected.
- [ ] Env var `OWUI_REPOS_PATH` and Valve `repos_path` both affect location,
      with Valve taking precedence.

### Phase 2

- [ ] `inspect_files(action="list")` returns sorted relative paths, honoring depth/glob/type.
- [ ] `inspect_files(action="read")` reads a range; binary files are rejected cleanly.
- [ ] `search_code(mode="text")` finds matches with line numbers and context.
- [ ] All outputs respect caps and include truncation markers.
- [ ] No path escapes the repo root.

### Phase 3

- [ ] `review_commits(action="list")` lists history, capped.
- [ ] `review_commits(action="show")` displays a single commit.
- [ ] `review_commits(action="compare")` shows changes between two refs, with `--stat` support.
- [ ] `search_code(mode="symbol")` locates definitions with reasonable precision.

### Phase 4

- [ ] Tool descriptions are written in English and usable by an LLM.
- [ ] A meta model configured with these tools can answer a sample
      "how does X work in Open WebUI?" question using the tools.

---

## 9. Implementation Notes (Open WebUI specifics)

- Tools are implemented as Python classes following the Open WebUI Tool API,
  with a `Valves` model (Pydantic) for runtime configuration.
- The `manage_repos` tool defines a `Valves` field `repos_path: str` (optional,
  default empty → fall back to env var → `/usr/local/src`). Use a **shared
  helper** for this resolution (§5.4).
- Use `subprocess.run(..., shell=False, capture_output=True, timeout=...)`
  with explicit timeouts. Handle timeouts gracefully (return a message).
- Return results as strings; avoid exceptions escaping the tool.
- Keep each tool a single cohesive unit (one Python file per tool, or a small
  package). Structure code for unit-testing the path sanitizer and output caps
  independently of Open WebUI.

---

## 10. Open Questions / Decisions Pending

- [ ] Exact symbol-search strategy (regex set vs. tree-sitter vs. ctags).
- [ ] Whether `review_commits(action="compare")` uses `...` (merge-base) or `..` (direct) diff by default.
- [ ] Default caps: `max_results` and byte limits (propose 50 results / 20 KB per
      tool call, adjustable).
- [ ] Whether `manage_repos` `fetch` should also fetch specific releases via `ref="release"`.

---

## 11. Iteration Workflow for Implementing Agents

1. Read this document fully before starting.
2. Implement **Phase 1** and satisfy its acceptance criteria.
3. Commit (the repo's `commit-msg` hook adds Pi co-authorship automatically).
4. Proceed to Phase 2, then 3, validating each before the next.
5. When a phase reveals a flaw in this design, update this document in the same
   commit and note the rationale.
