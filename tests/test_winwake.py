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


# --- the bridge port is unheld while the plugin is between connections (measured on MAME 0.289) ---------------------------------
def test_exact_identity_is_nudged_while_nobody_holds_the_bridge_port(monkeypatch):
    """After a client vanished, MAME's socket is gone until its plugin re-listens, and a debugger-stopped MAME runs the plugin only
    when nudged: refusing here would deadlock the reconnect."""
    p = Probe(monkeypatch, {10: (MAME_A, 111)}, {})
    w = winwake.waker_for(lambda: (10, 111), port=9411)
    assert w() == 1 and p.posted == [10] and w.last_refusal is None


def test_unheld_port_does_not_help_a_pid_only_configuration(monkeypatch):
    p = Probe(monkeypatch, {10: (MAME_A, 111)}, {})
    w = winwake.waker_for(10, port=9411)  # no creation time: the port is the only binding, so it must be owned
    assert w() == 0 and p.posted == [] and "does not own" in w.last_refusal


def test_unheld_port_does_not_help_a_recycled_pid(monkeypatch):
    p = Probe(monkeypatch, {10: (MAME_A, 222)}, {})
    w = winwake.waker_for(lambda: (10, 111), port=9411)
    assert w() == 0 and p.posted == [] and "creation time" in w.last_refusal


def test_unheld_port_does_not_help_a_non_mame_image(monkeypatch):
    p = Probe(monkeypatch, {10: (r"C:\Windows\notepad.exe", 111)}, {})
    w = winwake.waker_for(lambda: (10, 111), port=9411)
    assert w() == 0 and p.posted == [] and "not mame" in w.last_refusal


def test_port_held_by_another_process_is_refused_even_with_an_exact_identity(monkeypatch):
    p = Probe(monkeypatch, {10: (MAME_A, 111), 20: (MAME_A, 333)}, {9411: [20]})
    w = winwake.waker_for(lambda: (10, 111), port=9411)  # exact identity, but a different process holds THIS bridge port
    assert w() == 0 and p.posted == [] and "does not own" in w.last_refusal
