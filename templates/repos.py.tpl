"""
title: Code Explorer - Repos
author: A. Martin
author_url: https://github.com/amartinr
version: 1.2.2
icon_url: https://github.com/amartinr/open-webui-code-explorer/raw/main/docs/icon.svg
description: Clone, fetch, pull, and list code repositories for the meta model. Read-only with respect to source code; writes happen only inside the allow-listed repositories directory, and only via git.
required_open_webui_version: 0.9.6
"""
from typing import Optional

from pydantic import BaseModel, Field

# {{COMMON_CODE}}


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
        max_results: int = Field(
            50, description="Cap on item counts (repositories, refs)."
        )
        max_lines: int = Field(
            200, description="Cap on output lines. Whichever cap is hit first truncates."
        )
        max_bytes: int = Field(
            20480, description="Hard byte cap on tool output (20 KB default)."
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
            ref = validate_ref(ref)
        if url is not None:
            # Protocol allow-list + scp-like normalization BEFORE the clone so
            # a malicious URL never reaches git.
            url = validate_clone_url(url)
            remote = url
        else:
            remote = f"https://github.com/{repo}.git"

        # S3: optional host allow-list Valve (empty = unrestricted). Applied
        # after validate_clone_url, so only allow-listed protocols reach it.
        allowed_hosts = getattr(self.valves, "allowed_hosts", "")
        if allowed_hosts and allowed_hosts.strip():
            host = urlparse(remote).hostname or ""
            if not host_allowed(host, allowed_hosts):
                raise ToolError(
                    f"host not allowed: {host or '(none)'!r} (allowed_hosts: {allowed_hosts.strip()!r})",
                    cause="the allowed_hosts Valve restricts which origins cexp_clone_repo may clone from",
                )

        if root.exists():
            existing = await _remote_origin(str(root))
            if existing and _normalize_remote(existing) == _normalize_remote(remote):
                raise ToolError(
                    f"repo already exists: {repo} at {root} (cloned from {existing}, same origin)",
                    cause="use cexp_fetch_repo or cexp_pull_repo to update it (no destructive overwrite)",
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

        root.parent.mkdir(parents=True, exist_ok=True)
        try:
            res = await run_allowed(git_args("clone", "--no-progress", remote, str(root)), TIMEOUT_CLONE)
        except asyncio.CancelledError:
            # S4: a cancelled clone must not leave a partial directory
            # blocking <owner>/<name>. Reuse the failed-clone cleanup, then
            # re-raise so the cancellation keeps propagating.
            self._cleanup_failed_clone(root)
            raise
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
            try:
                res = await run_allowed(
                    git_args("-C", str(root), "-c", "advice.detachedHead=false", "checkout", "--quiet", ref_arg),
                    TIMEOUT_SEARCH,
                )
            except asyncio.CancelledError:
                # S4: same cleanup for a cancelled post-clone checkout.
                self._cleanup_failed_clone(root)
                raise
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
        res = await run_allowed(
            git_args("-C", str(root), "fetch", "--all", "--tags", "--prune", "--no-progress"),
            TIMEOUT_FETCH,
        )
        if res.returncode != 0:
            raise ToolError(f"fetch failed: {repo}", cause=trim_cause(res.stderr))
        after = await self._list_refs(str(root))

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
