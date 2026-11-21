r"""Clean application exit of managed MAME sessions, with real MAME 0.289 through the official MCP SDK stdio client.

Two focused scenarios (Shinobi only; nothing from the routing, screenshot or capture suites is repeated):
  S  production stop_session on three live sessions, one per execution state (running, paused, debugger-stopped): each must stop
     through the clean-exit request (exit_method clean_exit_request, exit code 0, no terminate/kill, no cleanup failure), within a
     bound far below the old 5 s SIGTERM wait, while the other sessions stay responsive; the process is confirmed gone independently.
  D  normal server shutdown (stdin closed) with three sessions in the same three states, inside the SDK's UNMODIFIED termination
     window (PROCESS_TERMINATION_TIMEOUT is read, recorded and never changed): the server must exit by itself (the SDK never kills
     its process group), log a verified cleanup per session with exit_method=clean_exit_request, and every MAME process must be gone
     as confirmed independently of the server's own report.
Every MAME pid the harness sees is recorded with its identity; the final cleanup only stops a pid whose image and creation time still
match (a pid that was already stopped is never touched).  Writes results.json under $MAMEMCP_HARNESS_RUNS (default <repo>/runs).
Usage:  <venv>/bin/python -I scripts/mcp_exit_e2e.py   (Windows: .venv\Scripts\python -I scripts\mcp_exit_e2e.py)
        off Windows also set MAMEMCP_HARNESS_MAME_DIR / _ROMPATH / _MAME_RUNTIME (see _fx.py). The server is started as 'python -I -m mame_mcp.server'.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fx import DISPLAY_ENV, MAME_DIR, MAME_RUNTIME, ROMPATH, Results, body_of  # noqa: E402

import mcp.client.stdio as sdk_stdio  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from mame_mcp import procinfo  # noqa: E402
from mame_mcp.launcher import PROJECT_ROOT  # noqa: E402

OUT = Path(os.environ.get("MAMEMCP_HARNESS_RUNS") or PROJECT_ROOT / "runs") / "mcp_exit_e2e"
R = Results("mcp_exit_e2e")
check, note = R.check, R.note
STATES = ("running", "paused", "debugger_stopped")
PIDS: dict[int, tuple[str | None, int | None]] = {}  # pid -> (image, creation time) recorded while it was known to be ours
SDK_KILLS: list[float] = []
SDK_WINDOW_S = float(sdk_stdio.PROCESS_TERMINATION_TIMEOUT)  # recorded, never changed
STOP_BOUND_S = 2.0  # a clean exit measured 0.06-0.36 s; the old SIGTERM path always cost >= 5 s


def alive(pid: int) -> bool:
    return bool(procinfo.process_image(pid))


def still_ours(pid: int) -> bool:
    img, created = procinfo.process_image(pid), procinfo.creation_time(pid)
    return bool(img) and (img, created) == PIDS.get(pid)


def server_env() -> dict[str, str]:
    env = {"SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "MAMEMCP_MAME_DIR": str(MAME_DIR), "MAMEMCP_RUNS_DIR": str(OUT / "sessions"), "MAMEMCP_ROMPATH": str(ROMPATH)}
    if MAME_RUNTIME is not None:
        env["MAMEMCP_MAME_RUNTIME"] = str(MAME_RUNTIME)
    env.update({k: os.environ[k] for k in DISPLAY_ENV if os.environ.get(k)})
    return env


def instrument_sdk() -> None:
    """Record (never prevent, never delay) an SDK force-kill of the server's process group."""
    orig = sdk_stdio._terminate_process_tree

    async def recorded(process):
        SDK_KILLS.append(time.time())
        await orig(process)

    sdk_stdio._terminate_process_tree = recorded


@asynccontextmanager
async def mcp_server(errname: str):
    # The production invocation. The interpreter running this harness must be a venv whose editable install of mame_mcp points at the
    # checkout under test: under -I neither PYTHONPATH nor the current directory selects the code.
    params = StdioServerParameters(command=sys.executable, args=["-I", "-m", "mame_mcp.server"], env=server_env())
    with open(OUT / errname, "a") as errlog:
        async with stdio_client(params, errlog=errlog) as (rd, wr):
            async with ClientSession(rd, wr) as s:
                await s.initialize()
                yield s


async def call(s, name, **args) -> dict:
    return body_of(await s.call_tool(name, args))


async def until(pred, s, sid, timeout=20.0):
    end, last = time.time() + timeout, None
    while time.time() < end:
        last = await call(s, "session_info", session_id=sid)
        if pred(last):
            return last
        await asyncio.sleep(0.2)
    return last


async def put_in_state(s, sid: str, state: str) -> dict:
    """running / paused / debugger_stopped (a fresh session starts debugger-stopped). Running and paused are proved by frames advancing."""
    if state == "debugger_stopped":
        return await call(s, "session_info", session_id=sid)
    await call(s, "execution_control", action="resume", session_id=sid)
    f0 = (await call(s, "session_info", session_id=sid)).get("clock", {}).get("frame", 0)
    await until(lambda x: x.get("clock", {}).get("frame", 0) > f0 + 5, s, sid)
    if state == "paused":
        await call(s, "execution_control", action="pause", session_id=sid)
        return await until(lambda x: x.get("execution", {}).get("state") == "paused", s, sid)
    return await call(s, "session_info", session_id=sid)


async def start_three(s) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for state in STATES:  # sequential: dependent operations, explicit ids
        r = await call(s, "start_session", system="shinobi")
        if r.get("pid"):
            PIDS[r["pid"]] = (procinfo.process_image(r["pid"]), procinfo.creation_time(r["pid"]))
        out[state] = r
    ok = all(r.get("state") == "ready" for r in out.values()) and len({r.get("pid") for r in out.values()}) == 3
    check("setup: three distinct shinobi sessions started and ready", ok, results={k: {x: v.get(x) for x in ("session_id", "pid", "port", "_error")} for k, v in out.items()})
    if not ok:
        raise RuntimeError("setup failed")
    for state, r in out.items():
        info = await put_in_state(s, r["session_id"], state)
        got = info.get("execution", {}).get("state")
        check(f"setup: session for '{state}' is {state} (frame {info.get('clock', {}).get('frame')})", got == state, state=got)
        if got != state:
            raise RuntimeError("setup state mismatch")
    return out


# ---- S: production stop_session in each state ----------------------------------------------------------------------------------
async def scenario_S() -> None:
    async with mcp_server("server_stderr_S.txt") as s:
        ses = await start_three(s)
        for state in STATES:
            r, sid, pid = ses[state], ses[state]["session_id"], ses[state]["pid"]
            others = [x["session_id"] for k, x in ses.items() if k != state and alive(x["pid"])]
            t0 = time.monotonic()
            st = await call(s, "stop_session", session_id=sid)
            dt = round(time.monotonic() - t0, 2)
            p = st.get("process") or {}
            check(f"S [{state}] stop_session used the clean-exit request: exit_method=clean_exit_request, exit code 0, no terminate/kill, no cleanup failure",
                  st.get("stopped") is True and p.get("exit_method") == "clean_exit_request" and p.get("exit_code") == 0 and p.get("terminated") is True
                  and "escalated_to_terminate" not in p and "escalated_to_kill" not in p and not p.get("still_running") and st.get("cleanup_failures") == []
                  and (p.get("exit_request") or {}).get("reply") == "accepted",
                  exit_method=p.get("exit_method"), exit_code=p.get("exit_code"), exit_request=p.get("exit_request"), identity_check=p.get("identity_check"),
                  failures=st.get("cleanup_failures"), error=st.get("_error"))
            check(f"S [{state}] it completed within {STOP_BOUND_S:g} s (previously >= 5 s of SIGTERM wait)", dt < STOP_BOUND_S, seconds=dt)
            time.sleep(0.3)
            check(f"S [{state}] independently: the MAME process (pid {pid}) is gone", not alive(pid))
            sleep_info = [await call(s, "session_info", session_id=o) for o in others]
            check(f"S [{state}] the remaining sessions stayed responsive and untouched", all(i.get("session", {}).get("state") == "ready" for i in sleep_info),
                  remaining=[i.get("session", {}).get("session_id") for i in sleep_info])
        ls = await call(s, "list_sessions")
        check("S zero live sessions after the three explicit stops", ls.get("live_count") == 0, live=ls.get("live_count"))


# ---- D: normal server shutdown inside the SDK's unmodified termination window ---------------------------------------------------
async def scenario_D() -> None:
    t_leave = None
    ses: dict = {}
    try:
        async with mcp_server("server_stderr_D.txt") as s:
            ses = await start_three(s)
            t_leave = time.time()
    finally:
        pass
    seconds = round(time.time() - t_leave, 2) if t_leave else None
    check(f"D the SDK termination window was left unmodified ({SDK_WINDOW_S:g} s)", float(sdk_stdio.PROCESS_TERMINATION_TIMEOUT) == SDK_WINDOW_S == 2.0, window_s=SDK_WINDOW_S)
    check("D the server exited by itself after stdin closed: the SDK never had to kill its process group", not SDK_KILLS and seconds is not None and seconds < SDK_WINDOW_S,
          sdk_force_kills=len(SDK_KILLS), seconds_from_close_to_return=seconds, window_s=SDK_WINDOW_S)
    lines = [ln for ln in (OUT / "server_stderr_D.txt").read_text(errors="replace").splitlines() if "shutdown cleanup:" in ln]
    by_run = {r["run_id"]: [ln for ln in lines if f"session={r['run_id']} " in ln] for r in ses.values()}
    check("D the server's own teardown logged exactly one verified cleanup per session (running, paused, debugger-stopped): process terminated through "
          "exit_method=clean_exit_request, exit code 0, no failures", len(lines) == 3 and all(len(v) == 1 for v in by_run.values())
          and all("process_terminated=True" in v[0] and "exit_method=clean_exit_request" in v[0] and "exit_code=0" in v[0] and "failures=[]" in v[0] for v in by_run.values()),
          lines=lines)
    end = time.time() + 3
    while time.time() < end and any(alive(r["pid"]) for r in ses.values()):
        time.sleep(0.2)
    left = {k: r["pid"] for k, r in ses.items() if alive(r["pid"])}
    check("D independently of the server's report: none of the three MAME processes is still running", not left, left=left)


def final_cleanup() -> None:
    """Stop only a process this harness recorded and that is still the same process (image + creation time); nothing else."""
    for pid in list(PIDS):
        if still_ours(pid):
            note("cleanup: a recorded MAME was still running and is stopped by the harness (this is a failure of the scenario above)", pid=pid)
            os.kill(pid, signal.SIGKILL)


async def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    pre = [p for p in os.listdir("/proc") if p.isdigit() and (procinfo.process_image(int(p)) or "").endswith("/lib/mame/mame")] if sys.platform == "linux" else []
    note("platform / preflight", platform=sys.platform, sdk_window_s=SDK_WINDOW_S, mame_already_running=pre, mame_dir=str(MAME_DIR), runtime=str(MAME_RUNTIME))
    if pre:
        note("a MAME is already running: not started, nothing touched")
        return 2
    instrument_sdk()
    try:
        await scenario_S()
        await scenario_D()
    except Exception as e:  # noqa: BLE001
        check(f"harness aborted: {type(e).__name__}: {e}", False)
    finally:
        final_cleanup()
    return R.save(OUT / "results.json", platform=sys.platform)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
