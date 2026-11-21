"""EXAMPLE (Shinobi): a direct bridge smoke test. Launches an isolated MAME, exercises the bridge, prints JSON, cleans up.

This is not an MCP tool and not part of the regression harnesses (scripts/): it drives the Python bridge client directly, without the
MCP server, as a quick check that the plugin and bridge work. The registers and address below are the 68000 / Shinobi ones.
Usage:  .venv\\Scripts\\python -I examples\\bridge_smoke.py
"""
import json
import time
from pathlib import Path

from mame_mcp.bridge import BridgeClient
from mame_mcp.launcher import launch
from mame_mcp.protocol import BridgeError, capture_args, read_memory_args, read_registers_args

MAME_DIR = Path(r"D:\arcade\mame")  # holds mame.exe (MAME 0.289)
ROMPATH = Path(r"D:\arcade\mame\ROMS")  # the ROM collection (read only)
port = 9411
proc, info = launch(system="shinobi", port=port, mame_dir=MAME_DIR, rompath=ROMPATH)
c = BridgeClient(port=port)
try:
    si = c.connect(timeout=30)
    print("session_info:", json.dumps({k: si[k] for k in ("mame_version", "machine", "execution")}, indent=1))
    print("cpus:", [(d["tag"], [s["name"] for s in d["address_spaces"]]) for d in si["cpus"]])
    print("resume:", c.execution_control("resume")["after_request"])
    time.sleep(1.5)
    print("regs:", json.dumps(c.read_registers(read_registers_args(":maincpu", ["CURPC", "SR", "SP", "D0"]))["registers"]))
    print("mem:", c.read_memory(read_memory_args(":maincpu", "program", 0x404, 8))["data_hex"])
except BridgeError as e:
    print("ERROR", e.to_dict())
finally:
    c.close()
    proc.kill()
    print("run_dir:", info["run_dir"])
