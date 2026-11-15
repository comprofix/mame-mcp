"""Launch a *separate, isolated* MAME instance with the mamemcp plugin.

Nothing here modifies the MAME installation: the plugin is loaded through -pluginspath/-plugin and all
writable state (home, ini, cfg, nvram, snapshots, evidence) lives in a run directory inside this project.
The ROM path is only read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = PROJECT_ROOT / "plugin"
FIXTURE_PLUGIN_DIR = PROJECT_ROOT / "tests" / "fixtures" / "plugins"  # TEST-ONLY plugin, never loaded by default
DEFAULT_MAME_DIR = Path(os.environ.get("MAME_DIR", r"D:\arcade\mame"))


def _fwd(p: Path | str) -> str:
    return str(p).replace("\\", "/")


def mame_version(mame_exe: Path) -> str:
    out = subprocess.run([str(mame_exe), "-version"], capture_output=True, text=True, timeout=60, cwd=mame_exe.parent)
    return out.stdout.strip()


def verify_roms(mame_exe: Path, system: str, rompath: Path) -> dict[str, Any]:
    r = subprocess.run([str(mame_exe), "-verifyroms", system, "-rompath", _fwd(rompath)],
                       capture_output=True, text=True, timeout=120, cwd=mame_exe.parent)
    return {"returncode": r.returncode, "output": (r.stdout + r.stderr).strip()}


def listxml_roms(mame_exe: Path, system: str) -> dict[str, Any]:
    r = subprocess.run([str(mame_exe), "-listxml", system], capture_output=True, text=True, timeout=120, cwd=mame_exe.parent)
    root = ET.fromstring(r.stdout)
    m = root.find(f"machine[@name='{system}']")
    if m is None:
        return {"error": f"system {system} not in -listxml output"}
    return {
        "name": m.get("name"), "cloneof": m.get("cloneof"), "romof": m.get("romof"),
        "roms": [{k: v for k, v in rom.attrib.items() if k in ("name", "size", "crc", "sha1", "region", "offset")}
                 for rom in m.findall("rom")],
        "cpus": [c.attrib for c in m.findall("chip") if c.get("type") == "cpu"],
    }


def build_command(*, mame_exe: Path, system: str, rompath: Path, run_dir: Path, port: int,
                  debug: bool = True, debugscript: Path | None = None, debuglog: bool = False,
                  throttle: bool = True, extra: list[str] | None = None, fixture: bool = False) -> list[str]:
    home = run_dir / "home"
    pluginspath = f"{_fwd(PLUGIN_DIR)};{_fwd(mame_exe.parent / 'plugins')}"
    plugins = "mamemcp"
    if fixture:
        pluginspath = f"{_fwd(FIXTURE_PLUGIN_DIR)};{pluginspath}"
        plugins = "mamemcp,mamemcp_fixture"
    cmd = [
        str(mame_exe), system,
        "-rompath", _fwd(rompath),
        "-pluginspath", pluginspath,
        "-plugin", plugins,
        "-homepath", _fwd(home), "-inipath", _fwd(home),
        "-cfg_directory", _fwd(home / "cfg"), "-nvram_directory", _fwd(home / "nvram"),
        "-snapshot_directory", _fwd(home / "snap"), "-state_directory", _fwd(home / "sta"),
        "-diff_directory", _fwd(home / "diff"), "-comment_directory", _fwd(home / "comments"),
        "-window", "-video", "none", "-sound", "none", "-skip_gameinfo",
    ]
    if not throttle:
        cmd.append("-nothrottle")
    if debug:
        cmd.append("-debug")
    if debugscript:
        cmd += ["-debugscript", _fwd(debugscript)]
    if debuglog:
        cmd.append("-debuglog")
    cmd += extra or []
    return cmd


def launch(*, system: str = "shinobi", port: int = 9411, run_dir: Path | None = None,
           mame_dir: Path = DEFAULT_MAME_DIR, rompath: Path | None = None, debug: bool = True,
           debugscript: Path | None = None, debuglog: bool = False, throttle: bool = True,
           extra: list[str] | None = None, write_launch_info: bool = True, fixture: bool = False,
           test_env: dict[str, str] | None = None) -> tuple[subprocess.Popen, dict[str, Any]]:
    mame_exe = mame_dir / "mame.exe"
    rompath = rompath or (mame_dir / "roms")
    run_dir = (run_dir or PROJECT_ROOT / "runs" / time.strftime("run-%Y%m%d-%H%M%S")).resolve()
    (run_dir / "home").mkdir(parents=True, exist_ok=True)
    evidence = run_dir / "evidence"
    evidence.mkdir(exist_ok=True)

    zip_path = rompath / f"{system}.zip"
    info: dict[str, Any] = {
        "launched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mame_exe": str(mame_exe),
        "mame_version": mame_version(mame_exe),
        "system": system,
        "rompath": str(rompath),
        "romset_zip": {"path": str(zip_path), "exists": zip_path.exists(),
                       "sha1": hashlib.sha1(zip_path.read_bytes()).hexdigest() if zip_path.exists() else None},
        "verifyroms": verify_roms(mame_exe, system, rompath),
        "expected_roms_from_listxml": listxml_roms(mame_exe, system),
        "note": "ROM hashes are the -listxml expected values; -verifyroms confirms the zip matches them.",
        "port": port,
        "run_dir": str(run_dir),
        "evidence_dir": str(evidence),
    }
    cmd = build_command(mame_exe=mame_exe, system=system, rompath=rompath, run_dir=run_dir, port=port,
                        debug=debug, debugscript=debugscript, debuglog=debuglog, throttle=throttle, extra=extra,
                        fixture=fixture)
    info["command"] = cmd
    if write_launch_info:
        (run_dir / "launch.json").write_text(json.dumps(info, indent=2))
    # TEST-ONLY fault-injection hooks (MAMEMCP_TEST_*) are never inherited from the ambient environment: they exist in the
    # MAME process only when a test passes them explicitly through `test_env`, and the launch record says so.
    env = {k: v for k, v in os.environ.items() if not k.startswith("MAMEMCP_TEST_")}
    env.update(MAMEMCP_PORT=str(port), MAMEMCP_EVIDENCE_DIR=_fwd(evidence))
    for k, v in (test_env or {}).items():
        if not k.startswith("MAMEMCP_TEST_"):
            raise ValueError(f"test_env keys must start with MAMEMCP_TEST_: {k}")
        env[k] = str(v)
    info["test_env"] = dict(test_env or {})
    if fixture:
        fx_dir = run_dir / "fixture"
        fx_dir.mkdir(exist_ok=True)
        env["MAMEMCP_FIXTURE_DIR"] = _fwd(fx_dir)
        info["fixture_dir"] = str(fx_dir)
    log = open(run_dir / "mame_stdout.txt", "w")
    # cwd = run_dir/home so any stray relative-path output (e.g. -debuglog debug.log) stays isolated.
    proc = subprocess.Popen(cmd, cwd=run_dir / "home", env=env, stdout=log, stderr=subprocess.STDOUT)
    info["pid"] = proc.pid
    try:  # (pid, creation time) identifies this process instance; a recycled pid will not match (see winwake)
        from . import winwake
        info["pid_created_filetime"] = winwake.creation_time(proc.pid)
    except Exception:  # never let identity bookkeeping break a launch
        info["pid_created_filetime"] = None
    if write_launch_info:
        (run_dir / "launch.json").write_text(json.dumps(info, indent=2))  # now including the pid
    return proc, info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Launch an isolated MAME instance with the mamemcp plugin")
    ap.add_argument("--system", default="shinobi")
    ap.add_argument("--port", type=int, default=9411)
    ap.add_argument("--run-dir", type=Path)
    ap.add_argument("--mame-dir", type=Path, default=DEFAULT_MAME_DIR)
    ap.add_argument("--no-debug", action="store_true", help="start without -debug (breakpoints unavailable)")
    ap.add_argument("--debugscript", type=Path)
    ap.add_argument("--debuglog", action="store_true")
    ap.add_argument("--no-throttle", action="store_true")
    a = ap.parse_args(argv)
    proc, info = launch(system=a.system, port=a.port, run_dir=a.run_dir, mame_dir=a.mame_dir,
                        debug=not a.no_debug, debugscript=a.debugscript, debuglog=a.debuglog,
                        throttle=not a.no_throttle)
    print(json.dumps({k: info[k] for k in ("mame_version", "pid", "port", "run_dir", "evidence_dir")}, indent=2))
    print("MAME started; Ctrl+C here stops it.", file=sys.stderr)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
