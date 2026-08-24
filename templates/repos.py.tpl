"""
title: Code Explorer - Repos
author: A. Martin
author_url: https://github.com/amartinr
version: 1.0.0
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
        nothing is modified - use cexp_fetch_repo, cexp_pull_repo, or cexp_list_repos instead.
        After cloning, the requested ref is checked out. Returns a JSON object
        with repo, path, default_branch, ref, and status.

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

    # ------------------------------------------------------------------
    # cexp_fetch_repo
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # cexp_list_repos
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # shared internals
    # ------------------------------------------------------------------

    def _ensure_repo_exists(self, root: Path, repo: str) -> None:
        if not root.is_dir() or not (root / ".git").exists():
            raise ToolError(
                f"repository not cloned yet: {repo} (use cexp_clone_repo first)",
                kind="not_found",
            )
