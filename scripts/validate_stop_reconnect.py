"""stop_session ownership ordering and bridge reconnect against real MAME 0.289 (in-process SessionManager / BridgeClient).

Both mechanisms were measured before they were fixed:

  * The plugin writes a one-byte heartbeat every second. When a client closes its socket while such a byte is still unread, Windows
    RESETS the connection and MAME's socket on the bridge port disappears within about a millisecond. stop_session used to close the
    bridge first and check afterwards that the MAME pid owned the bridge port, so it refused to stop its own MAME.
  * After a client vanishes, the plugin re-listens only from a periodic callback, which a debugger-stopped MAME runs only when it is
    nudged. The nudge refused to run because no socket was left on the bridge port, and BridgeClient.connect() never nudged.

Sections
  S  stop ordering: a fresh managed session per trial (debugger-stopped, running, paused), the client forced to hold an unread
     heartbeat byte at close (the failing condition made deterministic), stop_session and server shutdown must verify ownership and
     stop the process; the harness records whether MAME's socket really vanished at the close (precondition exercised).
  R  reconnect: the same condition then a production BridgeClient.connect() with the production wake identity; bounded failure
     against a frozen MAME; nothing but the read-only handshake is sent while connecting.

Run it against the unfixed modules to see it fail:  set PYTHONPATH to a directory holding the old mame_mcp package and omit -I.
Usage: .venv\\Scripts\\python -I scripts\\validate_stop_reconnect.py [S R]      Output: <runs>\\stop_reconnect\\results.json
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fx import MAME_DIR, ROMPATH, Results  # noqa: E402
from _inst import Inst, runs_root  # noqa: E402

from mame_mcp import winwake  # noqa: E402
from mame_mcp.bridge import BridgeClient  # noqa: E402
from mame_mcp.protocol import BridgeError  # noqa: E402
from mame_mcp.session import SessionConfig, SessionManager  # noqa: E402

OUT = runs_root() / "stop_reconnect"
R = Results("stop_reconnect")
check, note = R.check, R.note
STOP_TRIALS = 2
RECONNECT_TRIALS = 3
RECONNECT_BOUND_S = 8.0   # the production connect() bound is 10 s; the measured re-listen takes about 1-3.5 s


class UnreadHeartbeatClient(BridgeClient):
    """The failing condition, made deterministic: before the non-graceful close that stop_session and shutdown perform, wait long
    enough (> 1 s) that the plugin has written a heartbeat byte this client never read, then record whether MAME's socket on the
    bridge port vanished at once (the reset)."""

    def __init__(self, *a, pid: int | None = None, **kw):
        super().__init__(*a, **kw)
        self.pid, self.reset_seen = pid, None

    def close(self, graceful: bool = True) -> None:
        was = self.connected
        if was and not graceful:
            time.sleep(1.4)
        super().close(graceful)
        if was and not graceful and self.pid:
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < 0.05:
                if self.pid not in winwake.port_owner_pids(self.port):
                    self.reset_seen = True
                    return
            self.reset_seen = False


def alive(pid: int) -> bool:
    return bool(winwake.process_image(pid))


def one_session(state: str, via: str) -> dict:
    cfg = SessionConfig(mame_dir=MAME_DIR, rompaths=[ROMPATH], runs_dir=OUT / "sessions")
    clients: list[UnreadHeartbeatClient] = []

    def factory(port: int, pid: int, created: int | None) -> BridgeClient:
        c = UnreadHeartbeatClient(port=port, pid=pid, wake=winwake.waker_for((pid, created), port=port))
        clients.append(c)
        return c

    mgr = SessionManager(cfg, client_factory=factory)
    info = mgr.start("shinobi")
    pid = info["pid"]
    try:
        with mgr.use_client(None) as (client, _managed):
            if state in ("running", "paused"):
                client.execution_control("resume")
                time.sleep(1.0)
            if state == "paused":
                client.execution_control("pause")
                time.sleep(0.5)
        rep = mgr.stop() if via == "stop_session" else next(iter(mgr.shutdown()["sessions"].values()))  # shutdown reports per session
    finally:
        if alive(pid):  # never leave a MAME behind, whatever the outcome
            import subprocess
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    proc = (rep or {}).get("process") or {}
    return {"state": state, "via": via, "pid": pid, "report": rep, "process": proc, "gone": not alive(pid), "reset_seen": clients[0].reset_seen if clients else None}


def section_s() -> None:
    results = []
    for state in ("debugger_stopped", "running", "paused"):
        for i in range(STOP_TRIALS):
            results.append(one_session(state, "stop_session"))
    for i in range(STOP_TRIALS):
        results.append(one_session("running", "shutdown"))
    for state in ("debugger_stopped", "running", "paused"):
        for via in ("stop_session", "shutdown"):
            rs = [r for r in results if r["state"] == state and r["via"] == via]
            if not rs:
                continue
            check(f"S {via} while {state}: the owned MAME is stopped, ownership verified positively, no cleanup failure ({len(rs)} fresh sessions)",
                  all(r["process"].get("terminated") is True and "refused" not in r["process"] and r["gone"] and r["report"]["cleanup_failures"] == []
                      and r["process"].get("identity_check") == "image, creation time and bridge port verified" for r in rs),
                  trials=[{"terminated": r["process"].get("terminated"), "identity": r["process"].get("identity_check"), "failures": r["report"]["cleanup_failures"], "gone": r["gone"]} for r in rs])
    resetting = [r for r in results if r["state"] in ("running", "paused")]
    note("S precondition: MAME's bridge socket vanished at the close (connection reset) in %d of %d running/paused sessions" % (sum(1 for r in resetting if r["reset_seen"]), len(resetting)))
    check("S the failing condition was really exercised: the connection was reset at the close in every running/paused session",
          bool(resetting) and all(r["reset_seen"] for r in resetting), reset_seen=[r["reset_seen"] for r in resetting])


def freeze(pid: int, resume: bool = False) -> None:
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    nt = ctypes.WinDLL("ntdll")
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
    assert h, "OpenProcess failed"
    (nt.NtResumeProcess if resume else nt.NtSuspendProcess)(ctypes.c_void_p(h))
    k32.CloseHandle(ctypes.c_void_p(h))


def section_r() -> None:
    m = Inst(OUT, "reconnect", 9661, "shinobi")
    try:
        pid, port = m.proc.pid, 9661
        sid0 = m.c.session_id
        for state in ("debugger_stopped", "paused", "running"):
            if state == "paused":
                m.pause()
            if state == "running":
                m.resume()
            trials = []
            for _ in range(RECONNECT_TRIALS):
                m.info_()
                time.sleep(1.4)                       # a heartbeat byte arrives and stays unread
                posts0 = m.c.wake_posts
                m.c.close(graceful=False)
                t0 = time.perf_counter()
                vanished = None
                while time.perf_counter() - t0 < 0.05:
                    if pid not in winwake.port_owner_pids(port):
                        vanished = round((time.perf_counter() - t0) * 1000)
                        break
                t1 = time.perf_counter()
                try:
                    m.c.connect(timeout=10)
                    outcome, err = "reconnected", None
                except BridgeError as e:
                    outcome, err = "failed", e.code
                if outcome == "failed":  # recover for the next trial with a raw nudge (diagnostic only, not the production path)
                    import threading
                    stop = {"s": False}

                    def raw():
                        while not stop["s"]:
                            winwake.wake(pid)
                            time.sleep(0.25)

                    threading.Thread(target=raw, daemon=True).start()
                    try:
                        m.c.connect(timeout=20)
                    finally:
                        stop["s"] = True
                trials.append({"vanished_ms": vanished, "outcome": outcome, "error": err, "seconds": round(time.perf_counter() - t1, 2), "nudges": m.c.wake_posts - posts0,
                               "same_session": m.c.session_id == sid0})
            ok = all(t["outcome"] == "reconnected" and t["seconds"] < RECONNECT_BOUND_S and t["same_session"] for t in trials)
            check(f"R {state}: after a reset close (unread heartbeat) the production connect() re-attached to the same MAME session inside {RECONNECT_BOUND_S:g} s ({RECONNECT_TRIALS} trials)", ok, trials=trials)
            if state == "debugger_stopped":
                check("R debugger-stopped: the re-listen needed the nudge (the plugin does not run while MAME is stopped), and the nudge was the production waker's",
                      all(t["nudges"] > 0 for t in trials), nudges=[t["nudges"] for t in trials])
            if state != "debugger_stopped":
                check(f"R {state}: the connection was really reset at the close (socket gone within 50 ms), so the failing condition was exercised",
                      sum(1 for t in trials if t["vanished_ms"] is not None) >= 2, vanished_ms=[t["vanished_ms"] for t in trials])
        # bounded failure: a frozen MAME never answers, connect must give up inside its own deadline and say so
        m.info_()
        m.c.close(graceful=False)
        freeze(pid)
        try:
            t0 = time.perf_counter()
            try:
                m.c.connect(timeout=3)
                code = None
            except BridgeError as e:
                code = e.code
            el = time.perf_counter() - t0
            check("R a frozen MAME: connect() gives up inside its deadline with connect_failed (an error is never treated as readiness)", code == "connect_failed" and el < 4.5, code=code, seconds=round(el, 2))
        finally:
            freeze(pid, resume=True)
        m.c.connect(timeout=20)
        check("R after the MAME is thawed the same client re-attaches to the same session", m.c.session_id == sid0 and bool(m.info_().get("cpus")))
    finally:
        m.close()


def main() -> int:
    want = set(sys.argv[1:]) or {"S", "R"}
    for name, fn in (("S", section_s), ("R", section_r)):
        if name in want:
            print(f"\n=== section {name}", flush=True)
            try:
                fn()
            except Exception as e:  # a harness failure is a failure, recorded and continued
                import traceback
                check(f"{name} section completed without a harness error", False, error=repr(e), trace=traceback.format_exc()[-900:])
    return R.save(OUT / "results.json")


if __name__ == "__main__":
    sys.exit(main())
