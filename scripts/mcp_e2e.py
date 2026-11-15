"""End-to-end check through the official MCP Python SDK client over stdio against a real MAME.

Launches an isolated MAME, starts the MCP server as a subprocess exactly as an MCP client would,
and drives every tool. Writes runs/mcp_e2e/results.json.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mame_mcp.launcher import PROJECT_ROOT, launch

PORT = 9455
OUT = PROJECT_ROOT / "runs" / "mcp_e2e"
OUT.mkdir(parents=True, exist_ok=True)
RESULTS: list[dict] = []


def check(name: str, ok: bool, **ev) -> bool:
    RESULTS.append({"check": name, "status": "PASS" if ok else "FAIL", "evidence": ev})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {json.dumps(ev, default=str)[:300]}")
    return ok


def payload(res) -> dict:
    if res.structured_content is not None:
        return res.structured_content
    return json.loads(res.content[0].text)


async def main() -> int:
    proc, info = launch(port=PORT, run_dir=OUT / "run")
    params = StdioServerParameters(
        command=sys.executable, args=["-I", "-m", "mame_mcp.server"],
        env={"MAMEMCP_PORT": str(PORT), "MAMEMCP_LAUNCH_INFO": str(OUT / "run" / "launch.json"),
             "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")},
    )
    errlog = open(OUT / "server_stderr.txt", "w")
    try:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as s:
                init = await s.initialize()
                tools = await s.list_tools()
                names = sorted(t.name for t in tools.tools)
                check("MCP initialize + list_tools", names == sorted(["session_info", "read_memory", "read_registers", "capture_breakpoint",
                      "experiment_status", "cancel_experiment", "execution_control", "start_session", "stop_session"]), tools=names)
                check("no arbitrary lua/shell/debugger-command tool exposed",
                      not any(k in n for n in names for k in ("lua", "shell", "exec_", "command", "eval")))

                # MAME was started with -debug so it begins in a debugger stop; first call also auto-connects.
                si = payload(await s.call_tool("session_info", {}))
                check("session_info via MCP", si["machine"]["name"] == "shinobi" and si["execution"]["state"] in ("debugger_stopped", "paused", "running"),
                      state=si["execution"], mame=si["mame_version"], rom_verify=(si["rom_identity"].get("launch_info") or {}).get("verifyroms"))
                check("session_info rom identity: bridge hashes marked unavailable, launcher info merged",
                      si["rom_identity"]["hashes"].get("unavailable") is True and "expected_roms" in si["rom_identity"]["launch_info"])
                r = payload(await s.call_tool("execution_control", {"action": "resume"}))
                time.sleep(1.5)
                regs = payload(await s.call_tool("read_registers", {"cpu": ":maincpu", "registers": ["CURPC", "SR"]}))
                check("read_registers via MCP", [x["name"] for x in regs["registers"]] == ["CURPC", "SR"], regs=regs["registers"])
                mem = payload(await s.call_tool("read_memory", {"cpu": ":maincpu", "address": "0x404", "length": 4}))
                check("read_memory via MCP (hex address string)", mem["data_hex"] == "60000000" or len(mem["data_hex"]) == 8, data=mem["data_hex"])

                bad = await s.call_tool("read_memory", {"cpu": ":maincpu", "address": 0, "length": 100000})
                check("oversize read rejected as MCP tool error with reason", bad.is_error is True and "bad_argument" in bad.content[0].text, text=bad.content[0].text[:200])
                bad2 = await s.call_tool("capture_breakpoint", {"cpu": ":maincpu", "address": 1028, "max_events": 5})
                check("capture without wall-clock limit rejected", bad2.is_error is True, text=bad2.content[0].text[:160])

                cap = payload(await s.call_tool("capture_breakpoint", {
                    "cpu": ":maincpu", "address": 1028, "max_events": 5, "max_wall_seconds": 20,
                    "registers": ["D0", "SR"], "memory": [{"address": 1028, "size": 2}], "label": "mcp-e2e"}))
                check("capture_breakpoint returns id promptly", cap["asynchronous"] is True, id=cap["experiment_id"])
                st = {}
                for _ in range(80):
                    st = payload(await s.call_tool("experiment_status", {"experiment_id": cap["experiment_id"]}))
                    if st["state"] != "running":
                        break
                    await asyncio.sleep(0.1)
                check("experiment_status: completed by event limit, compact", st["state"] == "completed" and st["completion_reason"] == "event_limit"
                      and st["counts"]["events_captured"] == 5 and len(json.dumps(st)) < 6000, summary=st["summary"], response_bytes=len(json.dumps(st)))
                ev_path = Path(st["evidence"]["events_jsonl"])
                check("evidence files exist where reported", ev_path.exists() and Path(st["evidence"]["manifest_json"]).exists(), events=str(ev_path))

                cap2 = payload(await s.call_tool("capture_breakpoint", {"cpu": ":maincpu", "address": 1028, "max_events": 10000, "max_wall_seconds": 30}))
                await asyncio.sleep(0.5)
                cx = payload(await s.call_tool("cancel_experiment", {"experiment_id": cap2["experiment_id"]}))
                check("cancel_experiment via MCP", cx["state"] == "cancelled" and cx["cleanup"]["breakpoint_cleared"], cleanup=cx["cleanup"], counts=cx["counts"])

                # kill MAME underneath a live MCP session
                proc.kill(); proc.wait()
                await asyncio.sleep(0.5)
                dis = await s.call_tool("read_registers", {"cpu": ":maincpu", "registers": ["D0"]})
                body = dis.content[0].text
                check("MAME exit reported as useful MCP tool error", dis.is_error is True and ("mame_disconnected" in body or "connect_failed" in body), text=body[:240])
    finally:
        if proc.poll() is None:
            proc.kill()
        errlog.close()
    (OUT / "results.json").write_text(json.dumps({"results": RESULTS}, indent=2, default=str))
    fails = [r for r in RESULTS if r["status"] == "FAIL"]
    print(f"\n{len(RESULTS) - len(fails)} passed, {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
