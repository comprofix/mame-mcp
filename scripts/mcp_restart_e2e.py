"""Restart / reconnect end-to-end through the official MCP client, using ONE MCP server process across MAME restarts.

Scenarios (all against real MAME 0.289 / Shinobi; fixture plugin only for independent observation):
  A  baseline session + active experiment
  B  MAME killed during the active experiment: errors are clear, nothing is sent to a dead MAME
  C  MAME restarted on the same port; same MCP server process: first call reports session_changed and sends nothing,
     the next works against the NEW session; the old experiment id is refused as belonging to another session
  D  MAME killed while a state-changing request (capture_breakpoint) is in flight: error is flagged uncertain or the call
     succeeded; after another restart the new session saw exactly the requests sent to it (nothing replayed)
  E  MCP server process killed mid-experiment (MAME unaffected): a NEW server process reattaches to the same session and
     reads the experiment that kept running and finished inside MAME
Writes runs/mcp_restart_e2e/results.json.  Usage: .venv\\Scripts\\python -I scripts\\mcp_restart_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fx import Fixture, Results  # noqa: E402

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from mame_mcp import winwake  # noqa: E402
from mame_mcp.launcher import PROJECT_ROOT, launch  # noqa: E402

PORT = 9455 + 20
OUT = PROJECT_ROOT / "runs" / "mcp_restart_e2e"
RUN = OUT / "run"
R = Results("mcp_restart_e2e")
check, note = R.check, R.note
HANDLER = 0x404


def suspend(pid: int) -> None:
    """Freeze the MAME process (Windows NtSuspendProcess) so a request sent to it stays unanswered."""
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    nt = ctypes.WinDLL("ntdll")
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
    assert h, "OpenProcess failed"
    nt.NtSuspendProcess(ctypes.c_void_p(h))
    k32.CloseHandle(ctypes.c_void_p(h))


def body_of(res) -> dict:
    if res.is_error:
        m = re.search(r"\{.*\}", res.content[0].text, re.S)
        try:
            return {"_error": json.loads(m.group(0))} if m else {"_error": {"code": "?", "message": res.content[0].text}}
        except ValueError:
            return {"_error": {"code": "?", "message": res.content[0].text}}
    if res.structured_content is not None:
        return res.structured_content
    return json.loads(res.content[0].text)


def code_of(b: dict) -> str | None:
    return (b.get("_error") or {}).get("code")


def new_mame():
    for stale in ("cmd.json", "reply.json"):
        try:
            (RUN / "fixture" / stale).unlink()
        except OSError:
            pass
    proc, info = launch(port=PORT, run_dir=RUN, fixture=True)
    wake = winwake.waker_for(proc.pid)
    return proc, info, Fixture(info["fixture_dir"], wake=wake)


@asynccontextmanager
async def mcp_server():
    params = StdioServerParameters(
        command=sys.executable, args=["-I", "-m", "mame_mcp.server"],
        env={"MAMEMCP_PORT": str(PORT), "MAMEMCP_LAUNCH_INFO": str(RUN / "launch.json"), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
    )
    errlog = open(OUT / "server_stderr.txt", "a")
    async with stdio_client(params, errlog=errlog) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            yield s
    errlog.close()


async def call(s, name, **args) -> dict:
    return body_of(await s.call_tool(name, args))


async def wait_ready(s, timeout=90) -> dict | None:
    """Poll until a call is answered by the plugin (any answer except connect_failed)."""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = await call(s, "session_info")
        if code_of(last) not in ("connect_failed", "mame_disconnected"):
            return last
        await asyncio.sleep(0.5)
    return last


async def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    proc, info, fx = new_mame()
    procs = [proc]
    try:
        async with mcp_server() as s:
            # ---- A: baseline ------------------------------------------------------------------------
            si = await wait_ready(s)
            sid_a = (si.get("bridge") or {}).get("session_id")
            check("A session_info through MCP; remember session id", bool(sid_a), session=sid_a)
            await call(s, "execution_control", action="resume")
            for _ in range(100):
                st = await call(s, "session_info")
                if st.get("execution", {}).get("state") == "running":
                    break
                await asyncio.sleep(0.3)
            cap = await call(s, "capture_breakpoint", cpu=":maincpu", address=HANDLER, max_events=10000, max_wall_seconds=120, registers=["D0"], label="restart-e2e-A")
            exp_a = cap.get("experiment_id")
            check("A experiment started (long-running, will be orphaned by the MAME kill)", bool(exp_a), experiment=exp_a, session_in_id=exp_a and exp_a.split("-")[1])
            for _ in range(100):
                stat = await call(s, "experiment_status", experiment_id=exp_a)
                if stat.get("counts", {}).get("events_captured", 0) >= 3:
                    break
                await asyncio.sleep(0.2)
            check("A events flowing before the disconnect", stat.get("counts", {}).get("events_captured", 0) >= 3, counts=stat.get("counts"))

            # ---- B: MAME dies during the experiment -------------------------------------------------
            proc.kill()
            proc.wait()
            procs.remove(proc)
            t0 = time.time()
            b1 = await call(s, "experiment_status", experiment_id=exp_a)
            check("B call after MAME death fails with a clear, coded error (no hang, no fake data)", code_of(b1) in ("mame_disconnected", "connect_failed"),
                  code=code_of(b1), seconds=round(time.time() - t0, 1), error=b1.get("_error"))
            b2 = await call(s, "cancel_experiment", experiment_id=exp_a)
            check("B cancel while MAME is gone fails clearly and is not reported as done", b2.get("_error") is not None and code_of(b2) in ("mame_disconnected", "connect_failed"),
                  code=code_of(b2), error=b2.get("_error"))
            ro = await call(s, "read_memory", cpu=":maincpu", address=0, length=4)
            check("B reads fail clearly too", code_of(ro) in ("mame_disconnected", "connect_failed"), code=code_of(ro))

            # ---- C: MAME restarts on the same port; SAME MCP server process --------------------------
            proc, info, fx = new_mame()
            procs.append(proc)
            first = await wait_ready(s)
            check("C first call after restart reports session_changed (and nothing was sent for it)", code_of(first) == "session_changed",
                  error=first.get("_error"))
            second = await call(s, "session_info")
            sid_c = (second.get("bridge") or {}).get("session_id")
            check("C next call works against the NEW session (different session id)", bool(sid_c) and sid_c != sid_a, old=sid_a, new=sid_c)
            check("C new session is clean: no experiment, no owned breakpoints, request counter shows only our own calls",
                  second.get("active_experiment") is None and second.get("counters", {}).get("requests") == 2
                  and not any(b.get("owned_by_experiment") for d in second.get("cpus", []) for b in (d.get("breakpoints") or [])),
                  active=second.get("active_experiment"), counters=second.get("counters"), error=second.get("_error"))
            stale = await call(s, "experiment_status", experiment_id=exp_a)
            check("C stale experiment id from the old session is refused as belonging to another session", code_of(stale) == "experiment_from_other_session", error=stale.get("_error"))
            stale2 = await call(s, "cancel_experiment", experiment_id=exp_a)
            check("C cancelling a stale id cannot touch anything in the new session", code_of(stale2) == "experiment_from_other_session", error=stale2.get("_error"))
            lat = await call(s, "experiment_status")
            check("C default experiment_status does not inherit the old experiment", code_of(lat) == "unknown_experiment", error=lat.get("_error"))
            bridge_stats = second.get("bridge_client", {})
            note("C MCP-server transport counters (same server process throughout)", bridge_client=bridge_stats)

            # ---- D: MAME dies while a state-changing request is in flight ------------------------------
            await call(s, "execution_control", action="resume")
            res_holder: dict = {}

            async def do_capture():
                res_holder["r"] = await call(s, "capture_breakpoint", cpu=":maincpu", address=HANDLER, max_events=10000, max_wall_seconds=60, registers=["D0"], label="restart-e2e-D")

            async def killer():
                await asyncio.sleep(1.5)
                proc.kill()

            suspend(proc.pid)  # MAME cannot answer: the capture_breakpoint request is certainly in flight when it dies
            await asyncio.gather(do_capture(), killer())
            proc.wait()
            procs.remove(proc)
            d = res_holder["r"]
            err = d.get("_error")
            note("D outcome of the in-flight capture_breakpoint (MAME frozen, then killed)", result=err or d)
            check("D in-flight state-changing request fails with mame_disconnected and uncertain_state=true (never reported ok, never retried)",
                  bool(err) and err.get("code") == "mame_disconnected" and err.get("uncertain_state") is True, error=err)
            proc, info, fx = new_mame()
            procs.append(proc)
            first = await wait_ready(s)
            check("D after the second restart: session_changed again, nothing sent", code_of(first) == "session_changed", error=first.get("_error"))
            second = await call(s, "session_info")
            check("D new session received ONLY the handshake and this call (nothing replayed from the failed capture): requests == 2, no experiment, no owned breakpoint",
                  second.get("counters", {}).get("requests") == 2 and second.get("active_experiment") is None
                  and not any(b.get("owned_by_experiment") for dv in second.get("cpus", []) for b in (dv.get("breakpoints") or [])),
                  counters=second.get("counters"), active=second.get("active_experiment"))
            temps = fx.call("temps")["temps"]
            check("D new session: all debugger temporaries zero (no half-installed capture)", all(t == 0 for t in temps), temps=temps)
            sid_d = second["bridge"]["session_id"]

        # ---- E: MCP server process dies mid-experiment; MAME untouched -------------------------------------
        async with mcp_server() as s1:
            first = await wait_ready(s1)
            check("E fresh MCP server process attaches to the running MAME (same session id)", (first.get("bridge") or {}).get("session_id") == sid_d, session=(first.get("bridge") or {}).get("session_id"), error=first.get("_error"))
            await call(s1, "execution_control", action="resume")
            await asyncio.sleep(1.0)
            cap = await call(s1, "capture_breakpoint", cpu=":maincpu", address=HANDLER, max_events=10000, max_wall_seconds=6, registers=["D0"], label="restart-e2e-E")
            exp_e = cap.get("experiment_id")
            check("E experiment started (6 s wall limit)", bool(exp_e), experiment=exp_e)
        # the MCP client context closed: the server process was terminated without the experiment finishing
        check("E MAME still alive after the MCP server process ended", proc.poll() is None)
        await asyncio.sleep(8)
        async with mcp_server() as s2:
            first = await wait_ready(s2, 60)
            si2 = first if code_of(first) is None else await call(s2, "session_info")
            check("E new server process re-attaches to the SAME session (no session_changed)", (si2.get("bridge") or {}).get("session_id") == sid_d,
                  error=first.get("_error"), session=(si2.get("bridge") or {}).get("session_id"))
            st = await call(s2, "experiment_status", experiment_id=exp_e)
            check("E the experiment kept running inside MAME and finished on its wall-clock limit with verified cleanup",
                  st.get("state") == "completed" and st.get("completion_reason") == "wall_clock_limit" and st["cleanup"]["verified"] and st["counts"]["events_captured"] > 0,
                  summary=st.get("summary"), cleanup=st.get("cleanup"))
            temps = fx.call("temps")["temps"]
            check("E temporaries zero after the orphaned experiment ended", all(t == 0 for t in temps), temps=temps)
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
    return R.save(OUT / "results.json")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
