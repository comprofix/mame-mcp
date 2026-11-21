"""Screenshot capture through the OFFICIAL MCP SDK client against the production server and real MAME 0.289, for Shinobi
(landscape), 1944 (parent set, Europe 000620) and 1943kai (vertical, screen rotated 270). One server process
(`python -I -m mame_mcp.server`), MCP tools only, no fixture plugin and no test hooks. This is a harness run; a live
Claude Code session is a separate test.

Per machine:
  1  start_session; list_screens; a capture while debugger-stopped at frame 0 (a black image there is expected: boot progress is
     checked before any black image is judged)
  2  run until the screen is actually drawn; captures while running (frame/freshness reported, monotonic frames)
  3  pause: repeated paused captures do not advance the frame and are pixel-identical
  4  run on and pause again: the frame advanced and the new image is genuinely different (the scene changes)
  5  checkpoint: save, run on, load, capture right after the load (reported, not assumed), run a little, capture again
  6  evidence: files under the session's evidence/screenshots, unique names, returned image bytes == the file, sidecar metadata,
     geometry (orientation/rotation/size); stop_session; capture is refused afterwards
Writes results.json; the PNGs stay in the sessions' evidence directories for inspection.
Usage: .venv\\Scripts\\python -I scripts\\mcp_screenshot_e2e.py [--out DIR] [--systems shinobi,1944,1943kai]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import shutil
import struct
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fx import MAME_DIR, ROMPATH, Results, body_of, code_of  # noqa: E402

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from mame_mcp import png, winwake  # noqa: E402
from mame_mcp.launcher import PROJECT_ROOT  # noqa: E402

# expected geometry from MAME's own screen data, written down independently of the server: (native w, native h, orientation, png w, png h)
EXPECT = {"shinobi": (320, 224, 0, 320, 224), "1944": (384, 224, 0, 384, 224), "1943kai": (256, 224, 270, 224, 256)}
OUT = PROJECT_ROOT / "runs" / "mcp_screenshot_e2e"
R = Results("mcp_screenshot_e2e")
check, note = R.check, R.note


def server_env() -> dict[str, str]:
    # a hostile ambient environment: the production session must not inherit any of these
    return {"SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "MAMEMCP_MAME_DIR": str(MAME_DIR), "MAMEMCP_ROMPATH": str(ROMPATH), "MAMEMCP_RUNS_DIR": str(OUT / "sessions"),
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


async def info(s) -> dict:
    return await call(s, "session_info")


async def until(s, pred, timeout=30.0, tool="session_info", **args):
    end, last = time.time() + timeout, None
    while time.time() < end:
        last = await call(s, tool, **args)
        if pred(last):
            return last
        await asyncio.sleep(0.2)
    return last


async def run_for(s, seconds: float):
    await call(s, "execution_control", action="resume")
    await until(s, lambda x: x.get("execution", {}).get("state") == "running")
    await asyncio.sleep(seconds)


async def pause(s):
    await call(s, "execution_control", action="pause")
    r = await until(s, lambda x: x.get("execution", {}).get("state") == "paused")
    assert r["execution"]["state"] == "paused", r.get("execution")


async def frame(s) -> int:
    return (await info(s))["clock"]["frame"]


async def shot(s, **args):
    """(metadata, raw MCP result, png bytes from the image content)."""
    res = await s.call_tool("capture_screenshot", args)
    meta = body_of(res)
    data = None
    if not res.is_error:
        img = [c for c in res.content if c.type == "image"]
        data = base64.b64decode(img[0].data) if img else None
    return meta, res, data


def diff_pixels(a: bytes, b: bytes) -> int:
    (_, ra), (_, rb) = png.decode_pixels(a), png.decode_pixels(b)
    return sum(1 for x, y in zip(ra, rb) for i in range(0, len(x), 3) if x[i:i + 3] != y[i:i + 3])


def ihdr_size(data: bytes) -> tuple[int, int]:  # independent of mame_mcp.png: read the header straight from the bytes
    return struct.unpack(">II", data[16:24])


async def one_system(system: str, shots: list[dict]) -> None:
    exp_w, exp_h, exp_rot, png_w, png_h = EXPECT[system]
    async with mcp_server(f"server_stderr_{system}.txt") as s:
        r1 = await call(s, "start_session", system=system)
        check(f"{system} start_session ready, production plugin, parent set identity", r1.get("state") == "ready" and r1["machine"]["name"] == system,
              machine=r1.get("machine"), error=r1.get("_error"))
        if r1.get("state") != "ready":
            return
        pid, sid, evidence = r1["pid"], r1["session_id"], Path(r1["evidence_dir"])
        ls = await call(s, "list_screens")
        scr = ls["screens"]
        check(f"{system} list_screens: one screen, native size and orientation as MAME reports them", len(scr) == 1 and scr[0]["tag"] == ":screen"
              and (scr[0]["native_width"], scr[0]["native_height"], scr[0]["orientation_degrees"]) == (exp_w, exp_h, exp_rot), screens=scr)

        # ---- 1: debugger-stopped at boot ----------------------------------------------------------------
        m0, res0, d0 = await shot(s)
        check(f"{system} 1 capture while debugger-stopped returns PNG image content + metadata, nothing advanced",
              not res0.is_error and d0 is not None and [c.type for c in res0.content] == ["text", "image"] and m0["execution"]["state"] == "debugger_stopped"
              and m0["emulation_unchanged_by_capture"] is True and m0["frame"]["current_frame"] == 0, frame=m0.get("frame"), execution=m0.get("execution"), error=m0.get("_error"))
        note(f"{system} 1 boot-time image at frame {m0['frame']['current_frame']}", single_colour=m0["image"]["single_colour"], freshness=m0["frame"]["freshness"],
             note_text=m0["frame"]["freshness_note"])
        check(f"{system} 1 metadata names system, session, screen and evidence path", m0["system"] == system and m0["session_id"] == sid and m0["screen"] == ":screen"
              and Path(m0["evidence_path"]).parent == (evidence / "screenshots"), system_=m0["system"], session=m0["session_id"], path=m0["evidence_path"])
        shots.append({"system": system, "label": "boot_debugger_stopped", "path": m0["evidence_path"], "frame": m0["frame"]})

        # ---- 2: running; do not judge black before the machine has actually booted ----------------------
        await call(s, "execution_control", action="resume")
        await until(s, lambda x: x.get("execution", {}).get("state") == "running")
        drawn, last_meta, polls = None, None, 0
        deadline = time.time() + 90
        while time.time() < deadline:
            await asyncio.sleep(1.5)
            polls += 1
            mm, rr, dd = await shot(s)
            last_meta = mm
            if not rr.is_error and not mm["image"].get("single_colour", True) and mm["frame"]["current_frame"] >= 120:
                drawn = (mm, dd)
                break
        check(f"{system} 2 the screen shows a drawn (multi-colour) image once emulation has booted", drawn is not None,
              polls=polls, last_frame=(last_meta or {}).get("frame"), colours=(last_meta or {}).get("image"))
        if drawn is None:
            await call(s, "stop_session")
            return
        run_frames = []
        for _ in range(5):
            mm, rr, dd = await shot(s)
            run_frames.append((mm["frame"]["image_frame"], mm["frame"]["current_frame"], mm["frame"]["freshness"]))
            await asyncio.sleep(0.25)
        mono = all(b[0] >= a[0] for a, b in zip(run_frames, run_frames[1:]))
        check(f"{system} 2 captures while running report the frame of the image; frames never go backwards; image_frame <= current_frame",
              mono and run_frames[-1][0] > run_frames[0][0] and all(i <= c and f in ("fresh", "behind") for i, c, f in run_frames) and mm["execution"]["state"] == "running",
              captures=run_frames)

        # ---- 3: paused captures: no advance, identical pixels -------------------------------------------
        await pause(s)
        f_paused = await frame(s)
        pa, ra, da = await shot(s)
        await asyncio.sleep(1.0)
        pb, rb, db = await shot(s)
        await asyncio.sleep(0.5)
        f_after = await frame(s)
        pc, rc, dc = await shot(s)
        same_px = pa["image"]["pixel_sha256"] == pb["image"]["pixel_sha256"] == pc["image"]["pixel_sha256"]
        check(f"{system} 3 repeated paused captures do not advance the frame (screen counter and session clock unchanged)",
              pa["frame"]["current_frame"] == pb["frame"]["current_frame"] == pc["frame"]["current_frame"] == f_paused == f_after
              and pa["frame"]["image_frame"] == pb["frame"]["image_frame"] == pc["frame"]["image_frame"]
              and all(m["execution"]["state"] == "paused" and m["execution_after"]["state"] == "paused" and m["emulation_unchanged_by_capture"] for m in (pa, pb, pc)),
              frames=[pa["frame"]["current_frame"], pb["frame"]["current_frame"], pc["frame"]["current_frame"], f_paused, f_after])
        check(f"{system} 3 repeated paused captures are pixel-identical, fresh, with distinct files", same_px and {pa["frame"]["freshness"], pb["frame"]["freshness"], pc["frame"]["freshness"]} == {"fresh"}
              and len({pa["file_name"], pb["file_name"], pc["file_name"]}) == 3 and diff_pixels(da, db) == 0, files=[pa["file_name"], pb["file_name"], pc["file_name"]])
        shots.append({"system": system, "label": "paused_A", "path": pa["evidence_path"], "frame": pa["frame"]})

        # ---- 4: advance, then capture: refreshed output of a scene that changes ---------------------------
        # An attract screen can sit still for many seconds (1943kai's ranking table does), so advance in short rounds until the
        # image changes; every round must still report a later, fresh frame, and a static round proves nothing is stale because
        # the frame counter moved. Up to ~45 s of emulation.
        pd = rd_ = dd_ = None
        static_rounds = 0
        for _ in range(15):
            await run_for(s, 3.0)
            await pause(s)
            pd, rd_, dd_ = await shot(s)
            if pd["image"]["pixel_sha256"] != pa["image"]["pixel_sha256"]:
                break
            static_rounds += 1
        n_diff = diff_pixels(da, dd_)
        check(f"{system} 4 after emulation advanced the capture is a refreshed render: later frame, fresh, and the image really changed",
              pd["frame"]["current_frame"] > pa["frame"]["current_frame"] and pd["frame"]["image_frame"] > pa["frame"]["image_frame"] and pd["frame"]["freshness"] == "fresh"
              and pd["image"]["pixel_sha256"] != pa["image"]["pixel_sha256"] and n_diff > 100,
              frame_before=pa["frame"]["current_frame"], frame_after=pd["frame"]["current_frame"], differing_pixels=n_diff, total=png_w * png_h, static_rounds_before_change=static_rounds)
        shots.append({"system": system, "label": "paused_B_after_advance", "path": pd["evidence_path"], "frame": pd["frame"]})

        # ---- 5: checkpoint ---------------------------------------------------------------------------
        cp = await call(s, "save_checkpoint", label="screenshot validation")
        check(f"{system} 5 save_checkpoint with a screenshot taken at the same stopped state", "_error" not in cp and cp["frame"] == pd["frame"]["current_frame"], cp_frame=cp.get("frame"), shot_frame=pd["frame"]["current_frame"], error=cp.get("_error"))
        await run_for(s, 3.0)
        await pause(s)
        pe, re_, de = await shot(s)
        ld = await call(s, "load_checkpoint", checkpoint_id=cp["checkpoint_id"])
        check(f"{system} 5 load_checkpoint completed and verified", ld.get("state") == "completed" and (ld.get("verification") or {}).get("exact") is not False, state=ld.get("state"), error=ld.get("_error"))
        pf, rf, df = await shot(s)
        check(f"{system} 5 a capture right after the load works, is reported honestly and does not resume emulation",
              not rf.is_error and pf["frame"]["current_frame"] == cp["frame"] < pe["frame"]["current_frame"] and pf["execution"]["state"] == "paused"
              and pf["emulation_unchanged_by_capture"] and pf["state_epoch"] == (await info(s))["state_epoch"] and pf["frame"]["freshness"] in ("rendered_after_load_not_advanced", "stale_after_checkpoint_load"),
              frame=pf["frame"], execution=pf["execution"])
        note(f"{system} 5 image right after the load vs the image at save time", differing_pixels=diff_pixels(dd_, df), freshness=pf["frame"]["freshness"])
        shots.append({"system": system, "label": "right_after_load_paused", "path": pf["evidence_path"], "frame": pf["frame"]})
        await run_for(s, 2.0)
        await pause(s)
        pg, rg, dg = await shot(s)
        check(f"{system} 5 after the load and some emulated frames the capture is fresh, advanced past the load frame and the screen is drawn",
              not rg.is_error and pg["frame"]["freshness"] == "fresh" and (pg["frame"].get("frames_since_load") or 0) > 0 and pg["frame"]["current_frame"] > cp["frame"]
              and not pg["image"]["single_colour"], frame=pg["frame"], colours=pg["image"])
        shots.append({"system": system, "label": "after_load_and_run", "path": pg["evidence_path"], "frame": pg["frame"]})

        # ---- 6: evidence / geometry ---------------------------------------------------------------------
        allm = [m0, pa, pb, pc, pd, pe, pf, pg]
        alld = [d0, da, db, dc, dd_, de, df, dg]
        files = sorted((evidence / "screenshots").glob("*.png"))
        check(f"{system} 6 every returned image is byte-identical to the PNG on disk (returned file == evidence file) and the SHA-256 matches",
              all(Path(m["evidence_path"]).read_bytes() == d and hashlib.sha256(d).hexdigest() == m["sha256"] and m["file_size"] == len(d) for m, d in zip(allm, alld)))
        check(f"{system} 6 PNG validity: signature, CRCs, IEND, decodes; header size read independently equals the reported size",
              all(png.inspect_png(d).width == ihdr_size(d)[0] == m["image"]["width"] and png.inspect_png(d).height == ihdr_size(d)[1] == m["image"]["height"] and d[:8] == png.SIGNATURE for m, d in zip(allm, alld)))
        check(f"{system} 6 geometry: {png_w}x{png_h} (native {exp_w}x{exp_h}, orientation {exp_rot}), rotation/scaling/cropping documented",
              all((m["image"]["width"], m["image"]["height"]) == (png_w, png_h) and m["geometry"]["image_matches_native_after_rotation"] is True
                  and (m["geometry"]["native_width"], m["geometry"]["native_height"], m["geometry"]["orientation_degrees"]) == (exp_w, exp_h, exp_rot)
                  and {"rotation", "scaling", "cropping"} <= set(m["geometry"]) for m in allm))
        names = [Path(m["evidence_path"]).name for m in allm]
        check(f"{system} 6 unique filenames, all inside the session's evidence/screenshots, one metadata sidecar each", len(set(names)) == len(names)
              and all(Path(m["evidence_path"]).parent == evidence / "screenshots" and Path(m["sidecar_path"]).is_file() for m in allm) and len(files) >= len(allm),
              names=names, files_on_disk=len(files))
        side = json.loads(Path(pg["sidecar_path"]).read_text())
        check(f"{system} 6 the sidecar records system, session, screen, frame, execution state and hash", side["system"] == system and side["session_id"] == sid and side["screen"] == ":screen"
              and side["frame"]["current_frame"] == pg["frame"]["current_frame"] and side["execution"]["state"] == "paused" and side["sha256"] == pg["sha256"])
        # misuse
        bad = [code_of(await call(s, "capture_screenshot", screen=x)) for x in ("not a tag", "../../x.png", "screen")]
        unk = await call(s, "capture_screenshot", screen=":nosuchscreen")
        check(f"{system} 6 bad or unknown screen identifiers are refused (the unknown one lists the real screens), no output path argument exists",
              bad == ["bad_argument"] * 3 and code_of(unk) == "unknown_screen" and ":screen" in json.dumps(unk), bad=bad, unknown=unk.get("_error"))
        released = await call(s, "stop_session")
        gone = pid not in winwake_pids()
        check(f"{system} 6 stop_session stops only the owned MAME; the evidence stays; a capture afterwards is refused", released.get("stopped") is True and gone
              and (evidence / "screenshots").is_dir() and code_of(await call(s, "capture_screenshot")) == "no_session", stop=released.get("process"))


def winwake_pids() -> set[int]:
    import subprocess
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq mame.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    return {int(line.split('","')[1]) for line in out.splitlines() if line.startswith('"mame.exe"')}


async def main(systems: list[str]) -> int:
    global OUT
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    shots: list[dict] = []
    async with mcp_server("server_stderr_surface.txt") as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
        cs = tools["capture_screenshot"].input_schema
        check("A capture_screenshot takes only an optional screen; list_screens takes nothing; no path/Lua/debugger parameter", list(cs["properties"]) == ["screen"] and not cs.get("required")
              and not tools["list_screens"].input_schema.get("properties"), schema=cs)
        check("A no arbitrary Lua/debugger tool was added", not any(k in n for n in tools for k in ("lua", "shell", "eval", "command")), tools=sorted(tools))
        codes = [code_of(await call(s, t)) for t in ("capture_screenshot", "list_screens")]
        check("A both report no_session when no managed MAME exists", codes == ["no_session", "no_session"], codes=codes)
        check("A an invalid screen is refused before anything else", code_of(await call(s, "capture_screenshot", screen="bad tag")) in ("bad_argument", "no_session"))
    for system in systems:
        try:
            await one_system(system, shots)
        except Exception as e:  # noqa: BLE001  one machine failing must not hide the others
            import traceback
            check(f"{system} the scenario ran to completion", False, error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-600:])
    (OUT / "images.json").write_text(json.dumps(shots, indent=2, default=str))
    return R.save(OUT / "results.json", mame=str(MAME_DIR), systems=systems)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path)
    ap.add_argument("--systems", default="shinobi,1944,1943kai")
    a = ap.parse_args()
    if a.out:
        OUT = a.out.resolve()
    raise SystemExit(asyncio.run(main([x for x in a.systems.split(",") if x])))
