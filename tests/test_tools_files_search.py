"""Integration tests for the Phase 2 Files & Search script
(list_files, read_file, search_text).

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
    out = await repos_tools.clone_repo(name, url=f"file://{source}")
    assert not out.startswith("Error:"), out
    return make_tools(repos_path)


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


class TestListFiles:
    async def test_lists_structure_at_root(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.list_files("testowner/testrepo")

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
        result = parse_json(await tools.list_files("testowner/testrepo", type="file"))
        assert all(i["kind"] == "file" for i in result["items"])
        assert "sub" not in [i["path"] for i in result["items"]]

    async def test_type_dir(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.list_files("testowner/testrepo", type="dir"))
        assert all(i["kind"] == "dir" for i in result["items"])
        assert [i["path"] for i in result["items"]] == ["sub"]

    async def test_max_depth(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.list_files("testowner/testrepo", max_depth=1))
        paths = {i["path"] for i in result["items"]}
        assert "hello.py" in paths
        assert "sub" in paths
        assert "sub/deep.py" not in paths

    async def test_filter_include_glob(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.list_files("testowner/testrepo", filter="*.py"))
        paths = [i["path"] for i in result["items"]]
        assert "hello.py" in paths
        assert "sub/deep.py" in paths
        assert "world.md" not in paths
        assert "data.bin" not in paths

    async def test_filter_exclude_glob(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.list_files("testowner/testrepo", filter="!*.md"))
        paths = [i["path"] for i in result["items"]]
        assert "world.md" not in paths
        assert "hello.py" in paths

    async def test_subdirectory_path_relative_to_root(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.list_files("testowner/testrepo", path="sub"))
        assert [i["path"] for i in result["items"]] == ["sub/deep.py"]

    async def test_single_file_path(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.list_files("testowner/testrepo", path="hello.py"))
        assert [i["path"] for i in result["items"]] == ["hello.py"]
        # file path with a filter that excludes it -> empty
        result = parse_json(
            await tools.list_files("testowner/testrepo", path="hello.py", filter="*.md")
        )
        assert result["items"] == []

    async def test_path_not_found(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.list_files("testowner/testrepo", path="nope")
        assert out.startswith("Not found:")

    async def test_path_traversal_rejected(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        for bad in ["../evil", "..", "/etc"]:
            out = await tools.list_files("testowner/testrepo", path=bad)
            assert out.startswith("Error:"), bad

    async def test_invalid_type_and_depth(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.list_files("testowner/testrepo", type="bogus")
        assert out.startswith("Error:")
        out = await tools.list_files("testowner/testrepo", max_depth=-1)
        assert out.startswith("Error:")

    async def test_max_results_cap(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        tools.valves.max_results = 2
        result = parse_json(await tools.list_files("testowner/testrepo"))
        assert len(result["items"]) == 2
        assert result["truncated"]["total"] >= 2

    async def test_repo_not_cloned(self, repos_path):
        tools = make_tools(repos_path)
        out = await tools.list_files("o/r")
        assert out.startswith("Not found:")
        assert "not cloned yet" in out


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


class TestReadFile:
    async def test_read_full_file(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.read_file("testowner/testrepo", "hello.py")
        assert out == HELLO_PY

    async def test_read_line_range(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.read_file("testowner/testrepo", "hello.py", start=2, end=3)
        assert out == "    return 'hi'\n\n"

    async def test_range_beyond_eof_clamped(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        # hello.py has 6 lines; lines 3-4 are empty.
        out = await tools.read_file("testowner/testrepo", "hello.py", start=4, end=999)
        assert out == "\ndef world(x):\n    return x * 2\n"

    async def test_binary_rejected(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.read_file("testowner/testrepo", "data.bin")
        assert out.startswith("Error:")
        assert "binary" in out.lower()

    async def test_file_not_found(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.read_file("testowner/testrepo", "missing.py")
        assert out.startswith("Not found:")

    async def test_directory_rejected(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.read_file("testowner/testrepo", "sub")
        assert out.startswith("Error:")
        assert "directory" in out

    async def test_invalid_start(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.read_file("testowner/testrepo", "hello.py", start=0)
        assert out.startswith("Error:")
        out = await tools.read_file("testowner/testrepo", "hello.py", start=-3)
        assert out.startswith("Error:")

    async def test_start_beyond_eof(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.read_file("testowner/testrepo", "hello.py", start=100)
        assert out.startswith("Error:")
        assert "beyond" in out

    async def test_end_before_start(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.read_file("testowner/testrepo", "hello.py", start=3, end=1)
        assert out.startswith("Error:")

    async def test_path_traversal_rejected(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        for bad in ["../outside.txt", "/etc/passwd"]:
            out = await tools.read_file("testowner/testrepo", bad)
            assert out.startswith("Error:"), bad

    async def test_line_cap_with_marker(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        tools.valves.max_lines = 5
        out = await tools.read_file("testowner/testrepo", "big.txt")
        lines = out.splitlines()
        assert lines[0] == "line0000"
        assert any("truncated" in l for l in lines)
        assert any("of 6000 lines" in l for l in lines)

    async def test_byte_cap_with_marker(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        tools.valves.max_bytes = 300
        out = await tools.read_file("testowner/testrepo", "big.txt")
        assert len(out.encode("utf-8")) <= 300 + 200  # marker may add a bit
        assert "truncated" in out

    async def test_repo_not_cloned(self, repos_path):
        tools = make_tools(repos_path)
        out = await tools.read_file("o/r", "x.py")
        assert out.startswith("Not found:")


# ---------------------------------------------------------------------------
# search_text
# ---------------------------------------------------------------------------


class TestSearchText:
    async def test_finds_matches_with_line_numbers(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.search_text("testowner/testrepo", "def ")
        result = parse_json(out)
        items = result["items"]
        assert len(items) == 2
        by = {(i["path"], i["line"]): i["text"] for i in items}
        assert by[("hello.py", 1)] == "def hello():"
        assert by[("hello.py", 5)] == "def world(x):"

        out = await tools.search_text("testowner/testrepo", "class ")
        result = parse_json(out)
        assert len(result["items"]) == 1
        assert result["items"][0]["path"] == "sub/deep.py"
        assert result["items"][0]["text"] == "class Deep:"

    async def test_no_matches_empty_items(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.search_text("testowner/testrepo", "zzzz_nothing_zzzz"))
        assert result["items"] == []

    async def test_case_sensitivity(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        # insensitive by default: matches lowercase and uppercase
        insensitive = parse_json(await tools.search_text("testowner/testrepo", "HELLO"))
        assert len(insensitive["items"]) >= 1
        sensitive = parse_json(
            await tools.search_text("testowner/testrepo", "HELLO", case_sensitive=True)
        )
        assert sensitive["items"] == []
        exact = parse_json(
            await tools.search_text("testowner/testrepo", "hello", case_sensitive=True)
        )
        assert len(exact["items"]) >= 1

    async def test_context_lines(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.search_text("testowner/testrepo", "return 'hi'", context=1))
        item = result["items"][0]
        assert item["line"] == 2
        assert "context" in item
        assert any("def hello" in c for c in item["context"])

    async def test_filter_globs(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        py = parse_json(await tools.search_text("testowner/testrepo", "hello", filter="*.py"))
        md = parse_json(await tools.search_text("testowner/testrepo", "hello", filter="*.md"))
        assert len(py["items"]) >= 1
        assert md["items"] == []
        # exclusion
        not_md = parse_json(
            await tools.search_text("testowner/testrepo", "hello", filter="!*.md")
        )
        assert len(not_md["items"]) >= 1

    async def test_path_narrowing(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.search_text("testowner/testrepo", "Deep", path="sub"))
        assert len(result["items"]) == 1
        assert result["items"][0]["path"] == "sub/deep.py"

    async def test_regex_query(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        result = parse_json(await tools.search_text("testowner/testrepo", r"def \w+\("))
        assert len(result["items"]) >= 2

    async def test_empty_query_rejected(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.search_text("testowner/testrepo", "   ")
        assert out.startswith("Error:")

    async def test_path_not_found(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.search_text("testowner/testrepo", "hello", path="nope")
        assert out.startswith("Not found:")

    async def test_path_traversal_rejected(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.search_text("testowner/testrepo", "hello", path="..")
        assert out.startswith("Error:")

    async def test_max_results_cap(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        tools.valves.max_results = 2
        result = parse_json(await tools.search_text("testowner/testrepo", "line00"))
        assert len(result["items"]) == 2
        assert result["truncated"]["total"] >= 2

    async def test_negative_context_rejected(self, repos_path, source_repo):
        tools = await clone_source(repos_path, source_repo)
        out = await tools.search_text("testowner/testrepo", "hello", context=-1)
        assert out.startswith("Error:")

    async def test_repo_not_cloned(self, repos_path):
        tools = make_tools(repos_path)
        out = await tools.search_text("o/r", "hello")
        assert out.startswith("Not found:")


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
        # search_symbol is deliberately NOT present until Phase 3 (§5.4).
        assert sorted(discovered) == ["list_files", "read_file", "search_text"]
        for name in discovered:
            assert getattr(tools, name).__doc__

    def test_docstring_params_match_signature_and_are_single_line(self):
        import re

        param_pattern = re.compile(r":param (\w+):\s*(.+)")
        dist_file = Path(__file__).resolve().parent.parent / "dist" / "files_search.py"
        module = types.ModuleType("files_search_script")
        exec(compile(dist_file.read_text(encoding="utf-8"), dist_file.name, "exec"), module.__dict__)
        tools = module.Tools()

        for name in ["list_files", "read_file", "search_text"]:
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

        # list_files
        result = parse_json(await tools.list_files("testowner/testrepo", filter="*.py"))
        assert len(result["items"]) >= 1
        # read_file
        out = await tools.read_file("testowner/testrepo", "hello.py")
        assert out == HELLO_PY
        # search_text
        result = parse_json(await tools.search_text("testowner/testrepo", "def "))
        assert len(result["items"]) >= 1
        assert result["items"][0]["text"].startswith("def ")
