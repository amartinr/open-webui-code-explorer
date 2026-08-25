"""
title: Code Explorer - Commits
author: A. Martin
author_url: https://github.com/amartinr
version: 1.1.0
icon_url: https://github.com/amartinr/open-webui-code-explorer/raw/main/docs/icon.svg
description: Inspect branches, tags, and commit history of cloned repositories for the meta model. Read-only; uses only the git binary with a headless environment.
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
            50, description="Cap on item counts (commits, branches, tags)."
        )
        max_lines: int = Field(
            200, description="Cap on output lines. Whichever cap is hit first truncates."
        )
        max_bytes: int = Field(
            20480, description="Hard byte cap on tool output (20 KB default)."
        )

    # ------------------------------------------------------------------
    # cexp_list_branches
    # ------------------------------------------------------------------

    async def cexp_list_branches(
        self,
        repo: str,
        remote: bool = False,
    ) -> str:
        """List branches of a repository.

        Use to discover which branches exist before pointing cexp_clone_repo,
        cexp_list_commits, cexp_show_commit, or cexp_compare_commits at a branch name.
        Local branches by default; with remote=True, remote-tracking branches
        appear as origin/<name> (reflecting the last fetch, never the live
        network state). Returns a JSON object with an items array of
        {"branch", "current"} entries (current marks the checked-out branch).

        :param repo: "<owner>/<name>" of an already-cloned repository.
        :param remote: Optional; if True, also include remote-tracking branches.
        """
        try:
            return await self._list_branches(repo, remote)
        except Exception as e:
            return error_string(e)

    async def _list_branches(self, repo: str, remote: bool) -> str:
        root = resolve_repo_root(repo, resolve_repos_path(self.valves.repos_path))
        self._ensure_repo_exists(root, repo)
        args = git_args("-C", str(root), "branch", "--no-color")
        if remote:
            args.append("-a")
        res = await run_allowed(args, TIMEOUT_SEARCH)
        if res.returncode != 0:
            raise ToolError(f"list branches failed: {repo}", cause=trim_cause(res.stderr))
        items = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            current = line.startswith("*")
            name = line[1:].strip() if current else line
            # Detached HEAD shows a pseudo-entry like "* (HEAD detached at v1.0.0)".
            if "HEAD detached" in name:
                continue
            if name.startswith("remotes/"):
                name = name[len("remotes/"):]  # remotes/origin/main -> origin/main
            # Skip the symbolic remote HEAD pseudo-ref (origin/HEAD -> ...).
            if "->" in name:
                continue
            items.append({"branch": name, "current": current})
        data: dict = {"items": items}
        if len(items) > self.valves.max_results:
            data["truncated"] = {"shown": self.valves.max_results, "total": len(items)}
            data["items"] = items[: self.valves.max_results]
        return json_output(data, self.valves.max_bytes)

    # ------------------------------------------------------------------
    # cexp_list_tags
    # ------------------------------------------------------------------

    async def cexp_list_tags(self, repo: str) -> str:
        """List tags of a repository, newest first.

        Use to see which release tags exist before cexp_clone_repo(ref=\"release\"),
        cexp_compare_commits, or cexp_show_commit on a tag. Returns a JSON object with
        an items array of tag names.

        :param repo: "<owner>/<name>" of an already-cloned repository.
        """
        try:
            return await self._list_tags(repo)
        except Exception as e:
            return error_string(e)

    async def _list_tags(self, repo: str) -> str:
        root = resolve_repo_root(repo, resolve_repos_path(self.valves.repos_path))
        self._ensure_repo_exists(root, repo)
        # Newest-first, matching cexp_clone_repo's release resolution exactly:
        # semver tags (v?X.Y.Z) ordered by _release_sort_key (pure releases
        # before prereleases) first, then non-semver tags by commit date. This
        # deliberately does NOT use `git tag --sort=-version:refname`: git's
        # version sort treats a prerelease suffix (-rcX) as an EXTRA component
        # and orders it ABOVE the pure release (v2.0.0-rc2 > v2.0.0), so the
        # model would be told a prerelease is the newest release - contradicting
        # cexp_clone_repo(ref="release"), which resolves the pure release.
        res = await run_allowed(git_args("-C", str(root), "tag", "-l"), TIMEOUT_SEARCH)
        if res.returncode != 0:
            raise ToolError(f"list tags failed: {repo}", cause=trim_cause(res.stderr))
        tags = [t.strip() for t in res.stdout.splitlines() if t.strip()]
        semver_tags = sorted(
            (t for t in tags if _RELEASE_TAG_RE.match(t)),
            key=_release_sort_key,
            reverse=True,
        )
        nonsemver_tags = [t for t in tags if not _RELEASE_TAG_RE.match(t)]
        res = await run_allowed(
            git_args("-C", str(root), "tag", "--sort=-creatordate"), TIMEOUT_SEARCH
        )
        if res.returncode == 0:
            newest_first = [t.strip() for t in res.stdout.splitlines() if t.strip()]
            nonsemver_tags = [t for t in newest_first if t in nonsemver_tags]
        tags = semver_tags + nonsemver_tags
        data: dict = {"items": tags}
        if len(tags) > self.valves.max_results:
            data["truncated"] = {"shown": self.valves.max_results, "total": len(tags)}
            data["items"] = tags[: self.valves.max_results]
        return json_output(data, self.valves.max_bytes)

    # ------------------------------------------------------------------
    # cexp_list_commits
    # ------------------------------------------------------------------

    async def cexp_list_commits(
        self,
        repo: str,
        ref_a: Optional[str] = None,
        ref_b: Optional[str] = None,
        path: Optional[str] = None,
    ) -> str:
        """List commits, optionally between two refs, optionally for a path.

        Use to explore commit history. With no refs, shows the current HEAD
        history. With ref_a and ref_b, shows commits reachable from ref_b but
        not from ref_a (git's ref_a..ref_b range). Capable of narrowing to a
        single file or directory. Returns a JSON object with an items array
        of {"hash", "subject"} entries (newest first). Only plain
        branch/tag/commit refs are accepted; revision expressions (HEAD~1) are
        rejected.

        :param repo: "<owner>/<name>" of an already-cloned repository.
        :param ref_a: Optional start ref (branch, tag, or commit).
        :param ref_b: Optional end ref (branch, tag, or commit).
        :param path: Optional path to narrow the history to.
        """
        try:
            return await self._list_commits(repo, ref_a, ref_b, path)
        except Exception as e:
            return error_string(e)

    async def _list_commits(
        self,
        repo: str,
        ref_a: Optional[str],
        ref_b: Optional[str],
        path: Optional[str],
    ) -> str:
        root = resolve_repo_root(repo, resolve_repos_path(self.valves.repos_path))
        self._ensure_repo_exists(root, repo)
        if path is not None:
            resolve_path(repo, path, resolve_repos_path(self.valves.repos_path))
        if ref_a:
            ref_a = validate_ref(ref_a)
        if ref_b:
            ref_b = validate_ref(ref_b)
        args = git_args("-C", str(root), "log", "--oneline", "--no-decorate")
        if ref_a and ref_b:
            args.append(f"{ref_a}..{ref_b}")
        elif ref_b:
            args.append(ref_b)
        if path:
            args += ["--", path]
        res = await run_allowed(args, TIMEOUT_SEARCH)
        if res.returncode != 0:
            raise ToolError(f"list commits failed: {repo}", cause=trim_cause(res.stderr))
        items = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(" ", 1)
            items.append({"hash": parts[0], "subject": parts[1] if len(parts) > 1 else ""})
        data: dict = {"items": items}
        if len(items) > self.valves.max_results:
            data["truncated"] = {"shown": self.valves.max_results, "total": len(items)}
            data["items"] = items[: self.valves.max_results]
        return json_output(data, self.valves.max_bytes)

    # ------------------------------------------------------------------
    # cexp_show_commit
    # ------------------------------------------------------------------

    async def cexp_show_commit(
        self,
        repo: str,
        commit: str,
        path: Optional[str] = None,
    ) -> str:
        """Show a single commit (metadata + diff).

        Use to inspect what a specific commit changed. Returns raw git show
        output (commit message, author, date, and the diff), capped by the
        max_lines/max_bytes Valves with a truncation marker. Only plain
        branch/tag/commit refs are accepted; revision expressions (HEAD~1) are
        rejected.

        :param repo: "<owner>/<name>" of an already-cloned repository.
        :param commit: Commit hash or ref (branch, tag) to show (required).
        :param path: Optional path to narrow the shown diff to.
        """
        try:
            return await self._show_commit(repo, commit, path)
        except Exception as e:
            return error_string(e)

    async def _show_commit(self, repo: str, commit: str, path: Optional[str]) -> str:
        root = resolve_repo_root(repo, resolve_repos_path(self.valves.repos_path))
        self._ensure_repo_exists(root, repo)
        commit = validate_ref(commit)
        if path is not None:
            resolve_path(repo, path, resolve_repos_path(self.valves.repos_path))
        args = git_args("-C", str(root), "show", commit)
        if path:
            args += ["--", path]
        res = await run_allowed(args, TIMEOUT_SEARCH)
        if res.returncode != 0:
            raise ToolError(f"show commit failed: {commit!r}", cause=trim_cause(res.stderr))
        return truncate_output(res.stdout, self.valves.max_lines, self.valves.max_bytes)

    # ------------------------------------------------------------------
    # cexp_compare_commits
    # ------------------------------------------------------------------

    async def cexp_compare_commits(
        self,
        repo: str,
        ref_a: str,
        ref_b: str,
        path: Optional[str] = None,
        stat: bool = False,
        context: Optional[int] = None,
    ) -> str:
        """Compare changes between two refs (three-dot diff by default).

        Use to see what changed between two branches, tags, or commits. Uses
        the three-dot (merge-base) diff: shows changes on ref_b since its
        divergence from ref_a. With stat=True, returns the --stat summary
        instead of the full diff. Returns raw git diff output, capped by the
        max_lines/max_bytes Valves with a truncation marker. Only plain
        branch/tag/commit refs are accepted; revision expressions (HEAD~1) are
        rejected. When a whole-tree diff truncates, narrow it to a single file
        with `path` (and optionally `context` for more/less surrounding lines)
        and read either side at its ref with cexp_read_file(ref=...).

        :param repo: "<owner>/<name>" of an already-cloned repository.
        :param ref_a: First ref (branch, tag, or commit) (required).
        :param ref_b: Second ref (branch, tag, or commit) (required).
        :param path: Optional path to narrow the diff to.
        :param stat: Optional; if True, return the --stat summary only.
        :param context: Optional number of unified-context lines around the diff; defaults to git's own (3).
        """
        try:
            return await self._compare_commits(repo, ref_a, ref_b, path, stat, context)
        except Exception as e:
            return error_string(e)

    async def _compare_commits(
        self,
        repo: str,
        ref_a: str,
        ref_b: str,
        path: Optional[str],
        stat: bool,
        context: Optional[int],
    ) -> str:
        root = resolve_repo_root(repo, resolve_repos_path(self.valves.repos_path))
        self._ensure_repo_exists(root, repo)
        ref_a = validate_ref(ref_a)
        ref_b = validate_ref(ref_b)
        if context is not None and context < 0:
            raise ToolError(f"context must be >= 0, got {context}")
        if path is not None:
            resolve_path(repo, path, resolve_repos_path(self.valves.repos_path))
        args = git_args("-C", str(root), "diff")
        if context is not None:
            args.append(f"-U{context}")
        if stat:
            args.append("--stat")
        args.append(f"{ref_a}...{ref_b}")
        if path:
            args += ["--", path]
        res = await run_allowed(args, TIMEOUT_SEARCH)
        if res.returncode != 0:
            raise ToolError(
                f"compare failed: {ref_a}...{ref_b}", cause=trim_cause(res.stderr)
            )
        return truncate_output(res.stdout, self.valves.max_lines, self.valves.max_bytes)

    # ------------------------------------------------------------------
    # shared internals
    # ------------------------------------------------------------------

    def _ensure_repo_exists(self, root: Path, repo: str) -> None:
        if not root.is_dir() or not (root / ".git").exists():
            raise ToolError(
                f"repository not cloned yet: {repo} (use cexp_clone_repo first)",
                kind="not_found",
            )
