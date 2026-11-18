"""Checkpoint / RAM-write / input workflow through the OFFICIAL MCP SDK client against the production server and real
MAME 0.289 (Shinobi). One server process (`python -I -m mame_mcp.server`), MCP tools only, no fixture plugin and no
test hooks. This is HARNESS evidence; a later live Claude Code run is a separate test (see docs/VALIDATION.md).

  A  tool surface; refusals with no session
  B  the target flow: start -> input -> save -> change -> load -> verify restoration -> capture -> stop
  C  RAM write: known-writable target, widths, ROM / unmapped / device refusals, stopped-only, read-back
  D  input: discovery, bounded press released after its duration, running/stopped semantics, unsupported types, release on stop
  E  guards: an active capture refuses save/load/write; stale experiment ids after a load
  F  cross-session: a checkpoint saved in one session loads in the next session of the same machine
  G  normal server shutdown releases a held input and stops the owned MAME
Machine-specific addresses and inputs come from tests/fixtures/profiles/shinobi.json ("checkpoint_input").
Writes runs/mcp_checkpoint_e2e/results.json.   Usage: .venv\\Scripts\\python -I scripts\\mcp_checkpoint_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fx import Results, body_of, code_of  # noqa: E402

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from mame_mcp import winwake  # noqa: E402
from mame_mcp.launcher import PROJECT_ROOT  # noqa: E402

MAME_DIR = r"D:\arcade\mame"
ROMPATH = r"D:\arcade\ROMs\MAME 0.289 ROMs (merged)"
OUT = PROJECT_ROOT / "runs" / "mcp_checkpoint_e2e"
PROFILE = json.loads((PROJECT_ROOT / "tests" / "fixtures" / "profiles" / "shinobi.json").read_text())
CI = PROFILE["checkpoint_input"]
MAIN = PROFILE["maincpu"]["tag"]
HANDLER = int(PROFILE["maincpu"]["handler_pc"], 16)
H = lambda s: int(s, 16)  # noqa: E731
RAM, ROM, UNM, DEV = (H(CI[k]["address"]) for k in ("ram", "rom", "unmapped", "device_handled"))
PRESS = CI["input"]["press"]
R = Results("mcp_checkpoint_e2e")
check, note = R.check, R.note
EXPECTED_TOOLS = sorted([
    "start_session", "stop_session", "session_info", "read_memory", "read_registers", "capture_breakpoint", "experiment_status",
    "cancel_experiment", "execution_control", "list_inputs", "press_inputs", "release_inputs", "save_checkpoint", "load_checkpoint",
    "list_checkpoints", "write_memory"])


def server_env() -> dict[str, str]:
    # a hostile ambient environment: the production session must not inherit any of these
    return {"SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "MAMEMCP_MAME_DIR": MAME_DIR, "MAMEMCP_ROMPATH": ROMPATH, "MAMEMCP_RUNS_DIR": str(OUT / "sessions"),
            "MAMEMCP_TEST_FORCE_SCHEDULED_CHECKPOINT": "1", "MAMEMCP_TEST_FAIL_CLEAR": "1", "MAMEMCP_STATE_DIR": str(OUT / "ambient_state_dir")}


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


async def until(s, pred, tool="session_info", timeout=30.0, **args):
    end, last = time.time() + timeout, None
    while time.time() < end:
        last = await call(s, tool, **args)
        if pred(last):
            return last
        await asyncio.sleep(0.2)
    return last


async def rd(s, address: int, length: int = 1) -> str | None:
    r = await call(s, "read_memory", cpu=MAIN, space="program", address=address, length=length)
    return r.get("data_hex") or r.get("bytes_hex")


async def state(s) -> str:
    return (await call(s, "session_info")).get("execution", {}).get("state")


async def run(s):
    await call(s, "execution_control", action="resume")
    await until(s, lambda x: x.get("execution", {}).get("state") == "running")


async def pause(s):
    await call(s, "execution_control", action="pause")
    r = await until(s, lambda x: x.get("execution", {}).get("state") == "paused")
    assert r["execution"]["state"] == "paused", r.get("execution")


async def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    pids: list[int] = []
    async with mcp_server("server_stderr_main.txt") as s:
        # ---- A ---------------------------------------------------------------------------------------
        tools = {t.name: t for t in (await s.list_tools()).tools}
        check("A the tool surface is exactly the typed tools (no arbitrary Lua / debugger / command tool)", sorted(tools) == EXPECTED_TOOLS, tools=sorted(tools))
        wm = tools["write_memory"].input_schema
        check("A write_memory requires cpu, space, address, value and width (width is 1, 2 or 4)", sorted(wm["required"]) == ["address", "cpu", "space", "value", "width"] and
              sorted(wm["properties"]["width"].get("enum", [])) == [1, 2, 4], schema_required=wm["required"], width=wm["properties"]["width"])
        pi = tools["press_inputs"].input_schema
        check("A press_inputs requires explicit inputs and frames", sorted(pi["required"]) == ["frames", "inputs"], required=pi["required"])
        codes = [code_of(await call(s, t, **a)) for t, a in (("save_checkpoint", {}), ("load_checkpoint", {"checkpoint_id": "shinobi-20260101-000000-abcdef-cp001"}), ("list_inputs", {}),
                 ("press_inputs", {"inputs": PRESS, "frames": 3}), ("release_inputs", {}), ("write_memory", {"cpu": MAIN, "space": "program", "address": RAM, "value": 1, "width": 1}))]
        check("A every new tool reports no_session when no managed MAME exists", codes == ["no_session"] * 6, codes=codes)
        lc = await call(s, "list_checkpoints")
        check("A list_checkpoints needs no running MAME", "_error" not in lc and isinstance(lc.get("checkpoints"), list), count=len(lc.get("checkpoints", [])))

        # ---- B: the target workflow ------------------------------------------------------------------
        r1 = await call(s, "start_session", system="shinobi")
        pids.append(r1["pid"])
        sid1 = r1["session_id"]
        si = await call(s, "session_info")
        check("B start_session: ready, debugger-stopped, test hooks and the ambient state dir NOT inherited", r1.get("state") == "ready" and r1["execution"]["state"] == "debugger_stopped"
              and si["test_hooks"] == [] and str(OUT / "ambient_state_dir") not in json.dumps(si), result_state=r1.get("state"), hooks=si["test_hooks"], error=r1.get("_error"))
        li = await call(s, "list_inputs")
        names = {(p["tag"], f["field"]) for p in li["ports"] for f in p["fields"]}
        check("B list_inputs: this machine's real ports/fields; the press target is present, DIP/toggle fields are not listed by default", (PRESS[0]["port"], PRESS[0]["field"]) in names
              and li["fields_listed"] == CI["input"]["supported_count"] and li["unsupported_fields"] == CI["input"]["unsupported_count"], listed=li["fields_listed"], unsupported=li["unsupported_fields"])
        # reach a gameplay-ish state: run, drive the real input path, then stop emulation at a known frame
        await run(s)
        await asyncio.sleep(1.0)
        t0 = time.time()
        pr = await call(s, "press_inputs", inputs=PRESS, frames=20)
        check("B press_inputs(20 frames) waits and returns released; the machine saw exactly 20 frames; release verified", pr.get("state") == "released" and pr["frames_elapsed"] == 20 and pr["frames_observed_pressed"] == 20
              and pr["release_verified"] is True and pr["waited"] is True, seconds=round(time.time() - t0, 2), result={k: pr.get(k) for k in ("state", "frames_requested", "frames_elapsed", "frames_observed_pressed", "release_verified", "end_reason")}, error=pr.get("_error"))
        await pause(s)
        base_frame = (await call(s, "session_info"))["clock"]["frame"]
        wr0 = await call(s, "write_memory", cpu=MAIN, space="program", address=RAM, value=0x5A, width=1)
        check("B explicit RAM write before the save (the state to preserve): verified read-back", wr0.get("verified") is True and wr0["after"]["value"] == 0x5A, result={k: wr0.get(k) for k in ("before", "requested", "after", "verified")}, error=wr0.get("_error"))
        sv = await call(s, "save_checkpoint", label="after input, RAM=5A")
        cid = sv.get("checkpoint_id")
        sf = Path(sv.get("path", ""))
        check("B save_checkpoint: completed only with a real MAME state file under the session run dir; id returned; execution unchanged (paused)", sv.get("state") == "completed" and sf.is_file()
              and sf.read_bytes()[:8] == b"MAMESAVE" and sf.stat().st_size == sv["size_bytes"] and str(sf.resolve()).startswith(str(Path(r1["run_dir"]).resolve())) and sv["execution"]["state"] == "paused"
              and bool(cid), checkpoint_id=cid, mode=sv.get("mode"), size=sv.get("size_bytes"), frame=sv.get("frame"), error=sv.get("_error"))
        check("B the checkpoint records machine, MAME version, SHA-256, frame, label", sv.get("system") == "shinobi" and sv.get("mame_version", "").startswith("0.289") and len(sv.get("sha256", "")) == 64
              and sv.get("frame") == base_frame and sv.get("label") == "after input, RAM=5A", system=sv.get("system"), version=sv.get("mame_version"), frame=sv.get("frame"), base_frame=base_frame)
        # change state: RAM and emulated time
        await call(s, "write_memory", cpu=MAIN, space="program", address=RAM, value=0x77, width=1)
        await run(s)
        await asyncio.sleep(1.2)
        await pause(s)
        changed_frame = (await call(s, "session_info"))["clock"]["frame"]
        v_changed = await rd(s, RAM)
        check("B state changed after the save: later frame, different RAM byte", changed_frame > base_frame and v_changed == "77", base=base_frame, now=changed_frame, ram=v_changed)
        ld = await call(s, "load_checkpoint", checkpoint_id=cid)
        v = ld.get("verification", {})
        after_frame = (await call(s, "session_info"))["clock"]["frame"]
        v_after = await rd(s, RAM)
        check("B load_checkpoint: completed only after verification; the frame counter and RAM are back to the saved values; still paused; session identity unchanged", ld.get("state") == "completed"
              and v.get("exact") is True and v.get("load_effect_observed") is True and after_frame == base_frame and v_after == "5a" and ld["execution"]["state"] == "paused" and ld["session_id"] == sid1,
              verification=v, frame_after=after_frame, ram_after=v_after, error=ld.get("_error"))
        check("B the load bumped the state epoch (stale refs)", ld.get("state_epoch") == 1, state_epoch=ld.get("state_epoch"))
        await run(s)
        cap = await call(s, "capture_breakpoint", cpu=MAIN, address=HANDLER, max_events=20, max_wall_seconds=60, registers=["D0", "SR"], label="after-restore")
        exp = cap.get("experiment_id")
        st = await until(s, lambda x: x.get("state") in ("completed", "failed", "cancelled"), tool="experiment_status", experiment_id=exp, timeout=60)
        evd = Path(r1["evidence_dir"]) / (exp or "-")
        check("B capture after the restore completes normally: 20 events, cleanup verified, evidence files on disk", st.get("state") == "completed" and st["counts"]["events_captured"] == 20
              and st["cleanup"]["verified"] is True and (evd / "events.jsonl").is_file(), counts=st.get("counts"), cleanup=st.get("cleanup"), experiment=exp, error=st.get("_error"))
        await pause(s)

        # ---- C: RAM write ----------------------------------------------------------------------------
        await run(s)
        e_run = await call(s, "write_memory", cpu=MAIN, space="program", address=RAM, value=1, width=1)
        e_run2 = await call(s, "save_checkpoint")
        check("C write/save refused while running (execution_running)", code_of(e_run) == code_of(e_run2) == "execution_running", codes=[code_of(e_run), code_of(e_run2)])
        await pause(s)
        outs = {}
        for width, spec in CI["ram_widths"].items():
            a, val = H(spec["address"]), H(spec["value"])
            before = await rd(s, a, int(width))
            r = await call(s, "write_memory", cpu=MAIN, space="program", address=hex(a), value=hex(val), width=int(width))
            after = await rd(s, a, int(width))
            outs[width] = (before, r.get("requested", {}).get("value"), r.get("after", {}).get("value"), after)
            check(f"C {width}-byte RAM write reports before / requested / actual and matches an independent read_memory", r.get("verified") is True and r["requested"]["value"] == val and r["after"]["value"] == val
                  and after == f"{val:0{2 * int(width)}x}" and r["before"]["bytes_hex"] == before, outcome=outs[width], error=r.get("_error"))
        rom_before = await rd(s, ROM, 4)
        refusals = {}
        for name, addr in (("rom", ROM), ("unmapped", UNM), ("device_handled", DEV), ("io", H(CI["io"]["address"]))):
            e = await call(s, "write_memory", cpu=MAIN, space="program", address=addr, value=0x11, width=1)
            refusals[name] = code_of(e)
        check("C ROM, unmapped, device-handled and I/O targets refused with distinct codes; ROM unchanged", refusals == {"rom": "rom_target", "unmapped": "unmapped_target", "device_handled": "unsupported_target", "io": "unsupported_target"}
              and await rd(s, ROM, 4) == rom_before, codes=refusals)
        bad = [await call(s, "write_memory", cpu=MAIN, space="program", address=RAM, value=v_, width=w_) for v_, w_ in ((0x100, 1), (1, 3))] + \
              [await call(s, "write_memory", cpu=MAIN, space="program", address=RAM + 1, value=1, width=2), await call(s, "write_memory", cpu="maincpu", space="program", address=RAM, value=1, width=1),
               await call(s, "write_memory", cpu=":nope", space="program", address=RAM, value=1, width=1), await call(s, "write_memory", cpu=MAIN, space="program", address=RAM, value=-1, width=1)]
        bad_codes = [code_of(b) for b in bad]
        check("C bad value, width 3 (refused by the tool schema itself), alignment, cpu tag, unknown cpu, negative value: all rejected", bad_codes == ["bad_argument", "?", "bad_argument", "bad_argument", "unknown_cpu", "bad_argument"] and "width" in json.dumps(bad[1]), codes=bad_codes)

        # ---- D: inputs -------------------------------------------------------------------------------
        a = CI["input"]["unsupported"]
        errs = [await call(s, "press_inputs", inputs=[a["analog_or_toggle"]], frames=3), await call(s, "press_inputs", inputs=[{"port": a["unknown_port"], "field": "x"}], frames=3),
                await call(s, "press_inputs", inputs=[a["unknown_field"]], frames=3), await call(s, "press_inputs", inputs=PRESS, frames=0), await call(s, "press_inputs", inputs=PRESS, frames=601),
                await call(s, "press_inputs", inputs=PRESS * 2, frames=3), await call(s, "press_inputs", inputs=[], frames=3)]
        check("D unsupported (toggle/DIP), unknown port/field, 0 / 601 frames, duplicates, empty: refused, nothing held", [code_of(e) for e in errs] == ["unsupported_input", "unknown_port", "unknown_field", "bad_argument", "bad_argument", "bad_argument", "bad_argument"]
              and (await call(s, "session_info"))["input"]["state"] in ("none", "released", "cancelled"), codes=[code_of(e) for e in errs])
        arm = await call(s, "press_inputs", inputs=PRESS, frames=8)  # paused: armed, not waited
        check("D press while paused: armed and held, waiting_for_emulation, no frames elapse (returns immediately)", arm.get("state") == "holding" and arm.get("waiting_for_emulation") is True and arm["frames_elapsed"] == 0, result={k: arm.get(k) for k in ("state", "waiting_for_emulation", "frames_elapsed", "note")}, error=arm.get("_error"))
        blocked = [code_of(await call(s, "write_memory", cpu=MAIN, space="program", address=RAM, value=1, width=1)), code_of(await call(s, "save_checkpoint")), code_of(await call(s, "press_inputs", inputs=PRESS, frames=3))]
        check("D a held input blocks write_memory, save_checkpoint and a second press", blocked == ["input_active"] * 3, codes=blocked)
        rel = await call(s, "release_inputs")
        check("D release_inputs while stopped releases the armed press (idempotent afterwards)", rel.get("was_holding") is True and rel["state"] == "cancelled" and (await call(s, "release_inputs"))["was_holding"] is False, result={k: rel.get(k) for k in ("state", "was_holding", "release_errors")})
        await run(s)
        long = await call(s, "press_inputs", inputs=PRESS, frames=600, wait=False)
        await asyncio.sleep(0.5)
        mid = (await call(s, "session_info"))["input"]
        check("D wait=false returns at once; the press is held while running", long.get("state") == "holding" and mid["state"] == "holding" and mid["frames_elapsed"] > 5, armed=long.get("state"), mid={k: mid.get(k) for k in ("state", "frames_elapsed")})
        await pause(s)
        paused_status = (await call(s, "session_info"))["input"]
        await asyncio.sleep(0.8)
        later = (await call(s, "session_info"))["input"]
        check("D pausing mid-press freezes the count (no frame elapses while paused); the press stays held", paused_status["state"] == later["state"] == "holding" and paused_status["frames_elapsed"] == later["frames_elapsed"],
              at_pause=paused_status["frames_elapsed"], later=later["frames_elapsed"])
        await call(s, "release_inputs")
        # stop_session with a held input: released while the bridge is still connected, reported in the stop result
        await run(s)
        await call(s, "press_inputs", inputs=PRESS, frames=600, wait=False)
        await asyncio.sleep(0.4)
        # ---- E: guards (before the stop, with the press released first so the guard under test is the capture) ----
        await call(s, "release_inputs")
        await pause(s)
        sv2 = await call(s, "save_checkpoint", label="guard base")
        cap = await call(s, "capture_breakpoint", cpu=MAIN, address=HANDLER, max_events=10000, max_wall_seconds=300, registers=["D0"], label="guard")
        expa = cap.get("experiment_id")
        g = [code_of(await call(s, "save_checkpoint")), code_of(await call(s, "load_checkpoint", checkpoint_id=sv2["checkpoint_id"])), code_of(await call(s, "write_memory", cpu=MAIN, space="program", address=RAM, value=1, width=1))]
        check("E an active capture refuses save_checkpoint, load_checkpoint and write_memory", g == ["capture_active"] * 3, codes=g, experiment=expa)
        await call(s, "cancel_experiment", experiment_id=expa)
        # stale refs: an experiment from before a load
        old = (await call(s, "list_checkpoints"))
        ld2 = await call(s, "load_checkpoint", checkpoint_id=sv2["checkpoint_id"])
        e_status, e_cancel = await call(s, "experiment_status", experiment_id=expa), await call(s, "cancel_experiment", experiment_id=expa)
        e_stale = await call(s, "experiment_status", experiment_id=exp)
        check("E experiment ids from before a load are refused as stale (status + cancel); invalidated ids listed; the evidence files remain", code_of(e_status) == code_of(e_cancel) == code_of(e_stale) == "experiment_stale_after_load"
              and expa in ld2["invalidated_experiments"] and (evd / "events.jsonl").is_file(), codes=[code_of(e_status), code_of(e_cancel), code_of(e_stale)], invalidated=ld2.get("invalidated_experiments"))
        lcs = await call(s, "list_checkpoints")
        check("E list_checkpoints lists this session's checkpoints with id, label, frame, machine", {c["checkpoint_id"] for c in lcs["checkpoints"]} >= {cid, sv2["checkpoint_id"]} and all(c.get("system") == "shinobi" for c in lcs["checkpoints"]),
              ids=[c["checkpoint_id"] for c in lcs["checkpoints"]])
        bad_ids = [code_of(await call(s, "load_checkpoint", checkpoint_id=x)) for x in ("../../../etc/passwd", "x", "", cid + "/..", cid.replace("-cp001", "-cp999"))]
        check("E hostile / malformed / unknown checkpoint ids refused before anything is touched", bad_ids == ["bad_argument"] * 4 + ["checkpoint_not_found"], codes=bad_ids)

        # ---- D (cont.): stop_session releases a held input ------------------------------------------------
        await run(s)
        await call(s, "press_inputs", inputs=PRESS, frames=600, wait=False)
        await asyncio.sleep(0.4)
        stp = await call(s, "stop_session")
        ic = stp.get("input_cleanup") or {}
        check("D stop_session with a held input: released first (verified) and reported; the owned MAME is gone", stp.get("stopped") is True and ic.get("released") is True and ic.get("was_holding") is True
              and stp["process"]["terminated"] is True and stp["cleanup_failures"] == [], input_cleanup=ic, process=stp.get("process"), failures=stp.get("cleanup_failures"))
        check("D the stopped session has no process left", not winwake.process_image(pids[0]), pid=pids[0])

        # ---- F: cross-session ---------------------------------------------------------------------------
        r2 = await call(s, "start_session", system="shinobi")
        pids.append(r2["pid"])
        lc2 = await call(s, "list_checkpoints")
        prev = [c for c in lc2["checkpoints"] if c["checkpoint_id"] == cid]
        check("F a new session lists the earlier session's checkpoint, marked as not current", len(prev) == 1 and prev[0]["current_session"] is False and r2["session_id"] != sid1, ids=[c["checkpoint_id"] for c in lc2["checkpoints"]])
        e_old = await call(s, "experiment_status", experiment_id=exp)
        check("F experiment ids from the earlier session are refused in the new session", code_of(e_old) in ("unknown_experiment", "experiment_stale", "wrong_session", "experiment_from_other_session") or "_error" in e_old, code=code_of(e_old))
        ld3 = await call(s, "load_checkpoint", checkpoint_id=cid)
        check("F load of a checkpoint made in an earlier session: imported, verified (frame, registers, RAM), RAM restored", ld3.get("state") == "completed" and ld3.get("imported_from_earlier_session") is True
              and ld3["verification"]["exact"] is True and await rd(s, RAM) == "5a" and ld3["session_id"] == r2["session_id"], verification=ld3.get("verification"), error=ld3.get("_error"))
        tampered = [c for c in lc2["checkpoints"] if c["checkpoint_id"] == sv2["checkpoint_id"]][0]
        src = Path(r1["run_dir"]) / "home" / "sta" / "shinobi" / f"{tampered['checkpoint_id']}.sta"
        raw = bytearray(src.read_bytes())
        raw[100] ^= 0xFF
        src.write_bytes(raw)
        ld4 = await call(s, "load_checkpoint", checkpoint_id=tampered["checkpoint_id"])
        check("F a modified checkpoint file is refused (SHA-256) before the machine is touched", code_of(ld4) == "checkpoint_corrupt", code=code_of(ld4))

        # ---- G: server shutdown while an input is held ---------------------------------------------------------
        await run(s)
        await call(s, "press_inputs", inputs=PRESS, frames=600, wait=False)
        await asyncio.sleep(0.4)
        note("G closing the MCP connection (stdin) with a held input", pid=pids[1])
    # the `async with` above closed stdin: the server shuts down normally
    gone = False
    end = time.time() + 20
    while time.time() < end:
        if not winwake.process_image(pids[1]):
            gone = True
            break
        await asyncio.sleep(0.3)
    check("G normal server shutdown stops its owned MAME (held input released first by the teardown)", gone, pid=pids[1])
    return R.save(OUT / "results.json")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
