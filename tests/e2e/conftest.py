"""Spins up a real `uvicorn` process for e2e tests to drive with a real
browser -- these exercise the actual client JS + WS relay + persistence
together, which `fastapi.testclient`-based tests (fast, but in-process and
JS-free) can't. Each test gets its own process on a free port and its own
temp SQLite file, so tests never share room state.
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_healthy(base_url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{base_url}/health", timeout=0.5)
            return
        except Exception as exc:  # noqa: BLE001 -- just retrying until the server is up
            last_error = exc
            time.sleep(0.15)
    raise RuntimeError(f"live_server did not become healthy in time: {last_error}")


@pytest.fixture
def live_server(tmp_path):
    """Yields the base URL of a freshly-started, empty crdt-cad server.
    Auth is off (zero-config) unless the test sets `extra_env`; use the
    `live_server_factory` fixture instead for that."""
    yield from _start_server(tmp_path)


@pytest.fixture
def live_server_factory(tmp_path):
    """For tests that need non-default server config (e.g. CRDT_CAD_SECRET).
    Returns a callable; call it with a dict of extra env vars."""
    procs = []

    def _make(extra_env: dict | None = None) -> str:
        gen = _start_server(tmp_path, extra_env=extra_env)
        base_url = next(gen)
        procs.append(gen)
        return base_url

    yield _make
    for gen in procs:
        try:
            next(gen)
        except StopIteration:
            pass


def _start_server(tmp_path, extra_env: dict | None = None):
    port = _free_port()
    db_path = tmp_path / f"e2e-{port}.db"
    env = os.environ.copy()
    env["CRDT_CAD_DB_PATH"] = str(db_path)
    env.pop("CRDT_CAD_SECRET", None)  # zero-config default unless extra_env overrides it
    if extra_env:
        env.update(extra_env)

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "crdt_cad.server.app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_until_healthy(base_url)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
