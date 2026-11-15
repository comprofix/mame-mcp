"""Unit tests: client-side argument validation. (Integration evidence is in scripts/validate.py.)"""
import pytest

from mame_mcp import protocol as p
from mame_mcp.protocol import BridgeError


def code(fn, *a, **k):
    with pytest.raises(BridgeError) as e:
        fn(*a, **k)
    return e.value.code


def test_read_memory_ok_and_hex_string():
    a = p.read_memory_args(":maincpu", "program", "0x404", 16)
    assert a == {"cpu": ":maincpu", "space": "program", "mode": "inspect", "address": 0x404, "length": 16}


@pytest.mark.parametrize("length", [0, 4097, -1, "x", 1.5, True])
def test_read_memory_length_rejected(length):
    assert code(p.read_memory_args, ":maincpu", "program", 0, length) == "bad_argument"


@pytest.mark.parametrize("cpu", ["maincpu", "", ":main cpu", None, ":a;b"])
def test_cpu_tag_rejected(cpu):
    assert code(p.read_memory_args, cpu, "program", 0, 1) == "bad_argument"


def test_registers_names_must_be_identifiers():
    assert p.read_registers_args(":maincpu", ["D0", "SR"])["registers"] == ["D0", "SR"]
    assert code(p.read_registers_args, ":maincpu", ["D0;g"]) == "bad_argument"
    assert code(p.read_registers_args, ":maincpu", ["x"] * 65) == "bad_argument"


def test_capture_requires_both_limits_in_range():
    assert code(p.capture_args, ":maincpu", 0x404, 0, 5) == "bad_argument"
    assert code(p.capture_args, ":maincpu", 0x404, p.MAX_EVENTS + 1, 5) == "bad_argument"
    assert code(p.capture_args, ":maincpu", 0x404, 5, 0) == "bad_argument"
    assert code(p.capture_args, ":maincpu", 0x404, 5, p.MAX_WALL_SECONDS + 1) == "bad_argument"
    assert code(p.capture_args, ":maincpu", 0x404, 5, True) == "bad_argument"
    ok = p.capture_args(":maincpu", "0x404", 5, 2.5, ["D0"], [{"address": 0x404, "size": 2}], "x")
    assert ok["address"] == 0x404 and ok["memory"] == [{"space": "program", "address": 0x404, "size": 2}]


def test_capture_memory_field_validation():
    assert code(p.capture_args, ":maincpu", 0, 1, 1, None, [{"address": 0, "size": 3}]) == "bad_argument"
    assert code(p.capture_args, ":maincpu", 0, 1, 1, None, [{"address": 0, "size": 1}] * 9) == "bad_argument"


def test_read_mode_validated():
    assert p.read_memory_args(":soundcpu", "io", 0, 1, "direct")["mode"] == "direct"
    assert code(p.read_memory_args, ":maincpu", "program", 0, 1, "normal") == "bad_argument"


def test_capture_register_and_space_limits():
    assert code(p.capture_args, ":soundcpu", 0, 1, 1, ["A"] * (p.MAX_CAPTURE_REGISTERS + 1)) == "bad_argument"
    assert code(p.capture_args, ":soundcpu", 0, 1, 1, None, [{"address": 0, "size": 1, "space": "opcodes"}]) == "bad_argument"
    ok = p.capture_args(":soundcpu", 0, 1, 1, ["A"], [{"address": 0, "size": 1, "space": "io"}])
    assert ok["memory"][0]["space"] == "io"


def test_read_memory_bank_context_is_opt_in_and_boolean():
    base = p.read_memory_args(":maincpu", "program", 0x8000, 4)
    assert "bank_context" not in base
    assert p.read_memory_args(":maincpu", "program", 0x8000, 4, bank_context=True)["bank_context"] is True
    with pytest.raises(BridgeError):
        p.read_memory_args(":maincpu", "program", 0x8000, 4, bank_context="yes")


# ---- multi-location capture arguments
def _cap(**k):
    base = dict(cpu=":maincpu", address=None, max_events=5, max_wall_seconds=5)
    base.update(k)
    return p.capture_args(**base)


def test_locations_ok_default_ids_and_no_address_key():
    a = _cap(locations=[{"address": "0x11"}, {"address": 8, "id": "irq"}])
    assert a["locations"] == [{"id": "L1", "address": 0x11}, {"id": "irq", "address": 8}] and "address" not in a


def test_single_address_form_unchanged():
    a = _cap(address=0x404)
    assert a["address"] == 0x404 and "locations" not in a


@pytest.mark.parametrize("kw", [
    {},                                                                    # neither
    {"address": 1, "locations": [{"address": 2}]},                         # both
    {"locations": []},
    {"locations": [{"address": a} for a in range(p.MAX_LOCATIONS + 1)]},   # over the maximum
    {"locations": [{"address": 1, "id": "a"}, {"address": 2, "id": "a"}]},  # duplicate id
    {"locations": [{"address": 1}, {"address": 1, "id": "b"}]},            # duplicate address
    {"locations": [{"address": 1, "id": "bad id"}]},
    {"locations": [{"address": 1, "id": "1x"}]},
    {"locations": [{"address": 1, "extra": 1}]},
    {"locations": [{"id": "a"}]},
    {"locations": "nope"},
])
def test_locations_rejected(kw):
    assert code(_cap, **kw) == "bad_argument"


def test_locations_max_accepted():
    assert len(_cap(locations=[{"address": a} for a in range(p.MAX_LOCATIONS)])["locations"]) == p.MAX_LOCATIONS
