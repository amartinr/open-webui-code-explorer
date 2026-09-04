# PLAN — Storage lifecycle guardrails (removal quota & clone-size safeguards)

> **Audience:** a coding agent executing this proposal.
> **Pre-read:** `DESIGN.md` §5.1 (layout), §5.3 (persistence & permissions),
> §5.5 (Valves contract), §5.6 (shared helper contract), §9.3 (error/trim
> contract), §9.4 (timeouts), §9.7 (headless env), §12.1 (read-at-ref without
> network); the executed S1–S5 history for the established build/test/commit
> discipline.
> **State of the repo (current):** 15 tools, ~400 tests, version `1.2.5`
> (four version locations: three `templates/*.py.tpl` + `ALL_FRONTMATTER` in
> `build.py`). The audit trail (S5) IS implemented: `audit_event` in
> `common.py`, WARNING/ERROR/INFO events in the repos template, opt-in via the
> `audit_log` Valve. The Valves contract test requires the shared core on all
> four scripts plus the cloner-only extras on the two cloners.
> **Status:** DECIDED and EXECUTED on this branch (§4, §8) — Options A and B
> are implemented (`hardening:` commits), C and the rejected options are
> recorded as not taken.
> **This plan REPLACES the previous PLAN.md** (S5 audit trail, delivered).
> The S5 delivery is recorded in `DESIGN.md` §9.8 and in the commit history.

---

## 0. Execution log (updated after each step)

| Step | Description | Status |
|---|---|---|
| 1 | Valves `min_free_bytes` + `max_repo_bytes` (2 GiB defaults, 0 = disabled) on the two cloners (repos template + combined script); Valves contract test updated | ✅ |
| 2 | A: pre-flight `shutil.disk_usage` free-space check before clone (Error + cause + audit WARNING) | ✅ |
| 3 | B: two-phase clone (`--no-checkout` -> `_dir_size` gate -> checkout); oversized fetch discarded via `_discard_new_clone`; default-branch checkout added for the no-ref case | ✅ |
| 4 | Tests: hermetic fixtures (guardrails off in `make_tools`/runtime test) + `TestStorageGuardrails` (A/B enable, disable, audit, no-junk) | ✅ |
| 5 | Docs: DESIGN §5.5 table + new §5.7 + audit table rows; README storage-guardrails paragraph; META_MODEL_PROMPT §12 line | ✅ |
| 6 | Version bump `1.2.5` (4 places), regenerate `dist/`, full suite green | ✅ |

---

## 1. Context

The toolset lets a meta model clone, fetch, and pull repositories into a
dedicated storage area (`<repos_path>`, §5.1–§5.3) and remove them again
(`cexp_remove_repo`). Storage is a real, finite resource: the docs already
tell the operator to mount a dedicated volume at `/usr/local/src`, and
`cexp_remove_repo` + `cexp_list_repos` (size field) exist to manage it.

But nothing **prevents** storage from growing without bound, and nothing
**proactively** stops a single oversized clone from exhausting the disk. The
current posture is entirely reactive: a clone that runs out of disk fails
(possibly ENOSPC), the partial directory is cleaned up by
`_cleanup_failed_clone`, and the operator is left with a full disk and no
recorded cause beyond the audit event.

This plan (a) analyzes the weaknesses of the current tools along the storage
dimension, (b) proposes three candidate safeguards with trade-offs, and (c)
records the maintainer's decision in §4 — A and B implemented on this branch,
C deferred.

## 2. Weakness analysis of the current tools

### W1 — No total storage quota (unbounded growth)

- `cexp_clone_repo` accepts any repo; nothing sums the on-disk sizes of the
  existing clones against a budget. Ten years of clones accumulate without
  any operator-configured ceiling.
- `cexp_list_repos` *reports* sizes and `cexp_remove_repo` *enables* manual
  cleanup, but neither **enforces** anything. The model decides what to clone
  and when to clean up; there is no signal (error, warning, hint) when the
  storage area approaches a configured limit.
- Consequence: growth is unbounded until the volume is full; the operator
  discovers it via an unrelated failure (clone timeout/ENOSPC, other
  containers on the same volume).

### W2 — Removal is reactive and manual only

- `cexp_remove_repo(repo, dry_run=True)` is a solid primitive (path + size
  preview, strict inside-`repos_path` guards, symlinked roots refused), but it
  is the **only** removal mechanism and it requires the model to (a) think of
  removing repos, (b) know which ones are large. There is no:
  - auto-eviction or retention policy (e.g. "keep only N newest clones"),
  - "largest first" hint when storage pressure is detected,
  - quota-aware rejection that tells the model *why* a clone was refused and
    *what* to do about it (remove X, raise the Valve).
- The README's deployment guidance ("mount a volume") sets expectations, but
  nothing in the tool layer enforces or even measures against a budget.

### W3 — No free-space check before cloning (ENOSPC risk)

- `_clone_repo` validates URL, host allow-list, ref, and namespace collision,
  then runs `git clone` with a 600 s timeout. There is **no** pre-flight check
  of `shutil.disk_usage(repos_path)`: the tool happily starts a clone that
  cannot fit.
- If the clone exhausts the disk: git fails, `_cleanup_failed_clone` removes
  the partial directory, the failure is reported with `cause:` (the trimmed
  stderr, usually "No space left on device"), and an ERROR audit event is
  logged. Damage control works — but the disk was still filled, other
  processes on the volume may have been starved, and the failure mode is
  slower and messier than a cheap pre-flight rejection.
- `git clone` also has no built-in "reserve N bytes" semantics: even with
  free space at start, a giant repo can fill the volume mid-clone.

### W4 — No repo-size estimation before cloning (giant repos)

- `git ls-remote` does not report size; the tool has no idea how big a repo
  is until it has cloned it. A single multi-GB monorepo or a repo with a
  huge `.git` history is therefore indistinguishable from a small one until
  the bytes are already on disk.
- Estimating via the GitHub API (`GET /repos/{owner}/{name}` exposes a `size`
  field, KB of the `.git` dir) is **rejected** here: it would add a direct
  HTTP client to the process, breaking the "git is the only network talker"
  invariant (§3.2/§4.5), and `url` may point at any allow-listed host, not
  only github.com (the API estimate would not generalize).
- A git-only two-phase alternative exists: `git clone --no-checkout` (fetches
  only `.git`, no working tree), measure with the existing `_dir_size`, then
  either proceed to checkout or discard via `_cleanup_failed_clone`. This
  keeps every invariant (only git touches the network) at the cost of one
  extra clone phase.

### W5 — Partial/shallow clones conflict with the no-network-read invariant

- `--depth 1`, `--filter=blob:none` (blobless), and other partial-clone
  options shrink the initial download dramatically, which is tempting for
  storage control. They are **not** proposed here because the project's
  core guarantee is that `ref`-scoped reads (`cexp_read_file(ref=...)`,
  `cexp_search_text(ref=...)`, `cexp_compare_commits`) run **from the local
  object store, no network** (§12.1, README). With missing blobs, git would
  fetch on demand at read time (promisor remote), silently turning a read
  into a network operation and reintroducing credential/prompt concerns the
  headless env (§9.7) exists to suppress.
- If storage pressure becomes a dominant real-world problem, this trade-off
  deserves its own plan (partial-clone mode as an opt-in Valve), not a silent
  default change.

## 3. Candidate safeguards

### Option A — `min_free_bytes` pre-flight check (baseline, low cost)

Before `git clone`, compute `shutil.disk_usage(repos_path).free`; reject with
`Error:` + `cause:` (and an audit WARNING, reusing `audit_event`) when free
space is below a new Valve `min_free_bytes` (e.g. 2 GB default, 0 = disabled).

- **Pros:** one syscall, trivial implementation, covers the "disk almost
  full" case regardless of repo size; no new subprocesses; keeps every
  security invariant.
- **Cons:** does not bound clone size — a 10 GB repo still passes when 3 GB
  are free, then fails mid-clone.
- **Where:** `_clone_repo` in `templates/repos.py.tpl`; Valve on the two
  cloners (repos + combined), consistent with `allowed_hosts`.

### Option B — `max_repo_bytes` two-phase clone (bounds clone size, git-only)

`git clone --no-checkout <url> <root>` → measure with `_dir_size(root)` →
if above a new Valve `max_repo_bytes`, discard via `_cleanup_failed_clone`
and reject with `Error:` naming the measured size and the limit; else proceed
with the checkout (existing `_default_branch`/`ref` handling).

- **Pros:** the only git-only way to know the size before committing to the
  working tree; the checkout (the expensive part on disk) is gated by a real
  measurement; cleanup already exists and is proven (failed-clone path).
- **Cons:** one extra phase per clone (network cost of `.git` even for repos
  that will be rejected); `.git` size under-measures total on-disk footprint
  (working tree roughly doubles it for text-heavy repos) — the limit should
  be read as "`.git` budget", documented as such; slightly more code in
  `_clone_repo` (careful to keep timeout/cancel semantics: the 600 s timeout
  and `CancelledError` cleanup must cover both phases).
- **Where:** same as A.

### Option C — `max_total_bytes` quota (bounds aggregate growth)

Before `git clone`, sum `_dir_size` over existing clones (already computed by
`cexp_list_repos`); reject when the new clone would push the total over a new
Valve `max_total_bytes`.

- **Pros:** directly answers W1 — storage is a budget, not an accident;
  rejection message can name the current total, the limit, and suggest
  `cexp_remove_repo(repo, dry_run=True)` / `cexp_list_repos` to make room.
- **Cons:** summing every clone on each clone is O(clones) disk walks (each
  clone walk is already O(files)); interacts with B (which budget binds
  first?); most expensive of the three.
- **Where:** same as A.

### Combinations & defaults

- **Recommended combination:** A (always on, cheap) + B (bounds single-clone
  size) as the first deliverable; C deferred until real-world usage shows
  aggregate growth is a problem (it is the most invasive and the slowest).
- All three share the same rejection shape (`Error:` + `cause:` naming size,
  limit, and the narrowing action) and an audit WARNING, so the meta model
  sees a consistent "storage is a resource" contract — matching the existing
  META_MODEL_PROMPT.md §12 (manage clones deliberately).

## 4. Decision (RECORDED — A and B implemented)

| Option | Cost | Value | Decision |
|---|---|---|---|
| A: `min_free_bytes` pre-flight | Low | High (ENOSPC prevention) | **Taken** — default 2 GiB, 0 = disabled |
| B: `max_repo_bytes` two-phase | Medium | High (giant-repo bound) | **Taken** — default 2 GiB, 0 = disabled |
| C: `max_total_bytes` quota | Medium–High | Medium (until growth bites) | **Deferred** — revisit if real-world usage shows unbounded aggregate growth |
| Partial/shallow clone mode | High (invariant change) | Medium | Rejected (W5) |
| GitHub API size estimate | Low code, breaks invariant | Low–Medium | Rejected (W4) |

**Decision (maintainer, recorded on this branch):** A and B are implemented
here — one `hardening:` commit for the code/valves/tests, one `docs:` commit
for DESIGN/README/META_MODEL_PROMPT/PLAN. C stays deferred; the weaknesses
(W1/W2) it would address are documented in §2 for a future plan.

## 5. Scope (EXECUTED on this branch)

This branch contains the plan AND its execution (decision: §4). A and B touch
only `templates/repos.py.tpl` (clone flow + Valves), `build.py` (frontmatter
version + the combined script's Valves), the version strings in the other two
templates, `tests/`, and docs. `dist/` is regenerated via `build.py` and never
hand-edited. Per the established discipline: one `hardening:` commit per step,
each ending with `python build.py` + `python -m pytest` green, version bump.

Implementation notes (deviations or refinements beyond the proposal):

- **Phase-2 checkout is now explicit even without `ref`**: `--no-checkout`
  leaves the worktree empty, so the default branch is checked out when no
  `ref` is given (identical end state to pre-S6 `git clone`).
- **Failed phase-2 checkouts and `ref='release'` on tag-less repos discard the
  fetched object store** (`_discard_new_clone`, new helper next to
  `_cleanup_failed_clone`): a worktree-less `.git` is useless to the read
  tools (they read the working tree), so leaving it would only block the
  `<owner>/<name>` namespace. Pre-S6, a failed ref checkout left the full
  default clone behind; the error text is unchanged.
- **Defaults**: both Valves default to 2 GiB (`2147483648`) with `0` =
  disabled, matching the plan's §3 example for A and keeping B's rejection
  rare. `max_repo_bytes` is documented as a `.git` budget.

## 6. Tests (implemented)

- A: monkeypatch `shutil.disk_usage` to report `free` below the limit →
  clone rejected with the right `Error:` shape, no git subprocess spawned;
  above the limit → clone proceeds; Valve 0 = disabled; audit WARNING.
- B: synthetic repo via the `git_daemon` fixture; `max_repo_bytes`
  below/above the measured `.git` size → rejected + namespace freed (retry
  succeeds) vs accepted; Valve 0 = disabled; audit WARNING.
- No-junk: a failed phase-2 checkout and `ref='release'` on a tag-less repo
  leave no `.git` behind and a retry without `ref` succeeds.
- Valves contract: new Valves on the two cloners, absent from the read-only
  scripts (mirror of `allowed_hosts`), updated in `tests/test_valves.py`;
  hermetic fixtures disable the guardrails where the suite must not depend
  on the machine's real disk state.

## 7. Documentation (implemented)

- `DESIGN.md`: §5.5 Valves table rows, new §5.7 (storage guardrails + `.git`-budget semantics of B + rejection shape), Phase-1 `cexp_clone_repo` spec (two-phase clone), §9.8 audit event table rows.
- `README.md`: storage-guardrails paragraph under "Configure repository storage".
- `META_MODEL_PROMPT.md`: one line in §12.

## 8. Delivery checklist

- [x] Decision recorded in §4 (A+B taken, C deferred).
- [x] `templates/repos.py.tpl` + `build.py` edited; `dist/` regenerated via `build.py`.
- [x] `python -m pytest` all green (415 passed).
- [x] New Valves on both cloners; Valves contract test updated.
- [x] Docs updated (§7); version bumped to `1.2.5` (4 places).
- [x] Commits: `hardening:` (code/valves/tests/version) + `docs:` (DESIGN/README/META_MODEL/PLAN) on this branch.

## 9. Explicitly out of scope

- Auto-eviction/retention policies (W2 beyond the manual primitive) — future
  plan if usage demands it.
- Partial/shallow clone modes (W5) — invariant change, separate plan.
- HTTP-based size estimation (W4) — invariant break, rejected.
- Logging reads/searches, tool renames, or any API surface change not listed.
- Touching `dist/` by hand — always regenerate via `build.py`.
