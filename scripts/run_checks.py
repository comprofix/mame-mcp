"""One entry point for the checks: choose a set by WHAT CHANGED, look at the plan, then run it once.

    .\\.venv\\Scripts\\python -I scripts\\run_checks.py --plan lifecycle    # measure twice: what will run, why, how long (nothing starts)
    .\\.venv\\Scripts\\python -I scripts\\run_checks.py lifecycle           # cut once
    .\\.venv\\Scripts\\python -I scripts\\run_checks.py lifecycle --repeat 3 # only the natural-condition suites gain from repeats

Sets (pick by the code you changed; a harness is run for the code it exercises, not "to be safe"):
  unit       pytest: fake sockets, fake process table, bridge client. Always cheap; run it first, always.
  lifecycle  src/mame_mcp/session.py, bridge.py (connect / wake), winwake.py, server.py shutdown: unit + the deterministic stop/reconnect
             gate + the managed-session, restart and screenshot suites (the places the original symptoms appeared).
  tools      plugin/mamemcp/init.lua, operations.py, protocol.py, tool definitions in server.py: the harnesses that drive each typed tool.
  legacy     the older direct-bridge suites for the plugin's capture, banked-memory, checkpoint and hardening behaviour.
  latency    debugger-stopped first-request latency and late-reply retries. 15 minutes: opt-in, only when the request/wake path itself changes.
  all        lifecycle + tools + legacy (not latency).

Rules this runner keeps: harnesses run one at a time (they share the machine and are timing-sensitive; MAME is never run twice at once),
nothing is retried silently, a failure line is always printed in full, and the exit status is non-zero if any suite failed.
Logs go to --out (default: $MAMEMCP_HARNESS_RUNS, else a new temporary directory outside the repository). Only the newer harnesses honour
MAMEMCP_HARNESS_RUNS for their results.json; the older ones write to the git-ignored runs\\ folder.
Estimated seconds were measured on this machine with MAME 0.289 and are a guide, not a limit.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# name -> (argv after "python -I", estimated seconds, what it is for)
SUITES: dict[str, tuple[list[str], int, str]] = {
    "unit": (["-m", "pytest", "-q"], 15, "unit tests (no MAME); includes the tests that fail on the pre-fix session/bridge/winwake code"),
    "stop_reconnect": (["scripts/validate_stop_reconnect.py"], 75, "deterministic: forced connection reset at stop/shutdown, reconnect to a debugger-stopped MAME, frozen-MAME bound"),
    "session": (["scripts/mcp_session_e2e.py"], 15, "managed-session lifecycle through the MCP client: start/stop/exit/refusals"),
    "restart": (["scripts/mcp_restart_e2e.py"], 45, "one MCP server across MAME restarts; killed-server re-attach (section E)"),
    "screenshot": (["scripts/mcp_screenshot_e2e.py"], 60, "three fresh managed sessions through MCP; the natural stop_session path"),
    "e2e": (["scripts/mcp_e2e.py"], 6, "external-attachment smoke test through the MCP client"),
    "watchpoint": (["scripts/validate_watchpoint.py"], 110, "capture_watchpoint vs independent observers"),
    "disasm": (["scripts/validate_disasm.py"], 60, "disassemble_memory vs MAME's own dasm"),
    "sequence": (["scripts/validate_sequence.py"], 25, "press_sequence vs a per-frame port observer"),
    "wds": (["scripts/mcp_wds_e2e.py"], 15, "the three new tools through the MCP client"),
    "bank_capture": (["scripts/mcp_bank_capture_e2e.py"], 35, "examples/bank_capture.py on three different machines"),
    "checkpoint_e2e": (["scripts/mcp_checkpoint_e2e.py"], 25, "checkpoints, RAM writes and inputs through the MCP client"),
    "bank_e2e": (["scripts/mcp_bank_e2e.py"], 20, "banked capture through the MCP client"),
    "multi_e2e": (["scripts/mcp_multi_e2e.py"], 10, "multi-location capture through the MCP client; tool-list shape"),
    "multisession": (["scripts/mcp_multisession_e2e.py"], 120, "three simultaneous managed sessions (two shinobi, one 1942) through one MCP server: routing, isolation, limit, shutdown"),
    "validate": (["scripts/validate.py"], 30, "direct-bridge regression (Shinobi)"),
    "banked": (["scripts/validate_banked.py"], 105, "banked-memory validation (1942)"),
    "checkpoint_input": (["scripts/validate_checkpoint_input.py"], 95, "plugin-level checkpoints, held inputs, RAM writes"),
    "hardening": (["scripts/validate_hardening.py"], 75, "plugin hardening and refusals"),
    "multi": (["scripts/validate_multi.py"], 75, "multi-address capture validation (1942)"),
    "latency": (["scripts/validate_latency.py"], 910, "debugger-stopped first-request latency and late-reply retries"),
}
LIFECYCLE = ["unit", "stop_reconnect", "session", "restart", "screenshot", "e2e", "multisession"]
TOOLS = ["watchpoint", "disasm", "sequence", "wds", "bank_capture", "checkpoint_e2e", "bank_e2e", "multi_e2e"]
LEGACY = ["validate", "banked", "checkpoint_input", "hardening", "multi"]
SETS: dict[str, list[str]] = {"unit": ["unit"], "lifecycle": LIFECYCLE, "tools": TOOLS, "legacy": LEGACY, "latency": ["latency"], "all": LIFECYCLE + TOOLS + LEGACY}
NATURAL = {"screenshot", "restart"}  # suites whose original symptom was intermittent; repeats only mean something for these


def plan(names: list[str], repeat: int) -> list[tuple[str, str]]:
    return [(n, n if (i == 0 or n not in NATURAL) else f"{n}#{i + 1}") for i in range(repeat) for n in names if i == 0 or n in NATURAL]


def preflight(names: list[str]) -> list[str]:
    """Fail before the first minute is spent, not in the tenth."""
    problems: list[str] = []
    if names == ["unit"]:
        return problems
    sys.path.insert(0, str(HERE))
    from _fx import MAME_DIR, ROMPATH  # noqa: E402
    if not (MAME_DIR / "mame.exe").is_file():
        problems.append(f"MAME not found: {MAME_DIR / 'mame.exe'}")
    if not ROMPATH.is_dir():
        problems.append(f"ROM directory not found: {ROMPATH}")
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq mame.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    if "mame.exe" in out.lower():
        problems.append("a mame.exe is already running; close it first (the harnesses start and stop only their own MAME and are timing-sensitive)")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sets", nargs="*", help="set or suite names (default: lifecycle); see the docstring")
    ap.add_argument("--plan", action="store_true", help="print what would run, why and for how long, then exit")
    ap.add_argument("--repeat", type=int, default=1, help="repeat the natural-condition suites (screenshot, restart) this many times")
    ap.add_argument("--out", help="log directory (default: $MAMEMCP_HARNESS_RUNS, else a new temporary directory)")
    ap.add_argument("--stop-on-fail", action="store_true", help="stop at the first failing suite")
    args = ap.parse_args()

    names: list[str] = []
    for s in args.sets or ["lifecycle"]:
        got = SETS.get(s) or ([s] if s in SUITES else None)
        if got is None:
            ap.error(f"unknown set or suite '{s}'; sets: {', '.join(SETS)}; suites: {', '.join(SUITES)}")
        names += [n for n in got if n not in names]
    todo = plan(names, max(1, args.repeat))
    est = sum(SUITES[n][1] for n, _ in todo)

    print(f"{'suite':<18} {'est s':>6}  purpose")
    for n, label in todo:
        print(f"{label:<18} {SUITES[n][1]:>6}  {SUITES[n][2]}")
    print(f"{'total':<18} {est:>6}  (about {est / 60:.1f} min, run one after another)")
    if args.plan:
        return 0
    problems = preflight(names)
    if problems:
        print("\npreflight failed:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2

    out = Path(args.out or os.environ.get("MAMEMCP_HARNESS_RUNS") or tempfile.mkdtemp(prefix="mamemcp-checks-"))
    (out / "logs").mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "MAMEMCP_HARNESS_RUNS": str(out / "runs")}
    print(f"\nlogs: {out / 'logs'}\n")
    failed: list[str] = []
    t_all = time.time()
    for n, label in todo:
        argv = [sys.executable, "-I", *SUITES[n][0]]
        log = out / "logs" / f"{label.replace('#', '_')}.log"
        t0 = time.time()
        try:
            with open(log, "w", encoding="utf-8") as fh:
                rc = subprocess.run(argv, cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=max(600, SUITES[n][1] * 4)).returncode
        except subprocess.TimeoutExpired:
            rc = -1
        lines = [x for x in log.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]
        last = lines[-1][:110] if lines else "(no output)"
        print(f"{'ok  ' if rc == 0 else 'FAIL'} {label:<18} {time.time() - t0:6.0f}s  {last}", flush=True)
        if rc != 0:
            failed.append(label)
            for ln in [x for x in lines if x.startswith("[FAIL]") or x.startswith("FAILED")][:8]:
                print("       " + ln[:240])
            if rc == -1:
                print("       timed out")
            if args.stop_on_fail:
                break
    print(f"\n{len(todo) - len(failed)}/{len(todo)} suites passed in {(time.time() - t_all) / 60:.1f} min" + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
