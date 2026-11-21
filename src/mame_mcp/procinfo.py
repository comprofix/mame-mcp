"""Process facts used to prove that a pid is the MAME this server launched: image, creation time, bridge-port owners.

Windows uses winwake's Win32 probes. Linux reads /proc:
  * image: the /proc/<pid>/exe link (the resolved executable; a replaced or deleted binary reads " (deleted)" and fails);
  * creation time: starttime (field 22 of /proc/<pid>/stat, clock ticks since boot), which with the pid identifies one
    process instance, as the creation FILETIME does on Windows;
  * port owners: the socket inodes on that local port in /proc/net/tcp and tcp6, matched against /proc/*/fd. A socket whose
    owner cannot be read (another user's process) is reported as owner -1, so it can never be taken for "nobody holds it".
Other platforms report the probes as unavailable, and ownership then rests on the Popen handle alone.
"""
from __future__ import annotations

import os
import sys

from . import winwake

UNKNOWN_OWNER = -1


def _linux() -> bool:
    return sys.platform.startswith("linux")


def available() -> bool:
    return winwake.available() or _linux()


def process_image(pid: int) -> str | None:
    """Executable path of `pid`, or None if it cannot be determined."""
    if winwake.available():
        return winwake.process_image(pid)
    if not _linux():
        return None
    try:
        return os.readlink(f"/proc/{int(pid)}/exe")
    except OSError:
        return None


def creation_time(pid: int) -> int | None:
    """Process start time (Windows: FILETIME; Linux: clock ticks since boot), or None."""
    if winwake.available():
        return winwake.creation_time(pid)
    if not _linux():
        return None
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as f:
            stat = f.read().decode("ascii", "replace")
        # comm (field 2) may contain spaces and parentheses: the fields after the LAST ')' start at field 3 (state)
        return int(stat[stat.rindex(")") + 2:].split()[22 - 3])
    except (OSError, ValueError, IndexError):
        return None


def _socket_inodes(port: int) -> set[str]:
    inodes: set[str] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table) as f:
                rows = f.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:  # sl local_address rem_address st tx_queue:rx_queue tr:tm->when retrnsmt uid timeout inode ...
            cols = row.split()
            if len(cols) > 9 and int(cols[1].rsplit(":", 1)[1], 16) == port and cols[9] != "0":  # inode 0: no owning socket
                inodes.add(cols[9])
    return inodes


def port_owner_pids(port: int) -> set[int]:
    """PIDs owning a TCP socket (listening or connected) whose LOCAL port is `port`."""
    if winwake.available():
        return winwake.port_owner_pids(port)
    if not _linux():
        return set()
    wanted = {f"socket:[{i}]": i for i in _socket_inodes(port)}
    if not wanted:
        return set()
    found: set[str] = set()
    out: set[int] = set()
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            fds = os.listdir(f"/proc/{name}/fd")
        except OSError:  # gone, or another user's process
            continue
        for fd in fds:
            try:
                target = os.readlink(f"/proc/{name}/fd/{fd}")
            except OSError:
                continue
            if target in wanted:
                out.add(int(name))
                found.add(wanted[target])
    if found != set(wanted.values()):
        out.add(UNKNOWN_OWNER)
    return out
