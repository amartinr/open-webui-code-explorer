"""
title: Code Explorer - Files & Search
description: List, read, and search files in cloned repositories for the meta model. Read-only with respect to source code; never modifies repository contents. Pure-Python implementation: no external fd/rg binaries required.
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
    # cexp_list_files
    # ------------------------------------------------------------------

    async def cexp_list_files(
        self,
        repo: str,
        path: Optional[str] = None,
        max_depth: Optional[int] = None,
        filter: Optional[str] = None,
        type: Optional[str] = "all",
    ) -> str:
        """List files and directories under a path in a repository.

        Use to explore repository structure before reading files. Returns a
        JSON object with an items array of {"path", "kind"} entries, relative
        to the repository root and sorted. Respects .gitignore (via the
        pathspec package when available); hidden files are not shown by
        default.

        :param repo: "<owner>/<name>" of an already-cloned repository.
        :param path: Optional subdirectory or file, relative to the repository root; do NOT include the "<owner>/<name>" prefix (that belongs in `repo`); always use "/" as separator (e.g. "src/main.py"); defaults to the repository root.
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

        items = []
        if base.is_dir():
            for rel, is_dir in _walk_repo(root, base, max_depth):
                if is_dir and type == "file":
                    continue
                if not is_dir and type == "dir":
                    continue
                if not glob_match(rel, includes, excludes):
                    continue
                if not is_dir and rel.startswith("."):
                    continue
                items.append({"path": rel, "kind": "dir" if is_dir else "file"})
        items.sort(key=lambda i: i["path"])
        data: dict = {"items": items}
        if len(items) > self.valves.max_results:
            data["truncated"] = {"shown": self.valves.max_results, "total": len(items)}
            data["items"] = items[: self.valves.max_results]
        return json_output(data, self.valves.max_bytes)

    # ------------------------------------------------------------------
    # cexp_read_file
    # ------------------------------------------------------------------

    async def cexp_read_file(
        self,
        repo: str,
        path: str,
        start: Optional[int] = None,
        end: Optional[int] = None,
    ) -> str:
        """Read a text file, or a line range of it, from a repository.

        Use to inspect file contents. Example: cexp_read_file("owner/repo",
        "src/main.py"). Accepts an optional 1-based start/end line range.
        Returns raw text (no line numbers, no headers); a trailing marker is
        appended when output is truncated. Binary and non-UTF-8 files are
        rejected with a clear error. Note: the output is raw data - when
        presenting file content to the user, render it as a fenced markdown
        code block with a language tag.

        :param repo: "<owner>/<name>" of an already-cloned repository.
        :param path: File path INSIDE the repository, relative to its root; do NOT include the "<owner>/<name>" prefix (that belongs in `repo`); always use "/" as separator. Examples: "README.md", "src/main.py", "tests/test_x.py" (required).
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

        # Binary detection over the WHOLE file (DESIGN.md §7 Phase 2): a null
        # byte or failed strict UTF-8 decode anywhere marks the file as
        # binary / non-text. The old 8 KB sample missed bytes past the sample.
        bad = _scan_encoding(file_path)
        if bad == "binary":
            raise ToolError(f"binary file not supported: {path} (binary files are rejected)")
        if bad == "invalid_utf8":
            raise ToolError(f"not a UTF-8 text file: {path} (only UTF-8 text is supported)")

        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            total = sum(1 for _ in f)

        start_ = start if start is not None else 1
        if start_ < 1:
            raise ToolError(f"start must be >= 1, got {start_}")
        if total == 0:
            # Empty file: nothing to read. A default start (1) is fine; an
            # explicit start > 1 is out of range.
            if start is not None and start > 1:
                raise ToolError(f"start {start} is beyond the end of the file (0 lines)")
            return ""
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
    # cexp_search_text
    # ------------------------------------------------------------------

    async def cexp_search_text(
        self,
        repo: str,
        query: str,
        path: Optional[str] = None,
        filter: Optional[str] = None,
        context: Optional[int] = None,
        case_sensitive: bool = False,
    ) -> str:
        """Search repository contents with a pure-Python regex engine.

        Use to find where text or symbols appear. The query is a regular
        expression. Returns one item per match with path, line, and matched
        text (plus optional context lines). Honors .gitignore.

        :param repo: "<owner>/<name>" of an already-cloned repository.
        :param query: Regular expression to search for (required).
        :param path: Optional subdirectory or file to narrow the search, relative to the repository root; do NOT include the "<owner>/<name>" prefix; always use "/" as separator (e.g. "src/").
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
        pattern = _compile_pattern(query, case_sensitive)

        items = []
        for rel_f, fp in self._iter_text_files(root, base, includes, excludes):
            _, text = _binary_or_readable(fp)
            _search_in_text(pattern, text, 0, context or 0, rel_f, items)

        items.sort(key=lambda i: (i["path"], i.get("line") or 0))
        data: dict = {"items": items}
        if len(items) > self.valves.max_results:
            data["truncated"] = {"shown": self.valves.max_results, "total": len(items)}
            data["items"] = items[: self.valves.max_results]
        return json_output(data, self.valves.max_bytes)

    # ------------------------------------------------------------------
    # cexp_search_symbol
    # ------------------------------------------------------------------

    async def cexp_search_symbol(
        self,
        repo: str,
        query: str,
        path: Optional[str] = None,
        filter: Optional[str] = None,
    ) -> str:
        """Find definitions of a symbol (function, class, method, constant).

        Use to locate where a symbol is DEFINED rather than mentioned. Searches
        with language-aware patterns for definitions (def/class/fn/func/type/
        struct/enum/impl/interface/module/const/var/let... plus top-level
        `NAME =` assignments). Returns a JSON object with an items array of
        {"path", "line", "text"}. Case-sensitive by default, since
        identifiers are case-sensitive in virtually every language. Heuristic,
        not a full parser: expect occasional false positives/negatives on
        exotic syntax.

        :param repo: "<owner>/<name>" of an already-cloned repository.
        :param query: Symbol name or partial (required).
        :param path: Optional subdirectory or file to narrow the search, relative to the repository root; do NOT include the "<owner>/<name>" prefix; always use "/" as separator (e.g. "src/").
        :param filter: Optional space-separated glob patterns; a leading "!" excludes.
        """
        try:
            return await self._search_symbol(repo, query, path, filter)
        except Exception as e:
            return error_string(e)

    async def _search_symbol(
        self,
        repo: str,
        query: str,
        path: Optional[str],
        filter: Optional[str],
    ) -> str:
        repos_path = resolve_repos_path(self.valves.repos_path)
        root = resolve_repo_root(repo, repos_path)
        self._ensure_repo_exists(root, repo)
        if not query or not query.strip():
            raise ToolError("query must not be empty")
        base = resolve_path(repo, path, repos_path) if path else root
        if not base.exists():
            raise ToolError(f"path not found: {path} in {repo}", kind="not_found")
        includes, excludes = parse_filter(filter)

        # Definition patterns: a keyword followed by the symbol name, or a
        # top-level `NAME =` assignment (constants). Case-sensitive (symbols).
        q = re.escape(query.strip())
        _def_keywords = (
            "def|class|fn|func|function|type|struct|enum|trait|impl|interface|"
            "module|sub|procedure|macro|const|var|let|public|private|protected"
        )
        _modifiers = (
            "async|export|default|pub|static|suspend|extern|inline|final|"
            "abstract|override|virtual|sealed|readonly"
        )
        # Optional, repeatable visibility/async modifiers before the keyword
        # (async def, pub fn, export default class, public static void), then
        # an optional receiver (Go: func (r *R) Method()), then the name.
        pattern = _compile_pattern(
            rf"^\s*(?:(?:{_modifiers}|{_def_keywords})\s+)*"
            rf"(?:\([^)]*\)\s*)?{q}\w*(?:\s*[(:={{]|\b)",
            case_sensitive=True,
            multiline=True,
        )

        items = []
        for rel_f, fp in self._iter_text_files(root, base, includes, excludes):
            _, text = _binary_or_readable(fp)
            _search_in_text(pattern, text, 0, 0, rel_f, items)

        items.sort(key=lambda i: (i["path"], i.get("line") or 0))
        data: dict = {"items": items}
        if len(items) > self.valves.max_results:
            data["truncated"] = {"shown": self.valves.max_results, "total": len(items)}
            data["items"] = items[: self.valves.max_results]
        return json_output(data, self.valves.max_bytes)

    # ------------------------------------------------------------------
    # shared internals
    # ------------------------------------------------------------------

    def _iter_text_files(self, root: Path, base: Path, includes: List[str], excludes: List[str]):
        """Yield (rel_path, Path) for every text file under `base` that passes
        the .gitignore and filter globs. Shared by cexp_search_text/cexp_search_symbol."""
        spec = _load_ignore_spec(root)
        if base.is_file():
            rel_f = _try_decode_rel(base, root)
            if not glob_match(rel_f, includes, excludes):
                return
            if _is_binary_path(base):
                return
            yield rel_f, base
            return
        for dirpath, dirnames, filenames in os.walk(base):
            dp = Path(dirpath)
            dirnames[:] = [d for d in dirnames if d not in (".git", ".hg", ".svn")]
            try:
                rel_dp = dp.relative_to(root).as_posix()
            except ValueError:
                rel_dp = dp.as_posix()
            if rel_dp != ".":
                parts = rel_dp.split("/")
                if any(p in (".git", ".hg", ".svn") for p in parts):
                    dirnames[:] = []
                    continue
                if _ignore_match(spec, rel_dp, True):
                    dirnames[:] = []
                    continue
            kept = []
            for d in dirnames:
                rel_d = rel_dp + "/" + d if rel_dp != "." else d
                if not _ignore_match(spec, rel_d, True):
                    kept.append(d)
            dirnames[:] = kept
            for fname in filenames:
                rel_f = rel_dp + "/" + fname if rel_dp != "." else fname
                if _ignore_match(spec, rel_f, False):
                    continue
                if not glob_match(rel_f, includes, excludes):
                    continue
                fp = dp / fname
                if _is_binary_path(fp):
                    continue
                yield rel_f, fp

    def _ensure_repo_exists(self, root: Path, repo: str) -> None:
        if not root.is_dir() or not (root / ".git").exists():
            raise ToolError(
                f"repository not cloned yet: {repo} (use cexp_clone_repo first)",
                kind="not_found",
            )
