"""Unit tests for the config validation of examples/bank_capture.py (a CLIENT example, not a server tool). Nothing here starts MAME;
the run against real machines is scripts/mcp_bank_capture_e2e.py."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
import bank_capture  # noqa: E402

GOOD = {
    "system": "1942", "cpu": ":maincpu", "registers": ["PC", "A"], "warmup_seconds": 1,
    "limits": {"max_events": 6, "wall_seconds": 10, "total_wall_seconds": 120},
    "select": {"space": "program", "address": "0xC806", "length": 1, "access": "write"},
    "sites": [{"address": "0x11", "label": "handler"}, {"address": 0x2BA2}],
    "sample": {"address": "0x8000", "size": 1}, "signatures": {"0x84": 0, "60": "entry-1"},
    "inputs": [{"port": ":SYSTEM", "field": "Coin 1", "start": 0, "frames": 6}],
    "disassemble": {"count": 4}, "bank_context": True,
}


def cfg(**changes):
    c = copy.deepcopy(GOOD)
    for k, v in changes.items():
        if v is None:
            c.pop(k, None)
        else:
            c[k] = v
    return c


def test_a_complete_config_is_normalised():
    out = bank_capture.validate_config(GOOD)
    assert out["select"]["address"] == 0xC806 and [s["address"] for s in out["sites"]] == [0x11, 0x2BA2] and out["signatures"] == {0x84: 0, 0x60: "entry-1"}
    assert out["limits"] == {"max_events": 6, "wall_seconds": 10, "total_wall_seconds": 120}


@pytest.mark.parametrize("key", ["system", "cpu", "registers", "warmup_seconds", "limits"])
def test_nothing_is_defaulted(key):
    with pytest.raises(bank_capture.ConfigError, match="no defaults"):
        bank_capture.validate_config(cfg(**{key: None}))


@pytest.mark.parametrize("changes", [
    {"system": "1942 ; rm"}, {"system": 5}, {"cpu": "maincpu"}, {"cpu": ":a b"}, {"registers": ["A"] * 2}, {"registers": list("ABCDEFG")}, {"registers": "A"}, {"registers": ["1x"]},
    {"warmup_seconds": -1}, {"warmup_seconds": 31}, {"warmup_seconds": True},
    {"limits": {"max_events": 6, "wall_seconds": 10}}, {"limits": {"max_events": 0, "wall_seconds": 10, "total_wall_seconds": 120}}, {"limits": {"max_events": 10001, "wall_seconds": 10, "total_wall_seconds": 120}},
    {"limits": {"max_events": 6, "wall_seconds": 121, "total_wall_seconds": 120}}, {"limits": {"max_events": 6, "wall_seconds": 10, "total_wall_seconds": 5}}, {"limits": {"max_events": 6, "wall_seconds": 10, "total_wall_seconds": 1801}},
    {"select": {"space": "opcodes", "address": 0, "length": 1, "access": "write"}}, {"select": {"space": "program", "address": 0, "length": 0, "access": "write"}},
    {"select": {"space": "program", "address": 0, "length": 1, "access": "exec"}}, {"select": {"space": "program", "address": -1, "length": 1, "access": "write"}}, {"select": {"space": "program", "address": 0, "length": 1}},
    {"sites": []}, {"sites": [{"address": 1}] * 2}, {"sites": [{"address": i} for i in range(9)]}, {"sites": [{"label": "x"}]}, {"sites": [{"address": 1, "extra": 2}]},
    {"sample": {"address": 0, "size": 3}}, {"sample": {"address": 0}}, {"signatures": {"0x84": 0, "132": 1}}, {"signatures": {}}, {"signatures": {"0x100": 0}}, {"signatures": {"zz": 0}},
    {"inputs": []}, {"inputs": [{"port": ":P1", "field": "A"}]}, {"disassemble": {"count": 0}}, {"disassemble": {"count": 17}}, {"disassemble": {}}, {"bank_context": "yes"},
    {"unknown_key": 1},
])
def test_invalid_configs_are_refused(changes):
    with pytest.raises(bank_capture.ConfigError):
        bank_capture.validate_config(cfg(**changes))


def test_dependencies_between_options_are_enforced():
    with pytest.raises(bank_capture.ConfigError, match="at least one"):
        bank_capture.validate_config(cfg(select=None, sites=None, sample=None, signatures=None, disassemble=None, bank_context=None))
    with pytest.raises(bank_capture.ConfigError, match="signatures need a sample"):
        bank_capture.validate_config(cfg(sample=None, bank_context=None))
    with pytest.raises(bank_capture.ConfigError, match="disassemble needs"):
        bank_capture.validate_config(cfg(select=None))
    with pytest.raises(bank_capture.ConfigError, match="sample is only used"):
        bank_capture.validate_config(cfg(sites=None, select=GOOD["select"], signatures=None, bank_context=None, disassemble=None))
    with pytest.raises(bank_capture.ConfigError, match="bank_context"):
        bank_capture.validate_config(cfg(sample=None, signatures=None))


def test_a_select_only_and_a_sites_only_config_are_both_valid():
    sel = {"system": "x1", "cpu": ":maincpu", "registers": [], "warmup_seconds": 0, "limits": GOOD["limits"], "select": GOOD["select"]}
    assert bank_capture.validate_config(sel)["select"]["access"] == "write"
    sites = {"system": "x1", "cpu": ":maincpu", "registers": [], "warmup_seconds": 0, "limits": GOOD["limits"], "sites": [{"address": 0x100}]}
    assert "select" not in bank_capture.validate_config(sites)


def test_cli_refuses_a_bad_config_without_starting_anything(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"system": "1942"}))
    r = subprocess.run([sys.executable, "-I", str(ROOT / "examples" / "bank_capture.py"), "--config", str(bad), "--out", str(tmp_path / "o")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "missing required key" in r.stderr and not (tmp_path / "o").exists()
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps(GOOD))
    r = subprocess.run([sys.executable, "-I", str(ROOT / "examples" / "bank_capture.py"), "--config", str(ok), "--check"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and json.loads(r.stdout)["config"] == "valid"
    r = subprocess.run([sys.executable, "-I", str(ROOT / "examples" / "bank_capture.py"), "--config", str(ok)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "--out is required" in r.stderr


def test_the_script_has_no_machine_defaults_and_no_lua_or_debugger_access():
    src = (ROOT / "examples" / "bank_capture.py").read_text()
    body = src.split('"""', 2)[2]  # code after the module docstring
    for needle in ("1942", "1944", "0xC806", "0xD003", "-autoboot_script", "debugger", ".lua", 'cfg.get("cpu"', 'cfg.get("system"', 'cfg.get("select"'):
        assert needle not in body, needle
