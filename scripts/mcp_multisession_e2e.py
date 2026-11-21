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
  L  a failed start (MAME itself rejects a corrupt ROM set) does not disturb existing sessions; the slot stays usable
  M  normal server shutdown (stdin closed) cleans up every remaining owned MAME, including one with an active capture
Writes results.json under $MAMEMCP_HARNESS_RUNS (default <repo>/runs)/mcp_multisession_e2e.
Usage:  .venv\Scripts\python -I scripts\mcp_multisession_e2e.py
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
from _fx import EXPECTED_TOOLS, MAME_DIR, ROMPATH, Results, body_of, code_of  # noqa: E402

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from mame_mcp import pathutil, procinfo  # noqa: E402
from mame_mcp.launcher import PROJECT_ROOT  # noqa: E402

OUT = Path(os.environ.get("MAMEMCP_HARNESS_RUNS") or PROJECT_ROOT / "runs") / "mcp_multisession_e2e"
FX = json.loads((PROJECT_ROOT / "tests" / "fixtures" / "profiles" / "multisession.json").read_text())
SH, S42 = FX["shinobi"], FX["1942"]
H = lambda s: int(s, 16)  # noqa: E731
RAM = H(SH["ram"]["address"])
R = Results("mcp_multisession_e2e")
check, note = R.check, R.note
NOT_SESSION_BOUND = {"start_session", "list_sessions"}


class Abort(Exception):
    """A check that later scenarios depend on failed: stop here (cleanup still runs) instead of continuing on bad state."""


def gate(label: str, ok: bool, **evidence) -> None:
    check(label, ok, **evidence)
    if not ok:
        raise Abort(label)


def alive(pid: int) -> bool:
    return bool(procinfo.process_image(pid))


def mame_count() -> int:
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq mame.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
        return sum(1 for ln in out.splitlines() if ln.lower().startswith('"mame.exe"'))
    return len(subprocess.run(["pgrep", "-x", "mame"], capture_output=True, text=True).stdout.split())


def server_env() -> dict[str, str]:
    return {"SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "MAMEMCP_MAME_DIR": str(MAME_DIR), "MAMEMCP_RUNS_DIR": str(OUT / "sessions"),
            "MAMEMCP_ROMPATH": f"{ROMPATH};{OUT / 'bad_roms'}"}  # the second directory holds only a corrupt pacman.zip


@asynccontextmanager
async def mcp_server(errname: str):
    params = StdioServerParameters(command=sys.executable, args=["-I", "-m", "mame_mcp.server"], env=server_env())
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


async def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    (OUT / "bad_roms").mkdir(parents=True)
    corrupt = FX["corrupt_rom_system"]["system"]
    (OUT / "bad_roms" / f"{corrupt}.zip").write_bytes(b"this is not a zip archive")
    pids: set[int] = set()
    try:
        async with mcp_server("server_stderr.txt") as s:
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
            ok = all(r.get("state") == "ready" for r in started)
            check("B three simultaneous start_session calls (shinobi, shinobi, 1942) all ready", ok, seconds=round(time.time() - t0, 1),
                  results=[{k: r.get(k) for k in ("state", "session_id", "pid", "port", "_error")} for r in started])
            if not ok:
                return R.save(OUT / "results.json")
            A, B, C = (S[n] for n in "abc")
            sid = {n: S[n]["session_id"] for n in "abc"}
            pids.update(r["pid"] for r in started)
            check("B two sessions are the same system, the third differs", A["machine"]["name"] == B["machine"]["name"] == "shinobi" and C["machine"]["name"] == "1942")
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
                  and [by[sid[n]]["system"] for n in "abc"] == ["shinobi", "shinobi", "1942"], sessions=[(x["system"], x["lifecycle"], x["execution"].get("state")) for x in ls["sessions"]])
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
            wa = await call(s, "write_memory", cpu=SH["cpu"], space="program", address=RAM, value=H(SH["ram"]["marker_a"]), width=1, session_id=sid["a"])
            wb = await call(s, "write_memory", cpu=SH["cpu"], space="program", address=RAM, value=H(SH["ram"]["marker_b"]), width=1, session_id=sid["b"])
            ra, rb = await rd(s, sid["a"], SH["cpu"], RAM), await rd(s, sid["b"], SH["cpu"], RAM)
            check("D RAM writes with explicit ids land in exactly that session: A reads its marker, B reads its own", "_error" not in wa and "_error" not in wb
                  and ra == SH["ram"]["marker_a"][2:].lower() and rb == SH["ram"]["marker_b"][2:].lower(), a=ra, b=rb, errors=[wa.get("_error"), wb.get("_error")])
            rom_a = await rd(s, sid["a"], SH["cpu"], H(SH["rom_read"]["address"]), SH["rom_read"]["length"])
            rom_b = await rd(s, sid["b"], SH["cpu"], H(SH["rom_read"]["address"]), SH["rom_read"]["length"])
            rom_c = await rd(s, sid["c"], S42["cpu"], H(S42["rom_read"]["address"]), S42["rom_read"]["length"])
            check("D ROM reads route per session: the two shinobi sessions agree, the 1942 session reads its own machine", rom_a == rom_b and rom_a and rom_c and rom_c != rom_a,
                  shinobi_a=rom_a, shinobi_b=rom_b, s1942=rom_c)
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
            check("F the same-machine sessions give same-sized images and the 1942 session a different one (portrait vs landscape)", dims["a"] == dims["b"] != dims["c"], dims=dims)
            sc = await call(s, "list_screens", session_id=sid["c"])
            check("F list_screens routes too (1942 screen reported by session C)", "_error" not in sc and (sc.get("screens") or [{}])[0].get("tag") == ":screen", screens=sc.get("screens"))
            await run(s, sid["b"])

            # ---- G: overlapping bounded captures --------------------------------------------------------------------------------
            ev = FX["bounded_capture_events"]
            caps = await asyncio.gather(call(s, "capture_breakpoint", **cap_args(SH, ev), label="multi-a", session_id=sid["a"]),
                                        call(s, "capture_breakpoint", **cap_args(SH, ev), label="multi-b", session_id=sid["b"]),
                                        call(s, "capture_breakpoint", **cap_args(S42, ev), label="multi-c", session_id=sid["c"]))
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
            ld_c = await call(s, "load_checkpoint", checkpoint_id=cp, session_id=sid["c"])
            lc = await call(s, "list_checkpoints", session_id=sid["c"])
            check("I the 1942 session refuses the shinobi checkpoint (machine mismatch) and list_checkpoints flags it incompatible", code_of(ld_c) == "checkpoint_incompatible"
                  and next(c for c in lc["checkpoints"] if c["checkpoint_id"] == cp)["compatible"] is False, code=code_of(ld_c))

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
            long_cap = await call(s, "capture_breakpoint", **cap_args(S42, FX["long_capture_events"], wall=300), label="multi-c-long", session_id=sid["c"])
            exp_long = long_cap.get("experiment_id")
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

            # ---- L: a failed start does not disturb the others -------------------------------------------------------------------------------
            fa_before, fc_before = await frame_of(s, sid["a"]), await frame_of(s, sid["c"])
            bad = await call(s, "start_session", system=corrupt, ready_timeout_s=40)
            err = bad.get("_error") or {}
            check("L the failed start is reported with diagnostics (MAME rejected the corrupt ROM set)", err.get("code") == "mame_exited_during_startup"
                  and (err.get("detail") or {}).get("exit_code") not in (None, 0), code=err.get("code"), exit_code=(err.get("detail") or {}).get("exit_code"))
            ls = await call(s, "list_sessions")
            failed = [x for x in ls["sessions"] if x["lifecycle"] == "failed"]
            check("L the failed record is listed but not live; A and C untouched, responsive and advancing", len(failed) == 1 and ls["live_count"] == 2 and alive(A["pid"]) and alive(C["pid"])
                  and await state_of(s, sid["a"]) == "paused" and await frame_of(s, sid["a"]) == fa_before and await frame_of(s, sid["c"]) > fc_before, failed=[x["run_id"] for x in failed])
            ack = await call(s, "stop_session", session_id=failed[0]["run_id"]) if failed else {}
            D = await call(s, "start_session", system=FX["extra_start"]["system"])
            if D.get("pid"):
                pids.add(D["pid"])
            check("L the failed record is acknowledged by stop_session; the freed slot starts a new session (a third live session again)", ack.get("stopped") is True and D.get("state") == "ready"
                  and D["session_id"] not in (sid["a"], sid["b"], sid["c"]) and (await call(s, "list_sessions"))["live_count"] == 3, new=D.get("session_id"), error=D.get("_error"))
            sid["d"] = D.get("session_id")

            # ---- M: normal server shutdown -------------------------------------------------------------------------------------------------
            live_pids = {"a": A["pid"], "c": C["pid"], "d": D.get("pid")}
            final = await call(s, "experiment_status", experiment_id=exp_long, session_id=sid["c"])
            note("before shutdown: A paused, C running with an active capture, D debugger_stopped", capture_state=final.get("state"), pids=live_pids)
        # leaving the context closed the server's stdin (the SDK waits up to 2 s, then kills the process tree)
        end = time.time() + 15
        while time.time() < end and any(alive(p) for p in live_pids.values() if p):
            await asyncio.sleep(0.2)
        stderr = (OUT / "server_stderr.txt").read_text(errors="replace")
        lines = [ln for ln in stderr.splitlines() if "shutdown cleanup:" in ln]
        check("M normal server shutdown stopped every remaining owned MAME", not any(alive(p) for p in live_pids.values() if p), pids=live_pids)
        check("M the server's own graceful cleanup ran for each of the three sessions (not only the SDK's process-tree kill), the active capture's cleanup verified",
              len(lines) == 3 and all("process_terminated=True" in ln for ln in lines) and sum("capture_verified=True" in ln for ln in lines) == 3
              and all("failures=[]" in ln for ln in lines), lines=lines)
    except Exception as e:  # the SDK's task group may wrap the Abort in an exception group
        leaves = list(getattr(e, "exceptions", None) or [e])
        if all(isinstance(x, Abort) for x in leaves):
            note("scenario stopped: a check the remaining scenarios depend on failed; no dependent check was attempted", stopped_at=[str(x) for x in leaves])
        else:  # an unexpected error is a recorded failure, not a crash that skips the results file and the cleanup report
            check("harness error: the scenario stopped on an unexpected exception", False, error=repr(e))
    finally:
        leaked = [p for p in pids if alive(p)]
        for p in leaked:  # required cleanup: never leave a MAME behind
            try:
                os.kill(p, 9)
            except OSError:
                pass
        check("cleanup: no MAME process launched by this harness is left running", not leaked, leaked=leaked)
    return R.save(OUT / "results.json")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
