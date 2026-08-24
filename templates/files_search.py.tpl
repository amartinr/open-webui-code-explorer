"""
title: Code Explorer - Files & Search
description: List, read, and search files in cloned repositories for the meta model. Read-only with respect to source code; never modifies repository contents.
required_open_webui_version: 0.9.6
"""
import itertools
import os
from typing import Optional

from pydantic import BaseModel, Field

# {{COMMON_CODE}}


class Tools:
    def __init__(self):
        self.valves = self.Valves()
        check_binaries("fd", "rg")

    class Valves(BaseModel):
        repos_path: str = Field(
            "",
            description="Base directory for repository clones. Empty -> $OWUI_REPOS_PATH -> /usr/local/src. A dedicated volume must be mounted there and the process needs read/write permission; this Valve is a logical override only.",
        )
        max_results: int = Field(
            50, description="Cap on item counts (files, matches)."
        )
        max_lines: int = Field(
            200, description="Cap on output lines. Whichever cap is hit first truncates."
        )
        max_bytes: int = Field(
            20480, description="Hard byte cap on tool output (20 KB default)."
        )

    # ------------------------------------------------------------------
    # list_files
    # ------------------------------------------------------------------

    async def list_files(
        self,
        repo: str,
        path: Optional[str] = None,
        max_depth: Optional[int] = None,
        filter: Optional[str] = None,
        type: Optional[str] = "all",
    ) -> str:
        """List files and directories under a path in a repository.

        Use to explore repository structure before reading files. Returns
        paths relative to the repository root, sorted. Respects .gitignore;
        hidden files are not shown by default.

        :param repo: "<owner>/<name>" of an already-cloned repository.
        :param path: Optional subdirectory or file; defaults to the repository root.
        :param max_depth: Optional maximum directory depth (0 = only the given path).
        :param filter: Optional space-separated glob patterns; a leading "!" excludes (e.g. "*.py !*.md").
        :param type: "file", "dir", or "all" (default "all").
        """
        try:
            return await self._list_files(repo, path, max_depth, filter, type)
        except Exception as e:
            return error_string(e)

    async def _list_files(
        self,
        repo: str,
        path: Optional[str],
        max_depth: Optional[int],
        filter: Optional[str],
        type: Optional[str],
    ) -> str:
        repos_path = resolve_repos_path(self.valves.repos_path)
        root = resolve_repo_root(repo, repos_path)
        self._ensure_repo_exists(root, repo)
        base = resolve_path(repo, path, repos_path)
        if not base.exists():
            raise ToolError(f"path not found: {path or '.'} in {repo}", kind="not_found")
        if max_depth is not None and max_depth < 0:
            raise ToolError(f"max_depth must be >= 0, got {max_depth}")
        if type not in ("file", "dir", "all"):
            raise ToolError(f"type must be 'file', 'dir', or 'all', got {type!r}")
        includes, excludes = parse_filter(filter)

        if base.is_file():
            rel = str(base.relative_to(root))
            ok = type != "dir" and glob_match(rel, includes, excludes)
            items = [{"path": rel, "kind": "file"}] if ok else []
            return json_output({"items": items}, self.valves.max_bytes)

        args = ["fd", "--color", "never"]
        if max_depth is not None:
            args += ["--max-depth", str(max_depth)]
        if type == "file":
            args += ["--type", "f"]
        elif type == "dir":
            args += ["--type", "d"]
        for p in includes:
            args += ["--glob", p]
        for p in excludes:
            args += ["--exclude", p]
        if includes:
            # With --glob present, fd treats the lone positional as PATH.
            args.append(str(base))
        else:
            # Otherwise an absolute PATH alone is mistaken for a pattern; the
            # match-all pattern "." makes fd take the second positional as PATH.
            args += [".", str(base)]
        res = await run_allowed(args, TIMEOUT_SEARCH)
        if res.returncode == 2:
            raise ToolError(f"list failed: {repo}", cause=trim_cause(res.stderr))

        items = []
        for line in res.stdout.splitlines():
            if not line.strip():
                continue
            p = Path(line)
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                rel = p.as_posix()
            items.append({"path": rel, "kind": "dir" if p.is_dir() else "file"})
        items.sort(key=lambda i: i["path"])
        data: dict = {"items": items}
        if len(items) > self.valves.max_results:
            data["truncated"] = {"shown": self.valves.max_results, "total": len(items)}
            data["items"] = items[: self.valves.max_results]
        return json_output(data, self.valves.max_bytes)

    # ------------------------------------------------------------------
    # read_file
    # ------------------------------------------------------------------

    async def read_file(
        self,
        repo: str,
        path: str,
        start: Optional[int] = None,
        end: Optional[int] = None,
    ) -> str:
        """Read a text file, or a line range of it, from a repository.

        Use to inspect file contents. Returns raw text (no line numbers, no
        headers). Binary and non-UTF-8 files are rejected with a clear error.

        :param repo: "<owner>/<name>" of an already-cloned repository.
        :param path: Path to the file, relative to the repository root (required).
        :param start: Optional 1-based first line to read (inclusive).
        :param end: Optional 1-based last line to read (inclusive).
        """
        try:
            return await self._read_file(repo, path, start, end)
        except Exception as e:
            return error_string(e)

    async def _read_file(
        self,
        repo: str,
        path: str,
        start: Optional[int],
        end: Optional[int],
    ) -> str:
        repos_path = resolve_repos_path(self.valves.repos_path)
        root = resolve_repo_root(repo, repos_path)
        self._ensure_repo_exists(root, repo)
        file_path = resolve_path(repo, path, repos_path)
        if not file_path.exists():
            raise ToolError(f"file not found: {path} in {repo}", kind="not_found")
        if file_path.is_dir():
            raise ToolError(f"{path} is a directory, not a file")
        size = os.path.getsize(file_path)
        if size > MAX_READ_BYTES:
            raise ToolError(f"file too large: {path} ({size} bytes); maximum supported is {MAX_READ_BYTES}")

        # Binary detection on a sample (DESIGN.md §7 Phase 2): null bytes or a
        # failed UTF-8 decode mark the file as binary / non-text.
        with open(file_path, "rb") as f:
            sample = f.read(8192)
        if b"\x00" in sample:
            raise ToolError(f"binary file not supported: {path} (binary files are rejected)")
        try:
            sample.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolError(f"not a UTF-8 text file: {path} (only UTF-8 text is supported)")

        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            total = sum(1 for _ in f)

        start_ = start if start is not None else 1
        if start_ < 1:
            raise ToolError(f"start must be >= 1, got {start_}")
        if start_ > total:
            raise ToolError(f"start {start_} is beyond the end of the file ({total} lines)")
        end_ = end if end is not None else total
        if end_ < start_:
            raise ToolError(f"end {end_} is before start {start_}")
        end_ = min(end_, total)
        range_total = end_ - start_ + 1

        if range_total <= MAX_INLINE_LINES:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                lines = list(itertools.islice(f, start_ - 1, end_))
            return truncate_output("".join(lines), self.valves.max_lines, self.valves.max_bytes)

        # Large range: read only the lines that will be shown, then cap.
        shown = min(range_total, self.valves.max_lines)
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            head = list(itertools.islice(f, start_ - 1, start_ - 1 + shown))
        text = "".join(head)
        marker = f"\n... (truncated: showing {shown} of {range_total} lines)"
        result = text + marker
        if len(result.encode("utf-8")) > self.valves.max_bytes:
            bmarker = f"\n... (truncated: byte cap of {self.valves.max_bytes} reached)"
            budget = max(0, self.valves.max_bytes - len(bmarker.encode("utf-8")) - 1)
            result = _trim_bytes(text, budget) + bmarker
        return result

    # ------------------------------------------------------------------
    # search_text
    # ------------------------------------------------------------------

    async def search_text(
        self,
        repo: str,
        query: str,
        path: Optional[str] = None,
        filter: Optional[str] = None,
        context: Optional[int] = None,
        case_sensitive: bool = False,
    ) -> str:
        """Search repository contents with ripgrep and return matches as JSON.

        Use to find where text or symbols appear. The query is a ripgrep
        regular expression. Returns one item per match with path, line, and
        matched text (plus optional context lines).

        :param repo: "<owner>/<name>" of an already-cloned repository.
        :param query: Ripgrep regular expression to search for (required).
        :param path: Optional subdirectory or file to narrow the search.
        :param filter: Optional space-separated glob patterns; a leading "!" excludes.
        :param context: Optional number of context lines around each match.
        :param case_sensitive: Optional; default False (case-insensitive).
        """
        try:
            return await self._search_text(repo, query, path, filter, context, case_sensitive)
        except Exception as e:
            return error_string(e)

    async def _search_text(
        self,
        repo: str,
        query: str,
        path: Optional[str],
        filter: Optional[str],
        context: Optional[int],
        case_sensitive: bool,
    ) -> str:
        repos_path = resolve_repos_path(self.valves.repos_path)
        root = resolve_repo_root(repo, repos_path)
        self._ensure_repo_exists(root, repo)
        if not query or not query.strip():
            raise ToolError("query must not be empty")
        if context is not None and context < 0:
            raise ToolError(f"context must be >= 0, got {context}")
        base = resolve_path(repo, path, repos_path) if path else root
        if not base.exists():
            raise ToolError(f"path not found: {path} in {repo}", kind="not_found")
        includes, excludes = parse_filter(filter)

        args = ["rg", "--json", "--no-config", "--color", "never", "-n"]
        if not case_sensitive:
            args += ["-i"]
        if context is not None:
            args += ["-C", str(context)]
        for p in includes:
            args += ["--glob", p]
        for p in excludes:
            args += ["--glob", f"!{p}"]
        args += ["--", query, str(base)]
        res = await run_allowed(args, TIMEOUT_SEARCH)
        if res.returncode == 2:
            raise ToolError(f"search failed: {repo}", cause=trim_cause(res.stderr))

        items = []
        current_item = None
        pending: List[str] = []
        for line in res.stdout.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            t = obj.get("type")
            data = obj.get("data") or {}
            if t == "match":
                p = (data.get("path") or {}).get("text", "")
                ln = data.get("line_number")
                txt = ((data.get("lines") or {}).get("text") or "").rstrip("\n")
                item: dict = {"path": p, "line": ln, "text": txt}
                if pending:
                    item["context"] = pending
                    pending = []
                items.append(item)
                current_item = item
            elif t == "context":
                ln = data.get("line_number")
                txt = ((data.get("lines") or {}).get("text") or "").rstrip("\n")
                ctx = f"{ln}: {txt}" if ln is not None else txt
                if current_item is not None:
                    current_item.setdefault("context", []).append(ctx)
                else:
                    pending.append(ctx)
            elif t in ("begin", "end"):
                current_item = None
                pending = []

        for it in items:
            try:
                it["path"] = str(Path(it["path"]).relative_to(root))
            except ValueError:
                pass
        items.sort(key=lambda i: (i["path"], i.get("line") or 0))
        data: dict = {"items": items}
        if len(items) > self.valves.max_results:
            data["truncated"] = {"shown": self.valves.max_results, "total": len(items)}
            data["items"] = items[: self.valves.max_results]
        return json_output(data, self.valves.max_bytes)

    # ------------------------------------------------------------------
    # shared internals
    # ------------------------------------------------------------------

    def _ensure_repo_exists(self, root: Path, repo: str) -> None:
        if not root.is_dir() or not (root / ".git").exists():
            raise ToolError(
                f"repository not cloned yet: {repo} (use clone_repo first)",
                kind="not_found",
            )
