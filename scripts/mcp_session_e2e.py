"""Managed-session end-to-end through the official MCP SDK client, with real MAME 0.289 / Shinobi.

ONE MCP server process (the production entry point, `python -I -m mame_mcp.server`) is started with NO MAME running and
NO MAMEMCP_PORT / launch.json / pre-chosen run directory; everything below goes through the MCP tools only:
  A  initial connection with no MAME; session_info/stop_session/bridge tools behave without a session
  B  start_session(shinobi); identity, evidence dir, second start refused, test-only hooks not inherited
  C  stopped-state reads, resume, bounded capture at the validated Shinobi address (0x404), evidence files
  D  stop_session with a capture still running: capture cleanup verified, only the owned process stopped
  E  second start in the same server: new session id, old experiment ids refused
  F  unexpected emulator exit (the owned MAME is killed from outside): detected, reported, recoverable
  G  startup failure (second server, corrupt ROM zip): diagnostics, FAILED state, only its own process cleaned up
  H  normal MCP-server shutdown (stdin closed) stops its owned MAME
An UNRELATED MAME (separate isolated instance on its own port, launched by this script) must survive everything.
Writes runs/mcp_session_e2e/results.json.   Usage: .venv\\Scripts\\python -I scripts\\mcp_session_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fx import EXPECTED_TOOLS, MAME_DIR, ROMPATH, Results, body_of, code_of, launch_mame  # noqa: E402

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from mame_mcp import winwake  # noqa: E402
from mame_mcp.launcher import PROJECT_ROOT  # noqa: E402

OUT = PROJECT_ROOT / "runs" / "mcp_session_e2e"
UNRELATED_PORT = 9481
HANDLER = 0x404  # the validated Shinobi fixture address (68000 level-4 autovector handler), as in example_experiment.py
R = Results("mcp_session_e2e")
check, note = R.check, R.note


def alive(pid: int, created: int | None = None) -> bool:
    img = winwake.process_image(pid)
    return bool(img) and (created is None or winwake.creation_time(pid) == created)


async def wait_dead(pid: int, timeout: float = 15.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not alive(pid):
            return True
        await asyncio.sleep(0.2)
    return not alive(pid)


def server_env(**extra) -> dict[str, str]:
    env = {"SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "MAMEMCP_MAME_DIR": str(MAME_DIR), "MAMEMCP_ROMPATH": str(ROMPATH),
           "MAMEMCP_RUNS_DIR": str(OUT / "sessions")}
    env.update(extra)
    return env


@asynccontextmanager
async def mcp_server(env: dict[str, str], errname: str):
    params = StdioServerParameters(command=sys.executable, args=["-I", "-m", "mame_mcp.server"], env=env)
    errlog = open(OUT / errname, "a")
    try:
        async with stdio_client(params, errlog=errlog) as (rd, wr):
            async with ClientSession(rd, wr) as s:
                await s.initialize()
                yield s
    finally:
        errlog.close()


async def call(s, name, **args) -> dict:
    return body_of(await s.call_tool(name, args))


async def until(s, pred, tool="session_info", timeout=30.0, **args):
    end, last = time.time() + timeout, None
    while time.time() < end:
        last = await call(s, tool, **args)
        if pred(last):
            return last
        await asyncio.sleep(0.3)
    return last


async def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    # An unrelated MAME: not launched by the MCP server, must never be attached to, stopped or modified.
    uproc, uinfo = launch_mame(system="shinobi", port=UNRELATED_PORT, run_dir=OUT / "unrelated")
    upid, ucreated = uproc.pid, uinfo["pid_created_filetime"]
    note("unrelated MAME started by the harness (not by the MCP server)", pid=upid, port=UNRELATED_PORT)
    try:
        # the environment deliberately carries a test-only hook: production sessions must NOT inherit it
        async with mcp_server(server_env(MAMEMCP_TEST_FAIL_CLEAR="1"), "server_stderr_main.txt") as s:
            # ---- A: no MAME ----------------------------------------------------------------------------
            tools = sorted(t.name for t in (await s.list_tools()).tools)
            check("A server initialises with no MAME; tools listed", tools == EXPECTED_TOOLS, tools=tools)
            by_name = {t.name: t for t in (await s.list_tools()).tools}
            check("A start_session takes only a system name and a bounded timeout (no paths, executables or MAME arguments)",
                  sorted(by_name["start_session"].input_schema["properties"]) == ["ready_timeout_s", "system"])
            si = await call(s, "session_info")
            check("A session_info works before any MAME: no_session, config visible, nothing attached", si["session"]["state"] == "no_session"
                  and si["session"]["ownership"] == "none" and "cpus" not in si, session=si["session"])
            rm = await call(s, "read_memory", cpu=":maincpu", address=0, length=4)
            check("A bridge tools report no_session (the unrelated MAME on its own port is NOT attached)", code_of(rm) == "no_session", error=rm.get("_error"))
            st = await call(s, "stop_session")
            check("A stop_session with nothing to stop is a harmless no-op", st.get("stopped") is False and st.get("state") == "no_session", result=st)
            bad = await call(s, "start_session", system="shinobi -debug")
            bad2 = await call(s, "start_session", system="pacman")
            check("A invalid / unknown systems refused before any launch", code_of(bad) == "bad_argument" and code_of(bad2) == "rom_not_found",
                  codes=[code_of(bad), code_of(bad2)])

            # ---- B: start shinobi ----------------------------------------------------------------------
            t0 = time.time()
            r1 = await call(s, "start_session", system="shinobi")
            check("B start_session(shinobi) ready", r1.get("state") == "ready" and r1.get("machine", {}).get("name") == "shinobi",
                  seconds=round(time.time() - t0, 1), result={k: r1.get(k) for k in ("state", "session_id", "machine", "execution", "pid", "port", "mame_version")},
                  error=r1.get("_error"))
            pid1, sid1 = r1["pid"], r1["session_id"]
            check("B run and evidence directories created, unique, under the configured runs dir",
                  Path(r1["evidence_dir"]).is_dir() and Path(r1["run_dir"]).parent == (OUT / "sessions").resolve() and (Path(r1["run_dir"]) / "launch.json").is_file(),
                  run_dir=r1["run_dir"])
            check("B MAME starts stopped in the debugger; romset verified good", r1["execution"]["state"] == "debugger_stopped"
                  and "is good" in (r1["romset"]["verifyroms"] or ""), romset=r1["romset"])
            r1b = await call(s, "start_session", system="shinobi")
            check("B second start refused while a managed session is active (nothing launched)", code_of(r1b) == "session_active", error=r1b.get("_error"))
            check("B the launched MAME is a different process from the unrelated one", pid1 != upid and alive(upid, ucreated) and alive(pid1))

            # ---- C: inspect, resume, bounded capture ----------------------------------------------------
            si = await call(s, "session_info")
            check("C session_info (stopped): ready/managed, machine, CPUs, execution, wake configured from the launch result, ROM info merged",
                  si["session"]["state"] == "ready" and si["session"]["ownership"] == "managed" and si["machine"]["name"] == "shinobi"
                  and si["execution"]["state"] == "debugger_stopped" and si["bridge_client"]["wake_configured"] is True
                  and "is good" in (si["rom_identity"]["launch_info"]["verifyroms"] or "") and si["session"]["session_id"] == sid1,
                  state=si["session"]["state"], execution=si["execution"], bridge_client=si["bridge_client"])
            check("C test-only hooks are NOT inherited by the production session", si["test_hooks"] == [], test_hooks=si["test_hooks"])
            regs = await call(s, "read_registers", cpu=":maincpu", registers=["CURPC", "SR"])
            check("C stopped-state register read works", "_error" not in regs and {r["name"] for r in regs["registers"]} == {"CURPC", "SR"}, regs=regs.get("registers"), error=regs.get("_error"))
            await call(s, "execution_control", action="resume")
            run = await until(s, lambda x: x.get("execution", {}).get("state") == "running")
            check("C resume -> running", run.get("execution", {}).get("state") == "running", execution=run.get("execution"))
            cap = await call(s, "capture_breakpoint", cpu=":maincpu", address=HANDLER, max_events=30, max_wall_seconds=60, registers=["D0", "SR"], label="session-e2e")
            exp1 = cap.get("experiment_id")
            check("C bounded capture started at 0x404", bool(exp1) and exp1.startswith(f"exp-{sid1}-"), experiment=exp1, error=cap.get("_error"))
            st = await until(s, lambda x: x.get("state") in ("completed", "failed", "cancelled"), tool="experiment_status", experiment_id=exp1, timeout=60)
            check("C capture completed: 30 events, cleanup verified, execution still running", st.get("state") == "completed"
                  and st["counts"]["events_captured"] == 30 and st["cleanup"]["verified"] and (st.get("final_execution") or {}).get("state") == "running",
                  state=st.get("state"), counts=st.get("counts"), cleanup=st.get("cleanup"), reason=st.get("completion_reason"), final_execution=st.get("final_execution"))
            files = st.get("evidence") or st.get("evidence_files") or {}
            ev = Path(r1["evidence_dir"]) / exp1
            check("C evidence files exist in this session's evidence dir", (ev / "manifest.json").is_file() and (ev / "events.jsonl").is_file()
                  and sum(1 for _ in open(ev / "events.jsonl")) == 30, dir=str(ev), reported=files)

            # ---- D: stop with a capture still running -----------------------------------------------------
            cap2 = await call(s, "capture_breakpoint", cpu=":maincpu", address=HANDLER, max_events=10000, max_wall_seconds=300, registers=["D0"], label="session-e2e-active")
            exp_active = cap2.get("experiment_id")
            await asyncio.sleep(1.0)
            t0 = time.time()
            stp = await call(s, "stop_session")
            check("D stop_session cancelled the active capture (verified) and stopped the owned MAME", stp.get("stopped") is True
                  and stp["capture_cleanup"]["active_experiment"] == exp_active and stp["capture_cleanup"]["verified"] is True
                  and stp["process"]["terminated"] is True and stp["cleanup_failures"] == [],
                  seconds=round(time.time() - t0, 1), capture_cleanup=stp.get("capture_cleanup"), process=stp.get("process"), failures=stp.get("cleanup_failures"), error=stp.get("_error"))
            check("D owned MAME process is gone; the unrelated MAME is untouched", await wait_dead(pid1) and alive(upid, ucreated), pid=pid1)
            m = json.loads((Path(r1["evidence_dir"]) / exp_active / "manifest.json").read_text())
            check("D cancelled capture's evidence was finalised", m.get("state") == "cancelled" or m.get("completion_reason") == "cancelled", state=m.get("state"), reason=m.get("completion_reason"))
            si = await call(s, "session_info")
            check("D session_info after stop: no_session, last_session summarised, no bridge data", si["session"]["state"] == "no_session"
                  and si["session"]["last_session"]["session_id"] == sid1 and "cpus" not in si, last=si["session"].get("last_session", {}).get("state"))
            rm = await call(s, "read_memory", cpu=":maincpu", address=0, length=4)
            check("D bridge tools refuse cleanly after stop (nothing sent to the dead MAME)", code_of(rm) == "no_session", error=rm.get("_error"))

            # ---- E: second session in the same server process ---------------------------------------------
            r2 = await call(s, "start_session", system="shinobi")
            sid2, pid2 = r2.get("session_id"), r2.get("pid")
            check("E second start_session in the SAME server process: new session, new run dir", r2.get("state") == "ready" and sid2 != sid1
                  and r2["run_dir"] != r1["run_dir"] and pid2 != pid1, old=sid1, new=sid2, error=r2.get("_error"))
            old = await call(s, "experiment_status", experiment_id=exp1)
            old_cancel = await call(s, "cancel_experiment", experiment_id=exp_active)
            lat = await call(s, "experiment_status")
            check("E experiment ids from the old session are refused (status, cancel); default status does not inherit",
                  code_of(old) == "experiment_from_other_session" and code_of(old_cancel) == "experiment_from_other_session" and code_of(lat) == "unknown_experiment",
                  codes=[code_of(old), code_of(old_cancel), code_of(lat)])
            await call(s, "execution_control", action="resume")
            await until(s, lambda x: x.get("execution", {}).get("state") == "running")
            cap3 = await call(s, "capture_breakpoint", cpu=":maincpu", address=HANDLER, max_events=10000, max_wall_seconds=120, registers=["D0"], label="session-e2e-orphan")
            check("E new session captures normally", (cap3.get("experiment_id") or "").startswith(f"exp-{sid2}-"), experiment=cap3.get("experiment_id"), error=cap3.get("_error"))

            # ---- F: unexpected exit (killed from outside, capture running) -----------------------------------
            subprocess.run(["taskkill", "/F", "/PID", str(pid2)], capture_output=True)
            await wait_dead(pid2)
            si = await call(s, "session_info")
            check("F unexpected exit reported by session_info: state exited, exit code, evidence dir, no hang", si["session"]["state"] == "exited"
                  and si["session"]["exit_reason"] == "unexpected_exit" and si["session"]["exit_code"] is not None and si["session"]["evidence_dir"],
                  session={k: si["session"].get(k) for k in ("state", "exit_code", "exit_reason", "failure")})
            rm = await call(s, "read_memory", cpu=":maincpu", address=0, length=4)
            cs = await call(s, "capture_breakpoint", cpu=":maincpu", address=HANDLER, max_events=5, max_wall_seconds=5)
            check("F bridge tools refuse with session_exited (no reconnect attempt to the dead MAME)", code_of(rm) == "session_exited" and code_of(cs) == "session_exited",
                  codes=[code_of(rm), code_of(cs)])
            check("F the unrelated MAME is still untouched", alive(upid, ucreated))
            r3 = await call(s, "start_session", system="shinobi")
            sid3, pid3 = r3.get("session_id"), r3.get("pid")
            check("F start_session after an unexpected exit replaces the exited record; new session is clean", r3.get("state") == "ready" and sid3 not in (sid1, sid2),
                  new=sid3, error=r3.get("_error"))
            si = await call(s, "session_info")
            check("F exited session kept as last_session; active_experiment empty in the new one", si["session"]["last_session"]["state"] == "exited"
                  and si.get("active_experiment") is None, last=si["session"]["last_session"].get("exit_reason"))
            stp = await call(s, "stop_session")
            check("F final stop_session clean", stp.get("stopped") is True and stp["process"]["terminated"] is True and await wait_dead(pid3), failures=stp.get("cleanup_failures"))

        # ---- G: startup failure (a second server whose ROM path holds a corrupt shinobi.zip) ---------------------
        bad_roms = OUT / "bad_roms"
        bad_roms.mkdir()
        (bad_roms / "shinobi.zip").write_bytes(b"this is not a zip archive")
        async with mcp_server(server_env(MAMEMCP_ROMPATH=str(bad_roms)), "server_stderr_fail.txt") as s2:
            t0 = time.time()
            f = await call(s2, "start_session", system="shinobi", ready_timeout_s=40)
            e = f.get("_error") or {}
            d = e.get("detail") or {}
            check("G startup failure reported with diagnostics (code, exit code, MAME output, run dir), promptly", e.get("code") == "mame_exited_during_startup"
                  and d.get("exit_code") not in (None, 0) and d.get("stdout_tail") and d.get("run_dir"), seconds=round(time.time() - t0, 1),
                  code=e.get("code"), exit_code=d.get("exit_code"), tail=(d.get("stdout_tail") or "")[:160])
            si = await call(s2, "session_info")
            check("G session_info shows failed with the same diagnostics; no process left behind", si["session"]["state"] == "failed"
                  and si["session"]["failure"]["code"] == "mame_exited_during_startup", state=si["session"]["state"])
            check("G the unrelated MAME is still untouched after the failed start", alive(upid, ucreated))
            stp = await call(s2, "stop_session")
            check("G stop_session acknowledges the failed record", stp.get("stopped") is True, result={k: stp.get(k) for k in ("state", "stopped", "previous_state")})

        # ---- H: normal MCP-server shutdown cleans up its owned MAME ---------------------------------------------
        pid_h = None
        async with mcp_server(server_env(), "server_stderr_shutdown.txt") as s3:
            rh = await call(s3, "start_session", system="shinobi")
            pid_h = rh.get("pid")
            await call(s3, "execution_control", action="resume")
            await call(s3, "capture_breakpoint", cpu=":maincpu", address=HANDLER, max_events=10000, max_wall_seconds=120, registers=["D0"])
            check("H session with an active capture is running before server shutdown", rh.get("state") == "ready" and alive(pid_h), pid=pid_h)
        # leaving the context closed the server's stdin (the SDK waits up to 2 s, then kills the process tree)
        gone = await wait_dead(pid_h, 15) if pid_h else False
        stderr = (OUT / "server_stderr_shutdown.txt").read_text(errors="replace")
        check("H normal server shutdown stopped its owned MAME", gone, pid=pid_h)
        check("H the server's own graceful cleanup ran (not only the SDK's process-tree kill)", "shutdown cleanup:" in stderr and "process_terminated=True" in stderr and "capture_verified=True" in stderr,
              stderr_tail=stderr[-300:])
        check("H the unrelated MAME is STILL untouched at the very end", alive(upid, ucreated) and uproc.poll() is None)
    finally:
        if uproc.poll() is None:
            uproc.kill()
    return R.save(OUT / "results.json")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
