"""Unit tests for capture_watchpoint, disassemble_memory and press_sequence: argument validation (protocol.py), the Python-side
sequence orchestration (operations.py, fake bridge) and the advertised MCP tool surface (server.py).

They prove the Python-side decisions only. The plugin's own checks and the behaviour of real MAME 0.289 are exercised by
scripts/validate_watchpoint.py, validate_disasm.py, validate_sequence.py and scripts/mcp_wds_e2e.py.
"""
import asyncio
import inspect

import pytest

from mame_mcp import protocol, server
from mame_mcp.protocol import BridgeError
from test_operations import PRESS, rig  # noqa: F401  (the fake-bridge rig fixture is re-used)

P1 = ":P1"


def step(field, start, frames, port=P1):
    return {"port": port, "field": field, "start": start, "frames": frames}


# ------------------------------------------------------------------------------------------------ watchpoint_args
WATCH = dict(cpu=":soundcpu", space="program", address=0xF800, length=0x800, access="write", max_events=100, max_wall_seconds=10)


def test_watchpoint_args_normalise_and_require_every_field():
    out = protocol.watchpoint_args(**WATCH, registers=["PC", "SP"], label="x")
    assert out == {"cpu": ":soundcpu", "space": "program", "address": 0xF800, "length": 0x800, "access": "write", "max_events": 100,
                   "max_wall_seconds": 10, "registers": ["PC", "SP"], "label": "x"}
    assert protocol.watchpoint_args(**{**WATCH, "address": "0xF800"})["address"] == 0xF800
    assert protocol.watchpoint_args(**{**WATCH, "space": "io", "access": "read_write", "address": 0, "length": 0x100000000 - 0})["length"] == 0x100000000
    for key in WATCH:  # no defaults: a missing explicit field is a TypeError at the call, never a silent default
        with pytest.raises(TypeError):
            protocol.watchpoint_args(**{k: v for k, v in WATCH.items() if k != key})


@pytest.mark.parametrize("kw", [
    {"space": "opcodes"}, {"space": "cpu_space"}, {"space": ""}, {"space": None}, {"access": "rw"}, {"access": "exec"}, {"access": None},
    {"length": 0}, {"length": -1}, {"length": True}, {"length": 1.5}, {"address": -1}, {"address": 0x100000000}, {"address": "zz"}, {"address": True},
    {"address": 0xFFFFFFFF, "length": 2}, {"max_events": 0}, {"max_events": 10001}, {"max_events": True}, {"max_events": 1.5},
    {"max_wall_seconds": 0}, {"max_wall_seconds": 601}, {"max_wall_seconds": True}, {"max_wall_seconds": "5"}, {"cpu": "soundcpu"}, {"cpu": ":a b"},
    {"registers": ["A"] * 2}, {"registers": list("ABCDEFG")}, {"registers": ["A B"]}, {"registers": [1]},
])
def test_watchpoint_args_reject_bad_arguments(kw):
    with pytest.raises(BridgeError) as e:
        protocol.watchpoint_args(**{**WATCH, **kw})
    assert e.value.code == "bad_argument"


def test_watchpoint_args_carry_no_expression_or_command_field():
    assert set(inspect.signature(protocol.watchpoint_args).parameters) == {"cpu", "space", "address", "length", "access", "max_events", "max_wall_seconds", "registers", "label"}
    out = protocol.watchpoint_args(**WATCH, label="a" * 500)
    assert len(out["label"]) <= protocol.MAX_LABEL


# ------------------------------------------------------------------------------------------------ disassemble_args
def test_disassemble_args_are_explicit_and_bounded():
    assert protocol.disassemble_args(":maincpu", "0x404", 3) == {"cpu": ":maincpu", "address": 0x404, "count": 3}
    assert protocol.disassemble_args(":maincpu", 0, protocol.MAX_DISASM_INSTRUCTIONS)["count"] == 64
    for args in [(":maincpu", 0, 0), (":maincpu", 0, 65), (":maincpu", -1, 1), (":maincpu", 0x100000000, 1), (":maincpu", 0, True), (":maincpu", 0, 1.5), (":maincpu", "x", 1), ("maincpu", 0, 1), (":a b", 0, 1)]:
        with pytest.raises(BridgeError) as e:
            protocol.disassemble_args(*args)
        assert e.value.code == "bad_argument", args


# ------------------------------------------------------------------------------------------------ sequence_args
def test_sequence_args_accept_a_chord_a_gap_and_a_boundary_between_fields():
    steps = [step("P1 Button 1", 0, 10), step("P1 Button 2", 4, 10), step("P1 Left", 30, 5), step("P1 Button 2", 15, 2)]
    assert protocol.sequence_args(steps)["steps"] == steps
    # one field ending exactly where another starts is fine; the same field needs one idle frame
    protocol.sequence_args([step("A", 0, 5), step("B", 5, 5), step("A", 6, 5)])


def test_sequence_args_boundaries():
    assert protocol.sequence_args([step("A", 0, protocol.MAX_INPUT_FRAMES)])
    assert protocol.sequence_args([step("A", 0, 1), step("B", protocol.MAX_SEQUENCE_FRAMES - 1, 1)])
    fields = [step(f"F{i}", 0, 1, port=f":P{i}") for i in range(protocol.MAX_INPUT_FIELDS)]
    assert len(protocol.sequence_args(fields)["steps"]) == 8  # the maximum held at once
    many_in_turn = [step(f"F{i}", i * 2, 1) for i in range(protocol.MAX_SEQUENCE_FIELDS)]
    assert len(protocol.sequence_args(many_in_turn)["steps"]) == 16  # 16 distinct fields in turn is fine
    many_steps = [step("A", i * 2, 1) for i in range(protocol.MAX_SEQUENCE_STEPS)]
    assert len(protocol.sequence_args(many_steps)["steps"]) == 64


@pytest.mark.parametrize("steps", [
    [], "x", None, [step("A", 0, 1)] * 2,                                          # empty, wrong type, the same window twice
    [step("A", 0, 10), step("A", 5, 10)],                                          # overlap
    [step("A", 0, 10), step("A", 10, 5)],                                          # touching: no idle frame
    [step("A", 3, 5)],                                                            # earliest step must be at offset 0
    [step("A", 0, 0)], [step("A", 0, protocol.MAX_INPUT_FRAMES + 1)],              # frames bounds
    [step("A", -1, 5)], [step("A", 0.5, 5)], [step("A", True, 5)], [step("A", "0", 5)], [step("A", 0, "5")],
    [step("A", 0, 5), step("B", protocol.MAX_SEQUENCE_FRAMES, 1)],                 # past the total length
    [step("A", 0, 5)] + [step("A", 6 * i, 5) for i in range(1, protocol.MAX_SEQUENCE_STEPS + 1)],  # too many steps
    [step(f"F{i}", 0, 1, port=f":P{i}") for i in range(protocol.MAX_INPUT_FIELDS + 1)],            # 9 held at once
    [step(f"F{i}", i * 2, 1) for i in range(protocol.MAX_SEQUENCE_FIELDS + 1)],                     # 17 distinct fields
    [{"port": P1, "field": "A", "start": 0}], [{"port": P1, "field": "A", "start": 0, "frames": 1, "extra": 1}],
    [{"port": "P1", "field": "A", "start": 0, "frames": 1}], [{"port": P1, "field": "", "start": 0, "frames": 1}], [{"port": P1, "field": "Ä", "start": 0, "frames": 1}],
    ["A"], [None],
])
def test_sequence_args_reject_bad_sequences(steps):
    with pytest.raises(BridgeError) as e:
        protocol.sequence_args(steps)
    assert e.value.code == "bad_argument"


def test_sequence_args_do_not_mutate_or_alias_the_input():
    steps = [step("A", 0, 3)]
    out = protocol.sequence_args(steps)
    out["steps"][0]["frames"] = 99
    assert steps[0]["frames"] == 3


# ------------------------------------------------------------------------------------------------ press_sequence (fake bridge)
SEQ = [step("P1 Button 1", 0, 10), step("P1 Button 2", 4, 10)]


def test_press_sequence_sends_only_the_validated_steps_and_waits_until_released(rig):
    sent = []
    rig.handlers["input_sequence"] = lambda a: sent.append(a) or {"state": "holding", "kind": "sequence", "execution": {"state": "running"}}
    seq = iter([{"state": "holding", "execution": {"state": "running"}},
                {"state": "released", "kind": "sequence", "end_reason": "completed", "frames_elapsed": 14, "execution": {"state": "running"}}])
    rig.handlers["input_status"] = lambda a: next(seq)
    out = rig.mgr.press_sequence(SEQ)
    assert sent == [{"steps": SEQ}] and out["state"] == "released" and out["waited"] is True and rig.count("input_cancel") == 0


def test_press_sequence_while_stopped_is_armed_and_never_polls_or_resumes(rig):
    rig.handlers["input_sequence"] = {"state": "holding", "waiting_for_emulation": True, "execution": {"state": "debugger_stopped"}}
    out = rig.mgr.press_sequence(SEQ)
    assert out["waiting_for_emulation"] is True and out["waited"] is False and "no frame can elapse" in out["note"]
    assert rig.count("input_status") == 0 and rig.count("input_cancel") == 0
    assert not any(op in ("execution_control", "resume", "step") for op, _ in rig.client.calls)


def test_press_sequence_without_wait_returns_when_armed(rig):
    rig.handlers["input_sequence"] = {"state": "holding", "execution": {"state": "running"}}
    out = rig.mgr.press_sequence(SEQ, wait=False)
    assert out["state"] == "holding" and out["waited"] is False and rig.count("input_status") == 0


def test_press_sequence_stops_waiting_when_execution_stops_mid_sequence(rig):
    rig.handlers["input_sequence"] = {"state": "holding", "execution": {"state": "running"}}
    rig.handlers["input_status"] = {"state": "holding", "frames_elapsed": 3, "execution": {"state": "paused"}}
    out = rig.mgr.press_sequence(SEQ)
    assert out["state"] == "holding" and "no frame elapses" in out["note"] and rig.count("input_cancel") == 0


def test_press_sequence_wait_timeout_leaves_the_bounded_sequence_alone(rig):
    rig.handlers["input_sequence"] = {"state": "holding", "execution": {"state": "running"}}
    rig.handlers["input_status"] = {"state": "holding", "execution": {"state": "running"}}
    out = rig.mgr.press_sequence(SEQ, max_wait_s=0.05)
    assert out["state"] == "holding" and "max_wait_s" in out["note"] and rig.count("input_cancel") == 0


def test_error_while_waiting_for_a_sequence_releases_the_inputs(rig):
    rig.handlers["input_sequence"] = {"state": "holding", "execution": {"state": "running"}}
    rig.handlers["input_status"] = BridgeError("mame_disconnected", "connection lost")
    rig.handlers["input_cancel"] = {"state": "cancelled", "was_holding": True, "release_errors": []}
    with pytest.raises(BridgeError) as e:
        rig.mgr.press_sequence(SEQ)
    assert e.value.code == "mame_disconnected" and rig.count("input_cancel") == 1


def test_press_sequence_validation_precedes_the_bridge_and_refusals_pass_through(rig):
    rig.handlers["input_sequence"] = BridgeError("unsupported_input", "analog input")
    with pytest.raises(BridgeError) as e:
        rig.mgr.press_sequence(SEQ)
    assert e.value.code == "unsupported_input"
    n = rig.count("input_sequence")
    for bad in ([], [step("A", 0, 10), step("A", 5, 10)], [step("A", 2, 3)], [step("A", 0, 0)]):
        with pytest.raises(BridgeError) as e:
            rig.mgr.press_sequence(bad)
        assert e.value.code == "bad_argument"
    for kw in ({"wait": "yes"}, {"max_wait_s": 0}, {"max_wait_s": 121}, {"max_wait_s": True}):
        with pytest.raises(BridgeError):
            rig.mgr.press_sequence(SEQ, **kw)
    assert rig.count("input_sequence") == n


def test_press_sequence_is_a_state_changing_serialised_operation_needing_a_managed_session(tmp_path):
    from test_operations import OpsRig
    r = OpsRig(tmp_path)
    with pytest.raises(BridgeError) as e:
        r.mgr.press_sequence(SEQ)
    assert e.value.code == "no_session"


def test_press_inputs_is_unchanged_next_to_press_sequence(rig):
    rig.handlers["input_press"] = {"state": "holding", "execution": {"state": "running"}}
    rig.handlers["input_status"] = {"state": "released", "end_reason": "completed", "frames_elapsed": 4, "execution": {"state": "running"}}
    out = rig.mgr.press_inputs(PRESS, 4)
    assert out["state"] == "released" and rig.count("input_press") == 1 and rig.count("input_sequence") == 0


# ------------------------------------------------------------------------------------------------ server surface
EXPECTED = {"start_session", "stop_session", "session_info", "read_memory", "read_registers", "capture_breakpoint", "capture_watchpoint", "disassemble_memory",
            "experiment_status", "cancel_experiment", "execution_control", "list_inputs", "press_inputs", "press_sequence", "release_inputs", "save_checkpoint",
            "load_checkpoint", "list_checkpoints", "write_memory", "list_screens", "capture_screenshot"}


def tools():
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


def test_tool_surface_is_exactly_the_typed_tools_with_no_lua_shell_or_debugger_command():
    names = set(tools())
    assert names == EXPECTED
    assert not [n for n in names if any(w in n for w in ("lua", "eval", "exec_", "shell", "debugger", "command", "script"))]


def test_new_tools_advertise_explicit_required_arguments_and_closed_enums():
    t = tools()
    wp = t["capture_watchpoint"].input_schema
    assert set(wp["required"]) == {"cpu", "space", "address", "length", "access", "max_events", "max_wall_seconds"}
    assert wp["properties"]["space"]["enum"] == ["program", "data", "io"] and wp["properties"]["access"]["enum"] == ["read", "write", "read_write"]
    assert not {"condition", "action", "expression", "command"} & set(wp["properties"])
    assert t["disassemble_memory"].input_schema["required"] == ["cpu", "address", "count"]
    ps = t["press_sequence"].input_schema
    assert ps["required"] == ["steps"] and ps["properties"]["wait"]["default"] is True


def test_server_instructions_name_the_new_tools():
    text = server.mcp.instructions or ""
    for name in ("capture_watchpoint", "disassemble_memory", "press_sequence"):
        assert name in text
    assert "no arbitrary Lua" in text
