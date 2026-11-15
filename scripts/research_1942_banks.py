"""Bounded research task: does 1942 (Revision B) select a non-zero :maincpu bank in attract mode / ordinary play?

Everything that produces a research observation goes through the production MCP server (official SDK client over stdio):
session_info, execution_control(resume), capture_breakpoint, experiment_status, read_registers. This script only
launches MAME with the project launcher, optionally with an EXTERNAL ordinary-input script (-autoboot_script), and
orchestrates the MCP calls. No fixture plugin, no memory writes, no forced bank selection, no RAM trampoline.

Usage:  .venv\\Scripts\\python -I scripts\\research_1942_banks.py A|B
  A = attract mode, no input.  B = coin + 1P start + ordinary control pulses (scripts\\research_1942_input.lua).
Evidence: runs\\research1942\\<A|B>\\ (summary.json, evidence\\exp-*\\{manifest.json,events.jsonl}, input.log).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mame_mcp.launcher import PROJECT_ROOT, launch

ROMS = Path(r"D:\arcade\ROMs\MAME 0.289 ROMs (merged)")
ROOT = PROJECT_ROOT / "runs" / "research1942"
INPUT_LUA = PROJECT_ROOT / "scripts" / "research_1942_input.lua"
CPU = ":maincpu"
# Hit-time identification of the selected entry from the window bytes (profile tests/fixtures/profiles/1942.json):
BANK_BY_FIRST_BYTE = {0x84: 0, 0x60: 1, 0xDC: 2, 0xFF: 3}
# Candidate sites from the static analysis (docs/research_1942_banks.md): the instruction AFTER each `ld ($C806),a` / `ld (hl),n`.
SITES = [
    (0x0D18, "after LD (C806),A at 0D15 (A=2)", 2),
    (0x0E52, "after LD (C806),A at 0E4F (A=2)", 2),
    (0x152D, "after LD (C806),A at 152A (A=1)", 1),
    (0x19AF, "after LD (C806),A at 19AC (A=2)", 2),
    (0x1A6D, "after LD (C806),A at 1A6A (A=2)", 2),
    (0x1A7D, "after LD (C806),A at 1A7A (A=($E10A))", None),
    (0x2BA2, "after LD (HL),1 at 2BA0 (HL=C806)", None),
    (0x0166, "service-mode path: LD A,2 / LD (C806),A / JP ACFE (needs DSWB service switch)", None),
]
WALL, EVENTS = 10, 4


def payload(res) -> dict:
    if res.is_error:
        return {"_error": res.content[0].text}
    return res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)


async def main(run: str) -> int:
    assert run in ("A", "B")
    out = ROOT / run
    out.mkdir(parents=True, exist_ok=True)
    port = 9491 if run == "A" else 9492
    extra = []
    if run == "B":
        os.environ["MAMEMCP_INPUT_LOG"] = str(out / "input.log").replace("\\", "/")
        extra = ["-autoboot_script", str(INPUT_LUA).replace("\\", "/"), "-autoboot_delay", "0"]
    proc, info = launch(system="1942", port=port, run_dir=out / "run", rompath=ROMS, extra=extra)
    log = {"run": run, "launch": {k: info[k] for k in ("mame_version", "pid", "pid_created_filetime", "port", "command")},
           "romset_sha1": info["romset_zip"]["sha1"], "verifyroms": info["verifyroms"]["output"][:50], "experiments": [], "samples": []}
    params = StdioServerParameters(
        command=sys.executable, args=["-I", "-m", "mame_mcp.server"],
        env={"MAMEMCP_PORT": str(port), "MAMEMCP_LAUNCH_INFO": str(out / "run" / "launch.json"), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")})
    errlog = open(out / "server_stderr.txt", "w")
    try:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                call = lambda n, a=None: s.call_tool(n, a or {})  # noqa: E731
                si = payload(await call("session_info"))
                log["session_start"] = {"machine": si["machine"], "execution": si["execution"], "clock": si.get("clock"), "wake_configured": si["bridge_client"]["wake_configured"]}
                t_wall0 = time.time()
                log["resume"] = payload(await call("execution_control", {"action": "resume"}))
                time.sleep(1.5)
                for addr, why, a_expect in SITES:
                    t0 = time.time()
                    clk0 = payload(await call("session_info")).get("clock")
                    cap = payload(await call("capture_breakpoint", {
                        "cpu": CPU, "address": addr, "max_events": EVENTS, "max_wall_seconds": WALL, "registers": ["PC", "A", "HL"],
                        "memory": [{"address": 0x8000, "size": 1}, {"address": 0x8000, "size": 4}], "label": f"research-{run}-{addr:04X}"}))
                    st = {}
                    while time.time() - t0 < WALL + 15:
                        st = payload(await call("experiment_status", {"experiment_id": cap["experiment_id"]}))
                        if st.get("state") != "running":
                            break
                        time.sleep(0.5)
                    clk1 = payload(await call("session_info")).get("clock")
                    ev = [json.loads(x) for x in Path(st["evidence"]["events_jsonl"]).read_text().splitlines() if x.strip()] if st.get("evidence") else []
                    rows = [{"seq": e["seq"], "PC": e["regs"].get("PC"), "A": e["regs"].get("A"), "HL": e["regs"].get("HL"),
                             "b8000": e["mem"][0]["value"], "entry_from_b8000": BANK_BY_FIRST_BYTE.get(int(e["mem"][0]["value"], 16)),
                             "harvest_frame": e.get("harvest_frame")} for e in ev]
                    exp = {"address": hex(addr), "site": why, "experiment_id": cap["experiment_id"], "armed_while": "running", "clock_before": clk0, "clock_after": clk1,
                           "state": st.get("state"), "reason": st.get("completion_reason"), "summary": st.get("summary"), "counts": st.get("counts"),
                           "cleanup": st.get("cleanup"), "execution": st.get("execution"), "events": rows, "evidence": st.get("evidence"),
                           "a_expected_by_static_code": a_expect}
                    log["experiments"].append(exp)
                    print(f"[{run}] {addr:04X} {st.get('summary')} entries={sorted({r['entry_from_b8000'] for r in rows})} A={sorted({r['A'] for r in rows})}", flush=True)
                # PC sampling through read_registers: is the CPU ever inside the banked window?
                for _ in range(60):
                    r = payload(await call("read_registers", {"cpu": CPU, "registers": ["PC"]}))
                    log["samples"].append(r["registers"][0]["hex"] if "registers" in r else r)
                    time.sleep(0.25)
                log["session_end"] = payload(await call("session_info")).get("bridge_client")
                log["wall_seconds"] = round(time.time() - t_wall0, 1)
    finally:
        try:
            proc.kill()
        except Exception:
            pass
    (out / "summary.json").write_text(json.dumps(log, indent=2, default=str))
    print(f"wrote {out / 'summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "A")))
