# Open WebUI Code Explorer

Open WebUI Tools that give a meta model (an LLM configured in Open WebUI)
read-only access to source code (Open WebUI's own and community tool
repositories) for understanding, inspecting, and comparing it.

## Tools

Four self-contained scripts (one per Open WebUI Tool), generated from
`templates/*.py.tpl` + `common.py`:

| Script | Tools |
|---|---|
| `dist/repos.py` | `cexp_clone_repo`, `cexp_fetch_repo`, `cexp_pull_repo`, `cexp_list_repos` |
| `dist/files_search.py` | `cexp_list_files`, `cexp_read_file`, `cexp_search_text`, `cexp_search_symbol` |
| `dist/commits.py` | `cexp_list_branches`, `cexp_list_tags`, `cexp_list_commits`, `cexp_show_commit`, `cexp_compare_commits` |
| `dist/code_explorer.py` | all 13 tools in one script |

- `cexp_read_file` / `cexp_list_files` take a `ref` (branch, tag, or commit):
  read a file, or list the files, as they exist at that version (from the
  local clone, no network, no working-tree changes).
- `cexp_compare_commits` takes `context` (unified-context lines) and `path`:
  narrow a large diff to a single file.

Use the three per-group scripts for per-group tool access (e.g. attach only
read/search tools to a model). `dist/code_explorer.py` is a single-paste
alternative that exposes everything at once.

`common.py` (the security-critical logic) is inlined into each script at build
time; the scripts are self-contained (no `import common`).

`META_MODEL_PROMPT.md` is the recommended system prompt for the meta model.

## Requirements

- Python 3.9+ (build + tests).
- `git` >= 2.39 on `PATH`. `fd`/`rg` are NOT required: listing, reading, and
  searching are pure Python (pathspec, regex; stdlib fallbacks).

## Build

```sh
python build.py    # inline common.py into templates/ -> dist/
python -m pytest   # test suite
```

`dist/` is committed so the scripts can be pasted into Open WebUI without
building.

## Deploy

1. `python build.py` (or use the committed `dist/`).
2. **Admin → Tools → +**: paste `dist/repos.py`, `dist/files_search.py`,
   `dist/commits.py` (one per script), or just `dist/code_explorer.py`.
3. **Valves**: `repos_path` empty unless you're not using `OWUI_REPOS_PATH`;
   `max_results` 50, `max_lines` 200, `max_bytes` 20480.
4. **Admin → Models → <model> → Tools**: attach the script(s).
5. Test against a small public repo first.

Storage location resolution: Valve `repos_path` → env `OWUI_REPOS_PATH` →
`/usr/local/src`.

## Configure repository storage

Mount a volume at `/usr/local/src` and set once at the container level:

```sh
OWUI_REPOS_PATH=/usr/local/src
```

A volume is required so clones survive container recreation; the process needs
read/write permission on it. The Valve is a logical override; actual write
permission comes from the mounted volume.

## Security

- Subprocesses: `git` only, argument arrays, headless env (no prompts, no
  pager, no user/global config).
- Read-only for code: only `cexp_clone_repo` / `cexp_fetch_repo` /
  `cexp_pull_repo` write, only inside `<repos_path>`, only via `git`.
- Path sanitization: `repo` is `<owner>/<name>` (components
  `^[A-Za-z0-9_][A-Za-z0-9_.-]*$`); file paths checked for
  absolute/`..`/symlink escapes.
- Ref sanitization: refs validated before reaching git (no option injection,
  no `..` ranges, no revision expressions).
- **Clone-URL protocol allow-list**: only `https`, `http`, `git`, `ssh`;
  scp-like `git@host:path` is accepted (normalized to `ssh://`); `file://`
  (local exfiltration) and `ext::`/`sh::` (git's command-execution URL form)
  are blocked, as are credentials in URLs (they would be persisted in
  `<repo>/.git/config`). SSH clones work only with preconfigured credentials
  (`BatchMode=yes` fails cleanly otherwise).
- One clone per `<owner>/<name>`: cloning an existing repo returns an
  `Error:` naming the existing origin — `cexp_fetch_repo`/`cexp_pull_repo`
  when it is the same logical repo, `cexp_list_repos` otherwise (a different
  host is a namespace collision; a different transport of the same host is
  the same repo). Never overwrites.
- Bounded output: every result capped by the `max_results` / `max_lines` /
  `max_bytes` Valves, with explicit truncation markers.
- No shell, no code execution, no network from the model.

## Tool conventions

- `cexp_list_repos` reports each clone's `repo`, current `branch`, and
  `origin` (the clone URL, showing provider + protocol).
- Structured results are JSON (indented, UTF-8, always valid): the
  clone/fetch/pull/list/search/commit-enumeration tools, with a structured
  `truncated` field when capped. `cexp_read_file` and the diff tools return
  raw text: JSON-escaping code or diffs would obscure them.
- Errors are strings, never raised: `Error: <summary>` + optional `cause:`;
  `Not found:` for missing repos/files; `Timed out:` for timeouts.
- Timeouts: clone 600 s, fetch/pull 120 s, misc 30 s.
- `cexp_clone_repo(ref="release")` checks out the most recent release tag
  (highest `v?X.Y.Z` semver; fallback: newest tag by commit date).
- `cexp_pull_repo` is fast-forward only (`git pull --ff-only`): no merge
  commits; fails cleanly on a detached HEAD (use `cexp_fetch_repo`).
