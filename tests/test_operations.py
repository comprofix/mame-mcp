"""Unit tests for checkpoints, held inputs and RAM writes (operations.py) and their argument validation.

Fake process, fake bridge client with scripted plugin replies, fake process table: they prove the Python-side decision
logic (validation, ordering, serialisation, ownership, cleanup). The plugin's own checks and the real MAME behaviour are
exercised by scripts/validate_checkpoint_input.py and scripts/mcp_checkpoint_e2e.py.
"""
import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

from mame_mcp import launcher, operations, protocol
from mame_mcp.protocol import BridgeError
from test_session import FakeClient, Rig, _fast  # noqa: F401  (autouse fixture re-used)


# ------------------------------------------------------------------------------------------------ protocol
@pytest.mark.parametrize("width,value,address", [(1, 0xFF, 0x1000), (2, 0xFFFF, 0x1000), (4, 0xFFFFFFFF, 0x1000), (2, "0xBEEF", "0x2002")])
def test_write_args_accept_explicit_values(width, value, address):
    a = protocol.write_memory_args(":maincpu", "program", address, value, width)
    assert a["width"] == width and a["cpu"] == ":maincpu" and a["space"] == "program"


@pytest.mark.parametrize("kw", [
    dict(width=0), dict(width=3), dict(width=8), dict(width=True), dict(width="2"), dict(width=None),
    dict(width=1, value=0x100), dict(width=2, value=0x10000), dict(width=4, value=1 << 32), dict(value=-1),
    dict(width=2, address=0x1001), dict(width=4, address=0x1002),
    dict(cpu="maincpu"), dict(cpu=":main cpu"), dict(cpu=None), dict(space="pro gram"), dict(space=None), dict(space=""),
    dict(address=-1), dict(address=1 << 32), dict(address="zz"), dict(address=1.5), dict(value=True),
])
def test_write_args_reject_bad_arguments(kw):
    base = dict(cpu=":maincpu", space="program", address=0x1000, value=1, width=1)
    base.update(kw)
    with pytest.raises(BridgeError) as e:
        protocol.write_memory_args(base["cpu"], base["space"], base["address"], base["value"], base["width"])
    assert e.value.code == "bad_argument"


def test_press_args_validation():
    ok = [{"port": ":P1", "field": "P1 Button 1"}]
    assert protocol.press_args(ok, 5) == {"inputs": ok, "frames": 5}
    assert protocol.press_args([{"port": ":DSW", "field": "Enemy's Bullet Speed"}], 1)["frames"] == 1
    bad = [
        ([], 5), (ok * 2, 5), ([{"port": ":P1", "field": f"B{i}"} for i in range(9)], 5), ("x", 5), (None, 5),
        ([{"port": ":P1"}], 5), ([{"port": ":P1", "field": "A", "extra": 1}], 5), (["P1"], 5),
        ([{"port": "P1", "field": "A"}], 5), ([{"port": ":P1", "field": ""}], 5), ([{"port": ":P1", "field": "x" * 65}], 5),
        ([{"port": ":P1", "field": "A\nB"}], 5), ([{"port": ":P1", "field": "Ä"}], 5), ([{"port": ":P1", "field": 3}], 5),
        (ok, 0), (ok, 601), (ok, -1), (ok, True), (ok, 1.5), (ok, "5"), (ok, None),
    ]
    for inputs, frames in bad:
        with pytest.raises(BridgeError) as e:
            protocol.press_args(inputs, frames)
        assert e.value.code == "bad_argument", (inputs, frames)


def test_press_args_boundaries_and_distinct_fields():
    many = [{"port": ":P1", "field": f"F{i}"} for i in range(protocol.MAX_INPUT_FIELDS)]
    assert len(protocol.press_args(many, protocol.MAX_INPUT_FRAMES)["inputs"]) == 8
    with pytest.raises(BridgeError):
        protocol.press_args(many + [{"port": ":P1", "field": "F9"}], 1)


def test_labels_and_checkpoint_ids():
    assert protocol.check_label(None) is None and protocol.check_label("  stage 2 start ") == "stage 2 start"
    for bad in ["", "   ", "x" * 81, "a\nb", "a\x00b", 5]:
        with pytest.raises(BridgeError):
            protocol.check_label(bad)
    good = "shinobi-20261117-203045-a1b2c3-cp001"
    assert protocol.check_checkpoint_id(good).group("run") == "shinobi-20261117-203045-a1b2c3"
    for bad in ["", "../x", "..\\x-cp001", "shinobi-cp001", good + "/..", good + ".sta", "C:\\x\\" + good, good.upper(), None, 5,
                "shinobi-20261117-203045-a1b2c3-cp1", "shinobi-20261117-203045-a1b2c3-cp001\n"]:
        with pytest.raises(BridgeError) as e:
            protocol.check_checkpoint_id(bad)
        assert e.value.code == "bad_argument"


def test_list_inputs_args():
    assert protocol.list_inputs_args(True) == {"include_unsupported": True}
    with pytest.raises(BridgeError):
        protocol.list_inputs_args("yes")


# ------------------------------------------------------------------------------------------------ rig
class OpsClient(FakeClient):
    """FakeClient whose new-operation replies are scripted per test (handlers: op -> dict | callable | Exception)."""

    def __init__(self, handlers, **kw):
        super().__init__(active=None, **kw)
        self.handlers, self.calls = handlers, []

    def request(self, op, args=None, *, timeout=None, mutating=False, retry_safe=False):
        h = self.handlers.get(op)
        if h is None:
            return super().request(op, args, timeout=timeout, mutating=mutating, retry_safe=retry_safe)
        self.log.append(op)
        self.calls.append((op, args))
        r = h(args) if callable(h) else h
        if isinstance(r, Exception):
            raise r
        return r


class OpsRig(Rig):
    def __init__(self, tmp_path):
        self.handlers = {}
        super().__init__(tmp_path)
        self.tmp = tmp_path

    def _client(self, port, pid, created):
        c = OpsClient(self.handlers, **self.client_kw)
        self.clients.append(c)
        return c

    def start(self):
        info = self.mgr.start("shinobi")
        self.run_dir = Path(info["run_dir"])
        self.client = self.clients[-1]
        return info

    def state_file(self, cid, content=b"MAMESAVE" + b"\x02" * 200):
        p = self.run_dir / "home" / "sta" / "shinobi" / f"{cid}.sta"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def count(self, op):
        return sum(1 for o, _ in self.client.calls if o == op)


# a machine with a device that has an address space but no state interface (1944's :qsound): recorded as not covered
COVERAGE = {"registers_compared_on": [{"device": ":maincpu", "registers": 20}, {"device": ":audiocpu", "registers": 27}], "no_state_interface": [":qsound"],
            "state_read_error": [], "registers_excluded": [], "ram": {"partial": True, "bytes_hashed": 262144, "bytes_total": 4415628},
            "not_covered": "device-internal state of devices without a register interface, anonymous RAM and any other state in the file are not compared; this is not a whole-machine equality check"}
FP = {"frame": 120, "cpus": {":maincpu": {"PC": "404", "SR": "2700"}}, "exact": True}


def save_reply(rig, args, mode="immediate", fp=FP):
    p = rig.state_file(args["id"])
    return {"state": "completed", "kind": "save", "id": args["id"], "path": str(p).replace("\\", "/"), "mode": mode, "size": p.stat().st_size,
            "execution_before": "debugger_stopped", "execution": {"state": "debugger_stopped"}, "frames_run_during_operation": 0,
            "header": {"magic": "MAMESAVE", "version": 2, "system": "shinobi", "signature": "0badf00d"}, "fingerprint": fp, "frame": 120,
            "fingerprint_coverage": COVERAGE,
            "emu_time_s": 2.0, "mame_version": "0.289"}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(operations, "POLL_S", 0.01)
    r = OpsRig(tmp_path)
    r.start()
    r.handlers["checkpoint_save"] = lambda a: save_reply(r, a)
    r.handlers["session_info"] = lambda a: {"bridge": {"session_id": "s1-aaaaaa"}, "state_epoch": 1, "execution": {"state": "debugger_stopped"},
                                           "active_experiment": None, "unresolved_instrumentation": []}
    return r


# ------------------------------------------------------------------------------------------------ checkpoints: save
def test_save_checkpoint_writes_metadata_and_registers(rig):
    out = rig.mgr.save_checkpoint("start of stage 1")
    cid = out["checkpoint_id"]
    assert cid.startswith(rig.mgr.snapshot()["run_id"] + "-cp001") and out["state"] == "completed" and out["label"] == "start of stage 1"
    side = json.loads((rig.run_dir / "checkpoints" / f"{cid}.json").read_text())
    f = rig.run_dir / "home" / "sta" / "shinobi" / f"{cid}.sta"
    assert side["file"]["sha256"] == hashlib.sha256(f.read_bytes()).hexdigest() and side["fingerprint"] == FP and side["system"] == "shinobi"
    assert out["sha256"] == side["file"]["sha256"] and out["path"].endswith(f"{cid}.sta") and out["current_session"] is True
    assert rig.mgr.snapshot()["checkpoints"] == [cid]
    second = rig.mgr.save_checkpoint()
    assert second["checkpoint_id"].endswith("-cp002")
    ls = rig.mgr.list_checkpoints()
    assert [c["checkpoint_id"] for c in ls["checkpoints"]] == [second["checkpoint_id"], cid][: len(ls["checkpoints"])] or len(ls["checkpoints"]) == 2


def test_save_reports_and_persists_what_the_fingerprint_covers(rig):
    out = rig.mgr.save_checkpoint("coverage")
    assert out["fingerprint_coverage"] == COVERAGE and out["fingerprint_coverage"]["no_state_interface"] == [":qsound"]
    side = json.loads((rig.run_dir / "checkpoints" / f"{out['checkpoint_id']}.json").read_text())
    assert side["fingerprint_coverage"] == COVERAGE  # kept with the checkpoint, so a later session can still describe it
    listed = rig.mgr.list_checkpoints()["checkpoints"][0]
    assert listed["fingerprint_coverage"] == COVERAGE


def test_save_passes_the_plugin_refusals_through_and_registers_nothing(rig):
    for code in ("capture_active", "unresolved_instrumentation", "execution_running", "input_active", "unsupported_machine", "checkpoint_busy"):
        rig.handlers["checkpoint_save"] = BridgeError(code, f"refused: {code}")
        with pytest.raises(BridgeError) as e:
            rig.mgr.save_checkpoint()
        assert e.value.code == code
    assert rig.mgr.snapshot()["checkpoints"] == []
    assert not (rig.run_dir / "checkpoints").exists()


def test_save_is_complete_only_after_the_plugin_reports_it(rig):
    polls = []

    def status(_):
        polls.append(1)
        if len(polls) < 3:
            return {"state": "pending", "kind": "save"}
        return save_reply(rig, {"id": pending_id[0]}, mode="scheduled", fp={"frame": 100, "frame_max": 102, "cpus": {}, "exact": False})

    pending_id = []
    rig.handlers["checkpoint_save"] = lambda a: (pending_id.append(a["id"]), {"state": "pending", "kind": "save", "id": a["id"]})[1]
    rig.handlers["checkpoint_status"] = status
    out = rig.mgr.save_checkpoint()
    assert len(polls) == 3 and out["mode"] == "scheduled" and out["fingerprint_exact"] is False and "frame range" not in out["note"] and "briefly" in out["note"]


def test_pending_save_failure_and_timeout_are_errors_marked_uncertain(rig, monkeypatch):
    rig.handlers["checkpoint_save"] = {"state": "pending", "kind": "save", "id": "x"}
    rig.handlers["checkpoint_status"] = {"state": "failed", "kind": "save", "error": {"code": "checkpoint_failed", "message": "no file", "detail": {"x": 1}}}
    with pytest.raises(BridgeError) as e:
        rig.mgr.save_checkpoint()
    assert e.value.code == "checkpoint_failed" and e.value.uncertain is True and e.value.detail == {"x": 1}
    rig.handlers["checkpoint_status"] = {"state": "pending", "kind": "save"}
    monkeypatch.setattr(operations, "CHECKPOINT_WAIT_S", 0.05)
    with pytest.raises(BridgeError) as e2:
        rig.mgr.save_checkpoint()
    assert e2.value.code == "checkpoint_timeout" and e2.value.uncertain is True
    assert rig.mgr.snapshot()["checkpoints"] == []


def test_save_refuses_a_state_file_outside_the_session(rig, tmp_path):
    outside = tmp_path / "elsewhere.sta"
    outside.write_bytes(b"x" * 64)
    rig.handlers["checkpoint_save"] = lambda a: {**save_reply(rig, a), "path": str(outside)}
    with pytest.raises(BridgeError) as e:
        rig.mgr.save_checkpoint()
    assert e.value.code == "checkpoint_path_outside_session" and rig.mgr.snapshot()["checkpoints"] == []


def test_save_detects_size_mismatch_and_wrong_id(rig):
    rig.handlers["checkpoint_save"] = lambda a: {**save_reply(rig, a), "size": 5}
    with pytest.raises(BridgeError) as e:
        rig.mgr.save_checkpoint()
    assert e.value.code == "checkpoint_failed"
    rig.handlers["checkpoint_save"] = lambda a: {**save_reply(rig, a), "id": "something-else"}
    with pytest.raises(BridgeError):
        rig.mgr.save_checkpoint()


def test_bad_label_never_reaches_the_bridge(rig):
    with pytest.raises(BridgeError):
        rig.mgr.save_checkpoint("a\nb")
    assert rig.count("checkpoint_save") == 0


# ------------------------------------------------------------------------------------------------ checkpoints: load
def load_ok(args):
    return {"state": "completed", "kind": "load", "id": args["id"], "mode": "immediate", "execution_before": "debugger_stopped",
            "frames_run_during_operation": 0, "invalidated_experiments": ["exp-s1-aaaaaa-001"],
            "verification": {"method": "frame_and_cpu_registers_exact", "exact": True, "load_effect_observed": True, "expected_frame": 120}}


def test_load_checkpoint_verifies_integrity_sends_the_fingerprint_and_refreshes_identity(rig):
    cid = rig.mgr.save_checkpoint()["checkpoint_id"]
    rig.handlers["checkpoint_load"] = load_ok
    out = rig.mgr.load_checkpoint(cid)
    op, args = [c for c in rig.client.calls if c[0] == "checkpoint_load"][0]
    assert args == {"id": cid, "expect": FP}
    assert out["state"] == "completed" and out["verification"]["exact"] is True and out["session_id"] == "s1-aaaaaa" and out["state_epoch"] == 1
    assert out["invalidated_experiments"] == ["exp-s1-aaaaaa-001"] and out["imported_from_earlier_session"] is False
    assert rig.client.log[-1] == "session_info"  # identity re-checked after the load


def test_load_detects_a_session_change(rig):
    cid = rig.mgr.save_checkpoint()["checkpoint_id"]
    rig.handlers["checkpoint_load"] = load_ok
    rig.handlers["session_info"] = {"bridge": {"session_id": "s9-zzzzzz"}, "execution": {}}
    with pytest.raises(BridgeError) as e:
        rig.mgr.load_checkpoint(cid)
    assert e.value.code == "session_changed" and e.value.uncertain is True


def test_load_refuses_a_corrupt_file_before_calling_the_plugin(rig):
    cid = rig.mgr.save_checkpoint()["checkpoint_id"]
    (rig.run_dir / "home" / "sta" / "shinobi" / f"{cid}.sta").write_bytes(b"MAMESAVE-tampered")
    rig.handlers["checkpoint_load"] = load_ok
    with pytest.raises(BridgeError) as e:
        rig.mgr.load_checkpoint(cid)
    assert e.value.code == "checkpoint_corrupt" and rig.count("checkpoint_load") == 0


def test_load_missing_and_malformed_ids(rig):
    rid = rig.mgr.snapshot()["run_id"]
    with pytest.raises(BridgeError) as e:
        rig.mgr.load_checkpoint(f"{rid}-cp009")
    assert e.value.code == "checkpoint_not_found"
    with pytest.raises(BridgeError) as e:
        rig.mgr.load_checkpoint("shinobi-20200101-000000-abcdef-cp001")  # an earlier session that does not exist
    assert e.value.code == "checkpoint_not_found"
    for bad in ["../../etc/passwd", f"{rid}-cp001/../x", "", None]:
        with pytest.raises(BridgeError) as e:
            rig.mgr.load_checkpoint(bad)
        assert e.value.code == "bad_argument"
    assert rig.count("checkpoint_load") == 0


def test_load_passes_plugin_refusals_and_failures_through(rig):
    cid = rig.mgr.save_checkpoint()["checkpoint_id"]
    for code in ("capture_active", "unresolved_instrumentation", "execution_running", "input_active", "checkpoint_not_applied", "checkpoint_incompatible"):
        rig.handlers["checkpoint_load"] = BridgeError(code, code)
        with pytest.raises(BridgeError) as e:
            rig.mgr.load_checkpoint(cid)
        assert e.value.code == code


def make_earlier_session(rig, system="shinobi", mame="0.289", run="shinobi-20250101-010101-abcdef", tamper=False):
    origin = rig.cfg.runs_dir / run
    cid = f"{run}-cp001"
    f = origin / "home" / "sta" / system / f"{cid}.sta"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"MAMESAVE" + b"\x07" * 300)
    meta = {"id": cid, "label": "old", "system": system, "run_id": run, "session_id": "s0", "created_utc": "2025-01-01T01:01:01Z", "mame_version": mame,
            "frame": 77, "fingerprint": {**FP, "frame": 77}, "file": {"name": f.name, "size": f.stat().st_size, "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}}
    (origin / "checkpoints").mkdir()
    (origin / "checkpoints" / f"{cid}.json").write_text(json.dumps(meta))
    if tamper:
        f.write_bytes(b"MAMESAVE" + b"\x08" * 300)
    return cid, f


def test_load_imports_a_checkpoint_from_an_earlier_session(rig):
    cid, src = make_earlier_session(rig)
    listed = rig.mgr.list_checkpoints()["checkpoints"]
    assert [c["checkpoint_id"] for c in listed] == [cid] and listed[0]["current_session"] is False
    rig.handlers["checkpoint_load"] = load_ok
    out = rig.mgr.load_checkpoint(cid)
    dest = rig.run_dir / "home" / "sta" / "shinobi" / f"{cid}.sta"
    assert out["imported_from_earlier_session"] is True and dest.read_bytes() == src.read_bytes()
    assert [c for c in rig.client.calls if c[0] == "checkpoint_load"][0][1]["expect"]["frame"] == 77
    assert cid in rig.mgr.snapshot()["checkpoints"]


def test_import_validation(rig):
    rig.handlers["checkpoint_load"] = load_ok
    cid, _ = make_earlier_session(rig, system="1942", run="1942-20250101-010101-abcdef")
    with pytest.raises(BridgeError) as e:
        rig.mgr.load_checkpoint(cid)
    assert e.value.code == "checkpoint_incompatible" and "1942" in e.value.message
    cid, _ = make_earlier_session(rig, mame="0.280", run="shinobi-20250101-010102-abcdef")
    with pytest.raises(BridgeError) as e:
        rig.mgr.load_checkpoint(cid)
    assert e.value.code == "checkpoint_incompatible" and "0.280" in e.value.message
    cid, _ = make_earlier_session(rig, tamper=True, run="shinobi-20250101-010103-abcdef")
    with pytest.raises(BridgeError) as e:
        rig.mgr.load_checkpoint(cid)
    assert e.value.code == "checkpoint_corrupt"
    assert not (rig.run_dir / "home" / "sta" / "shinobi" / f"{cid}.sta").exists()  # a bad import is not left behind
    assert rig.count("checkpoint_load") == 0


def test_import_rejects_metadata_that_does_not_match_its_id(rig):
    cid, _ = make_earlier_session(rig)
    side = rig.cfg.runs_dir / cid.rsplit("-cp", 1)[0] / "checkpoints" / f"{cid}.json"
    meta = json.loads(side.read_text())
    meta["id"] = "something-else"
    side.write_text(json.dumps(meta))
    with pytest.raises(BridgeError) as e:
        rig.mgr.load_checkpoint(cid)
    assert e.value.code == "checkpoint_incompatible"


# ------------------------------------------------------------------------------------------------ serialisation and lifecycle
def test_state_changing_operations_are_serialised(rig, monkeypatch):
    monkeypatch.setattr(operations, "EXCLUSIVE_WAIT_S", 0.2)
    gate, started = threading.Event(), threading.Event()

    def slow_save(a):
        started.set()
        gate.wait(5)
        return save_reply(rig, a)

    rig.handlers["checkpoint_save"] = slow_save
    rig.handlers["write_memory"] = {"verified": True}
    t = threading.Thread(target=lambda: rig.mgr.save_checkpoint())
    t.start()
    assert started.wait(5)
    for call in (lambda: rig.mgr.write_memory(":maincpu", "program", 0x1000, 1, 1), lambda: rig.mgr.release_inputs(),
                 lambda: rig.mgr.press_inputs([{"port": ":P1", "field": "A"}], 3), lambda: rig.mgr.save_checkpoint()):
        with pytest.raises(BridgeError) as e:
            call()
        assert e.value.code == "operation_in_progress" and "save_checkpoint" in e.value.message
    assert rig.count("write_memory") == 0 and rig.count("input_press") == 0
    gate.set()
    t.join(5)
    assert rig.mgr.write_memory(":maincpu", "program", 0x1000, 1, 1) == {"verified": True}  # free again


def test_stop_session_during_a_pending_checkpoint_abandons_the_wait(rig):
    rig.handlers["checkpoint_save"] = {"state": "pending", "kind": "save", "id": "x"}
    rig.handlers["checkpoint_status"] = {"state": "pending", "kind": "save"}
    box = {}

    def run():
        try:
            rig.mgr.save_checkpoint()
        except BridgeError as e:
            box["e"] = e

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.15)
    stop = rig.mgr.stop()
    t.join(5)
    assert stop["stopped"] is True and not t.is_alive()
    assert box["e"].code in ("session_stopping", "session_exited")
    assert rig.procs[0].rc is not None


def test_operations_need_a_managed_session(tmp_path):
    r = OpsRig(tmp_path)
    for call in (lambda: r.mgr.save_checkpoint(), lambda: r.mgr.load_checkpoint("shinobi-20250101-010101-abcdef-cp001"),
                 lambda: r.mgr.press_inputs([{"port": ":P1", "field": "A"}], 1), lambda: r.mgr.release_inputs(),
                 lambda: r.mgr.write_memory(":maincpu", "program", 0, 0, 1)):
        with pytest.raises(BridgeError) as e:
            call()
        assert e.value.code == "no_session"
    assert r.mgr.list_checkpoints()["checkpoints"] == []  # metadata listing needs no MAME


def test_external_attachment_routing_is_unchanged(tmp_path):
    r = OpsRig(tmp_path)
    ext = FakeClient()
    with r.mgr.use_client(ext) as (client, managed):
        assert client is ext and managed is False
    with pytest.raises(BridgeError) as e:
        with r.mgr.use_managed():
            pass
    assert e.value.code == "no_session"  # an external MAME is never handed to the managed-only operations


# ------------------------------------------------------------------------------------------------ inputs
PRESS = [{"port": ":P1", "field": "P1 Button 1"}]


def test_press_waits_until_released(rig):
    rig.handlers["input_press"] = {"state": "holding", "input_id": "inp-1", "frames_requested": 4, "execution": {"state": "running"}}
    seq = iter([{"state": "holding", "execution": {"state": "running"}}, {"state": "holding", "execution": {"state": "running"}},
                {"state": "released", "end_reason": "completed", "frames_elapsed": 4, "execution": {"state": "running"}}])
    rig.handlers["input_status"] = lambda a: next(seq)
    out = rig.mgr.press_inputs(PRESS, 4)
    assert out["state"] == "released" and out["waited"] is True and rig.count("input_cancel") == 0


def test_press_without_wait_returns_as_soon_as_armed(rig):
    rig.handlers["input_press"] = {"state": "holding", "execution": {"state": "running"}}
    out = rig.mgr.press_inputs(PRESS, 4, wait=False)
    assert out["state"] == "holding" and out["waited"] is False and rig.count("input_status") == 0


def test_press_while_stopped_is_armed_and_never_waits(rig):
    rig.handlers["input_press"] = {"state": "holding", "waiting_for_emulation": True, "execution": {"state": "debugger_stopped"}}
    out = rig.mgr.press_inputs(PRESS, 4)
    assert out["waiting_for_emulation"] is True and rig.count("input_status") == 0 and "no frame can elapse" in out["note"]
    assert rig.count("input_cancel") == 0  # held on purpose: the caller resumes, waits or releases


def test_press_stops_waiting_when_execution_stops_mid_press(rig):
    rig.handlers["input_press"] = {"state": "holding", "execution": {"state": "running"}}
    rig.handlers["input_status"] = {"state": "holding", "frames_elapsed": 2, "execution": {"state": "paused"}}
    out = rig.mgr.press_inputs(PRESS, 40)
    assert out["state"] == "holding" and "no frame elapses" in out["note"] and rig.count("input_cancel") == 0


def test_press_wait_timeout_leaves_the_bounded_press_alone(rig):
    rig.handlers["input_press"] = {"state": "holding", "execution": {"state": "running"}}
    rig.handlers["input_status"] = {"state": "holding", "execution": {"state": "running"}}
    out = rig.mgr.press_inputs(PRESS, 40, max_wait_s=0.05)
    assert out["state"] == "holding" and "max_wait_s" in out["note"] and rig.count("input_cancel") == 0


def test_error_during_the_wait_releases_the_inputs(rig):
    rig.handlers["input_press"] = {"state": "holding", "execution": {"state": "running"}}
    rig.handlers["input_status"] = BridgeError("mame_disconnected", "connection lost")
    rig.handlers["input_cancel"] = {"state": "cancelled", "was_holding": True, "release_errors": []}
    with pytest.raises(BridgeError) as e:
        rig.mgr.press_inputs(PRESS, 40)
    assert e.value.code == "mame_disconnected" and rig.count("input_cancel") == 1


def test_a_failed_release_during_error_cleanup_does_not_mask_the_error(rig):
    rig.handlers["input_press"] = {"state": "holding", "execution": {"state": "running"}}
    rig.handlers["input_status"] = BridgeError("request_timeout", "slow")
    rig.handlers["input_cancel"] = BridgeError("mame_disconnected", "gone")
    with pytest.raises(BridgeError) as e:
        rig.mgr.press_inputs(PRESS, 40)
    assert e.value.code == "request_timeout" and rig.count("input_cancel") == 1


def test_press_refusals_pass_through_and_validation_precedes_the_bridge(rig):
    rig.handlers["input_press"] = BridgeError("unsupported_input", "analog input")
    with pytest.raises(BridgeError) as e:
        rig.mgr.press_inputs(PRESS, 3)
    assert e.value.code == "unsupported_input"
    n = rig.count("input_press")
    for args in [([], 3), (PRESS, 0), (PRESS, 601), (PRESS * 2, 3)]:
        with pytest.raises(BridgeError):
            rig.mgr.press_inputs(*args)
    with pytest.raises(BridgeError):
        rig.mgr.press_inputs(PRESS, 3, wait="yes")
    with pytest.raises(BridgeError):
        rig.mgr.press_inputs(PRESS, 3, max_wait_s=0)
    assert rig.count("input_press") == n


def test_release_inputs_is_idempotent_passthrough(rig):
    rig.handlers["input_cancel"] = {"state": "cancelled", "was_holding": True, "release_errors": []}
    assert rig.mgr.release_inputs()["was_holding"] is True
    rig.handlers["input_cancel"] = {"state": "cancelled", "was_holding": False, "release_errors": []}
    assert rig.mgr.release_inputs()["was_holding"] is False


def test_stop_session_releases_held_inputs_before_stopping(rig):
    rig.handlers["input_cancel"] = {"state": "cancelled", "was_holding": True, "end_reason": "cancelled", "release_errors": []}
    rep = rig.mgr.stop()
    assert rep["input_cleanup"]["released"] is True and rep["input_cleanup"]["was_holding"] is True and rep["cleanup_failures"] == []
    log = rig.client.log
    assert log.index("input_cancel") < log.index("close")  # released while the bridge was still connected
    assert rig.procs[0].rc is not None


def test_stop_session_reports_a_failed_input_release_but_still_stops(rig):
    rig.handlers["input_cancel"] = {"state": "cancelled", "was_holding": True, "release_errors": [":P1/P1 Button 1: boom"]}
    rep = rig.mgr.stop()
    assert rep["input_cleanup"]["released"] is False and any("held inputs" in f for f in rep["cleanup_failures"])
    assert rep["process"]["terminated"] is True
    (rig.tmp / "two").mkdir()
    r2 = OpsRig(rig.tmp / "two")
    r2.handlers["input_cancel"] = BridgeError("mame_disconnected", "gone")
    r2.start()
    rep2 = r2.mgr.stop()
    assert rep2["input_cleanup"]["released"] is False and "error" in rep2["input_cleanup"] and rep2["process"]["terminated"] is True


def test_server_shutdown_releases_held_inputs(rig):
    rig.handlers["input_cancel"] = {"state": "cancelled", "was_holding": True, "end_reason": "cancelled", "release_errors": []}
    rep = rig.mgr.shutdown()
    assert rep["input_cleanup"]["released"] is True and rig.count("input_cancel") == 1 and rig.procs[0].rc is not None


def test_stop_after_an_unexpected_exit_skips_the_bridge(rig):
    rig.procs[0].rc = 9
    rep = rig.mgr.stop()
    assert rep["input_cleanup"] is None and rig.count("input_cancel") == 0  # nothing to release: the process is already gone


# ------------------------------------------------------------------------------------------------ RAM writes
def test_write_memory_sends_only_validated_explicit_fields(rig):
    rig.handlers["write_memory"] = lambda a: {"verified": True, "before": {"value": 1}, "requested": {"value": a["value"]}, "after": {"value": a["value"]}}
    out = rig.mgr.write_memory(":maincpu", "program", "0xC70010", "0x1234", 2)
    assert out["after"]["value"] == 0x1234
    op, args = rig.client.calls[-1]
    assert op == "write_memory" and args == {"cpu": ":maincpu", "space": "program", "address": 0xC70010, "width": 2, "value": 0x1234}


def test_write_memory_refusals_pass_through_and_bad_arguments_never_reach_the_bridge(rig):
    for code in ("rom_target", "unmapped_target", "unsupported_target", "execution_running", "capture_active", "unresolved_instrumentation",
                 "input_active", "checkpoint_busy", "write_verification_failed"):
        rig.handlers["write_memory"] = BridgeError(code, code)
        with pytest.raises(BridgeError) as e:
            rig.mgr.write_memory(":maincpu", "program", 0x1000, 1, 1)
        assert e.value.code == code
    n = rig.count("write_memory")
    for args in [(":maincpu", "program", 0x1000, 1, 3), (":maincpu", "program", 0x1001, 1, 2), (":maincpu", "program", 0x1000, 256, 1),
                 ("maincpu", "program", 0x1000, 1, 1), (":maincpu", None, 0x1000, 1, 1)]:
        with pytest.raises(BridgeError):
            rig.mgr.write_memory(*args)
    assert rig.count("write_memory") == n


# ------------------------------------------------------------------------------------------------ launcher / test-hook isolation
def test_launcher_sets_the_state_dir_and_does_not_inherit_ambient_hooks(tmp_path, monkeypatch):
    seen = {}

    class P:
        pid = 4321

    def popen(cmd, **kw):
        seen["env"], seen["cmd"] = kw["env"], cmd
        return P()

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(launcher, "mame_version", lambda exe: "0.289")
    monkeypatch.setattr(launcher, "verify_roms", lambda *a: {"returncode": 0, "output": "ok"})
    monkeypatch.setattr(launcher, "listxml_roms", lambda *a: {})
    monkeypatch.setenv("MAMEMCP_TEST_FORCE_SCHEDULED_CHECKPOINT", "1")
    monkeypatch.setenv("MAMEMCP_TEST_FAIL_CLEAR", "1")
    monkeypatch.setenv("MAMEMCP_STATE_DIR", "C:/ambient/elsewhere")
    run = tmp_path / "run"
    launcher.launch(system="shinobi", port=9999, run_dir=run, mame_dir=tmp_path, rompath=tmp_path)
    env = seen["env"]
    assert env["MAMEMCP_STATE_DIR"] == str((run / "home" / "sta").resolve()).replace("\\", "/")
    assert "MAMEMCP_TEST_FORCE_SCHEDULED_CHECKPOINT" not in env and "MAMEMCP_TEST_FAIL_CLEAR" not in env
    sd = seen["cmd"][seen["cmd"].index("-state_directory") + 1]
    assert sd == env["MAMEMCP_STATE_DIR"]  # plugin and MAME agree on where the .sta files go


def test_launcher_has_no_default_machine(capsys):
    """The launcher is machine-agnostic: the caller always names the machine, in code and on the command line."""
    with pytest.raises(TypeError):
        launcher.launch(port=9999)  # no `system`: nothing is launched
    with pytest.raises(SystemExit) as e:
        launcher.main(["--port", "9999"])
    assert e.value.code == 2 and "--system" in capsys.readouterr().err
