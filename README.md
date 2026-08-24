# Open WebUI Code Explorer

A set of **Open WebUI Tools** that give a "meta model" (an LLM configured
inside Open WebUI) first-class, read-only access to source code — of Open
WebUI itself and of community tool repositories — for understanding,
inspecting, and comparing code. See [`DESIGN.md`](DESIGN.md) for the full
design, security model, and acceptance criteria.

## Scripts (one per Open WebUI Tool)

| Script | Tools | Phase |
|---|---|---|
| **Repos** (`dist/repos.py`) | `clone_repo`, `fetch_repo`, `pull_repo`, `list_repos` | 1 (implemented) |
| **Files & Search** (`dist/files_search.py`) | `list_files`, `read_file`, `search_text`, `search_symbol` | 2/3 (implemented) |
| **Commits** (`dist/commits.py`) | `list_branches`, `list_tags`, `list_commits`, `show_commit`, `compare_commits` | 3 (implemented) |

Each script is self-contained: `common.py` (the single source of truth for the
security-critical logic) is inlined into every script at build time, so there
is no `import common` at runtime.

`META_MODEL_PROMPT.md` contains the recommended system prompt for the meta
model (usage rules + presentation guidance), as designed in Phase 4.

## Requirements

- Python 3.10+ (build + tests).
- `git` (>= 2.39) on `PATH` (checked at tool load time, error surfaced at
  call time). **`fd` and `rg` are NOT required**: listing, reading, and
  searching are pure-Python, using `pathspec` (for `.gitignore`, including
  nested `.gitignore` files with git semantics) and `regex` (for searches)
  when those packages are present, and falling back to the standard library
  otherwise.

## Build

```sh
python build.py          # inlines common.py into templates/ -> dist/
python -m pytest         # run the test suite
```

`dist/` is a build artifact; it is committed so the scripts can be pasted into
Open WebUI without running the build.

## Deploy (Open WebUI admin)

1. Run `python build.py` (or use the committed `dist/`).
2. **Admin → Tools → +** and paste the contents of `dist/repos.py`.
3. In the tool's **Valves**, confirm the defaults:
   - `repos_path` — leave empty unless you are not using `OWUI_REPOS_PATH`.
   - `max_results` (50), `max_lines` (200), `max_bytes` (20480).
4. **Admin → Models → <model> → Tools**: attach the script(s) you want.
5. Test against a small public repo before wiring the meta model.

> The repository storage location resolves in this order (DESIGN.md §5.2):
> Valve `repos_path` → env `OWUI_REPOS_PATH` → `/usr/local/src`.

## Configure repository storage

The recommended setup: mount a dedicated volume at `/usr/local/src` and set
the env var once at the container level:

```sh
OWUI_REPOS_PATH=/usr/local/src
```

A volume is required so clones survive container recreation; the process needs
read/write permission on it. The `repos_path` Valve is a *logical* override —
actual write permission is still granted by the mounted volume.

## Security model (summary)

- **Allow-listed subprocesses only**: `git` only, invoked with argument arrays
  (`shell=False`), in a fixed headless environment
  (`GIT_TERMINAL_PROMPT=0`, `GIT_CONFIG_GLOBAL=/dev/null`, `LC_ALL=C`, …) so
  git can never prompt, page, localize, or read user/global config. File
  listing/reading/searching is pure Python (no `fd`/`rg` binaries needed).
- **Read-only for code**: only `clone_repo` / `fetch_repo` / `pull_repo` write,
  and only inside `<repos_path>` and only via `git`.
- **Path sanitization**: `repo` must be `<owner>/<name>` with components
  matching `^[A-Za-z0-9_][A-Za-z0-9_.-]*$` (never `.`/`..`); file paths are
  checked for absolute/`..`/symlink escapes.
- **Bounded output**: every result is capped by the `max_results` /
  `max_lines` / `max_bytes` Valves with an explicit truncation marker.
- **No shell, no code execution, no network from the model.**

## Tool conventions (Phase 1)

- Successful results are returned as **JSON objects** (indented, UTF-8, always
  valid): `clone_repo`, `fetch_repo`, `pull_repo`, and `list_repos` expose
  named fields and a structured `truncated` field (e.g. `{"shown": 2,
  "total": 5}`) instead of ad-hoc text markers. `read_file` and the diff
  tools (`show_commit`, `compare_commits`, Phase 3) return raw text on
  purpose: JSON-escaping code or diffs would obscure them.
- Errors are returned as strings, never raised: `Error: <summary>` with an
  optional `cause:` line, `Not found:` for missing repos, `Timed out:` for
  timeouts.
- Timeouts: clone 600 s, fetch/pull 120 s, misc 30 s.
- `clone_repo(ref="release")` checks out the most recent release tag (highest
  `v?X.Y.Z` semver; fallback: newest tag by commit date).
- `pull_repo` is fast-forward only (`git pull --ff-only`): it never creates
  merge commits and fails cleanly on a detached HEAD (use `fetch_repo` there).
