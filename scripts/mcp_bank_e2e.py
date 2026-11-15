"""MCP end-to-end bank-switch example: the official MCP SDK client drives the real server over stdio against a real
MAME 0.289 running 1942 (Revision B). Bank-select interventions come from the test fixture plugin only (the MCP server
has no write tool); each one is recorded in runs/mcp_bank_e2e/results.json.

Shows, through the MCP tools only:
  1. read_memory(bank_context=true) at the same logical address under two different selected entries,
  2. capture_breakpoint whose memory field follows the bank selected AT each hit while the entry changes mid-experiment,
  3. a read after the experiment reporting a different entry than the first event (so a later sample is not an event's bank).
Machine-specific addresses come from tests/fixtures/profiles/1942.json.
Usage:  .venv\\Scripts\\python -I scripts\\mcp_bank_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fx import Fixture  # noqa: E402
from validate_banked import FRAME, FRAME_NEXT, MAIN, PROFILE, ROMS, SEL, WIN0, expected, le, provenance  # noqa: E402

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from mame_mcp import winwake  # noqa: E402
from mame_mcp.launcher import PROJECT_ROOT, launch  # noqa: E402

PORT = 9453
OUT = PROJECT_ROOT / "runs" / "mcp_bank_e2e"
OUT.mkdir(parents=True, exist_ok=True)
RESULTS: list[dict] = []
INTERVENTIONS: list[dict] = []


def check(name: str, ok: bool, **ev) -> bool:
    RESULTS.append({"check": name, "status": "PASS" if ok else "FAIL", "evidence": ev})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {json.dumps(ev, default=str)[:360]}", flush=True)
    return ok


def payload(res) -> dict:
    if res.structured_content is not None:
        return res.structured_content
    return json.loads(res.content[0].text)


def entry_of(bank_context: dict) -> int | None:
    return {b["tag"]: b["entry"] for b in bank_context["banks"]}.get(MAIN["bank_tag"])


async def main() -> int:
    proc, info = launch(system="1942", port=PORT, run_dir=OUT / "run", rompath=ROMS, fixture=True)
    created = winwake.creation_time(proc.pid)
    fx = Fixture(info["fixture_dir"], wake=winwake.waker_for(lambda: (proc.pid, created), port=PORT))
    params = StdioServerParameters(
        command=sys.executable, args=["-I", "-m", "mame_mcp.server"],
        env={"MAMEMCP_PORT": str(PORT), "MAMEMCP_LAUNCH_INFO": str(OUT / "run" / "launch.json"), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")})
    errlog = open(OUT / "server_stderr.txt", "w")

    def select(entry: int, why: str) -> None:
        fx.call("space_write_u8", cpu=MAIN["tag"], space="program", address=SEL, value=entry)
        INTERVENTIONS.append({"kind": "write_select_register", "address": hex(SEL), "value": entry, "why": why})

    try:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                tools = {t.name: t for t in (await s.list_tools()).tools}
                check("MCP: read_memory advertises bank_context; no memory-write or bank-select tool exists",
                      "bank_context" in tools["read_memory"].input_schema["properties"] and not any(k in n for n in tools for k in ("write", "select", "poke", "bank")), tools=sorted(tools))
                si = payload(await s.call_tool("session_info", {}))
                bc = si.get("bridge_client", {})
                check("MCP: session_info shows machine 1942, MAME 0.289, wake nudge configured", si["machine"]["name"] == "1942" and si["mame_version"] == "0.289" and bc.get("wake_configured") is True,
                      machine=si["machine"]["description"], wake_configured=bc.get("wake_configured"), wake_posts=bc.get("wake_posts"))
                # let the game boot and select entry 0 itself, then run
                fx.call("user_resume")
                time.sleep(4.0)

                # 1. same logical address, two selected entries
                got = {}
                for n in (1, 2):
                    select(n, f"mcp e2e: read at entry {n}")
                    r = payload(await s.call_tool("read_memory", {"cpu": MAIN["tag"], "address": hex(WIN0), "length": 8, "bank_context": True}))
                    got[n] = (r["data_hex"], entry_of(r["bank_context"]))
                check("MCP read_memory(0x8000, bank_context) returns the selected entry's bytes for entries 1 and 2, with the entry reported",
                      all(got[n] == (expected(n, WIN0, 8).hex(), n) for n in (1, 2)) and got[1][0] != got[2][0], got=got, provenance=[provenance(1, WIN0), provenance(2, WIN0)])

                # 2. capture while the entry changes mid-experiment (user breakpoint at the next instruction switches 1 -> 2)
                fx.call("set_visible_cpu", cpu=MAIN["tag"])
                fx.call("user_stop")
                for _ in range(40):
                    if payload(await s.call_tool("session_info", {}))["execution"]["state"] == "debugger_stopped":
                        break
                    time.sleep(0.25)
                select(1, "mcp e2e: entry at the first hit")
                fx.call("debugger_printf", cpu=MAIN["tag"], text='"MARK|MCP"')
                idx = fx.call("user_bp", cpu=MAIN["tag"], address=FRAME_NEXT, action=f'printf "OBS|%X|%X",b@0x{WIN0:X},d@0x{WIN0:X};b@0x{SEL:X}=2;g')["index"]
                INTERVENTIONS.append({"kind": "user_breakpoint_action_writes_select_register", "address": hex(FRAME_NEXT), "value": 2, "why": "mcp e2e: switch entry 1 -> 2 after the first hit"})
                cap = payload(await s.call_tool("capture_breakpoint", {
                    "cpu": MAIN["tag"], "address": FRAME, "max_events": 3, "max_wall_seconds": 30, "registers": ["PC"],
                    "memory": [{"address": WIN0, "size": 1}, {"address": WIN0, "size": 4}], "label": "mcp-bank-switch"}))
                payload(await s.call_tool("execution_control", {"action": "resume"}))
                st = {}
                for _ in range(120):
                    st = payload(await s.call_tool("experiment_status", {"experiment_id": cap["experiment_id"]}))
                    if st["state"] != "running":
                        break
                    time.sleep(0.25)
                fx.call("user_bpclear", cpu=MAIN["tag"], index=idx)
                ev = [json.loads(x) for x in Path(st["evidence"]["events_jsonl"]).read_text().splitlines() if x.strip()]
                e1, e2 = expected(1, WIN0, 4), expected(2, WIN0, 4)
                vals = [(int(e["mem"][0]["value"], 16), int(e["mem"][1]["value"], 16)) for e in ev]
                check("MCP capture: events follow the bank selected at each hit (entry 1 at the first hit, entry 2 after the mid-experiment switch)",
                      st["state"] == "completed" and vals == [(e1[0], le(e1)), (e2[0], le(e2)), (e2[0], le(e2))],
                      captured=[(hex(a), hex(d)) for a, d in vals], entry1=e1.hex(), entry2=e2.hex(), evidence=st["evidence"]["events_jsonl"])
                after = payload(await s.call_tool("read_memory", {"cpu": MAIN["tag"], "address": hex(WIN0), "length": 4, "bank_context": True}))
                check("MCP: a read after the experiment reports entry 2, unlike event 1 (a later bank sample must not be attributed to the earlier event)",
                      entry_of(after["bank_context"]) == 2 and vals[0][1] != le(bytes.fromhex(after["data_hex"])), later_entry=entry_of(after["bank_context"]), later_bytes=after["data_hex"])
                mani = json.loads(Path(st["evidence"]["manifest_json"]).read_text())
                check("MCP capture manifest: bank context per event unavailable (stated), memory fields are logical addresses", mani["bank_context"]["per_event"] == "unavailable", bank_context=mani["bank_context"])
                select(0, "mcp e2e: restore")
                # stopped + idle, then a request through the MCP server's own identity-bound nudge (launch.json pid + creation time + port)
                fx.call("user_stop")
                before = payload(await s.call_tool("session_info", {}))["bridge_client"]["wake_posts"]
                time.sleep(8.0)
                t0 = time.time()
                idle = await s.call_tool("session_info", {})
                took = round(time.time() - t0, 2)
                bc2 = payload(idle)["bridge_client"]
                check("MCP: a request after 8 s idle while debugger-stopped was answered, with no timeout and no nudge refusal "
                      "(observation only: if wake_posts did not rise, the nudge was not needed in this sample and this does NOT demonstrate it)",
                      not idle.is_error and bc2["timeouts"] == 0 and bc2["wake_last_refusal"] is None,
                      seconds=took, wake_posts_before=before, wake_posts_after=bc2["wake_posts"], wake_last_refusal=bc2["wake_last_refusal"], timeouts=bc2["timeouts"])
                si2 =payload(await s.call_tool("session_info", {}))
                check("MCP: transport and nudge provenance visible in session_info", "bridge_client" in si2, bridge_client=si2.get("bridge_client"))
    finally:
        try:
            proc.kill()
        except Exception:
            pass
    fails = [r for r in RESULTS if r["status"] == "FAIL"]
    (OUT / "results.json").write_text(json.dumps({"mame": "0.289", "target": PROFILE["description"], "interventions": INTERVENTIONS, "results": RESULTS}, indent=2, default=str))
    print(f"\n{len(RESULTS) - len(fails)} passed, {len(fails)} failed -> {OUT / 'results.json'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
