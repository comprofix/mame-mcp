"""The plugin's directory-path logic (path_prefixes / mkdir_p in plugin/mamemcp/init.lua), run in MAME's own Lua engine.

There is no standalone Lua here, so the marked pure block is cut out of the plugin and run through -autoboot_script on
the Shinobi driver (needs shinobi.zip in MAMEMCP_ROMPATH, default ~/mame/roms). Skipped when no MAME executable is configured (see launcher.default_mame_exe)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from mame_mcp import launcher

pytestmark = pytest.mark.integration

CASES = {
    "linux_absolute": "/tmp/runs/s1/evidence/screenshots",
    "relative": "runs/s1/evidence",
    "windows_drive": "D:/arcade/mame-mcp/runs/s1/evidence",
    "windows_backslash": "D:\\arcade\\runs\\evidence",
    "windows_drive_relative": "C:runs/evidence",
    "windows_root_of_drive": "/runs/evidence",
    "unc": "//server/share/runs/evidence",
    "trailing_and_double_separators": "/tmp//runs/evidence/",
}
EXPECTED = {
    "linux_absolute": ["/tmp", "/tmp/runs", "/tmp/runs/s1", "/tmp/runs/s1/evidence", "/tmp/runs/s1/evidence/screenshots"],
    "relative": ["runs", "runs/s1", "runs/s1/evidence"],
    "windows_drive": ["D:/arcade", "D:/arcade/mame-mcp", "D:/arcade/mame-mcp/runs", "D:/arcade/mame-mcp/runs/s1",
                      "D:/arcade/mame-mcp/runs/s1/evidence"],
    "windows_backslash": ["D:/arcade", "D:/arcade/runs", "D:/arcade/runs/evidence"],
    "windows_drive_relative": ["C:runs", "C:runs/evidence"],
    "windows_root_of_drive": ["/runs", "/runs/evidence"],
    "unc": ["//server/share/runs", "//server/share/runs/evidence"],
    "trailing_and_double_separators": ["/tmp", "/tmp/runs", "/tmp/runs/evidence"],
}


def _plugin_block(name: str) -> str:
    src = (launcher.PLUGIN_DIR / "mamemcp" / "init.lua").read_text()
    start, end = src.index(f"-- BEGIN {name}"), src.index(f"-- END {name}")
    return src[start:end]


def _lua_str(s: str) -> str:
    return json.dumps(s)  # a JSON string literal is a valid Lua string literal for these inputs


@pytest.fixture(scope="module")
def lua_results(tmp_path_factory):
    exe = launcher.default_mame_exe()
    if not exe.is_file():
        pytest.skip(f"no MAME executable at {exe}")
    rompath = os.environ.get("MAMEMCP_ROMPATH") or str(Path.home() / "mame" / "roms")
    if not any((Path(d) / "shinobi.zip").is_file() for d in rompath.split(os.pathsep)):
        pytest.skip(f"shinobi.zip not found in ROM path {rompath} (set MAMEMCP_ROMPATH)")
    tmp = tmp_path_factory.mktemp("plugin_paths")
    absolute = (tmp / "abs" / "a" / "b").as_posix()
    lines = [
        "local lfs = require('lfs')",
        _plugin_block("path_prefixes"),
        "local function mkdir_p(path) for _, dir in ipairs(path_prefixes(path)) do lfs.mkdir(dir) end "
        "return lfs.attributes(path, 'mode') == 'directory' end",
        "local out = {}",
    ]
    for key, path in CASES.items():
        lines.append(f"out[#out + 1] = 'PFX ' .. {_lua_str(key)} .. '=' .. table.concat(path_prefixes({_lua_str(path)}), '|')")
    lines += [
        # real directory creation: an absolute Linux path, and a relative one taken against MAME's working directory
        f"out[#out + 1] = 'MKDIR absolute=' .. tostring(mkdir_p({_lua_str(absolute)}))",
        "out[#out + 1] = 'MKDIR relative=' .. tostring(mkdir_p('rel/x/y'))",
        "for _, l in ipairs(out) do print('MAMEMCP_TEST ' .. l) end",
        "manager.machine:exit()",
    ]
    script = tmp / "paths.lua"
    script.write_text("\n".join(lines) + "\n")
    cwd = tmp / "cwd"
    cwd.mkdir()
    home = tmp / "home"
    cmd = [str(exe), "shinobi", "-rompath", rompath, "-noreadconfig", "-autoboot_script", str(script), "-video", "none", "-sound", "none",
           "-nothrottle", "-skip_gameinfo", "-homepath", str(home), "-cfg_directory", str(home / "cfg"),
           "-nvram_directory", str(home / "nvram"), "-seconds_to_run", "10"]
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    got = {}
    for line in r.stdout.splitlines():
        if line.startswith("MAMEMCP_TEST "):
            kind, _, rest = line[len("MAMEMCP_TEST "):].partition(" ")
            key, _, value = rest.partition("=")
            got[(kind, key)] = value
    assert got, f"MAME produced no results (rc {r.returncode}): {r.stdout[-800:]} {r.stderr[-800:]}"
    return got, tmp, cwd


@pytest.mark.parametrize("key", list(CASES))
def test_path_prefixes_keep_roots_and_drives(lua_results, key):
    got, _, _ = lua_results
    assert got[("PFX", key)].split("|") == EXPECTED[key]


def test_mkdir_p_creates_absolute_paths_at_the_root_not_under_the_working_directory(lua_results):
    got, tmp, cwd = lua_results
    assert got[("MKDIR", "absolute")] == "true" and (tmp / "abs" / "a" / "b").is_dir()
    if tmp.anchor == "/":  # the Linux defect: the absolute path recreated relative to the working directory
        assert not (cwd / tmp.relative_to("/")).exists()


def test_mkdir_p_creates_relative_paths_under_the_working_directory(lua_results):
    got, _, cwd = lua_results
    assert got[("MKDIR", "relative")] == "true" and (cwd / "rel" / "x" / "y").is_dir()
