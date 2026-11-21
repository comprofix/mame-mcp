r"""Multi-session end-to-end through the official MCP SDK stdio client, with real MAME 0.289.

ONE production MCP server (`python -I -m mame_mcp.server`, default session limit, no MAMEMCP_PORT) drives three simultaneously active
managed sessions: two of the same system (shinobi) and a third (1942). Machine-specific addresses and inputs come from
tests/fixtures/profiles/multisession.json. Everything goes through MCP tools only.
  A  server start: tool surface (every session-bound tool takes session_id, list_sessions exists), no sessions, default limit 3
  B  three simultaneous starts: distinct sessions, processes, ports (each owned by its own process) and output directories; the
     fourth start is refused by the limit before any process is launched
  C  omitted session_id is refused as ambiguous and nothing changes; unknown ids are refused
  D  explicit routing of RAM writes and reads (distinct markers), ROM reads and execution state per session
  E  pause/resume in one session leaves the others' execution state unchanged
  F  screenshots routed per session (own evidence directory, own system)
  G  independent bounded captures overlapping across the three sessions; evidence in each session's own directory
  H  wrong-session experiment ids are refused without anything being sent
  I  checkpoints: save/load affect only the target; stale experiment ids after a load; deliberate cross-session reuse needs an
     explicit target and a compatible machine
  J  input and sequence cleanup affect only their target
  K  stopping one session leaves the others responsive (a capture keeps running); stale id is reported as ended
  L  a failed start (MAME itself rejects a corrupt ROM set, or with the same-machine profile a ROM set verified absent in
     preflight and given as an empty folder) does not disturb existing sessions; the slot stays usable
With MAMEMCP_MULTISESSION_PROFILE=multisession_same_machine.json all three sessions run shinobi, and every check that needs two
different machines is recorded as NOT EXERCISED (INFO), never as passed.
  M  normal server shutdown (stdin closed) cleans up every remaining owned MAME, including one with an active capture
Writes results.json under $MAMEMCP_HARNESS_RUNS (default <repo>/runs)/mcp_multisession_e2e.
Usage:  .venv\Scripts\python -I scripts\mcp_multisession_e2e.py [--only LM]
  --only LM  fresh bounded setup (sessions A and C), then only L and M (shared scenario functions; A..K are not repeated)
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
from _fx import DISPLAY_ENV, EXPECTED_TOOLS, MAME_DIR, MAME_RUNTIME, ROMPATH, Results, body_of, code_of  # noqa: E402

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from mame_mcp import launcher, pathutil, procinfo  # noqa: E402
from mame_mcp import session as session_mod  # noqa: E402
import mcp.client.stdio as sdk_stdio  # noqa: E402
from mame_mcp.launcher import PROJECT_ROOT  # noqa: E402

OUT = Path(os.environ.get("MAMEMCP_HARNESS_RUNS") or PROJECT_ROOT / "runs") / "mcp_multisession_e2e"
# Default: the mixed-machine profile (two shinobi + one 1942). MAMEMCP_MULTISESSION_PROFILE selects another profile file in
# tests/fixtures/profiles, e.g. multisession_same_machine.json (three shinobi) on a machine without the 1942 set.
PROFILE = os.environ.get("MAMEMCP_MULTISESSION_PROFILE", "").strip() or "multisession.json"
FX = json.loads((PROJECT_ROOT / "tests" / "fixtures" / "profiles" / PROFILE).read_text())
SPEC = {x["name"]: FX[x["system"]] for x in FX["sessions"]}  # each session's own machine facts (cpu, capture, ram, inputs)
SH = FX["shinobi"]  # sessions a and b are shinobi in every profile
MIXED = len({x["system"] for x in FX["sessions"]}) > 1
H = lambda s: int(s, 16)  # noqa: E731
RAM = H(SH["ram"]["address"])
R = Results("mcp_multisession_e2e")
check, note = R.check, R.note
NOT_SESSION_BOUND = {"start_session", "list_sessions"}


def not_exercised(label: str, **evidence) -> None:
    """A check that needs two DIFFERENT machines, on a same-machine profile: recorded as not exercised, never as passed."""
    note(f"NOT EXERCISED (all sessions run the same machine): {label}", **evidence)


def marker(n: str) -> str | None:
    """RAM marker for session n: the profile's per-session marker, else the shinobi block's marker_<n> (mixed profile: a, b)."""
    x = next(x for x in FX["sessions"] if x["name"] == n)
    return x.get("marker") or (SPEC[n].get("ram") or {}).get(f"marker_{n}")


class Abort(Exception):
    """A check that later scenarios depend on failed: stop here (cleanup still runs) instead of continuing on bad state."""


def gate(label: str, ok: bool, **evidence) -> None:
    check(label, ok, **evidence)
    if not ok:
        raise Abort(label)


def alive(pid: int) -> bool:
    return bool(procinfo.process_image(pid))


def identity(pid: int) -> tuple[str | None, int | None]:
    return procinfo.process_image(pid), procinfo.creation_time(pid)


def still_ours(pid: int, ident: tuple[str | None, int | None]) -> bool:
    """The pid is still the process recorded at start (same image and creation time), so a reused pid is never killed."""
    img, created = identity(pid)
    return bool(img) and img == ident[0] and created is not None and created == ident[1]


def mame_count() -> int:
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq mame.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
        return sum(1 for ln in out.splitlines() if ln.lower().startswith('"mame.exe"'))
    return len(subprocess.run(["pgrep", "-x", "mame"], capture_output=True, text=True).stdout.split())


def server_env() -> dict[str, str]:
    env = {"SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "MAMEMCP_MAME_DIR": str(MAME_DIR), "MAMEMCP_RUNS_DIR": str(OUT / "sessions"),
           "MAMEMCP_ROMPATH": f"{ROMPATH};{OUT / 'bad_roms'}"}  # the second directory holds only the failed-start fixture
    if MAME_RUNTIME is not None:
        env["MAMEMCP_MAME_RUNTIME"] = str(MAME_RUNTIME)
    env.update({k: os.environ[k] for k in DISPLAY_ENV if os.environ.get(k)})
    return env


def mame_exe() -> Path:
    return MAME_DIR / ("mame.exe" if sys.platform == "win32" else "mame")


def bad_rom_fixture() -> tuple[str, str]:
    """Create the failed-start fixture in the harness output only. Returns (system, kind). Mixed profile: a corrupt pacman.zip as
    before. Same-machine profile: a system verified (here) to be absent from every ROM path, given to the server as an EMPTY
    folder, so MAME itself fails for missing files; no ROM data is created, downloaded or modified."""
    (OUT / "bad_roms").mkdir(parents=True)
    if "missing_rom_system" in FX:
        system = FX["missing_rom_system"]["system"]
        paths = [ROMPATH, *launcher.mame_config_paths(mame_exe())["rompath"]]
        present = [str(p) for p in paths if (p / f"{system}.zip").exists() or (p / f"{system}.7z").exists() or (p / system).exists()]
        known = system in subprocess.run([str(mame_exe()), "-listfull", system], capture_output=True, text=True).stdout.split()
        if present or not known:
            raise SystemExit(f"preflight: missing-ROM fixture '{system}' unusable (known to MAME: {known}; present in: {present})")
        (OUT / "bad_roms" / system).mkdir()
        note("preflight: missing-ROM fixture established", system=system, known_to_mame=known, searched=[str(p) for p in paths], fixture="empty folder")
        return system, "missing"
    system = FX["corrupt_rom_system"]["system"]
    (OUT / "bad_roms" / f"{system}.zip").write_bytes(b"this is not a zip archive")
    return system, "corrupt"


@asynccontextmanager
async def mcp_server(errname: str):
    params = StdioServerParameters(command=sys.executable, args=["-I", "-m", "mame_mcp.server"], env=server_env())
    errlog = open(OUT / errname, "a")
    try:
        async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as s:
                await s.initialize()
                yield s
    finally:
        errlog.close()


async def call(s, name, **args) -> dict:
    return body_of(await s.call_tool(name, args))


async def until(pred, name, s, timeout=30.0, **args):
    end, last = time.time() + timeout, None
    while time.time() < end:
        last = await call(s, name, **args)
        if pred(last):
            return last
        await asyncio.sleep(0.25)
    return last


async def info(s, sid) -> dict:
    return await call(s, "session_info", session_id=sid)


async def state_of(s, sid) -> str | None:
    return (await info(s, sid)).get("execution", {}).get("state")


async def frame_of(s, sid) -> int | None:
    return (await info(s, sid)).get("clock", {}).get("frame")


async def rd(s, sid, cpu, address, length=1) -> str | None:
    r = await call(s, "read_memory", cpu=cpu, space="program", address=address, length=length, session_id=sid)
    return r.get("data_hex") or r.get("bytes_hex")


async def run(s, sid):
    await call(s, "execution_control", action="resume", session_id=sid)
    return await until(lambda x: x.get("execution", {}).get("state") == "running", "session_info", s, session_id=sid)


async def pause(s, sid):
    await call(s, "execution_control", action="pause", session_id=sid)
    return await until(lambda x: x.get("execution", {}).get("state") == "paused", "session_info", s, session_id=sid)


async def wait_done(s, sid, exp, timeout=60.0):
    return await until(lambda x: x.get("state") in ("completed", "failed", "cancelled"), "experiment_status", s, timeout=timeout,
                       session_id=sid, experiment_id=exp)


def cap_args(spec, events, wall=60):
    c = spec["capture"]
    return {"cpu": spec["cpu"], "address": H(c["address"]), "max_events": events, "max_wall_seconds": wall, "registers": c["registers"]}


# Every MAME pid this harness saw start, with the identity recorded while it was known to be ours: pid -> (image, creation time).
# Module-level on purpose: a helper defined inside main() can be shadowed by a scenario's local variable of the same name.
PIDS: dict[int, tuple[str | None, int | None]] = {}


def track_pid(pid: int | None) -> None:
    if pid and pid not in PIDS:
        PIDS[pid] = identity(pid)


# ---- SDK teardown instrumentation (scenario M) ---------------------------------------------------------------------------------------
# Leaving stdio_client closes the server's stdin, waits PROCESS_TERMINATION_TIMEOUT (2 s in mcp 2.3) for it to exit and otherwise kills
# the server's whole process group, MAME children included. Then "no MAME left" proves nothing about the server's own cleanup. So the
# wait is raised to a bound longer than the server's own shutdown bound, and every SDK force-kill is recorded: M passes only if the SDK
# never had to kill and the server itself logged a verified teardown of every session.
SERVER_EXIT_BOUND_S = session_mod.SHUTDOWN_BOUND_S + 10.0
SDK_KILLS: list[float] = []


def instrument_sdk_teardown() -> None:
    if not (hasattr(sdk_stdio, "PROCESS_TERMINATION_TIMEOUT") and callable(getattr(sdk_stdio, "_terminate_process_tree", None))):
        raise SystemExit("preflight: this MCP SDK has no PROCESS_TERMINATION_TIMEOUT / _terminate_process_tree to instrument; scenario M "
                         "could not tell the server's own cleanup from the SDK's process-group kill")
    sdk_stdio.PROCESS_TERMINATION_TIMEOUT = SERVER_EXIT_BOUND_S
    orig = sdk_stdio._terminate_process_tree

    async def recorded(process):
        SDK_KILLS.append(time.time())
        await orig(process)

    sdk_stdio._terminate_process_tree = recorded


async def start_long_capture(s, sid_c: str) -> str | None:
    r = await call(s, "capture_breakpoint", **cap_args(SPEC["c"], FX["long_capture_events"], wall=300), label="multi-c-long", session_id=sid_c)
    return r.get("experiment_id")


# ---- L: a failed start does not disturb the others; the failed record is acknowledged and the slot reused ---------------------------
async def scenario_L(s, ctx: dict) -> None:
    """Needs two live sessions: A paused and C running (ctx A, C, sid). Leaves three live sessions (A, C, D) in ctx."""
    sid, A, C = ctx["sid"], ctx["A"], ctx["C"]
    corrupt, bad_kind = ctx["bad_system"], ctx["bad_kind"]
    fa_before, fc_before = await frame_of(s, sid["a"]), await frame_of(s, sid["c"])
    bad = await call(s, "start_session", system=corrupt, ready_timeout_s=40)
    err = bad.get("_error") or {}
    check(f"L the failed start is reported with diagnostics (MAME rejected the {'missing' if bad_kind == 'missing' else 'corrupt'} ROM set of {corrupt})",
          err.get("code") == "mame_exited_during_startup"
          and (err.get("detail") or {}).get("exit_code") not in (None, 0), code=err.get("code"), exit_code=(err.get("detail") or {}).get("exit_code"))
    ls = await call(s, "list_sessions")
    failed = [x for x in ls["sessions"] if x["lifecycle"] == "failed"]
    check("L the failed record is listed but not live; A and C untouched, responsive and advancing", len(failed) == 1 and ls["live_count"] == 2 and alive(A["pid"]) and alive(C["pid"])
          and await state_of(s, sid["a"]) == "paused" and await frame_of(s, sid["a"]) == fa_before and await frame_of(s, sid["c"]) > fc_before, failed=[x["run_id"] for x in failed])
    ack = await call(s, "stop_session", session_id=failed[0]["run_id"]) if failed else {}
    D = await call(s, "start_session", system=FX["extra_start"]["system"])
    track_pid(D.get("pid"))
    check("L the failed record is acknowledged by stop_session; the freed slot starts a new session (a third live session again)", ack.get("stopped") is True and D.get("state") == "ready"
          and D.get("session_id") not in sid.values() and (await call(s, "list_sessions"))["live_count"] == 3, new=D.get("session_id"), error=D.get("_error"))
    gate("L the replacement session D is ready (scenario M needs it)", D.get("state") == "ready" and bool(D.get("pid")), error=D.get("_error"))
    ctx["D"], sid["d"] = D, D["session_id"]


# ---- M: normal server shutdown (stdin closed) with an active capture -----------------------------------------------------------------
async def scenario_M_before(s, ctx: dict) -> None:
    """Runs inside the server context, just before it is left. Needs A paused, C running with the long capture, D started."""
    sid = ctx["sid"]
    ctx["live"] = {n: ctx[n.upper()] for n in "acd"}
    final = await call(s, "experiment_status", experiment_id=ctx["exp_long"], session_id=sid["c"])
    states = {n: await state_of(s, sid[n]) for n in "acd"}
    gate("M before shutdown: A paused, C running with its capture still active, D debugger_stopped", final.get("state") == "running"
         and states == {"a": "paused", "c": "running", "d": "debugger_stopped"}, capture_state=final.get("state"), states=states,
         pids={n: r["pid"] for n, r in ctx["live"].items()})
    ctx["m_t0"] = time.time()


def scenario_M_after(ctx: dict) -> None:
    """Runs after the server context was left (stdin closed, SDK waited up to SERVER_EXIT_BOUND_S)."""
    live = ctx["live"]
    exit_s = round(time.time() - ctx["m_t0"], 1)
    check("M the server exited on its own after stdin closed: the SDK never had to kill its process group", not SDK_KILLS,
          sdk_force_kills=len(SDK_KILLS), seconds_to_exit=exit_s, bound_s=SERVER_EXIT_BOUND_S)
    stderr = (OUT / ctx["errlog"]).read_text(errors="replace")
    lines = [ln for ln in stderr.splitlines() if "shutdown cleanup:" in ln]
    by_run = {r["run_id"]: [ln for ln in lines if f"session={r['run_id']} " in ln] for r in live.values()}
    check("M the server's own teardown logged exactly one verified cleanup per remaining session (A, C with its active capture, D): "
          "process terminated by the server, capture cleanup verified, no failures",
          len(lines) == len(live) and all(len(v) == 1 for v in by_run.values())
          and all("process_terminated=True" in v[0] and "capture_verified=True" in v[0] and "failures=[]" in v[0] for v in by_run.values()),
          lines=lines)
    end = time.time() + 5  # bounded: the server already reported each process terminated
    while time.time() < end and any(still_ours(r["pid"], PIDS[r["pid"]]) for r in live.values() if r["pid"] in PIDS):
        time.sleep(0.2)
    left = {n: r["pid"] for n, r in live.items() if alive(r["pid"])}
    check("M independently: none of the remaining sessions' MAME processes is still running", not left, left=left)


async def setup_LM(s, ctx: dict) -> None:
    """Fresh bounded setup for the targeted L/M run: sessions A and C only, A paused, C running with the long capture."""
    names = ["a", "c"]
    spec = {x["name"]: x for x in FX["sessions"]}
    started = await asyncio.gather(*[call(s, "start_session", system=spec[n]["system"]) for n in names])
    for r in started:
        track_pid(r.get("pid"))
    gate("setup: sessions A and C started and ready", all(r.get("state") == "ready" for r in started),
         results=[{k: r.get(k) for k in ("state", "session_id", "pid", "port", "_error")} for r in started])
    ctx["A"], ctx["C"] = started
    sid = ctx["sid"] = {n: r["session_id"] for n, r in zip(names, started)}
    await run(s, sid["a"])
    await pause(s, sid["a"])
    await run(s, sid["c"])
    ctx["exp_long"] = await start_long_capture(s, sid["c"])
    await asyncio.sleep(0.8)
    st = await call(s, "experiment_status", experiment_id=ctx["exp_long"], session_id=sid["c"]) if ctx["exp_long"] else {}
    states = {n: await state_of(s, sid[n]) for n in names}
    gate("setup: A paused, C running with an active long capture", states == {"a": "paused", "c": "running"} and st.get("state") == "running",
         states=states, capture=st.get("state"))


async def main(only: str | None = None) -> int:
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    note("profile", profile=PROFILE, systems=[x["system"] for x in FX["sessions"]], mixed_machines=MIXED, platform=sys.platform,
         only=only or "all")
    instrument_sdk_teardown()
    corrupt, bad_kind = bad_rom_fixture()
    ctx: dict = {"bad_system": corrupt, "bad_kind": bad_kind, "errlog": "server_stderr.txt"}
    try:
        if only == "LM":
            async with mcp_server(ctx["errlog"]) as s:
                await setup_LM(s, ctx)
                await scenario_L(s, ctx)
                await scenario_M_before(s, ctx)
            scenario_M_after(ctx)
        else:
            await full_run(ctx)
    except Exception as e:  # the SDK's task group may wrap the Abort in an exception group
        leaves = list(getattr(e, "exceptions", None) or [e])
        if all(isinstance(x, Abort) for x in leaves):
            note("scenario stopped: a check the remaining scenarios depend on failed; no dependent check was attempted", stopped_at=[str(x) for x in leaves])
        else:  # an unexpected error is a recorded failure, not a crash that skips the results file and the cleanup report
            check("harness error: the scenario stopped on an unexpected exception", False, error=repr(e))
    finally:
        leaked = [p for p, ident in PIDS.items() if still_ours(p, ident)]
        for p in leaked:  # required cleanup: never leave a MAME behind; only a pid whose image and creation time still match
            try:
                os.kill(p, 9)
            except OSError:
                pass
        reused = [p for p, ident in PIDS.items() if alive(p) and not still_ours(p, ident)]
        check("cleanup: no MAME process launched by this harness is left running", not leaked, leaked=leaked, pid_reused_not_killed=reused,
              recorded={p: list(i) for p, i in PIDS.items()})
    return R.save(OUT / "results.json")


async def full_run(ctx: dict) -> None:
    """Scenarios A..M against one server. Exceptions (Abort included) propagate to main(), which records them and cleans up."""
    async with mcp_server(ctx["errlog"]) as s:
        # ---- A: surface ---------------------------------------------------------------------------------------------
        tools = {t.name: t for t in (await s.list_tools()).tools}
        check("A tool surface is exactly the expected typed tools, now including list_sessions", sorted(tools) == EXPECTED_TOOLS, tools=sorted(tools))
        missing = [n for n, t in tools.items() if n not in NOT_SESSION_BOUND and "session_id" not in t.input_schema["properties"]]
        check("A every session-bound tool accepts session_id (optional); start_session takes no id", not missing
              and "session_id" not in tools["start_session"].input_schema["properties"]
              and all("session_id" not in t.input_schema.get("required", []) for t in tools.values()), missing=missing)
        ls0 = await call(s, "list_sessions")
        check("A list_sessions with no MAME: no sessions, default limit 3, no external attachment", ls0["sessions"] == [] and ls0["max_sessions"] == 3
              and ls0["external"] == {"configured": False}, result=ls0)

        # ---- B: three simultaneous starts -------------------------------------------------------------------------------
        t0 = time.time()
        started = await asyncio.gather(*[call(s, "start_session", system=x["system"]) for x in FX["sessions"]])
        S = {x["name"]: r for x, r in zip(FX["sessions"], started)}
        for r in started:
            track_pid(r.get("pid"))
        ok = all(r.get("state") == "ready" for r in started)
        check(f"B three simultaneous start_session calls ({', '.join(x['system'] for x in FX['sessions'])}) all ready", ok, seconds=round(time.time() - t0, 1),
              results=[{k: r.get(k) for k in ("state", "session_id", "pid", "port", "_error")} for r in started])
        if not ok:
            raise Abort("B three simultaneous starts")
        A, B, C = (S[n] for n in "abc")
        sid = {n: S[n]["session_id"] for n in "abc"}
        want = {x["name"]: x["system"] for x in FX["sessions"]}
        check("B each session runs the machine it was started for", all(S[n]["machine"]["name"] == want[n] for n in "abc"),
              machines={n: S[n]["machine"]["name"] for n in "abc"})
        if not MIXED:
            not_exercised("B two sessions are the same system, the third differs")
        check("B session ids, run ids, processes, ports, run and evidence directories are all distinct",
              len({r["session_id"] for r in started}) == len({r["run_id"] for r in started}) == len({r["pid"] for r in started})
              == len({r["port"] for r in started}) == len({r["run_dir"] for r in started}) == len({r["evidence_dir"] for r in started}) == 3
              and all(Path(r["evidence_dir"]).is_dir() and (Path(r["run_dir"]) / "launch.json").is_file() for r in started)
              and all(pathutil.same_directory(Path(r["run_dir"]).parent, OUT / "sessions") for r in started),
              ids=sid, ports=[r["port"] for r in started], pids=[r["pid"] for r in started])
        owners = {n: procinfo.port_owner_pids(S[n]["port"]) for n in "abc"}
        check("B every session's bridge port is owned by that session's own process", all(S[n]["pid"] in owners[n] for n in "abc") and all(alive(S[n]["pid"]) for n in "abc"),
              owners={n: sorted(v) for n, v in owners.items()})
        ls = await call(s, "list_sessions")
        by = {x["session_id"]: x for x in ls["sessions"]}
        check("B list_sessions: three ready sessions, identity, system and execution status (all debugger_stopped at start)",
              ls["live_count"] == 3 and set(by) == set(sid.values()) and all(x["lifecycle"] == "ready" and x["execution"]["state"] == "debugger_stopped" for x in by.values())
              and [by[sid[n]]["system"] for n in "abc"] == [want[n] for n in "abc"], sessions=[(x["system"], x["lifecycle"], x["execution"].get("state")) for x in ls["sessions"]])
        n0 = mame_count()
        r4 = await call(s, "start_session", system="shinobi")
        check("B a fourth start is refused by the limit before any process is launched", code_of(r4) == "session_limit_reached"
              and mame_count() == n0 and (await call(s, "list_sessions"))["live_count"] == 3, code=code_of(r4), mame_processes=(n0, mame_count()))

        # ---- C: ambiguity and unknown ids ---------------------------------------------------------------------------------
        before = {n: await state_of(s, sid[n]) for n in "abc"}
        amb = {
            "read_memory": await call(s, "read_memory", cpu=SH["cpu"], space="program", address=0, length=4),
            "execution_control": await call(s, "execution_control", action="resume"),
            "session_info": await call(s, "session_info"),
            "stop_session": await call(s, "stop_session"),
            "save_checkpoint": await call(s, "save_checkpoint"),
            "load_checkpoint": await call(s, "load_checkpoint", checkpoint_id=f"{A['run_id']}-cp001"),
            "write_memory": await call(s, "write_memory", cpu=SH["cpu"], space="program", address=RAM, value=1, width=1),
            "release_inputs": await call(s, "release_inputs"),
            "capture_screenshot": await call(s, "capture_screenshot"),
        }
        check("C omitted session_id is refused as ambiguous by every kind of tool (read, control, info, stop, checkpoint, write, inputs, screenshot)",
              all(code_of(v) == "session_ambiguous" for v in amb.values()), codes={k: code_of(v) for k, v in amb.items()})
        listed = {x["session_id"] for x in (amb["read_memory"]["_error"].get("detail") or {}).get("sessions", [])}
        check("C the ambiguity error names the candidate sessions", listed == set(sid.values()), listed=sorted(listed))
        after = {n: await state_of(s, sid[n]) for n in "abc"}
        check("C nothing was done: all three still debugger_stopped and alive", before == after == {n: "debugger_stopped" for n in "abc"}
              and all(alive(S[n]["pid"]) for n in "abc"), before=before, after=after)
        unk = await call(s, "read_memory", cpu=SH["cpu"], space="program", address=0, length=4, session_id="s1-unknown")
        check("C an unknown session id is refused (never redirected)", code_of(unk) == "unknown_session", code=code_of(unk))

        # ---- D: explicit routing of writes and reads -----------------------------------------------------------------------
        mk = {n: marker(n) for n in "abc" if marker(n)}  # sessions with a RAM marker: a, b (mixed) or a, b, c (same machine)
        w = {n: await call(s, "write_memory", cpu=SPEC[n]["cpu"], space="program", address=H(SPEC[n]["ram"]["address"]), value=H(mk[n]),
                           width=1, session_id=sid[n]) for n in mk}
        r_ = {n: await rd(s, sid[n], SPEC[n]["cpu"], H(SPEC[n]["ram"]["address"])) for n in mk}
        check(f"D RAM writes with explicit ids land in exactly that session: each of {', '.join(sorted(mk))} reads back its own distinct marker",
              len(set(mk.values())) == len(mk) and all("_error" not in w[n] and r_[n] == mk[n][2:].lower() for n in mk),
              read=r_, markers=mk, errors={n: w[n].get("_error") for n in mk})
        rom = {n: await rd(s, sid[n], SPEC[n]["cpu"], H(SPEC[n]["rom_read"]["address"]), SPEC[n]["rom_read"]["length"]) for n in "abc"}
        if MIXED:
            check("D ROM reads route per session: the two shinobi sessions agree, the 1942 session reads its own machine",
                  rom["a"] == rom["b"] and rom["a"] and rom["c"] and rom["c"] != rom["a"], shinobi_a=rom["a"], shinobi_b=rom["b"], s1942=rom["c"])
        else:
            check("D ROM reads answer in every session and agree (same machine)", all(rom.values()) and len(set(rom.values())) == 1, rom=rom)
            not_exercised("D a session of a different machine reads its own (different) ROM")
        infos = {n: await info(s, sid[n]) for n in "abc"}
        check("D session_info(session_id) reports that session's machine, id and directories", all(infos[n]["session"]["session_id"] == sid[n]
              and infos[n]["machine"]["name"] == S[n]["machine"]["name"] and infos[n]["session"]["evidence_dir"] == S[n]["evidence_dir"] for n in "abc"))

        # ---- E: pause / resume independence -----------------------------------------------------------------------------------
        await asyncio.gather(run(s, sid["a"]), run(s, sid["c"]))
        fa1, fb1, fc1 = [await frame_of(s, sid[n]) for n in "abc"]
        await asyncio.sleep(1.0)
        fa2, fb2, fc2 = [await frame_of(s, sid[n]) for n in "abc"]
        states = {n: await state_of(s, sid[n]) for n in "abc"}
        check("E resume of A and C only: A and C running with advancing frames, B still debugger_stopped at its frame",
              states == {"a": "running", "b": "debugger_stopped", "c": "running"} and fa2 > fa1 and fc2 > fc1 and fb1 == fb2,
              states=states, frames={"a": (fa1, fa2), "b": (fb1, fb2), "c": (fc1, fc2)})
        await pause(s, sid["a"])
        fa3, fc3 = await frame_of(s, sid["a"]), await frame_of(s, sid["c"])
        await asyncio.sleep(0.8)
        fa4, fc4, fb4 = await frame_of(s, sid["a"]), await frame_of(s, sid["c"]), await frame_of(s, sid["b"])
        states = {n: await state_of(s, sid[n]) for n in "abc"}
        check("E pausing A leaves C running (frames still advance) and B unchanged", states == {"a": "paused", "b": "debugger_stopped", "c": "running"}
              and fa4 == fa3 and fc4 > fc3 and fb4 == fb1, states=states, frames={"a": (fa3, fa4), "b": fb4, "c": (fc3, fc4)})
        await run(s, sid["a"])

        # ---- F: screenshots routed per session ---------------------------------------------------------------------------------
        shots = {}
        for n in "abc":
            res = await s.call_tool("capture_screenshot", {"session_id": sid[n]})
            shots[n] = res.structured_content if res.structured_content is not None else {"_error": body_of(res)}
        gate("F screenshots with explicit ids come from that session: its id, system, run and evidence directory",
              all("_error" not in shots[n] and shots[n]["session_id"] == sid[n] and shots[n]["system"] == S[n]["system"] and shots[n]["run_id"] == S[n]["run_id"]
                  and Path(shots[n]["evidence_path"]).is_file() and pathutil.is_within(shots[n]["evidence_path"], S[n]["evidence_dir"]) for n in "abc"),
              paths={n: shots[n].get("evidence_path") for n in "abc"}, evidence_dirs={n: S[n]["evidence_dir"] for n in "abc"},
              errors={n: shots[n].get("_error") for n in "abc"})
        dims = {n: (shots[n]["image"]["width"], shots[n]["image"]["height"]) for n in "abc"}
        if MIXED:
            check("F the same-machine sessions give same-sized images and the 1942 session a different one (portrait vs landscape)", dims["a"] == dims["b"] != dims["c"], dims=dims)
        else:
            check("F every session gives an image of the same size (same machine)", len(set(dims.values())) == 1, dims=dims)
            not_exercised("F a session of a different machine gives a differently shaped screenshot")
        check("F the three screenshot files are distinct files", len({str(Path(shots[n]["evidence_path"]).resolve()) for n in "abc"}) == 3)
        sc = await call(s, "list_screens", session_id=sid["c"])
        check(f"F list_screens routes too ({want['c']} screen reported by session C)", "_error" not in sc and (sc.get("screens") or [{}])[0].get("tag") == ":screen", screens=sc.get("screens"))
        await run(s, sid["b"])

        # ---- G: overlapping bounded captures --------------------------------------------------------------------------------
        ev = FX["bounded_capture_events"]
        caps = await asyncio.gather(*[call(s, "capture_breakpoint", **cap_args(SPEC[n], ev), label=f"multi-{n}", session_id=sid[n]) for n in "abc"])
        exp = {n: c.get("experiment_id") for n, c in zip("abc", caps)}
        gate("G three captures started, one per session; ids name their own session", all(exp[n] and exp[n].startswith(f"exp-{sid[n]}-") for n in "abc"),
              ids=exp, errors=[c.get("_error") for c in caps])
        snap = await asyncio.gather(*[call(s, "experiment_status", experiment_id=exp[n], session_id=sid[n]) for n in "abc"])
        check("G the three captures are running at the same time (overlapping), each at its own count", all(x.get("state") == "running" for x in snap),
              states=[x.get("state") for x in snap], counts=[(x.get("counts") or {}).get("events_captured") for x in snap])
        done = await asyncio.gather(*[wait_done(s, sid[n], exp[n]) for n in "abc"])
        check("G all three completed with their full event count and verified cleanup", all(x.get("state") == "completed" and x["counts"]["events_captured"] == ev
              and x["cleanup"]["verified"] for x in done), counts=[(x.get("counts") or {}).get("events_captured") for x in done], reasons=[x.get("completion_reason") for x in done])
        files_ok = True
        for n in "abc":
            d = Path(S[n]["evidence_dir"]) / exp[n]
            files_ok &= (d / "manifest.json").is_file() and sum(1 for _ in open(d / "events.jsonl")) == ev
            files_ok &= not any((Path(S[m]["evidence_dir"]) / exp[n]).exists() for m in "abc" if m != n)
        check("G each experiment's evidence is in its own session's evidence directory only", files_ok)
        lat = await call(s, "experiment_status", session_id=sid["c"])
        check("G the default experiment of a session is that session's own latest", lat.get("experiment_id") == exp["c"], latest=lat.get("experiment_id"))

        # ---- H: wrong-session experiment ids -------------------------------------------------------------------------------------
        wrong = [await call(s, "experiment_status", experiment_id=exp["a"], session_id=sid["b"]),
                 await call(s, "cancel_experiment", experiment_id=exp["a"], session_id=sid["c"]),
                 await call(s, "experiment_status", experiment_id=exp["c"], session_id=sid["a"])]
        check("H an experiment id used against another session is refused (status and cancel), nothing sent", all(code_of(x) == "experiment_from_other_session" for x in wrong),
              codes=[code_of(x) for x in wrong])
        amb_exp = await call(s, "experiment_status", experiment_id=exp["a"])
        own = await call(s, "experiment_status", experiment_id=exp["a"], session_id=sid["a"])
        check("H without a session_id the id does not pick a session (ambiguous); with its own session it works", code_of(amb_exp) == "session_ambiguous" and own.get("state") == "completed")

        # ---- I: checkpoints -------------------------------------------------------------------------------------------------------
        await asyncio.gather(pause(s, sid["a"]), pause(s, sid["b"]))
        ram_a0, ram_b0 = await rd(s, sid["a"], SH["cpu"], RAM), await rd(s, sid["b"], SH["cpu"], RAM)
        sv = await call(s, "save_checkpoint", label="multi-a", session_id=sid["a"])
        cp = sv.get("checkpoint_id")
        gate("I save_checkpoint(A): id names A's run; saved with A's machine and version; B and C have none", bool(cp) and cp.startswith(A["run_id"] + "-cp") and sv.get("system") == "shinobi"
              and (await call(s, "list_checkpoints", session_id=sid["b"])).get("checkpoints", [{}])[0].get("checkpoint_id") == cp, id=cp, error=sv.get("_error"))
        await call(s, "write_memory", cpu=SH["cpu"], space="program", address=RAM, value=H(SH["ram"]["changed"]), width=1, session_id=sid["a"])
        frame_a_saved, frame_b_before = sv.get("frame"), await frame_of(s, sid["b"])
        exp_c_before = await call(s, "experiment_status", experiment_id=exp["c"], session_id=sid["c"])
        ld_amb = await call(s, "load_checkpoint", checkpoint_id=cp)
        check("I load_checkpoint without a session_id is refused as ambiguous even though the id names A; A is unchanged", code_of(ld_amb) == "session_ambiguous"
              and await rd(s, sid["a"], SH["cpu"], RAM) == SH["ram"]["changed"][2:].lower())
        ld = await call(s, "load_checkpoint", checkpoint_id=cp, session_id=sid["a"])
        v = ld.get("verification") or {}
        ra1, rb1 = await rd(s, sid["a"], SH["cpu"], RAM), await rd(s, sid["b"], SH["cpu"], RAM)
        frame_b_after = await frame_of(s, sid["b"])
        check("I load_checkpoint(A) restored A (verified exact, RAM back) and changed nothing in B (RAM, frame) or C", ld.get("state") == "completed" and v.get("exact") is True
              and ld.get("session_id") == sid["a"] and ra1 == ram_a0 and rb1 == ram_b0 and frame_b_before == frame_b_after and await state_of(s, sid["c"]) == "running",
              verification=v, ram_a=(ram_a0, ra1), ram_b=(ram_b0, rb1), frame_b=(frame_b_before, frame_b_after), error=ld.get("_error"))
        stale_a = await call(s, "experiment_status", experiment_id=exp["a"], session_id=sid["a"])
        ok_b = await call(s, "experiment_status", experiment_id=exp["b"], session_id=sid["b"])
        ok_c = await call(s, "experiment_status", experiment_id=exp["c"], session_id=sid["c"])
        check("I A's earlier experiment id is now stale after its own load; B's and C's experiment ids are unaffected", code_of(stale_a) == "experiment_stale_after_load"
              and ok_b.get("state") == "completed" and ok_c.get("state") == "completed", codes=[code_of(stale_a), ok_b.get("state"), ok_c.get("state")])
        await call(s, "write_memory", cpu=SH["cpu"], space="program", address=RAM, value=H(SH["ram"]["changed"]), width=1, session_id=sid["b"])
        fa_b = await frame_of(s, sid["a"])
        ld_x = await call(s, "load_checkpoint", checkpoint_id=cp, session_id=sid["b"])
        rb2, fb2c = await rd(s, sid["b"], SH["cpu"], RAM), await frame_of(s, sid["b"])
        check("I deliberate same-machine reuse: A's checkpoint loaded into B only with an explicit target (imported, verified); B takes A's saved state, A is untouched",
              ld_x.get("state") == "completed" and ld_x.get("imported_from_earlier_session") is True and ld_x.get("source_run_id") == A["run_id"] and ld_x.get("target_run_id") == B["run_id"]
              and (ld_x.get("verification") or {}).get("exact") is True and rb2 == ram_a0 and fb2c == frame_a_saved and await frame_of(s, sid["a"]) == fa_b,
              result={k: ld_x.get(k) for k in ("state", "imported_from_earlier_session", "source_run_id", "target_run_id", "session_id")}, ram_b=rb2, frame_b=fb2c, error=ld_x.get("_error"))
        if MIXED:
            ld_c = await call(s, "load_checkpoint", checkpoint_id=cp, session_id=sid["c"])
            lc = await call(s, "list_checkpoints", session_id=sid["c"])
            check("I the 1942 session refuses the shinobi checkpoint (machine mismatch) and list_checkpoints flags it incompatible", code_of(ld_c) == "checkpoint_incompatible"
                  and next(c for c in lc["checkpoints"] if c["checkpoint_id"] == cp)["compatible"] is False, code=code_of(ld_c))
        else:  # C is the same machine, so A's checkpoint is compatible with it; nothing here shows a cross-machine load is refused
            not_exercised("I a session of a different machine refuses the checkpoint (checkpoint_incompatible) and list_checkpoints flags it incompatible",
                          reason="same-machine compatibility is not evidence that cross-machine loads are refused; no load into C was made")

        # ---- J: input and sequence cleanup ------------------------------------------------------------------------------------------
        pr = await call(s, "press_inputs", inputs=[SH["input"]["press"]], frames=300, wait=False, session_id=sid["a"])
        sq = await call(s, "press_sequence", steps=SH["input"]["sequence"], wait=False, session_id=sid["b"])
        ia, ib = (await info(s, sid["a"])).get("input", {}), (await info(s, sid["b"])).get("input", {})
        check("J a press is held on A and a sequence on B (armed while stopped); each session reports only its own", pr.get("state") == "holding" and sq.get("state") == "holding"
              and ia.get("state") == "holding" and ia.get("kind") != "sequence" and ib.get("state") == "holding" and ib.get("kind") == "sequence", a=ia.get("state"), b=(ib.get("state"), ib.get("kind")),
              errors=[pr.get("_error"), sq.get("_error")])
        rel_c = await call(s, "release_inputs", session_id=sid["c"])
        ia2, ib2 = (await info(s, sid["a"])).get("input", {}), (await info(s, sid["b"])).get("input", {})
        check("J release_inputs on C (nothing held there) releases neither A's press nor B's sequence", rel_c.get("was_holding") is False and ia2.get("state") == "holding" and ib2.get("state") == "holding")
        rel_a = await call(s, "release_inputs", session_id=sid["a"])
        ia3, ib3 = (await info(s, sid["a"])).get("input", {}), (await info(s, sid["b"])).get("input", {})
        check("J release_inputs on A releases A only: B's sequence is still held", rel_a.get("was_holding") is True and ia3.get("state") != "holding" and ib3.get("state") == "holding",
              a=ia3.get("state"), b=ib3.get("state"))

        # ---- K: stop B (sequence still held) ----------------------------------------------------------------------------------------
        exp_long = await start_long_capture(s, sid["c"])
        await asyncio.sleep(0.8)
        n_before = ((await call(s, "experiment_status", experiment_id=exp_long, session_id=sid["c"])).get("counts") or {}).get("events_captured")
        t0 = time.time()
        stp = await call(s, "stop_session", session_id=sid["b"])
        check("K stop_session(B) stops only B: held sequence released by the cleanup, process gone, no cleanup failures", stp.get("stopped") is True and stp.get("session_id") == sid["b"]
              and (stp.get("input_cleanup") or {}).get("was_holding") is True and stp["input_cleanup"]["released"] is True and stp["process"]["terminated"] is True
              and stp["cleanup_failures"] == [] and {x["session_id"] for x in stp["remaining_sessions"]} == {sid["a"], sid["c"]}, seconds=round(time.time() - t0, 1),
              input_cleanup=stp.get("input_cleanup"), process=stp.get("process"), error=stp.get("_error"))
        await asyncio.sleep(0.5)
        n_after = ((await call(s, "experiment_status", experiment_id=exp_long, session_id=sid["c"])).get("counts") or {}).get("events_captured")
        check("K A and C stay alive and responsive after B's stop; C's long capture kept running; A's execution state is unchanged",
              not alive(B["pid"]) and alive(A["pid"]) and alive(C["pid"]) and await state_of(s, sid["a"]) == "paused" and await state_of(s, sid["c"]) == "running"
              and n_after and n_before is not None and n_after > n_before and (await call(s, "list_sessions"))["live_count"] == 2, captured=(n_before, n_after))
        gone = await call(s, "read_memory", cpu=SH["cpu"], space="program", address=0, length=4, session_id=sid["b"])
        check("K B's id is reported as ended (not unknown, never redirected to another session); stopping it again is a no-op",
              code_of(gone) == "session_ended" and (await call(s, "stop_session", session_id=sid["b"])).get("stopped") is False)
        sa = await call(s, "session_info", session_id=sid["b"])
        check("K session_info of the ended id reports its own ended summary, not another session's", sa["session"]["session_id"] == sid["b"] and sa["session"]["state"] == "exited")

        # ---- L and M (shared with the targeted --only LM run) ------------------------------------------------------------------------
        ctx.update(A=A, C=C, sid={n: v for n, v in sid.items() if n != "b"}, exp_long=exp_long)  # B has ended (K)
        await scenario_L(s, ctx)
        await scenario_M_before(s, ctx)
    scenario_M_after(ctx)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", choices=["LM"], help="LM: a fresh, bounded setup (sessions A and C only) then scenarios L and M; A..K are not run")
    sys.exit(asyncio.run(main(ap.parse_args().only)))
