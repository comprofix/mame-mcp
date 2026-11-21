"""Optional Windows-only nudge for a debugger-stopped MAME.

Source reading (mame0289 src/osd/modules/debugger/debugwin.cpp, debugger_windows::wait_for_debugger):
while the emulation is stopped in the debugger the main thread blocks in GetMessage(&message, nullptr, 0, 0).
Lua periodic callbacks (which service the bridge socket) only run after GetMessage returns, i.e. when some
window message reaches that thread. On an idle desktop that can take many seconds. Posting WM_NULL (a no-op
message) to the MAME process' threads makes GetMessage return so the callbacks run.

This touches only a process that passes ALL of these checks immediately before every post:
  * its image file name starts with "mame";
  * (when known) its creation time equals the one recorded when the PID was captured (a recycled PID fails);
  * (when a port is given) it owns a TCP socket on the bridge's loopback port, i.e. it is the MAME serving
    THIS bridge, not another MAME instance and not a stale PID that now belongs to something else. The one exception:
    when the creation time is known (an exact identity) and NO process holds the port, because the plugin is between
    connections, the nudge is still allowed; a port held by any other process is always refused.
It never reads or writes MAME memory and sends no input. It is not general process control.
"""
from __future__ import annotations

import sys

WM_NULL = 0x0000
TH32CS_SNAPTHREAD = 0x00000004


def available() -> bool:
    return sys.platform == "win32"


def wake(pid: int) -> int:
    """Post WM_NULL to every thread of `pid`. Returns the number of threads that accepted the message."""
    if not available():
        return 0
    import ctypes
    from ctypes import wintypes

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD),
                    ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG),
                    ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snap in (None, wintypes.HANDLE(-1).value):
        return 0
    posted = 0
    try:
        te = THREADENTRY32()
        te.dwSize = ctypes.sizeof(te)
        ok = k32.Thread32First(snap, ctypes.byref(te))
        while ok:
            if te.th32OwnerProcessID == pid and u32.PostThreadMessageW(te.th32ThreadID, WM_NULL, 0, 0):
                posted += 1
            ok = k32.Thread32Next(snap, ctypes.byref(te))
    finally:
        k32.CloseHandle(snap)
    return posted


def process_image(pid: int) -> str | None:
    """Executable path of `pid`, or None if it cannot be determined."""
    if not available():
        return None
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        k32.CloseHandle(h)


def is_mame(pid: int) -> bool:
    """Only ever nudge a process whose image is named mame*.exe."""
    img = process_image(pid)
    return bool(img) and img.replace("\\", "/").rsplit("/", 1)[-1].lower().startswith("mame")


def creation_time(pid: int) -> int | None:
    """Process creation time as a 64-bit FILETIME integer, or None. (pid, creation_time) identifies one process
    instance for the life of the machine, unlike a PID which Windows recycles."""
    if not available():
        return None
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return None
        return (c.dwHighDateTime << 32) | c.dwLowDateTime
    finally:
        k32.CloseHandle(h)


def port_owner_pids(port: int) -> set[int]:
    """PIDs owning an IPv4 TCP socket (listening or connected) whose LOCAL port is `port`."""
    if not available():
        return set()
    import ctypes
    from ctypes import wintypes

    ip = ctypes.WinDLL("iphlpapi", use_last_error=True)
    size = wintypes.DWORD(0)
    TCP_TABLE_OWNER_PID_ALL, AF_INET = 5, 2
    ip.GetExtendedTcpTable(None, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
    for _ in range(3):  # the table can grow between the sizing call and the read
        buf = (ctypes.c_ubyte * (size.value + 4096))()
        size2 = wintypes.DWORD(ctypes.sizeof(buf))
        if ip.GetExtendedTcpTable(buf, ctypes.byref(size2), False, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0) == 0:
            break
        size = wintypes.DWORD(size2.value)
    else:
        return set()
    n = int.from_bytes(bytes(buf[:4]), "little")
    out: set[int] = set()
    for i in range(n):  # MIB_TCPROW_OWNER_PID: state, localAddr, localPort, remoteAddr, remotePort, owningPid (6 x DWORD)
        row = bytes(buf[4 + 24 * i: 4 + 24 * (i + 1)])
        local_port = int.from_bytes(row[8:10], "big")  # network byte order, in the low 16 bits
        if local_port == port:
            out.add(int.from_bytes(row[20:24], "little"))
    return out


def verified_target(pid: int, *, created: int | None = None, port: int | None = None) -> tuple[bool, str]:
    """Is `pid` the MAME this bridge talks to? Returns (ok, reason)."""
    img = process_image(pid)
    if not img:
        return False, "process not found (stale pid)"
    if not img.replace("\\", "/").rsplit("/", 1)[-1].lower().startswith("mame"):
        return False, "image is not mame*"
    if created is not None and creation_time(pid) != created:
        return False, "creation time differs from the recorded one (recycled pid)"
    if port is not None:
        owners = port_owner_pids(port)
        if pid not in owners:
            if owners or created is None:
                return False, f"pid does not own a socket on bridge port {port} (another instance or stale pid)"
            # Nobody holds the bridge port at all: MAME's plugin closed the departed client's socket and has not re-listened
            # yet. It re-listens from a periodic callback, which a debugger-stopped MAME runs only when it is nudged, so
            # refusing here would deadlock the reconnect. The process is still identified exactly (image + the creation time
            # recorded at launch); a port held by ANY other process, or a pid-only configuration, is still refused above.
            return True, f"ok (bridge port {port} is not held by any process: the plugin is between connections)"
    return True, "ok"


def waker_for(pid_source, *, port: int | None = None):
    """Build a zero-argument callable for BridgeClient(wake=...). `pid_source` is a pid, or a callable returning
    a pid, a (pid, creation_time) pair, or None. Every call re-verifies identity (see verified_target) and posts
    WM_NULL only to a verified MAME; otherwise it posts nothing and returns 0. `last_refusal` records why."""
    if not available():
        return None

    def _wake() -> int:
        src = pid_source() if callable(pid_source) else pid_source
        pid, created = (src if isinstance(src, tuple) else (src, None))
        if not pid:
            return 0
        ok, why = verified_target(int(pid), created=created, port=port)
        _wake.last_refusal = None if ok else why  # type: ignore[attr-defined]
        return wake(int(pid)) if ok else 0

    _wake.last_refusal = None  # type: ignore[attr-defined]
    return _wake
