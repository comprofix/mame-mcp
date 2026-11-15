"""Logic tests for the wake nudge's identity binding, with the Windows probes replaced by fakes (no MAME needed)."""
from __future__ import annotations

from mame_mcp import winwake


class Probe:
    """Fake process table: pid -> (image, creation_time); port -> owner pids."""

    def __init__(self, monkeypatch, procs, ports):
        self.posted: list[int] = []
        monkeypatch.setattr(winwake, "available", lambda: True)
        monkeypatch.setattr(winwake, "process_image", lambda pid: procs.get(pid, (None, None))[0])
        monkeypatch.setattr(winwake, "creation_time", lambda pid: procs.get(pid, (None, None))[1])
        monkeypatch.setattr(winwake, "port_owner_pids", lambda port: set(ports.get(port, ())))
        monkeypatch.setattr(winwake, "wake", lambda pid: (self.posted.append(pid), 1)[1])


MAME_A = r"D:\arcade\mame\mame.exe"


def test_exact_identity_is_nudged(monkeypatch):
    p = Probe(monkeypatch, {10: (MAME_A, 111)}, {9411: [10]})
    w = winwake.waker_for(lambda: (10, 111), port=9411)
    assert w() == 1 and p.posted == [10] and w.last_refusal is None


def test_recycled_pid_wrong_creation_time_is_refused(monkeypatch):
    p = Probe(monkeypatch, {10: (MAME_A, 222)}, {9411: [10]})
    w = winwake.waker_for(lambda: (10, 111), port=9411)
    assert w() == 0 and p.posted == [] and "creation time" in w.last_refusal


def test_other_mame_instance_is_refused_even_with_its_own_exact_identity(monkeypatch):
    p = Probe(monkeypatch, {10: (MAME_A, 111), 20: (MAME_A, 333)}, {9411: [10], 9412: [20]})
    w = winwake.waker_for(lambda: (20, 333), port=9411)
    assert w() == 0 and p.posted == [] and "port" in w.last_refusal


def test_pid_only_configuration_still_requires_port_ownership(monkeypatch):
    p = Probe(monkeypatch, {20: (MAME_A, 333)}, {9411: [10]})
    w = winwake.waker_for(20, port=9411)
    assert w() == 0 and p.posted == []


def test_dead_pid_and_non_mame_image_are_refused(monkeypatch):
    p = Probe(monkeypatch, {30: (r"C:\Windows\notepad.exe", 1)}, {9411: [30]})
    assert winwake.waker_for(99, port=9411)() == 0
    w = winwake.waker_for(30, port=9411)
    assert w() == 0 and "not mame" in w.last_refusal and p.posted == []


def test_pid_source_is_reread_every_call(monkeypatch):
    p = Probe(monkeypatch, {10: (MAME_A, 111), 11: (MAME_A, 112)}, {9411: [10]})
    cur = {"v": (10, 111)}
    w = winwake.waker_for(lambda: cur["v"], port=9411)
    assert w() == 1
    cur["v"] = (11, 112)  # restart wrote a new launch.json naming a pid that does not own the port
    assert w() == 0 and p.posted == [10]
    cur["v"] = None
    assert w() == 0
