"""Launch a *separate, isolated* MAME instance with the mamemcp plugin.

Nothing here modifies the MAME installation: the plugin is loaded through -pluginspath/-plugin and all
writable state (home, ini, cfg, nvram, snapshots, evidence) lives in a run directory inside this project.
The ROM path is only read.

The executable and MAME's own resource folders are separate: a Windows MAME keeps `plugins` next to mame.exe, while a
packaged Linux MAME runs from e.g. /usr/games/mame with its plugins under /usr/share/games/mame. When MAME's plugin
folder (the one holding boot.lua and the json library) is not configured explicitly, it is taken from next to the
executable if present there, otherwise from MAME's own configuration (-showconfig pluginspath).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
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
MAME_EXE_NAME = "mame.exe" if sys.platform == "win32" else "mame"
_SHOWCONFIG: dict[str, dict[str, list[Path]]] = {}


def _fwd(p: Path | str) -> str:
    return str(p).replace("\\", "/")


def default_mame_exe() -> Path:
    """Windows: mame.exe in DEFAULT_MAME_DIR (unchanged behaviour). Elsewhere: `mame` found on PATH, resolved; if there
    is none, the bare name (which then fails the existence check with a clear error)."""
    if sys.platform == "win32":
        return DEFAULT_MAME_DIR / MAME_EXE_NAME
    found = shutil.which(MAME_EXE_NAME)
    return Path(found).resolve() if found else Path(MAME_EXE_NAME)


def resolve_mame_exe(value: str) -> Path:
    """A configured executable: a path, or a bare command name looked up on PATH."""
    if os.sep in value or (os.altsep and os.altsep in value):
        return Path(value)
    found = shutil.which(value)
    return Path(found).resolve() if found else Path(value)


class RuntimeIdentityError(Exception):
    """The executable MAME will actually run as could not be established from a trustworthy source."""


def _is_script(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"#!"
    except OSError:
        return False


def _is_elf(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def _root_protected(path: Path) -> bool:
    st = path.stat()
    return st.st_uid == 0 and not (st.st_mode & 0o022)


def _dpkg_packages(*paths: Path) -> set[str] | None:
    """The installed packages that own each of `paths` (`dpkg-query -S`, no shell), or None when any is unowned/unavailable."""
    owners: set[str] = set()
    for p in paths:
        try:
            r = subprocess.run(["dpkg-query", "-S", "--", str(p)], capture_output=True, text=True, timeout=15,
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0:
            return None
        for line in r.stdout.splitlines():
            pkgs, sep, path = line.partition(": ")
            if sep and path.strip() == str(p):
                owners.update(x.strip().split(":")[0] for x in pkgs.split(","))
    return owners


def runtime_executable(mame_exe: Path, configured: Path | None = None) -> Path:
    """The executable the launched process will really be running as, established BEFORE launch, never read back from
    the process that appeared. The launch command and the runtime image differ when the launch command is a wrapper script
    (Debian's /usr/games/mame runs `exec /usr/lib/mame/mame "$@"`). The wrapper is not parsed or interpreted here; the runtime is
      * the launch command itself, resolved through symlinks, when it is not a `#!` script (Windows always);
      * otherwise `configured` (MAMEMCP_MAME_RUNTIME), which must be an existing ELF executable;
      * otherwise, on Linux only, Debian's packaging layout /usr/lib/<name>/<name>, accepted only when it is an ELF file,
        owned by root and not group/world-writable, and dpkg reports the SAME installed package as owner of both it and the
        (equally root-protected) wrapper.
    Anything else raises RuntimeIdentityError."""
    real = Path(os.path.realpath(mame_exe))
    if sys.platform == "win32" or not _is_script(real):
        if configured is not None and Path(os.path.realpath(configured)) != real and sys.platform != "win32":
            raise RuntimeIdentityError(f"MAMEMCP_MAME_RUNTIME {configured} does not match the launch executable {real}, which is not a wrapper script")
        return real
    if configured is not None:
        rt = Path(os.path.realpath(configured))
        if not rt.is_absolute() or not rt.is_file() or not _is_elf(rt):
            raise RuntimeIdentityError(f"MAMEMCP_MAME_RUNTIME {configured} is not an existing ELF executable")
        return rt
    if sys.platform.startswith("linux"):
        cand = Path("/usr/lib") / real.name / real.name
        if cand.is_file() and _is_elf(cand) and _root_protected(cand) and _root_protected(real):
            pkgs, cpkgs = _dpkg_packages(real), _dpkg_packages(cand)
            if pkgs and pkgs == cpkgs:
                return Path(os.path.realpath(cand))
    raise RuntimeIdentityError(
        f"{mame_exe} is a wrapper script and the executable it runs could not be established from a trusted source; set "
        "MAMEMCP_MAME_RUNTIME to the real MAME executable (e.g. /usr/lib/mame/mame)")


def mame_config_paths(mame_exe: Path) -> dict[str, list[Path]]:
    """The `pluginspath` and `rompath` folders of MAME's own configuration (`-showconfig`: built-in defaults plus the
    system and user mame.ini MAME reads), with $VARIABLES and ~ expanded and relative entries taken against the
    executable's folder. Read once per executable; any failure yields empty lists."""
    key = str(mame_exe)
    if key not in _SHOWCONFIG:
        out: dict[str, list[Path]] = {"pluginspath": [], "rompath": []}
        try:
            r = subprocess.run([str(mame_exe), "-showconfig"], capture_output=True, text=True, timeout=30,
                               cwd=mame_exe.parent, stdin=subprocess.DEVNULL)
            for line in r.stdout.splitlines():
                name, _, value = line.strip().partition(" ")
                if name in out:
                    for entry in value.strip().strip('"').split(";"):
                        if entry.strip():
                            p = Path(os.path.expanduser(os.path.expandvars(entry.strip())))
                            out[name].append(p if p.is_absolute() else mame_exe.parent / p)
        except (OSError, subprocess.SubprocessError):
            pass
        _SHOWCONFIG[key] = out
    return _SHOWCONFIG[key]


def mame_plugins_dir(mame_exe: Path) -> Path:
    """MAME's own plugin folder (holds boot.lua and the json library the bridge plugin needs)."""
    local = mame_exe.parent / "plugins"
    if (local / "boot.lua").is_file() or not mame_exe.is_file():
        return local
    for p in mame_config_paths(mame_exe)["pluginspath"]:
        if (p / "boot.lua").is_file():
            return p
    return local


def default_rompaths(mame_exe: Path) -> list[Path]:
    """`roms` next to the executable when that folder exists (the Windows layout), otherwise MAME's configured rompath."""
    local = mame_exe.parent / "roms"
    if local.is_dir() or not mame_exe.is_file():
        return [local]
    return mame_config_paths(mame_exe)["rompath"] or [local]


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
                  throttle: bool = True, extra: list[str] | None = None, fixture: bool = False,
                  plugins_dir: Path | None = None) -> list[str]:
    home = run_dir / "home"
    pluginspath = f"{_fwd(PLUGIN_DIR)};{_fwd(plugins_dir or mame_plugins_dir(mame_exe))}"
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


def launch(*, system: str, port: int = 9411, run_dir: Path | None = None,
           mame_dir: Path | None = None, rompath: Path | None = None, debug: bool = True,
           debugscript: Path | None = None, debuglog: bool = False, throttle: bool = True,
           extra: list[str] | None = None, write_launch_info: bool = True, fixture: bool = False,
           test_env: dict[str, str] | None = None, mame_exe: Path | None = None,
           plugins_dir: Path | None = None) -> tuple[subprocess.Popen, dict[str, Any]]:
    """`mame_exe` names the executable; without it, `mame_dir` is the folder holding it (the Windows layout), and
    without either, default_mame_exe() is used."""
    if mame_exe is None:
        mame_exe = mame_dir / MAME_EXE_NAME if mame_dir is not None else default_mame_exe()
    plugins_dir = plugins_dir or mame_plugins_dir(mame_exe)
    rompath = rompath or default_rompaths(mame_exe)[0]
    run_dir = (run_dir or PROJECT_ROOT / "runs" / time.strftime("run-%Y%m%d-%H%M%S")).resolve()
    (run_dir / "home").mkdir(parents=True, exist_ok=True)
    evidence = run_dir / "evidence"
    evidence.mkdir(exist_ok=True)

    zip_path = rompath / f"{system}.zip"
    info: dict[str, Any] = {
        "launched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mame_exe": str(mame_exe),
        "mame_plugins": str(plugins_dir),
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
                        fixture=fixture, plugins_dir=plugins_dir)
    info["command"] = cmd
    if write_launch_info:
        (run_dir / "launch.json").write_text(json.dumps(info, indent=2))
    # TEST-ONLY fault-injection hooks (MAMEMCP_TEST_*) are never inherited from the ambient environment: they exist in the
    # MAME process only when a test passes them explicitly through `test_env`, and the launch record says so.
    env = {k: v for k, v in os.environ.items() if not k.startswith("MAMEMCP_TEST_") and k not in ("MAMEMCP_FIXTURE_DIR", "MAMEMCP_STATE_DIR")}
    # MAMEMCP_STATE_DIR mirrors -state_directory (build_command) so the plugin can find the checkpoint files it asks MAME to write
    env.update(MAMEMCP_PORT=str(port), MAMEMCP_EVIDENCE_DIR=_fwd(evidence), MAMEMCP_STATE_DIR=_fwd(run_dir / "home" / "sta"))
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
    # cwd = run_dir/home so any stray relative-path output (e.g. -debuglog debug.log) stays isolated.
    # stdin is detached: when started by an MCP server, the inherited stdin would be the MCP protocol pipe.
    # POSIX: restore_signals=False keeps the SIG_IGN that CPython sets at startup for SIGPIPE and SIGXFSZ, instead of
    # resetting both to SIG_DFL in the child. The plugin detects a vanished bridge client by a failing heartbeat write; with
    # SIGPIPE at its default that write kills MAME instead, unless the MAME build happens to ignore SIGPIPE itself (Debian's
    # 0.289 does, other builds do not). SIGXFSZ ignored means a write beyond RLIMIT_FSIZE fails with EFBIG rather than
    # killing MAME. Signals with handlers in this process are reset by exec either way. Not passed on Windows (no effect there).
    posix_kw: dict[str, Any] = {} if sys.platform == "win32" else {"restore_signals": False}
    with open(run_dir / "mame_stdout.txt", "w") as log:  # the child keeps its own inherited handle
        proc = subprocess.Popen(cmd, cwd=run_dir / "home", env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, **posix_kw)
    info["pid"] = proc.pid
    # (pid, creation time) identifies this process instance; a recycled pid will not match (see procinfo). The key keeps its
    # Windows name: on Linux the value is the process start time in clock ticks since boot.
    try:
        from . import procinfo
        info["pid_created_filetime"] = procinfo.creation_time(proc.pid)
    except Exception:  # never let identity bookkeeping break a launch
        info["pid_created_filetime"] = None
    if write_launch_info:
        (run_dir / "launch.json").write_text(json.dumps(info, indent=2))  # now including the pid
    return proc, info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Launch an isolated MAME instance with the mamemcp plugin")
    ap.add_argument("--system", required=True, help="MAME short name of the machine to launch (no default machine)")
    ap.add_argument("--port", type=int, default=9411)
    ap.add_argument("--run-dir", type=Path)
    ap.add_argument("--mame-dir", type=Path, help="folder holding the MAME executable (default: see default_mame_exe)")
    ap.add_argument("--mame-exe", type=Path, help="MAME executable (overrides --mame-dir)")
    ap.add_argument("--no-debug", action="store_true", help="start without -debug (breakpoints unavailable)")
    ap.add_argument("--debugscript", type=Path)
    ap.add_argument("--debuglog", action="store_true")
    ap.add_argument("--no-throttle", action="store_true")
    a = ap.parse_args(argv)
    proc, info = launch(system=a.system, port=a.port, run_dir=a.run_dir, mame_dir=a.mame_dir, mame_exe=a.mame_exe,
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
