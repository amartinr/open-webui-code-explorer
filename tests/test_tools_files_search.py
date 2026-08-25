"""Integration tests for the Phase 2 Files & Search script
(cexp_list_files, cexp_read_file, cexp_search_text).

All tests operate on a local file:// repository created on the fly, so no
network access is needed.
"""

import inspect
import json
import types
from pathlib import Path

import pytest

from common import git_args, run_allowed
from dist.files_search import Tools
from dist.repos import Tools as ReposTools

IDENT = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]

HELLO_PY = "def hello():\n    return 'hi'\n\n\ndef world(x):\n    return x * 2\n"
WORLD_MD = "# Title\n\nSome *markdown* text.\n"
DEEP_PY = "class Deep:\n    pass\n"


async def run_git(cwd: Path, *args: str):
    return await run_allowed(git_args("-C", str(cwd), *args), 60)


async def init_repo(path: Path) -> Path:
    """Create a local git repo with code, markdown, a binary, and a big file."""
    path.mkdir(parents=True, exist_ok=True)
    res = await run_git(path, "init", "-b", "main")
    assert res.returncode == 0, res.stderr

    (path / "hello.py").write_text(HELLO_PY)
    (path / "world.md").write_text(WORLD_MD)
    (path / "sub").mkdir()
    (path / "sub" / "deep.py").write_text(DEEP_PY)
    (path / "data.bin").write_bytes(b"\x00\x01\x02binary\x00data")
    (path / "big.txt").write_text("\n".join(f"line{i:04d}" for i in range(6000)) + "\n")

    res = await run_git(path, "add", "-A")
    assert res.returncode == 0
    res = await run_git(path, *IDENT, "commit", "-m", "init")
    assert res.returncode == 0
    return path


@pytest.fixture
async def source_repo(tmp_path):
    return await init_repo(tmp_path / "src")


@pytest.fixture
def repos_path(tmp_path):
    return tmp_path / "repos"


def make_tools(repos_path: Path) -> Tools:
    tools = Tools()
    tools.valves.repos_path = str(repos_path)
    return tools


def parse_json(out: str) -> dict:
    return json.loads(out)


async def clone_source(repos_path: Path, source: Path, name: str = "testowner/testrepo") -> Tools:
    """Clone with the Repos script tools, then hand the Files & Search tools."""
    repos_tools = ReposTools()
    repos_tools.valves.repos_path = str(repos_path)
    out = await repos_tools.cexp_clone_repo(name, url=f"file://{source}")
    assert not out.startswith("Error:"), out
    return make_tools(repos_path)


# ---------------------------------------------------------------------------
# cexp_list_files
# ---------------------------------------------------------------------------


class TestListFiles:
    async def test_lists_structure_at_root(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_list_files("testowner/testrepo")

        result = parse_json(out)
        by_path = {i["path"]: i["kind"] for i in result["items"]}
        assert by_path["hello.py"] == "file"
        assert by_path["world.md"] == "file"
        assert by_path["sub"] == "dir"
        assert by_path["data.bin"] == "file"
        assert by_path["big.txt"] == "file"
        assert "truncated" not in result
        # sorted
        assert result["items"] == sorted(result["items"], key=lambda i: i["path"])

    async def test_type_file(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.cexp_list_files("testowner/testrepo", type="file"))
        assert all(i["kind"] == "file" for i in result["items"])
        assert "sub" not in [i["path"] for i in result["items"]]

    async def test_type_dir(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.cexp_list_files("testowner/testrepo", type="dir"))
        assert all(i["kind"] == "dir" for i in result["items"])
        assert [i["path"] for i in result["items"]] == ["sub"]

    async def test_max_depth(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.cexp_list_files("testowner/testrepo", max_depth=1))
        paths = {i["path"] for i in result["items"]}
        assert "hello.py" in paths
        assert "sub" in paths
        assert "sub/deep.py" not in paths

    async def test_filter_include_glob(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.cexp_list_files("testowner/testrepo", filter="*.py"))
        paths = [i["path"] for i in result["items"]]
        assert "hello.py" in paths
        assert "sub/deep.py" in paths
        assert "world.md" not in paths
        assert "data.bin" not in paths

    async def test_filter_exclude_glob(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.cexp_list_files("testowner/testrepo", filter="!*.md"))
        paths = [i["path"] for i in result["items"]]
        assert "world.md" not in paths
        assert "hello.py" in paths

    async def test_subdirectory_path_relative_to_root(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.cexp_list_files("testowner/testrepo", path="sub"))
        assert [i["path"] for i in result["items"]] == ["sub/deep.py"]

    async def test_single_file_path(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.cexp_list_files("testowner/testrepo", path="hello.py"))
        assert [i["path"] for i in result["items"]] == ["hello.py"]
        # file path with a filter that excludes it -> empty
        result = parse_json(
            await tools.cexp_list_files("testowner/testrepo", path="hello.py", filter="*.md")
        )
        assert result["items"] == []

    async def test_path_not_found(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_list_files("testowner/testrepo", path="nope")
        assert out.startswith("Not found:")

    async def test_path_traversal_rejected(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        for bad in ["../evil", "..", "/etc"]:
            out = await tools.cexp_list_files("testowner/testrepo", path=bad)
            assert out.startswith("Error:"), bad

    async def test_invalid_type_and_depth(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_list_files("testowner/testrepo", type="bogus")
        assert out.startswith("Error:")
        out = await tools.cexp_list_files("testowner/testrepo", max_depth=-1)
        assert out.startswith("Error:")

    async def test_max_results_cap(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        tools.valves.max_results = 2
        result = parse_json(await tools.cexp_list_files("testowner/testrepo"))
        assert len(result["items"]) == 2
        assert result["truncated"]["total"] >= 2

    async def test_nested_gitignore_honored(self, repos_path, source_repo):
        """A .gitignore in a subdirectory applies relative to that subdir,
        like git/fd/ripgrep (DESIGN.md §7 Phase 2)."""
        tools = await clone_source(repos_path, source_repo)
        clone = repos_path / "testowner" / "testrepo"
        (clone / "sub" / ".gitignore").write_text("*.gen\n")
        (clone / "sub" / "a.gen").write_text("sentinel_xyz\n")

        result = parse_json(await tools.cexp_list_files("testowner/testrepo"))
        paths = [i["path"] for i in result["items"]]
        assert "sub/a.gen" not in paths
        assert "sub/deep.py" in paths

        # cexp_search_text must also skip ignored files.
        result = parse_json(await tools.cexp_search_text("testowner/testrepo", "sentinel_xyz"))
        assert result["items"] == []

    async def test_nested_gitignore_negation(self, repos_path, source_repo):
        """Negation in a nested .gitignore (git semantics)."""
        tools = await clone_source(repos_path, source_repo)
        clone = repos_path / "testowner" / "testrepo"
        (clone / "sub" / ".gitignore").write_text("*.gen\n!keep.gen\n")
        (clone / "sub" / "a.gen").write_text("gen\n")
        (clone / "sub" / "keep.gen").write_text("keep\n")

        result = parse_json(await tools.cexp_list_files("testowner/testrepo"))
        paths = [i["path"] for i in result["items"]]
        assert "sub/a.gen" not in paths
        assert "sub/keep.gen" in paths

    async def test_repo_not_cloned(self, repos_path):
        tools = make_tools(repos_path)
        out = await tools.cexp_list_files("o/r")
        assert out.startswith("Not found:")
        assert "not cloned yet" in out


# ---------------------------------------------------------------------------
# cexp_read_file
# ---------------------------------------------------------------------------


class TestReadFile:
    async def test_read_full_file(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_read_file("testowner/testrepo", "hello.py")
        assert out == HELLO_PY

    async def test_read_line_range(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_read_file("testowner/testrepo", "hello.py", start=2, end=3)
        assert out == "    return 'hi'\n\n"

    async def test_range_beyond_eof_clamped(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        # hello.py has 6 lines; lines 3-4 are empty.
        out = await tools.cexp_read_file("testowner/testrepo", "hello.py", start=4, end=999)
        assert out == "\ndef world(x):\n    return x * 2\n"

    async def test_binary_rejected(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_read_file("testowner/testrepo", "data.bin")
        assert out.startswith("Error:")
        assert "binary" in out.lower()

    async def test_empty_file_returns_empty(self, repos_path, source_repo):
        """Regression: an empty file (0 lines) must return '', not fail with
        'start 1 is beyond the end of the file'."""
        tools = await clone_source(repos_path, source_repo)
        clone = repos_path / "testowner" / "testrepo"
        (clone / "empty.txt").write_text("")
        out = await tools.cexp_read_file("testowner/testrepo", "empty.txt")
        assert out == ""
        # Explicit start=1 on an empty file is also fine; start>1 is a real error.
        assert await tools.cexp_read_file("testowner/testrepo", "empty.txt", start=1) == ""
        out = await tools.cexp_read_file("testowner/testrepo", "empty.txt", start=2)
        assert out.startswith("Error:")
        assert "0 lines" in out

    async def test_binary_bytes_past_sample_rejected(self, repos_path, source_repo):
        """Regression: a null byte AFTER the old 8 KB sample must still be
        rejected (the old sample check silently returned corrupted text)."""
        tools = await clone_source(repos_path, source_repo)
        clone = repos_path / "testowner" / "testrepo"
        with open(clone / "late_bin.bin", "wb") as f:
            f.write(b"A" * 9000 + b"\x00\xff")
        out = await tools.cexp_read_file("testowner/testrepo", "late_bin.bin")
        assert out.startswith("Error:")
        assert "binary" in out.lower()

    async def test_invalid_utf8_past_sample_rejected(self, repos_path, source_repo):
        """Regression: invalid UTF-8 AFTER the old 8 KB sample must be rejected
        (not silently replaced with U+FFFD)."""
        tools = await clone_source(repos_path, source_repo)
        clone = repos_path / "testowner" / "testrepo"
        with open(clone / "late_invalid.txt", "wb") as f:
            f.write(b"B" * 9000 + b"\xff\xfe")
        out = await tools.cexp_read_file("testowner/testrepo", "late_invalid.txt")
        assert out.startswith("Error:")
        assert "UTF-8" in out

    async def test_multibyte_across_chunk_boundary(self, repos_path, source_repo):
        """A multibyte UTF-8 char straddling the scan chunk boundary must not
        cause a false 'invalid UTF-8' (incremental decoder handles it)."""
        tools = await clone_source(repos_path, source_repo)
        clone = repos_path / "testowner" / "testrepo"
        with open(clone / "straddle.txt", "wb") as f:
            f.write(b"C" * 65535 + "é".encode("utf-8"))
        tools.valves.max_bytes = 200000  # line is ~65 KB; keep the é visible
        out = await tools.cexp_read_file("testowner/testrepo", "straddle.txt", start=1, end=1)
        assert not out.startswith("Error:")
        assert "é" in out

    async def test_file_not_found(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_read_file("testowner/testrepo", "missing.py")
        assert out.startswith("Not found:")

    async def test_directory_rejected(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_read_file("testowner/testrepo", "sub")
        assert out.startswith("Error:")
        assert "directory" in out

    async def test_invalid_start(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_read_file("testowner/testrepo", "hello.py", start=0)
        assert out.startswith("Error:")
        out = await tools.cexp_read_file("testowner/testrepo", "hello.py", start=-3)
        assert out.startswith("Error:")

    async def test_start_beyond_eof(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_read_file("testowner/testrepo", "hello.py", start=100)
        assert out.startswith("Error:")
        assert "beyond" in out

    async def test_end_before_start(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_read_file("testowner/testrepo", "hello.py", start=3, end=1)
        assert out.startswith("Error:")

    async def test_path_traversal_rejected(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        for bad in ["../outside.txt", "/etc/passwd"]:
            out = await tools.cexp_read_file("testowner/testrepo", bad)
            assert out.startswith("Error:"), bad

    async def test_line_cap_with_marker(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        tools.valves.max_lines = 5
        out = await tools.cexp_read_file("testowner/testrepo", "big.txt")
        lines = out.splitlines()
        assert lines[0] == "line0000"
        assert any("truncated" in l for l in lines)
        assert any("of 6000 lines" in l for l in lines)

    async def test_byte_cap_with_marker(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        tools.valves.max_bytes = 300
        out = await tools.cexp_read_file("testowner/testrepo", "big.txt")
        assert len(out.encode("utf-8")) <= 300 + 200  # marker may add a bit
        assert "truncated" in out

    async def test_repo_not_cloned(self, repos_path):
        tools = make_tools(repos_path)
        out = await tools.cexp_read_file("o/r", "x.py")
        assert out.startswith("Not found:")


# ---------------------------------------------------------------------------
# cexp_read_file at a ref (DESIGN.md §12.1)
# ---------------------------------------------------------------------------


class TestReadFileAtRef:
    async def test_ref_defaults_to_working_tree_parity(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        no_ref = await tools.cexp_read_file("testowner/testrepo", "hello.py")
        at_main = await tools.cexp_read_file("testowner/testrepo", "hello.py", ref="main")
        assert no_ref == at_main == HELLO_PY

    async def test_read_at_tag_differs_from_working_tree(self, repos_path, source_repo):
        """Tag the first commit, then advance the clone with a second commit
        that changes hello.py: ref reads must return the tagged (old) content
        while the working tree has the new content."""
        tools = await clone_source(repos_path, source_repo)
        clone = repos_path / "testowner" / "testrepo"
        await run_git(clone, "tag", "v1.0.0")
        (clone / "hello.py").write_text("def hello():\n    return 'new'\n")
        await run_git(clone, "add", "hello.py")
        await run_git(clone, *IDENT, "commit", "-m", "change hello")

        out_old = await tools.cexp_read_file("testowner/testrepo", "hello.py", ref="v1.0.0")
        assert out_old == HELLO_PY
        out_new = await tools.cexp_read_file("testowner/testrepo", "hello.py", ref="main")
        assert out_new == "def hello():\n    return 'new'\n"
        # working tree == main (the checkout advanced with the commit)
        assert await tools.cexp_read_file("testowner/testrepo", "hello.py") == out_new

    async def test_line_range_at_ref(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_read_file("testowner/testrepo", "hello.py", ref="main", start=1, end=1)
        assert out == "def hello():\n"
        out = await tools.cexp_read_file("testowner/testrepo", "hello.py", ref="main", start=5, end=6)
        assert out == "def world(x):\n    return x * 2\n"

    async def test_unknown_ref_error_names_ref(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_read_file("testowner/testrepo", "hello.py", ref="v9.9.9")
        assert out.startswith("Error:")
        assert "v9.9.9" in out

    async def test_missing_path_at_ref_not_found(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_read_file("testowner/testrepo", "nope.py", ref="main")
        assert out.startswith("Not found:")

    async def test_directory_at_ref_rejected(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_read_file("testowner/testrepo", "sub", ref="main")
        assert out.startswith("Error:")
        assert "directory" in out

    async def test_binary_blob_at_ref_rejected(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_read_file("testowner/testrepo", "data.bin", ref="main")
        assert out.startswith("Error:")
        assert "binary" in out.lower()

    async def test_invalid_utf8_blob_at_ref_rejected(self, repos_path, source_repo):
        """Invalid UTF-8 in the blob must be rejected with the same message as
        the working-tree reader (DESIGN.md §12.1: same binary/UTF-8 scan)."""
        tools = await clone_source(repos_path, source_repo)
        clone = repos_path / "testowner" / "testrepo"
        (clone / "bad.txt").write_bytes(b"B" * 9000 + b"\xff\xfe")
        await run_git(clone, "add", "bad.txt")
        await run_git(clone, *IDENT, "commit", "-m", "add bad")
        out = await tools.cexp_read_file("testowner/testrepo", "bad.txt", ref="main")
        assert out.startswith("Error:")
        assert "UTF-8" in out

    async def test_utf8_multibyte_at_ref_preserved(self, repos_path, source_repo):
        """Non-ASCII content must survive the blob read byte-for-byte (the
        reason text=False exists: locale decoding would corrupt it)."""
        tools = await clone_source(repos_path, source_repo)
        clone = repos_path / "testowner" / "testrepo"
        (clone / "acc.txt").write_text("café\nmañana\n", encoding="utf-8")
        await run_git(clone, "add", "acc.txt")
        await run_git(clone, *IDENT, "commit", "-m", "add accents")
        out = await tools.cexp_read_file("testowner/testrepo", "acc.txt", ref="main")
        assert out == "café\nmañana\n"

    async def test_truncation_marker_at_ref(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        tools.valves.max_lines = 5
        out = await tools.cexp_read_file("testowner/testrepo", "big.txt", ref="main")
        lines = out.splitlines()
        assert lines[0] == "line0000"
        assert any("truncated" in l for l in lines)
        assert any("of 6000 lines" in l for l in lines)

    async def test_malicious_refs_rejected_before_git(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        for bad in ["--all", "HEAD~1", "a..b", "main^"]:
            out = await tools.cexp_read_file("testowner/testrepo", "hello.py", ref=bad)
            assert out.startswith("Error:"), bad
            assert "invalid ref" in out, bad

    async def test_repo_not_cloned(self, repos_path):
        tools = make_tools(repos_path)
        out = await tools.cexp_read_file("o/r", "x.py", ref="main")
        assert out.startswith("Not found:")


# ---------------------------------------------------------------------------
# cexp_search_text
# ---------------------------------------------------------------------------


class TestSearchText:
    async def test_finds_matches_with_line_numbers(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_search_text("testowner/testrepo", "def ")
        result = parse_json(out)
        items = result["items"]
        assert len(items) == 2
        by = {(i["path"], i["line"]): i["text"] for i in items}
        assert by[("hello.py", 1)] == "def hello():"
        assert by[("hello.py", 5)] == "def world(x):"

        out = await tools.cexp_search_text("testowner/testrepo", "class ")
        result = parse_json(out)
        assert len(result["items"]) == 1
        assert result["items"][0]["path"] == "sub/deep.py"
        assert result["items"][0]["text"] == "class Deep:"

    async def test_no_matches_empty_items(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.cexp_search_text("testowner/testrepo", "zzzz_nothing_zzzz"))
        assert result["items"] == []

    async def test_case_sensitivity(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        # insensitive by default: matches lowercase and uppercase
        insensitive = parse_json(await tools.cexp_search_text("testowner/testrepo", "HELLO"))
        assert len(insensitive["items"]) >= 1
        sensitive = parse_json(
            await tools.cexp_search_text("testowner/testrepo", "HELLO", case_sensitive=True)
        )
        assert sensitive["items"] == []
        exact = parse_json(
            await tools.cexp_search_text("testowner/testrepo", "hello", case_sensitive=True)
        )
        assert len(exact["items"]) >= 1

    async def test_context_lines(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.cexp_search_text("testowner/testrepo", "return 'hi'", context=1))
        item = result["items"][0]
        assert item["line"] == 2
        assert "context" in item
        assert any("def hello" in c for c in item["context"])

    async def test_filter_globs(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        py = parse_json(await tools.cexp_search_text("testowner/testrepo", "hello", filter="*.py"))
        md = parse_json(await tools.cexp_search_text("testowner/testrepo", "hello", filter="*.md"))
        assert len(py["items"]) >= 1
        assert md["items"] == []
        # exclusion
        not_md = parse_json(
            await tools.cexp_search_text("testowner/testrepo", "hello", filter="!*.md")
        )
        assert len(not_md["items"]) >= 1

    async def test_path_narrowing(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.cexp_search_text("testowner/testrepo", "Deep", path="sub"))
        assert len(result["items"]) == 1
        assert result["items"][0]["path"] == "sub/deep.py"

    async def test_regex_query(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.cexp_search_text("testowner/testrepo", r"def \w+\("))
        assert len(result["items"]) >= 2

    async def test_empty_query_rejected(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_search_text("testowner/testrepo", "   ")
        assert out.startswith("Error:")

    async def test_path_not_found(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_search_text("testowner/testrepo", "hello", path="nope")
        assert out.startswith("Not found:")

    async def test_path_traversal_rejected(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_search_text("testowner/testrepo", "hello", path="..")
        assert out.startswith("Error:")

    async def test_max_results_cap(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        tools.valves.max_results = 2
        result = parse_json(await tools.cexp_search_text("testowner/testrepo", "line00"))
        assert len(result["items"]) == 2
        assert result["truncated"]["total"] >= 2

    async def test_negative_context_rejected(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_search_text("testowner/testrepo", "hello", context=-1)
        assert out.startswith("Error:")

    async def test_repo_not_cloned(self, repos_path):
        tools = make_tools(repos_path)
        out = await tools.cexp_search_text("o/r", "hello")
        assert out.startswith("Not found:")


# ---------------------------------------------------------------------------
# cexp_search_symbol
# ---------------------------------------------------------------------------


class TestSearchSymbol:
    async def test_finds_definitions(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_search_symbol("testowner/testrepo", "hello")
        result = parse_json(out)
        items = result["items"]
        assert len(items) == 1
        assert items[0]["path"] == "hello.py"
        assert items[0]["line"] == 1
        assert items[0]["text"] == "def hello():"

    async def test_finds_class(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_search_symbol("testowner/testrepo", "Deep")
        result = parse_json(out)
        assert len(result["items"]) == 1
        assert result["items"][0]["path"] == "sub/deep.py"
        assert result["items"][0]["text"] == "class Deep:"

    async def test_partial_match(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_search_symbol("testowner/testrepo", "worl")
        result = parse_json(out)
        assert len(result["items"]) == 1
        assert result["items"][0]["text"] == "def world(x):"

    async def test_case_sensitive(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_search_symbol("testowner/testrepo", "HELLO")
        result = parse_json(out)
        assert result["items"] == []

    async def test_does_not_match_calls_or_mentions(self, repos_path, source_repo):
        """Definitions only: a call `hello()` must not match the definition."""
        tools = await clone_source(repos_path, source_repo)
        clone = repos_path / "testowner" / "testrepo"
        (clone / "use.py").write_text("x = hello()\n")
        out = await tools.cexp_search_symbol("testowner/testrepo", "hello")
        result = parse_json(out)
        # Only the def in hello.py, not the call in use.py.
        assert len(result["items"]) == 1
        assert result["items"][0]["path"] == "hello.py"

    async def test_empty_query_rejected(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_search_symbol("testowner/testrepo", "   ")
        assert out.startswith("Error:")

    async def test_path_narrowing(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.cexp_search_symbol("testowner/testrepo", "def", path="sub")
        result = parse_json(out)
        assert result["items"] == []  # no 'def' definition in sub
        out = await tools.cexp_search_symbol("testowner/testrepo", "Deep", path="sub")
        assert len(parse_json(out)["items"]) == 1

    async def test_finds_async_def(self, repos_path, source_repo):
        """Regression: `async def` must match (modifier before the keyword)."""
        tools = await clone_source(repos_path, source_repo)
        clone = repos_path / "testowner" / "testrepo"
        (clone / "asyncmod.py").write_text(
            "async def fetch_data():\n    pass\n\n"
            "async def run_task() -> dict:\n    return {}\n"
        )
        out = await tools.cexp_search_symbol("testowner/testrepo", "fetch_data")
        result = parse_json(out)
        assert len(result["items"]) == 1
        assert result["items"][0]["path"] == "asyncmod.py"
        assert result["items"][0]["text"] == "async def fetch_data():"

    async def test_finds_async_method_with_return_type(self, repos_path, source_repo):
        """Regression: indented `async def ... -> type:` must match."""
        tools = await clone_source(repos_path, source_repo)
        clone = repos_path / "testowner" / "testrepo"
        (clone / "classmod.py").write_text(
            "class Service:\n    async def run(self) -> dict:\n        return {}\n"
        )
        out = await tools.cexp_search_symbol("testowner/testrepo", "run")
        result = parse_json(out)
        assert len(result["items"]) == 1
        assert result["items"][0]["text"] == "    async def run(self) -> dict:"

    async def test_finds_export_function(self, repos_path, source_repo):
        """Regression: `export function` (JS/TS) must match."""
        tools = await clone_source(repos_path, source_repo)
        clone = repos_path / "testowner" / "testrepo"
        (clone / "util.js").write_text("export function helper() {}\n")
        out = await tools.cexp_search_symbol("testowner/testrepo", "helper")
        result = parse_json(out)
        assert len(result["items"]) == 1
        assert result["items"][0]["text"] == "export function helper() {}"

    async def test_finds_pub_fn(self, repos_path, source_repo):
        """Regression: `pub fn` (Rust) must match."""
        tools = await clone_source(repos_path, source_repo)
        clone = repos_path / "testowner" / "testrepo"
        (clone / "lib.rs").write_text("pub fn compute() -> u32 { 1 }\n")
        out = await tools.cexp_search_symbol("testowner/testrepo", "compute")
        result = parse_json(out)
        assert len(result["items"]) == 1
        assert result["items"][0]["text"] == "pub fn compute() -> u32 { 1 }"

    async def test_finds_go_receiver(self, repos_path, source_repo):
        """Regression: `func (r *R) Method()` (Go receiver) must match."""
        tools = await clone_source(repos_path, source_repo)
        clone = repos_path / "testowner" / "testrepo"
        (clone / "svc.go").write_text(
            "package svc\n\ntype R struct {}\n\nfunc (r *R) Method() int { return 0 }\n"
        )
        out = await tools.cexp_search_symbol("testowner/testrepo", "Method")
        result = parse_json(out)
        assert len(result["items"]) == 1
        assert result["items"][0]["text"] == "func (r *R) Method() int { return 0 }"

    async def test_async_call_does_not_match_definition(self, repos_path, source_repo):
        """Regression: an `await` call must NOT match (only definitions)."""
        tools = await clone_source(repos_path, source_repo)
        clone = repos_path / "testowner" / "testrepo"
        (clone / "useasync.py").write_text(
            "async def real():\n    pass\n\n"
            "result = await real()\n"
        )
        out = await tools.cexp_search_symbol("testowner/testrepo", "real")
        result = parse_json(out)
        # Only the definition line, not the `await real()` call.
        assert len(result["items"]) == 1
        assert result["items"][0]["line"] == 1


# ---------------------------------------------------------------------------
# Open WebUI loading contract (DESIGN.md §9.1, §9.6)
# ---------------------------------------------------------------------------


class TestOpenWebUILoading:
    def test_dist_script_loads_via_exec_and_discovers_tools(self):
        dist_file = Path(__file__).resolve().parent.parent / "dist" / "files_search.py"
        source = dist_file.read_text(encoding="utf-8")
        module = types.ModuleType("files_search_script")
        exec(compile(source, dist_file.name, "exec"), module.__dict__)

        tools = module.Tools()
        discovered = [
            func
            for func in dir(tools)
            if callable(getattr(tools, func))
            and not func.startswith("_")
            and not inspect.isclass(getattr(tools, func))
        ]
        # cexp_search_symbol was added in Phase 3 (§5.4).
        assert sorted(discovered) == ["cexp_list_files", "cexp_read_file", "cexp_search_symbol", "cexp_search_text"]
        for name in discovered:
            assert getattr(tools, name).__doc__

    def test_docstring_params_match_signature_and_are_single_line(self):
        import re

        param_pattern = re.compile(r":param (\w+):\s*(.+)")
        dist_file = Path(__file__).resolve().parent.parent / "dist" / "files_search.py"
        module = types.ModuleType("files_search_script")
        exec(compile(dist_file.read_text(encoding="utf-8"), dist_file.name, "exec"), module.__dict__)
        tools = module.Tools()

        for name in ["cexp_list_files", "cexp_read_file", "cexp_search_text"]:
            func = getattr(tools, name)
            doc = func.__doc__ or ""
            sig_params = set(inspect.signature(func).parameters)
            parsed = {}
            for line in doc.splitlines():
                m = param_pattern.match(line.strip())
                if m:
                    parsed[m.group(1)] = m.group(2)
            assert set(parsed) == sig_params, f"{name}: :param lines {set(parsed)} != signature {sig_params}"
            for pname, desc in parsed.items():
                assert desc[-1:] in ".)!", f"{name}:{pname} description truncated or unterminated: {desc!r}"

    def test_dist_script_is_self_contained(self):
        dist_file = Path(__file__).resolve().parent.parent / "dist" / "files_search.py"
        source = dist_file.read_text(encoding="utf-8")
        assert "import common" not in source
        assert "{{COMMON_CODE}}" not in source

    def test_path_params_warn_against_repo_prefix(self):
        """The `path` params must steer the model away from the ambiguity of
        passing the <owner>/<name> prefix in `path` (regression: an agent
        passed 'owner/repo/README.md' as the path). Each must say so and give
        an example (DESIGN.md §7 Phase 4)."""
        dist_file = Path(__file__).resolve().parent.parent / "dist" / "files_search.py"
        module = types.ModuleType("files_search_script")
        exec(compile(dist_file.read_text(encoding="utf-8"), dist_file.name, "exec"), module.__dict__)
        tools = module.Tools()
        for name in ["cexp_list_files", "cexp_read_file", "cexp_search_text", "cexp_search_symbol"]:
            doc = getattr(tools, name).__doc__ or ""
            path_line = next(
                (l for l in doc.splitlines() if l.strip().startswith(":param path:")), ""
            )
            assert "do NOT include" in path_line, f"{name}: path param lacks the prefix warning"
            assert "\"/\"" in path_line, f"{name}: path param lacks a separator note"
        # cexp_read_file carries an explicit call example in its description
        # (wrapped across two docstring lines).
        assert 'Example: cexp_read_file("owner/repo",' in getattr(tools, "cexp_read_file").__doc__
        assert '"src/main.py").' in getattr(tools, "cexp_read_file").__doc__

    async def test_works_without_fd_rg_binaries(self, tmp_path, monkeypatch):
        """The whole point of the pure-Python implementation: the deployment
        environment has no fd/rg binaries. Simulate it by making shutil.which
        return None for them (git still needed for cloning)."""
        import common as common_mod
        import shutil

        real_which = shutil.which

        def fake_which(cmd):
            if cmd in ("fd", "rg"):
                return None
            return real_which(cmd)

        monkeypatch.setattr(common_mod.shutil, "which", fake_which)

        src = tmp_path / "src"
        await init_repo(src)
        tools = await clone_source(tmp_path / "repos", src)

        # cexp_list_files
        result = parse_json(await tools.cexp_list_files("testowner/testrepo", filter="*.py"))
        assert len(result["items"]) >= 1
        # cexp_read_file
        out = await tools.cexp_read_file("testowner/testrepo", "hello.py")
        assert out == HELLO_PY
        # cexp_search_text
        result = parse_json(await tools.cexp_search_text("testowner/testrepo", "def "))
        assert len(result["items"]) >= 1
        assert result["items"][0]["text"].startswith("def ")
