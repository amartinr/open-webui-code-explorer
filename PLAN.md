# PLAN — Storage lifecycle guardrails (removal quota & clone-size safeguards)

> **Audience:** a coding agent executing this proposal.
> **Pre-read:** `DESIGN.md` §5.1 (layout), §5.3 (persistence & permissions),
> §5.5 (Valves contract), §5.6 (shared helper contract), §9.3 (error/trim
> contract), §9.4 (timeouts), §9.7 (headless env), §12.1 (read-at-ref without
> network); the executed S1–S5 history for the established build/test/commit
> discipline.
> **State of the repo (current):** 15 tools, ~400 tests, version `1.2.4`
> (four version locations: three `templates/*.py.tpl` + `ALL_FRONTMATTER` in
> `build.py`). The audit trail (S5) IS implemented: `audit_event` in
> `common.py`, WARNING/ERROR/INFO events in the repos template, opt-in via the
> `audit_log` Valve. The Valves contract test requires the shared core on all
> four scripts plus `allowed_hosts` on the two cloners.
> **Status:** PROPOSAL — decision pending (§4). This plan documents two
> storage-lifecycle gaps and lays out the options; no code is changed until
> the decision is made.
> **This plan REPLACES the previous PLAN.md** (S5 audit trail, delivered).
> The S5 delivery is recorded in `DESIGN.md` §9.8 and in the commit history;
> if the new work is declined, the old plan can be restored from git.

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
asks the maintainer to decide which (if any) to implement.

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

## 4. Decision (PENDING)

| Option | Cost | Value | Implement? |
|---|---|---|---|
| A: `min_free_bytes` pre-flight | Low | High (ENOSPC prevention) | **Proposed: yes** |
| B: `max_repo_bytes` two-phase | Medium | High (giant-repo bound) | **Proposed: yes** |
| C: `max_total_bytes` quota | Medium–High | Medium (until growth bites) | **Proposed: defer** |
| Partial/shallow clone mode | High (invariant change) | Medium | Rejected (W5) |
| GitHub API size estimate | Low code, breaks invariant | Low–Medium | Rejected (W4) |

**Decision:** record the outcome here when the maintainer decides. Options A
and B are the recommended scope for the next hardening batch; C is a
candidate follow-up. If the maintainer declines, this plan is closed with
the weaknesses documented and no code changes.

## 5. Scope (if approved — NOT implemented in this branch)

This branch contains **only this plan**. If A/B are approved, a follow-up
implementation branch must touch only `common.py` (if a helper is added),
`templates/*.py.tpl`, `build.py` (frontmatter + version), and tests; `dist/`
is regenerated via `build.py` and never hand-edited. Per the established
discipline: one `hardening:` commit per step, each ending with `python
build.py` + `python -m pytest` green, version bump, push.

## 6. Tests (if approved)

- A: monkeypatch `shutil.disk_usage` to report `free` below the limit →
  clone rejected with the right `Error:` shape, no git subprocess spawned;
  above the limit → clone proceeds; Valve 0 = disabled.
- B: synthetic repo sized via the `git_daemon` fixture; `max_repo_bytes`
  below/above the measured `.git` size → rejected/cleaned vs accepted;
  timeout and `CancelledError` cleanup still cover both phases.
- C (if taken): quota accounting across multiple clones; rejection message
  names current total and suggests `cexp_remove_repo`.
- Valves contract: new Valves on the two cloners, absent from the read-only
  scripts (mirror of `allowed_hosts`), updated in `tests/test_valves.py`.

## 7. Documentation (if approved)

- `DESIGN.md`: extend §5.5 (Valves contract) and the security section with
  the storage guards; document the `.git`-budget semantics of B.
- `README.md`: storage-management paragraph (guardrails + how the model
  reacts to storage rejections).
- `META_MODEL_PROMPT.md`: one line in §12 ("a storage rejection names the
  limit and the freeing action; honor it").

## 8. Delivery checklist

- [ ] Decision recorded in §4.
- [ ] (If A/B) `common.py` + `templates/*.py.tpl` + `build.py` edited; `dist/` regenerated.
- [ ] (If A/B) `python -m pytest` all green.
- [ ] (If A/B) New Valves on both cloners; Valves contract test updated.
- [ ] (If A/B) Docs updated (§7); version bumped.
- [ ] This branch: PLAN.md only, committed and pushed.

## 9. Explicitly out of scope

- Auto-eviction/retention policies (W2 beyond the manual primitive) — future
  plan if usage demands it.
- Partial/shallow clone modes (W5) — invariant change, separate plan.
- HTTP-based size estimation (W4) — invariant break, rejected.
- Logging reads/searches, tool renames, or any API surface change not listed.
- Touching `dist/` by hand — always regenerate via `build.py`.
