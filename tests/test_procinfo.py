"""Process identity probes (procinfo) against this test process, and the wake nudge staying Windows-only (no MAME needed)."""
from __future__ import annotations

import os
import socket
import subprocess
import sys

import pytest

from mame_mcp import procinfo, winwake

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc probes")


@linux_only
def test_image_and_creation_time_identify_a_process_instance():
    assert procinfo.available()
    assert procinfo.process_image(os.getpid()) == os.path.realpath(sys.executable)
    me = procinfo.creation_time(os.getpid())
    assert isinstance(me, int) and procinfo.creation_time(os.getpid()) == me
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        assert procinfo.creation_time(child.pid) >= me and procinfo.process_image(child.pid)
    finally:
        child.kill()
        child.wait()
    assert procinfo.process_image(child.pid) is None and procinfo.creation_time(child.pid) is None  # reaped: gone


@linux_only
def test_port_owners_are_found_through_proc_and_released_with_the_socket():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert procinfo.port_owner_pids(port) == {os.getpid()}
    finally:
        s.close()
    assert procinfo.port_owner_pids(port) == set()


@linux_only
def test_a_socket_whose_owner_cannot_be_read_is_never_reported_as_unowned(monkeypatch):
    monkeypatch.setattr(procinfo, "_socket_inodes", lambda port: {"999999999999"})
    assert procinfo.port_owner_pids(1) == {procinfo.UNKNOWN_OWNER}


def test_wake_nudge_exists_only_on_windows(monkeypatch):
    monkeypatch.setattr(winwake, "available", lambda: False)
    assert winwake.waker_for((10, 111), port=9411) is None and winwake.wake(10) == 0
