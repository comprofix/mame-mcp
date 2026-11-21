"""Debugger-stopped first-request latency and late-reply/retry checks against a real MAME (Shinobi).

Writes runs/latency/results.json. MAME is started with -debug, so it begins in a debugger stop; no
request is made during the idle gaps. Latency is *measured* here; the GetMessage explanation is a
source reading (mame0289 src/osd/modules/debugger/debugwin.cpp), not something this script proves.
Usage:  .venv\\Scripts\\python -I scripts\\validate_latency.py [--gaps 1,3,6,12,20,30] [--reps 2]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fx import launch_mame  # noqa: E402

from mame_mcp import winwake  # noqa: E402
from mame_mcp.bridge import BridgeClient  # noqa: E402
from mame_mcp.launcher import PROJECT_ROOT  # noqa: E402
from mame_mcp.protocol import BridgeError, read_registers_args  # noqa: E402

OUT = PROJECT_ROOT / "runs" / "latency"
OUT.mkdir(parents=True, exist_ok=True)
PORT = 9461
RESULTS: list[dict] = []
NO_WAKE_LIMIT = 30.0  # without the nudge a request may not be answered for a long time


def check(name, ok, **ev):
    RESULTS.append({"check": name, "status": "PASS" if ok else "FAIL", "evidence": ev})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {json.dumps(ev, default=str)[:300]}", flush=True)
    return ok


def note(name, **ev):
    RESULTS.append({"check": name, "status": "INFO", "evidence": ev})
    print(f"[INFO] {name}  {json.dumps(ev, default=str)[:300]}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gaps", default="1,3,6,12,20,30")
    ap.add_argument("--reps", type=int, default=2)
    a = ap.parse_args()
    gaps = [float(x) for x in a.gaps.split(",")]
    proc, info = launch_mame(system="shinobi", port=PORT, run_dir=OUT / "run")
    c = BridgeClient(port=PORT, default_timeout=60)
    samples: dict[str, list] = {}
    wake_samples: dict[str, list] = {}
    unanswered: list = []
    try:
        si = c.connect(timeout=40)
        t0 = time.time()
        while si["execution"]["state"] != "debugger_stopped" and time.time() - t0 < 30:
            time.sleep(0.3)
            si = c.session_info()
        check("MAME is debugger-stopped (started with -debug)", si["execution"]["state"] == "debugger_stopped", state=si["execution"])

        # --- measured first-request latency after idle (no retries: long timeout, one attempt) -----------
        def timed(proc_pid):
            """Latency of one session_info with no nudge; None if unanswered within the limit (then recover with the nudge)."""
            t = time.monotonic()
            try:
                c._request_once("session_info", timeout=NO_WAKE_LIMIT)
                return time.monotonic() - t
            except BridgeError:
                c.wake = winwake.waker_for(proc_pid)
                c._request_once("session_info", timeout=20)
                c.wake = None
                return None

        for rep in range(a.reps):
            for gap in gaps:
                time.sleep(gap)
                first = timed(proc.pid)
                second = timed(proc.pid)
                for lbl, v in (("first", first), ("immediately-after", second)):
                    if v is None:
                        unanswered.append((f"{gap:g}s {lbl}", NO_WAKE_LIMIT))
                samples.setdefault(f"{gap:g}s", []).append(round(first, 3) if first is not None else f">{NO_WAKE_LIMIT:g} (unanswered)")
                print(f"  idle {gap:g}s: first {first if first is None else round(first, 3)}, immediately after {second if second is None else round(second, 3)}", flush=True)
        nums = [x for v in samples.values() for x in v if isinstance(x, float)]
        worst = max(nums) if nums else 0.0
        check("MEASURED first-request latency after idle while debugger-stopped, NO nudge (informational)", True,
              samples_s=samples, worst_answered_s=worst, unanswered_within_limit=unanswered, limit_s=NO_WAKE_LIMIT,
              note="measured on this host/MAME 0.289 Windows debugger front-end; irregular, not a guarantee")
        check("INFORMATIONAL: " + ("some first requests were NOT answered within %gs without the nudge" % NO_WAKE_LIMIT if unanswered
              else "every first request was answered within %gs in this run" % NO_WAKE_LIMIT), True,
              unanswered=unanswered, over_15s=[(k, x) for k, v in samples.items() for x in v if isinstance(x, float) and x >= 15])

        # --- same gaps WITH the opt-in WM_NULL nudge (client posts it while a request is unanswered) -----------
        c.wake = winwake.waker_for(proc.pid)
        for rep in range(a.reps):
            for gap in gaps:
                time.sleep(gap)
                t = time.monotonic()
                c._request_once("session_info")
                first = time.monotonic() - t
                wake_samples.setdefault(f"{gap:g}s", []).append(round(first, 3))
                print(f"  [wake] idle {gap:g}s: first {first:.3f}s", flush=True)
        wworst = max(max(v) for v in wake_samples.values())
        check("MEASURED first-request latency after idle with the WM_NULL nudge enabled (informational)", True,
              samples_s=wake_samples, worst_s=wworst, wake_posts=c.wake_posts)
        check("with the nudge, no first request took longer than 2 s in this run", wworst < 2.0, worst_s=wworst)
        # the checks below test protocol logic (ids, retries), not latency: keep the nudge on so they are deterministic

        # --- a late reply is matched by id and cannot be mistaken for a later reply ----------------------
        # Deterministic: send a request but do not read its reply (as if the client had timed out), then
        # issue the next request. The orphan's reply arrives first and must be discarded.
        c.stale_discarded = 0
        orphan = {"v": 1, "id": "orphan-1", "op": "session_info", "args": {}, "session_id": c.session_id}
        c._sock.sendall((json.dumps(orphan) + chr(10)).encode())
        regs = c.read_registers(read_registers_args(":maincpu", ["CURPC"]))
        check("orphaned session_info reply is discarded by request id; read_registers gets its own reply",
              "registers" in regs and "machine" not in regs and c.stale_discarded == 1,
              stale_discarded=c.stale_discarded, next_request_id=c.last_request_id)
        c.stale_discarded = 0
        # and a timed-out attempt (1 ms) whose reply arrives later is likewise discarded
        try:
            c._request_once("session_info", timeout=0.001)
        except BridgeError:
            pass
        regs2 = c.read_registers(read_registers_args(":maincpu", ["CURPC"]))
        check("late reply of a timed-out attempt never becomes the next answer", "registers" in regs2 and "machine" not in regs2,
              stale_discarded=c.stale_discarded)

        # --- real-MAME retry path, nudge OFF: a read-only request with a 3 s timeout either answers, or times out
        # and is retried once; the outcome depends on MAME's stall and is recorded, not judged.
        wk, c.wake = c.wake, None
        time.sleep(8)
        c.read_retries = c.timeouts = 0
        c.transport_events.clear()
        c.default_timeout = 3
        try:
            res = c.session_info()
            outcome = "answered" if res["machine"]["name"] == "shinobi" else "wrong payload"
        except BridgeError as e:
            outcome = f"raised {e.code} after retry"
        note("real MAME, no nudge: read-only session_info with 3 s timeout after idle", outcome=outcome, timeouts=c.timeouts,
             read_only_retries=c.read_retries, events=c.transport_events)
        check("a retried read-only request never returns a payload of another op", outcome in ("answered",) or outcome.startswith("raised"), outcome=outcome)
        c.wake, c.default_timeout = wk, 60
        c._request_once("session_info", timeout=30)  # recover/drain
        c.read_retries = 0
        try:
            c.request("read_memory", {"cpu": ":maincpu", "space": "program", "mode": "inspect", "address": 0, "length": 4}, timeout=0.001)
        except BridgeError as e:
            check("read_memory is not retried on timeout", e.code == "request_timeout" and c.read_retries == 0, code=e.code, retries=c.read_retries)
        else:
            check("read_memory is not retried on timeout", c.read_retries == 0, note="answered before the 1 ms timeout", retries=c.read_retries)
        c.close(graceful=True)
    except BridgeError as e:
        check("UNEXPECTED bridge error", False, error=e.to_dict())
    finally:
        c.close(graceful=False)
        proc.kill()
    n_fail = sum(1 for r in RESULTS if r["status"] == "FAIL")
    out = OUT / "results.json"
    out.write_text(json.dumps({"checks": RESULTS, "latency_samples_s": samples, "latency_samples_with_wake_s": wake_samples, "mame": info["mame_version"]}, indent=2))
    print(f"\n{len(RESULTS) - n_fail} passed, {n_fail} failed -> {out}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
