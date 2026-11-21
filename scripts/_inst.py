"""One isolated real MAME with the fixture plugin plus a bridge client (test-only helper for validate_watchpoint.py,
validate_disasm.py and validate_sequence.py).

`fx` is the TEST-ONLY independent observer/intervention plugin (tests/fixtures/plugins/mamemcp_fixture); `c` is the production
bridge client. Nothing here is part of the server."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from _fx import Fixture, launch_mame

from mame_mcp import winwake
from mame_mcp.bridge import BridgeClient
from mame_mcp.launcher import PROJECT_ROOT
from mame_mcp.protocol import BridgeError

PROFILES = PROJECT_ROOT / "tests" / "fixtures" / "profiles"


def runs_root() -> Path:
    """Where a harness writes its MAME run directories and results.json: $MAMEMCP_HARNESS_RUNS if set (use a scratch directory
    outside the repository for throwaway runs), else the git-ignored runs/ next to the other harnesses."""
    return Path(os.environ.get("MAMEMCP_HARNESS_RUNS") or PROJECT_ROOT / "runs")


def profile(name: str = "watch_disasm_sequence") -> dict[str, Any]:
    return json.loads((PROFILES / f"{name}.json").read_text())


def H(v: str | int) -> int:
    return v if isinstance(v, int) else int(v, 16)


class Inst:
    def __init__(self, root: Path, name: str, port: int, system: str, test_env: dict | None = None, rompath: Path | None = None):
        self.name, self.run_dir, self.system = name, root / name, system
        shutil.rmtree(self.run_dir, ignore_errors=True)
        self.proc, self.info = launch_mame(system=system, port=port, run_dir=self.run_dir, fixture=True, test_env=test_env, **({"rompath": rompath} if rompath else {}))
        wake = winwake.waker_for((self.proc.pid, self.info.get("pid_created_filetime")), port=port)
        self.fx = Fixture(self.info["fixture_dir"], wake=wake)
        self.c = BridgeClient(port=port, default_timeout=30, wake=wake)
        self.c.connect(timeout=90)
        # The bridge can answer while MAME is still loading ROMs; a state-changing request in that window crashed MAME 0.289, so
        # wait (by wall clock) until devices are listed.
        end = time.monotonic() + 120
        while time.monotonic() < end and not self.info_().get("cpus"):
            time.sleep(0.5)

    def close(self) -> None:
        try:
            self.c.close(graceful=False)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()

    # bridge helpers -------------------------------------------------------------------------------------------------
    def rq(self, op: str, args: dict | None = None, mutating: bool = True, **kw):
        return self.c.request(op, args or {}, mutating=mutating and op not in ("session_info", "input_status", "list_inputs", "experiment_status", "disassemble"), **kw)

    def err(self, op: str, args: dict | None = None) -> BridgeError | None:
        try:
            self.rq(op, args)
        except BridgeError as e:
            return e
        return None

    def info_(self) -> dict:
        return self.c.request("session_info", {}, retry_safe=True)

    def state(self) -> str:
        return self.info_()["execution"]["state"]

    def wait(self, pred, timeout: float = 30.0, step: float = 0.1):
        end = time.time() + timeout
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(step)
        return None

    def resume(self) -> None:
        self.fx.call("user_resume")
        assert self.wait(lambda: self.state() == "running", 30), "could not reach running"

    def stop_debugger(self) -> None:
        self.fx.call("user_stop")
        assert self.wait(lambda: self.state() == "debugger_stopped", 30), "could not reach debugger_stopped"

    def pause(self) -> None:
        if self.state() != "running":
            self.resume()
        self.rq("execution_control", {"action": "pause"})
        assert self.wait(lambda: self.state() == "paused", 30), "could not reach paused"

    def poll_done(self, exp_id: str | None = None, timeout: float = 60.0) -> dict:
        end = time.time() + timeout
        st: dict = {}
        while time.time() < end:
            st = self.c.request("experiment_status", {"experiment_id": exp_id} if exp_id else {}, retry_safe=True)
            if st["state"] != "running":
                return st
            time.sleep(0.1)
        return st

    @staticmethod
    def events(st: dict) -> list[dict]:
        p = (st.get("evidence") or {}).get("events_jsonl")
        if not p or not Path(p).is_file():
            return []
        return [json.loads(x) for x in Path(p).read_text().splitlines() if x.strip()]


def code(e: BridgeError | None) -> str | None:
    return e.code if e else None
