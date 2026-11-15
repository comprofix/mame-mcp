"""Reproducible example experiment (Shinobi): capture the level-4 interrupt handler entry.

What it does, with MAME 0.289 and shinobi.zip:
  1. launches an isolated MAME (runs/example/run), loads the mamemcp plugin;
  2. reads the 68000 level-4 autovector (address 0x70) with read_memory to find the handler address;
  3. captures 30 entries to that handler with CURPC, D0, SR, SP and the vector long recorded;
  4. prints a compact summary and where the JSONL/manifest evidence lives.

Expected: handler address 0x404 on this set; 30 events (hit order 1..30), every event at PC 0x404,
0 dropped, breakpoint cleaned up, execution left running. Nothing is hard-coded into the server:
the address is read from the running machine here, in the experiment script.
"""
import json
import sys
import time

from mame_mcp.bridge import BridgeClient
from mame_mcp.launcher import PROJECT_ROOT, launch
from mame_mcp.protocol import capture_args, read_memory_args

PORT = 9466


def main() -> int:
    proc, info = launch(port=PORT, run_dir=PROJECT_ROOT / "runs" / "example" / "run")
    c = BridgeClient(port=PORT)
    try:
        si = c.connect(timeout=40)
        print(f"MAME {si['mame_version']}  machine={si['machine']['name']}  cpus={[d['tag'] for d in si['cpus']]}")
        c.execution_control("resume")
        time.sleep(1.0)
        vec = c.read_memory(read_memory_args(":maincpu", "program", 0x70, 4))["data_hex"]
        handler = int(vec, 16)
        print(f"level-4 autovector @0x70 = 0x{vec}  -> handler 0x{handler:X}")
        cap = c.capture_breakpoint(capture_args(":maincpu", handler, 30, 15, registers=["D0", "SR", "SP"],
                                                memory=[{"address": 0x70, "size": 4}], label="shinobi-irq4-entry"))
        exp = cap["experiment_id"]
        while True:
            st = c.experiment_status(exp)
            if st["state"] != "running":
                break
            time.sleep(0.1)
        print("status :", st["summary"])
        print("cleanup:", st["cleanup"], " final execution:", st["final_execution"]["state"])
        print("evidence:", st["evidence"]["events_jsonl"])
        print("manifest:", st["evidence"]["manifest_json"])
        events = [json.loads(line) for line in open(st["evidence"]["events_jsonl"])]
        print("first event:", json.dumps(events[0]))
        ok = (len(events) == 30 and all(e["pc"] == f"0x{handler:X}" for e in events)
              and st["counts"]["events_dropped"] == 0 and st["cleanup"]["breakpoint_cleared"])
        print("RESULT:", "as expected" if ok else "UNEXPECTED")
        return 0 if ok else 1
    finally:
        c.close(graceful=False)
        proc.kill()


if __name__ == "__main__":
    sys.exit(main())
