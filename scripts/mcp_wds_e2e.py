"""capture_watchpoint / disassemble_memory / press_sequence through the OFFICIAL MCP SDK client against the production server and
real MAME 0.289 (Shinobi). One server process (`python -I -m mame_mcp.server`), MCP tools only, no fixture plugin and no test hooks.
This is a harness run; a live Claude Code session is a separate test.

  A  tool surface and schemas; refusals with no session
  B  disassemble_memory: stopped / running / paused, Z80 and 68000, expected bytes and lengths, refusals
  C  capture_watchpoint: armed while stopped records nothing, runs after resume, evidence on disk, verified cleanup, refusals, cancel
  D  press_sequence: running (waits to completion), stopped arming, release_inputs, validation, stop_session releases a held sequence
Timing here is the bridge's own report; the INDEPENDENT-observer checks of the same operations are in scripts/validate_watchpoint.py,
validate_disasm.py and validate_sequence.py. Machine-specific values come from tests/fixtures/profiles/watch_disasm_sequence.json.
Writes <runs>/mcp_wds_e2e/results.json (<runs> = $MAMEMCP_HARNESS_RUNS or runs\\).   Usage: .venv\\Scripts\\python -I scripts\\mcp_wds_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fx import EXPECTED_TOOLS, MAME_DIR, ROMPATH, Results, body_of, code_of  # noqa: E402
from _inst import H, profile, runs_root  # noqa: E402

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

OUT = runs_root() / "mcp_wds_e2e"
P = profile()
Z, K, IN = P["shinobi"]["z80"], P["shinobi"]["m68k"], P["shinobi"]["input"]
R = Results("mcp_wds_e2e")
check, note = R.check, R.note


def server_env() -> dict[str, str]:
    return {"SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "MAMEMCP_MAME_DIR": str(MAME_DIR), "MAMEMCP_ROMPATH": str(ROMPATH), "MAMEMCP_RUNS_DIR": str(OUT / "sessions")}


@asynccontextmanager
async def mcp_server():
    OUT.mkdir(parents=True, exist_ok=True)
    params = StdioServerParameters(command=sys.executable, args=["-I", "-m", "mame_mcp.server"], env=server_env())
    errlog = open(OUT / "server_stderr.txt", "a")
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
        await asyncio.sleep(0.2)
    return last


async def state(s) -> str:
    return (await call(s, "session_info"))["execution"]["state"]


async def run(s):
    await call(s, "execution_control", action="resume")
    assert await until(s, lambda x: x["execution"]["state"] == "running", timeout=30), "not running"


async def pause(s):
    await call(s, "execution_control", action="pause")
    assert await until(s, lambda x: x["execution"]["state"] == "paused", timeout=30), "not paused"


def events_of(st: dict) -> list[dict]:
    p = (st.get("evidence") or {}).get("events_jsonl")
    return [json.loads(x) for x in Path(p).read_text().splitlines() if x.strip()] if p and Path(p).is_file() else []


def st_(key: str, start: int, frames: int) -> dict:
    return {"port": IN["port"], "field": IN[key]["field"], "start": start, "frames": frames}


async def main_run() -> None:
    async with mcp_server() as s:
        # ---- A -------------------------------------------------------------------------------------------------------------
        tools = {t.name: t for t in (await s.list_tools()).tools}
        check("A the server advertises exactly the typed tools (new: capture_watchpoint, disassemble_memory, press_sequence); no Lua / shell / debugger-command tool", sorted(tools) == EXPECTED_TOOLS, extra=sorted(set(tools) - set(EXPECTED_TOOLS)), missing=sorted(set(EXPECTED_TOOLS) - set(tools)))
        wp = tools["capture_watchpoint"].input_schema
        check("A capture_watchpoint requires every explicit field and closes the enums; no expression/condition/command parameter", set(wp["required"]) == {"cpu", "space", "address", "length", "access", "max_events", "max_wall_seconds"}
              and wp["properties"]["access"]["enum"] == ["read", "write", "read_write"] and wp["properties"]["space"]["enum"] == ["program", "data", "io"] and not {"condition", "action", "expression", "command"} & set(wp["properties"]))
        none = [code_of(await call(s, "capture_watchpoint", cpu=Z["cpu"], space="program", address=0, length=1, access="write", max_events=1, max_wall_seconds=1)),
                code_of(await call(s, "disassemble_memory", cpu=Z["cpu"], address=0, count=1)), code_of(await call(s, "press_sequence", steps=[st_("b1", 0, 3)]))]
        check("A without a session all three tools refuse with no_session", none == ["no_session"] * 3, codes=none)
        e_val = [code_of(await call(s, "press_sequence", steps=[])), code_of(await call(s, "press_sequence", steps=[st_("b1", 2, 3)])), code_of(await call(s, "disassemble_memory", cpu=Z["cpu"], address=0, count=0))]
        check("A client-side validation answers before any session is needed: bad_argument for an empty sequence, a first step not at offset 0, a zero count", e_val == ["bad_argument"] * 3, codes=e_val)

        # ---- B -------------------------------------------------------------------------------------------------------------
        r1 = await call(s, "start_session", system="shinobi", ready_timeout_s=120)
        si = await until(s, lambda x: bool(x.get("cpus")), timeout=90)
        check("B start_session(shinobi) is ready and stopped in the debugger; session_info lists the new operations", r1.get("state") == "ready" and si["execution"]["state"] == "debugger_stopped"
              and {"capture_watchpoint", "disassemble", "input_sequence"} <= set(si["bridge"]["operations"]) and si["test_hooks"] == [], state=si["execution"]["state"], error=r1.get("_error"))
        zd, kd = Z["disasm"], K["disasm"]
        d = await call(s, "disassemble_memory", cpu=Z["cpu"], address=zd["address"], count=zd["count"])
        check("B Z80 disassembly while debugger-stopped: addresses, MAME's text, lengths and bytes (fixture expectation), declared-map mapping, view = program", [i["bytes_hex"] for i in d["instructions"]] == zd["expected_bytes"]
              and [i["length"] for i in d["instructions"]] == zd["expected_lengths"] and d["instruction_view"]["used"] == "program" and d["mapping"]["banked"] is False and d["instructions"][0]["text"].startswith("in") and not d.get("_error"),
              first=d["instructions"][:2], error=d.get("_error"))
        d = await call(s, "disassemble_memory", cpu=K["cpu"], address=kd["address"], count=kd["count"])
        check("B 68000 disassembly: variable lengths 4,4,2 and the stored (big-endian) byte order", [i["bytes_hex"] for i in d["instructions"]] == kd["expected_bytes"] and [i["length"] for i in d["instructions"]] == kd["expected_lengths"], got=[i["bytes_hex"] for i in d["instructions"]], error=d.get("_error"))
        await run(s)
        e_run = await call(s, "disassemble_memory", cpu=Z["cpu"], address=zd["address"], count=2)
        check("B running execution is refused with execution_running (and the machine keeps running)", code_of(e_run) == "execution_running" and await state(s) == "running", code=code_of(e_run))
        await pause(s)
        f0 = (await call(s, "session_info"))["clock"]["frame"]
        d = await call(s, "disassemble_memory", cpu=Z["cpu"], address=zd["address"], count=2)
        check("B paused execution is accepted and the call does not advance emulation", d["execution"]["state"] == "paused" and (await call(s, "session_info"))["clock"]["frame"] == f0, frame=f0)
        errs = {"unknown cpu": code_of(await call(s, "disassemble_memory", cpu=":nocpu", address=0, count=1)), "screen": code_of(await call(s, "disassemble_memory", cpu=P["shinobi"]["no_spaces_device"], address=0, count=1)),
                "beyond mask": code_of(await call(s, "disassemble_memory", cpu=Z["cpu"], address=0x10000, count=1)), "count 65": code_of(await call(s, "disassemble_memory", cpu=Z["cpu"], address=0, count=65))}
        check("B unknown CPU, a device without address spaces, an address beyond the space and an oversized count are refused with typed errors", errs == {"unknown cpu": "unknown_cpu", "screen": "not_a_cpu", "beyond mask": "out_of_range", "count 65": "bad_argument"}, codes=errs)

        # ---- C -------------------------------------------------------------------------------------------------------------
        ram = Z["ram"]
        base = dict(cpu=Z["cpu"], space="program", address=ram["address"], length=H(ram["length"]), access="write", max_events=25, max_wall_seconds=60, registers=Z["registers"])
        cap = await call(s, "capture_watchpoint", **base, label="e2e write")
        exp = cap.get("experiment_id")
        check("C capture_watchpoint armed while PAUSED: running experiment, all armed, execution unchanged", cap.get("state") == "running" and cap.get("all_armed") is True and cap["execution"]["state"] == "paused" and bool(exp), error=cap.get("_error"), state=cap.get("state"))
        await asyncio.sleep(1.0)
        st0 = await call(s, "experiment_status", experiment_id=exp)
        check("C nothing is recorded while stopped (the tool never resumes execution)", st0["counts"]["events_captured"] == 0 and st0["execution"]["state"] == "paused", counts=st0["counts"])
        second = code_of(await call(s, "capture_watchpoint", **base))
        check("C only one experiment may be active", second in ("experiment_active", "capture_active"), code=second)
        await run(s)
        st = await until(s, lambda x: x.get("state") != "running", tool="experiment_status", experiment_id=exp, timeout=60)
        ev = events_of(st)
        check("C after the resume: completed at the event limit with verified cleanup; events typed, in range, sequenced, with the chosen registers and no bank id", st["state"] == "completed" and st["completion_reason"] == "event_limit" and len(ev) == 25
              and st["cleanup"]["verified"] is True and all(e["access"] == "write" and H(ram["address"]) <= H(e["address"]) < H(ram["address"]) + H(ram["length"]) and set(e["regs"]) == {"PC", "SP"} and "bank" not in e for e in ev)
              and [e["seq"] for e in ev] == list(range(1, 26)), counts=st["counts"], reason=st["completion_reason"], first=ev[:1])
        sess = await call(s, "session_info")
        cpu_row = [c for c in sess["cpus"] if c["tag"] == Z["cpu"]][0]
        check("C session_info afterwards: no leftover watchpoint of the experiment, no unresolved instrumentation, coverage limitation stated", sess["unresolved_instrumentation"] == [] and not [w for w in cpu_row["watchpoints"]["listed"] if w.get("owned_by_experiment")]
              and "only address spaces" in cpu_row["watchpoints"]["coverage"], coverage=cpu_row["watchpoints"].get("coverage"))
        bad = {"opcodes space": await call(s, "capture_watchpoint", **{**base, "space": "opcodes"}), "length 0": await call(s, "capture_watchpoint", **{**base, "length": 0}),
               "access exec": await call(s, "capture_watchpoint", **{**base, "access": "exec"}), "screen": await call(s, "capture_watchpoint", **{**base, "cpu": P["shinobi"]["no_spaces_device"]}),
               "8 registers": await call(s, "capture_watchpoint", **{**base, "registers": list("ABCDEFGH")})}
        check("C schema or validation refuses: an opcodes space, zero length, an unknown access type, a device without spaces, too many registers; nothing was started",
              all("_error" in v for v in bad.values()) and code_of(bad["screen"]) == "not_a_cpu" and (await call(s, "session_info")).get("active_experiment") in (None, {}), codes={k: code_of(v) for k, v in bad.items()})
        await pause(s)
        cap = await call(s, "capture_watchpoint", **{**base, "access": "read_write", "address": Z["rom_unwritten"]["address"], "length": H(Z["rom_unwritten"]["length"]), "max_events": 500, "registers": []})
        cx = await call(s, "cancel_experiment", experiment_id=cap["experiment_id"])
        check("C cancel of a read_write capture removes both watchpoints and verifies the cleanup", cx["state"] == "cancelled" and cx["cleanup"]["verified"] is True and len(cx["cleanup"]["locations"]) == 2 and all(r["cleared"] for r in cx["cleanup"]["locations"]), state=cx.get("state"))

        # ---- D -------------------------------------------------------------------------------------------------------------
        await run(s)
        seq = [st_("b1", 0, 10), st_("b2", 4, 10), st_("left", 30, 5)]
        r = await call(s, "press_sequence", steps=seq)
        sq = r.get("sequence") or {}
        check("D press_sequence while running waits for the end: released, completed, release verified; the bridge's own report has no deviations and every step matches", r.get("state") == "released" and r.get("end_reason") == "completed" and r["kind"] == "sequence"
              and sq.get("deviations") == [] and all(x["matches_schedule"] for x in sq["steps"]) and r["frames_elapsed"] == 35 and r["waited"] is True, frames=r.get("frames_elapsed"), deviations=sq.get("deviations"), error=r.get("_error"))
        ver = await until(s, lambda x: x["input"].get("release_verified") is not None, timeout=10)
        check("D the release is verified on the next emulated frame (the machine's port reads idle again)", ver["input"].get("release_verified") is True, input={k: ver["input"].get(k) for k in ("state", "release_verified")})
        check("D the report names its timing basis (the bridge's own per-frame port reads) and the origin rule, rather than claiming frame-exactness", "not an independent observer" in sq["timing_basis"] and "latency_frames_not_counted" in sq["origin"], origin=sq["origin"])
        await pause(s)
        arm = await call(s, "press_sequence", steps=[st_("b1", 0, 6), st_("left", 3, 6)])
        check("D press_sequence while paused: armed and held, waiting_for_emulation, zero frames, returns at once", arm.get("state") == "holding" and arm.get("waiting_for_emulation") is True and arm.get("waited") is False and arm["frames_elapsed"] == 0, result={k: arm.get(k) for k in ("state", "waiting_for_emulation", "frames_elapsed")}, error=arm.get("_error"))
        blocked = [code_of(await call(s, "press_sequence", steps=[st_("b1", 0, 3)])), code_of(await call(s, "press_inputs", inputs=[{"port": IN["port"], "field": IN["b1"]["field"]}], frames=3)), code_of(await call(s, "save_checkpoint"))]
        check("D a held sequence refuses a second sequence, a press and a checkpoint with input_active", blocked == ["input_active"] * 3, codes=blocked)
        f1 = (await call(s, "session_info"))["clock"]["frame"]
        await asyncio.sleep(0.8)
        mid = (await call(s, "session_info"))["input"]
        check("D nothing elapses while it is held and stopped: the frame counter and the sequence's elapsed frames are unchanged", mid["frames_elapsed"] == 0 and (await call(s, "session_info"))["clock"]["frame"] == f1, frame=f1)
        await run(s)
        done = await until(s, lambda x: x["input"]["state"] != "holding", timeout=30)
        check("D after the resume the held sequence runs to the end and is released", done["input"]["state"] == "released" and done["input"]["frames_elapsed"] == 9 and done["input"]["release_verified"] is True, input={k: done["input"].get(k) for k in ("state", "frames_elapsed", "release_verified")})
        long = await call(s, "press_sequence", steps=[st_("b1", 0, 300), st_("left", 100, 100)], wait=False)
        await asyncio.sleep(0.4)
        rel = await call(s, "release_inputs")
        rows = [x["state"] for x in (await call(s, "session_info"))["input"]["sequence"]["steps"]]
        check("D wait=false returns at once; release_inputs mid-sequence cancels it and reports which steps were cut", long.get("state") == "holding" and rel["was_holding"] is True and rel["state"] == "cancelled" and rows == ["released_early", "not_started"], steps=rows)
        bad_seq = {"overlap": [st_("b1", 0, 10), st_("b1", 5, 10)], "touching": [st_("b1", 0, 10), st_("b1", 10, 5)], "late first step": [st_("b1", 3, 5)], "unknown field": [{**st_("b1", 0, 5), "field": "Nope"}],
                   "dip field": [{"port": ":DSW1", "field": "Coin A", "start": 0, "frames": 3}]}
        codes = {k: code_of(await call(s, "press_sequence", steps=v)) for k, v in bad_seq.items()}
        check("D invalid sequences are refused (client-side or by the machine) and nothing is held", codes == {"overlap": "bad_argument", "touching": "bad_argument", "late first step": "bad_argument", "unknown field": "unknown_field", "dip field": "unsupported_input"}
              and (await call(s, "session_info"))["input"]["state"] in ("cancelled", "released", "none"), codes=codes)
        await run(s)
        await call(s, "press_sequence", steps=[st_("b1", 0, 600)], wait=False)
        await asyncio.sleep(0.4)
        stp = await call(s, "stop_session")
        ic = stp.get("input_cleanup") or {}
        check("D stop_session with a running sequence releases it first (verified), reports it, and the owned MAME is gone", stp.get("stopped") is True and ic.get("released") is True and ic.get("was_holding") is True and stp["process"]["terminated"] is True and stp["cleanup_failures"] == [], input_cleanup=ic, failures=stp.get("cleanup_failures"))

    # ---- G: normal server shutdown releases a held sequence ------------------------------------------------------------------
    pid = None
    async with mcp_server() as s2:
        r = await call(s2, "start_session", system="shinobi", ready_timeout_s=120)
        pid = r.get("pid")
        await until(s2, lambda x: bool(x.get("cpus")), timeout=90)
        await run(s2)
        await call(s2, "press_sequence", steps=[st_("b1", 0, 3000)], wait=False)
        await asyncio.sleep(0.4)
    await asyncio.sleep(3.0)
    from mame_mcp import winwake
    check("G closing the MCP connection (server shutdown) with a held sequence stops the owned MAME", pid is not None and not winwake.process_image(pid), pid=pid)


def main() -> int:
    try:
        asyncio.run(main_run())
    except Exception as e:
        import traceback
        check("e2e harness completed without a harness error", False, error=repr(e), trace=traceback.format_exc()[-900:])
    return R.save(OUT / "results.json")


if __name__ == "__main__":
    sys.exit(main())
