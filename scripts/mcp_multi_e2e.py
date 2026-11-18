r"""MCP end-to-end multi-address capture: the official MCP SDK client drives the real server over stdio against real
MAME 0.289 running 1942 (Revision B), :maincpu. Only MCP tools are used (no fixture plugin).
Shows: tool schema advertises `locations`; invalid requests rejected by the server; one experiment on two locations
(0x11 and 0x08) with an unhit third (0x166); events name location/address/pc; per-location counts; cleanup; legacy
single-address call still works.
Usage:  .venv\Scripts\python -I scripts\mcp_multi_e2e.py
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

PORT = 9461
OUT = PROJECT_ROOT / "runs" / "mcp_multi_e2e"
ROMS = Path(r"D:\arcade\mame\roms")
RESULTS: list[dict] = []


def check(name, ok, **ev):
    RESULTS.append({"check": name, "status": "PASS" if ok else "FAIL", "evidence": ev})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {json.dumps(ev, default=str)[:400]}", flush=True)
    return ok


def payload(res):
    if res.is_error:
        return {"_error": res.content[0].text}
    return res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)


async def wait_done(s, exp, timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = payload(await s.call_tool("experiment_status", {"experiment_id": exp}))
        if st.get("state") != "running":
            return st
        await asyncio.sleep(0.4)
    return st


async def main() -> int:
    proc, info = launch(system="1942", port=PORT, run_dir=OUT / "run", rompath=ROMS)
    params = StdioServerParameters(command=sys.executable, args=["-I", "-m", "mame_mcp.server"],
                                   env={"MAMEMCP_PORT": str(PORT), "MAMEMCP_LAUNCH_INFO": str(OUT / "run" / "launch.json"), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")})
    errlog = open(OUT / "server_stderr.txt", "w")
    try:
        async with stdio_client(params, errlog=errlog) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = {t.name: t for t in (await s.list_tools()).tools}
                props = tools["capture_breakpoint"].input_schema["properties"]
                check("MCP: capture_breakpoint advertises `locations` and still `address`; no watchpoint/command/eval/lua tool (the typed input/checkpoint/write_memory tools are the later, separately validated milestone)",
                      "locations" in props and "address" in props and not any(k in n for n in tools for k in ("watch", "command", "eval", "lua")), tools=sorted(tools))
                await s.call_tool("execution_control", {"action": "resume"})
                await asyncio.sleep(4)
                base = {"cpu": ":maincpu", "max_events": 20, "max_wall_seconds": 20, "registers": ["PC", "HL"], "memory": [{"address": 0, "size": 1}]}
                bad = payload(await s.call_tool("capture_breakpoint", {**base, "locations": [{"address": 0x11}, {"address": 0x11}]}))
                check("MCP: duplicate locations rejected with an error, nothing installed", "_error" in bad and "duplicate" in bad["_error"].lower(), error=bad.get("_error", "")[:200])
                cap = payload(await s.call_tool("capture_breakpoint", {**base, "label": "mcp-multi",
                              "locations": [{"id": "frame", "address": 0x11}, {"id": "irq8", "address": 0x08}, {"id": "svc", "address": 0x166}]}))
                check("MCP: install response: all_armed, 3 locations, limit documented", cap.get("all_armed") is True and len(cap["locations"]) == 3 and cap.get("location_limit") == 8, cap={k: cap.get(k) for k in ("all_armed", "location_limit")})
                st = await wait_done(s, cap["experiment_id"])
                ev = [json.loads(x) for x in Path(st["evidence"]["events_jsonl"]).read_text().splitlines() if x.strip()]
                counts = {l["id"]: l["events_captured"] for l in st["locations"]}
                check("MCP: 20 events, one global sequence, both firing locations present, unhit 'svc' armed with no hits",
                      st["state"] == "completed" and [e["seq"] for e in ev] == list(range(1, 21)) and counts["frame"] > 0 and counts["irq8"] > 0 and counts["svc"] == 0
                      and next(l for l in st["locations"] if l["id"] == "svc")["no_hits_observed"], counts=counts)
                check("MCP: every event names location, address and hit-time pc consistent with its location",
                      all({"frame": "0x11", "irq8": "0x8"}[e["location"]] == e["address"] == e["pc"] for e in ev), first=ev[0])
                m = json.loads(Path(st["evidence"]["manifest_json"]).read_text())
                check("MCP: manifest records locations, installation, per-location counts and verified cleanup",
                      len(m["parameters"]["locations"]) == 3 and m["installation"]["all_armed"] and len(m["per_location"]) == 3 and m["cleanup"]["verified"] and m["cleanup"]["breakpoint_cleared"], cleanup=m["cleanup"]["locations"])
                ins = m["instrumentation"]
                check("MCP: manifest `instrumentation` is still an ARRAY (existing readers use instrumentation[0]) with one breakpoints entry that lists the 3 locations (id, address, breakpoint_index, action) and ONE shared set of debugger temporaries",
                      isinstance(ins, list) and len(ins) == 1 and ins[0]["kind"] == "breakpoints" and ins[0]["cpu"] == ":maincpu"
                      and [(l["id"], l["address"]) for l in ins[0]["locations"]] == [("frame", "0x11"), ("irq8", "0x8"), ("svc", "0x166")]
                      and all(isinstance(l["breakpoint_index"], int) and "MCP|" in l["action"] for l in ins[0]["locations"])
                      and "(hit-time pc)" in ins[0]["debugger_temporaries"]["registers"] and "counter" in ins[0]["debugger_temporaries"],
                      instrumentation_type=type(ins).__name__, locations=ins[0]["locations"] if isinstance(ins, list) else None)
                check("MCP: manifest installation/per_location/cleanup structure: installation.locations all armed with breakpoint indexes; per_location ids+counts equal the status counts; cleanup.locations all cleared",
                      [r["armed"] for r in m["installation"]["locations"]] == [True] * 3 and {r["id"]: r["events_captured"] for r in m["per_location"]} == counts
                      and all(r["cleared"] for r in m["cleanup"]["locations"]) and m["final_state"] == "completed" and m["bank_context"]["per_event"] == "unavailable",
                      per_location=m["per_location"])
                legacy = payload(await s.call_tool("capture_breakpoint", {**base, "address": 0x11, "max_events": 3}))
                st2 = await wait_done(s, legacy["experiment_id"])
                m2 = json.loads(Path(st2["evidence"]["manifest_json"]).read_text())
                check("MCP: legacy single-address manifest: instrumentation is an array, parameters.address kept, one location L1", isinstance(m2["instrumentation"], list) and m2["parameters"]["address"] == "0x11" and [l["id"] for l in m2["instrumentation"][0]["locations"]] == ["L1"], parameters=m2["parameters"])
                check("MCP: legacy single-address call still works", st2["state"] == "completed" and st2["counts"]["events_captured"] == 3 and "breakpoint_index" in legacy)
                si = payload(await s.call_tool("session_info", {}))
                check("MCP: no timeouts or retries in the transport", si["bridge_client"].get("timeouts", 0) == 0, bridge=si["bridge_client"])
    finally:
        proc.kill()
    n = sum(1 for r in RESULTS if r["status"] == "PASS")
    (OUT / "results.json").write_text(json.dumps(RESULTS, indent=2, default=str))
    print(f"{n} passed, {len(RESULTS) - n} failed")
    return 0 if n == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
