"""Pytest bootstrap: make the repo root importable (common, dist), plus the
shared `git daemon` fixture (Enhancement B, ENHANCEMENT_B.md §5.1)."""

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class DaemonSource(Path):
    """A repo path under the git-daemon base with a precomputed git:// URL
    (`url` attribute). Used by the test files' source fixtures so tests can
    clone over git:// without changing their signatures."""

    url: str = ""


def daemon_source(git_daemon, name: str) -> DaemonSource:
    """A DaemonSource under the daemon base with its git:// URL set."""
    base, _, url_for = git_daemon
    p = DaemonSource(base / name)
    p.url = url_for(p)
    return p


@pytest.fixture(scope="session")
def git_daemon(tmp_path_factory):
    """A local `git daemon` (git:// protocol) serving non-bare repos from a
    session-scoped base dir.

    Why: the clone-URL allow-list blocks `file://` (ENHANCEMENT_B.md §2.1),
    so the integration tests clone over `git://127.0.0.1:<port>/<name>`
    instead. Verified behaviour: the daemon serves non-bare repos directly
    (no export markers), serves ALL branches, and reads live state
    (mutations to a served repo are visible to later clones, so tests that
    mutate the source then clone/fetch/pull work without a push step).

    Yields (base, port, url_for) where url_for(path) maps a path under base
    to its git:// URL (paths outside base map to their basename, which the
    daemon refuses -> clone fails, used by the failed-clone cleanup test).
    """
    base = tmp_path_factory.mktemp("daemon")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # reserve a free port, then release it
    proc = subprocess.Popen(
        [
            "git",
            "daemon",
            "--reuseaddr",
            "--export-all",
            f"--base-path={base}",
            "--listen=127.0.0.1",
            f"--port={port}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"git daemon exited early: {proc.stderr.read()!r}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        proc.terminate()
        raise RuntimeError("git daemon did not accept connections in time")

    def url_for(path: Path) -> str:
        try:
            rel = path.relative_to(base).as_posix()
        except ValueError:
            rel = path.name  # outside the base: the daemon will refuse it
        return f"git://127.0.0.1:{port}/{rel}"

    yield base, port, url_for
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
