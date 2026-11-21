"""Unit tests for several managed sessions in one server: routing by session_id, ambiguity, stale ids, the session limit, locking
and failure isolation (fake processes, fake bridge clients, fake process table; see test_session.Rig). They prove the decision
logic only; scripts/mcp_multisession_e2e.py exercises three real MAME processes through the MCP SDK."""
import asyncio
import threading
import time
from pathlib import Path

import pytest

from mame_mcp import operations, protocol, session
from mame_mcp.protocol import BridgeError
from mame_mcp.session import EXITED, FAILED, READY, SessionConfig
from test_operations import load_ok, make_earlier_session
from test_session import FakeClient, Rig, _fast  # noqa: F401  (autouse fixture re-used)


class RecClient(FakeClient):
    """FakeClient that records every request and answers any other operation with its own session id. Per-session scripted
    replies live in rig.handlers[session_id][op] (value, callable(args) or Exception)."""

    rig: "MRig"

    def __init__(self, **kw):
        kw.setdefault("active", None)
        super().__init__(**kw)
        self.calls = []

    def request(self, op, args=None, *, timeout=None, mutating=False, retry_safe=False):
        h = self.rig.handlers.get(self.sid, {}).get(op)
        if h is not None:
            self.log.append(op)
            self.calls.append((op, args))
            r = h(args) if callable(h) else h
            if isinstance(r, Exception):
                raise r
            return r
        if op in ("session_info", "cancel_experiment", "input_cancel"):  # lifecycle operations FakeClient already scripts (and logs)
            return super().request(op, args, timeout=timeout, mutating=mutating, retry_safe=retry_safe)
        self.log.append(op)
        self.calls.append((op, args))
        return {"client": self.sid, "op": op}

    def n(self, op):
        return sum(1 for o, _ in self.calls if o == op)


class MRig(Rig):
    def __init__(self, tmp_path, max_sessions=3, **kw):
        super().__init__(tmp_path, max_sessions=max_sessions, unique_ids=True, active=None, **kw)
        self.handlers: dict[str, dict] = {}
        self.tmp = tmp_path

        class C(RecClient):
            pass

        C.rig = self
        self.client_cls = C

    def start(self, system="shinobi", **kw):
        return self.mgr.start(system, **kw)

    def client_of(self, info) -> RecClient:
        return next(c for c in self.clients if c.sid == info["session_id"])

    def proc_of(self, info):
        return next(p for p in self.procs if p.pid == info["pid"])


@pytest.fixture
def rig(tmp_path):
    return MRig(tmp_path)


def write(rig, info=None):
    return rig.mgr.write_memory(":maincpu", "program", 0x1000, 1, 1, **({"session_id": info["session_id"]} if info else {}))


# ---------------------------------------------------------------------------------------------- identity and isolation
def test_two_sessions_of_the_same_system_are_distinct_and_listed(rig):
    a, b = rig.start(), rig.start()
    assert a["session_id"] != b["session_id"] and a["run_id"] != b["run_id"]
    assert a["port"] != b["port"] and a["run_dir"] != b["run_dir"] and a["evidence_dir"] != b["evidence_dir"] and a["pid"] != b["pid"]
    assert {kw["port"] for kw in rig.launches} == {a["port"], b["port"]} and len(rig.procs) == 2
    listed = rig.mgr.list_sessions()
    assert listed["max_sessions"] == 3 and listed["live_count"] == 2
    assert {s["session_id"]: (s["system"], s["state"]) for s in listed["sessions"]} == {a["session_id"]: ("shinobi", READY), b["session_id"]: ("shinobi", READY)}


def test_explicit_session_id_routes_every_operation_to_that_session_only(rig):
    a, b = rig.start(), rig.start()
    ca, cb = rig.client_of(a), rig.client_of(b)
    with rig.mgr.use_client(None, b["session_id"]) as (client, managed):
        assert client is cb and managed is True
    assert write(rig, b)["client"] == b["session_id"]
    rig.mgr.press_inputs([{"port": ":P1", "field": "A"}], 3, session_id=a["session_id"])
    rig.mgr.press_sequence([{"port": ":P1", "field": "A", "start": 0, "frames": 2}], session_id=b["session_id"])
    rig.mgr.release_inputs(session_id=b["session_id"])
    assert (ca.n("write_memory"), cb.n("write_memory")) == (0, 1)
    assert (ca.n("input_press"), cb.n("input_press"), ca.n("input_sequence"), cb.n("input_sequence")) == (1, 0, 0, 1)
    assert "input_cancel" in cb.log and "input_cancel" not in ca.log
    # the run id is accepted as an alias of the session id, for a session that has not got a bridge id yet
    with rig.mgr.use_client(None, a["run_id"]) as (client, _):
        assert client is ca


AMBIGUOUS_CALLS = {
    "write_memory": lambda m: m.write_memory(":maincpu", "program", 0x1000, 1, 1),
    "release_inputs": lambda m: m.release_inputs(),
    "press_inputs": lambda m: m.press_inputs([{"port": ":P1", "field": "A"}], 3),
    "press_sequence": lambda m: m.press_sequence([{"port": ":P1", "field": "A", "start": 0, "frames": 2}]),
    "save_checkpoint": lambda m: m.save_checkpoint(),
    "load_checkpoint": lambda m: m.load_checkpoint("shinobi-20250101-010101-abcdef-cp001"),
    "capture_screenshot": lambda m: m.capture_screenshot(),
    "use_client": lambda m: m.use_client(None).__enter__(),
    "use_managed": lambda m: m.use_managed().__enter__(),
    "snapshot": lambda m: m.snapshot(),
    "stop": lambda m: m.stop(),
}


@pytest.mark.parametrize("name", sorted(AMBIGUOUS_CALLS))
def test_omitted_session_id_is_refused_when_several_sessions_are_live(rig, name):
    a, b = rig.start(), rig.start()
    before = [list(c.log) for c in rig.clients]
    with pytest.raises(BridgeError) as e:
        AMBIGUOUS_CALLS[name](rig.mgr)
    assert e.value.code == "session_ambiguous" and len(e.value.detail["sessions"]) == 2
    assert [list(c.log) for c in rig.clients] == before  # nothing was sent to either MAME
    assert all(p.calls == [] and p.rc is None for p in rig.procs)  # and nothing was stopped
    assert {s["session_id"] for s in e.value.detail["sessions"]} == {a["session_id"], b["session_id"]}


def test_omitted_session_id_selects_only_the_sole_live_session_and_nothing_is_remembered(rig):
    a = rig.start()
    assert write(rig)["client"] == a["session_id"]  # backward compatible with one session
    b = rig.start()
    with pytest.raises(BridgeError) as e:  # the most recently started session is NOT a default
        write(rig)
    assert e.value.code == "session_ambiguous"
    rig.mgr.stop(a["session_id"])
    assert write(rig)["client"] == b["session_id"]  # sole again
    rig.start()
    with pytest.raises(BridgeError) as e2:
        write(rig)
    assert e2.value.code == "session_ambiguous"


def test_unknown_and_stale_ids_never_select_another_session(rig):
    a, b = rig.start(), rig.start()
    with pytest.raises(BridgeError) as e:
        write(rig, {"session_id": "s1-nonexistent"})
    assert e.value.code == "unknown_session"
    with pytest.raises(BridgeError) as bad:
        write(rig, {"session_id": "../etc/passwd"})
    assert bad.value.code == "bad_argument"
    stopped = rig.mgr.stop(a["session_id"])
    assert stopped["stopped"] is True and [s["session_id"] for s in stopped["remaining_sessions"]] == [b["session_id"]]
    cb = rig.client_of(b)
    before = list(cb.log)
    for call in (lambda: write(rig, a), lambda: rig.mgr.release_inputs(session_id=a["session_id"]),
                 lambda: rig.mgr.save_checkpoint(session_id=a["session_id"])):
        with pytest.raises(BridgeError) as s:  # B is now the SOLE live session, but a stale explicit id must not fall back to it
            call()
        assert s.value.code == "session_ended" and s.value.detail["session"]["session_id"] == a["session_id"]
    assert cb.log == before and rig.proc_of(b).calls == []
    again = rig.mgr.stop(a["session_id"])  # stopping an already-ended session is a harmless report, not a second stop
    assert again["stopped"] is False and rig.proc_of(a).calls == ["terminate"]
    assert rig.mgr.snapshot(a["session_id"])["state"] == EXITED  # its own summary, not B's
    with pytest.raises(BridgeError) as u:
        rig.mgr.snapshot("s1-nonexistent")
    assert u.value.code == "unknown_session"


def test_an_exited_session_is_not_selected_by_omission_and_does_not_affect_others(rig):
    a, b = rig.start(), rig.start()
    rig.proc_of(a).rc = 7
    with pytest.raises(BridgeError) as e:
        write(rig, a)
    assert e.value.code == "session_exited" and e.value.detail["exit_code"] == 7
    assert write(rig)["client"] == b["session_id"]  # the exited session is not live: B is the sole live one
    assert write(rig, b)["client"] == b["session_id"]
    states = {s["session_id"]: s["state"] for s in rig.mgr.list_sessions()["sessions"]}
    assert states == {a["session_id"]: EXITED, b["session_id"]: READY}
    assert rig.mgr.stop(a["session_id"])["stopped"] is True and rig.proc_of(a).calls == []  # acknowledged, nothing killed
    assert rig.proc_of(b).calls == [] and write(rig, b)


def test_external_attachment_is_explicit_and_never_used_by_managed_only_operations(rig):
    ext = object()
    a = rig.start()
    with rig.mgr.use_client(ext, "external") as (client, managed):
        assert client is ext and managed is False
    with rig.mgr.use_client(ext, None) as (client, managed):  # unchanged: a lone managed session takes precedence over the attachment
        assert client is rig.client_of(a) and managed is True
    b = rig.start()
    with pytest.raises(BridgeError) as e:  # but with two managed sessions the attachment never breaks the tie
        rig.mgr.use_client(ext, None).__enter__()
    assert e.value.code == "session_ambiguous"
    with pytest.raises(BridgeError) as m:
        write(rig, {"session_id": "external"})
    assert m.value.code == "external_not_supported"
    with pytest.raises(BridgeError) as n:
        rig.mgr.use_client(None, "external").__enter__()
    assert n.value.code == "unknown_session"
    with pytest.raises(BridgeError) as s:
        rig.mgr.stop("external")
    assert s.value.code == "external_not_supported"
    assert b and all(p.calls == [] for p in rig.procs)


# ---------------------------------------------------------------------------------------------- limit and start logic
def test_session_limit_counts_live_sessions_and_refuses_before_launching(tmp_path):
    r = MRig(tmp_path, max_sessions=2)
    a, b = r.start(), r.start()
    with pytest.raises(BridgeError) as e:
        r.start()
    assert e.value.code == "session_limit_reached" and e.value.detail["max_sessions"] == 2 and len(e.value.detail["sessions"]) == 2
    assert len(r.launches) == 2 and len(r.procs) == 2 and r.mgr.list_sessions()["live_count"] == 2
    r.mgr.stop(a["session_id"])
    c = r.start()  # a stopped session frees its slot
    assert c["session_id"] not in (a["session_id"], b["session_id"]) and len(r.launches) == 3


def test_exited_and_failed_sessions_do_not_count_toward_the_limit(tmp_path):
    r = MRig(tmp_path, max_sessions=2)
    a, b = r.start(), r.start()
    r.proc_of(a).rc = 3
    c = r.start()  # a has exited: its slot is free, the record moves to history
    assert c["state"] == READY and {s["session_id"] for s in r.mgr.list_sessions()["sessions"]} == {b["session_id"], c["session_id"]}
    assert r.mgr.snapshot(a["session_id"])["state"] == EXITED


def test_a_start_that_is_still_starting_counts_toward_the_limit(tmp_path):
    r = MRig(tmp_path, max_sessions=1)
    entered, release = threading.Event(), threading.Event()
    orig = r._launch

    def slow(**kw):
        entered.set()
        release.wait(5)
        return orig(**kw)

    r.mgr._launch = slow
    out = {}
    t = threading.Thread(target=lambda: out.update(r=r.start()), daemon=True)
    t.start()
    assert entered.wait(2)
    with pytest.raises(BridgeError) as e:
        r.start()
    assert e.value.code == "session_limit_reached" and e.value.detail["sessions"][0]["state"] == "starting"
    assert len(r.launches) == 0  # the refused start launched nothing; the first is still inside launch
    release.set()
    t.join(5)
    assert out["r"]["state"] == READY and len(r.launches) == 1


def test_limit_is_exact_under_simultaneous_starts(tmp_path):
    r = MRig(tmp_path, max_sessions=3)
    results, lock = [], threading.Lock()
    gate = threading.Event()

    def go():
        gate.wait(2)
        try:
            res = r.start()
        except BridgeError as e:
            res = e
        with lock:
            results.append(res)

    threads = [threading.Thread(target=go, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(10)
    ok = [x for x in results if isinstance(x, dict)]
    refused = [x for x in results if isinstance(x, BridgeError)]
    assert len(ok) == 3 and len(refused) == 5 and {x.code for x in refused} == {"session_limit_reached"}
    assert len(r.launches) == 3 and len({kw["port"] for kw in r.launches}) == 3 and len({o["session_id"] for o in ok}) == 3
    assert len({kw["run_dir"] for kw in r.launches}) == 3


@pytest.mark.parametrize("env,expected,error", [
    ({}, 3, False), ({"MAMEMCP_MAX_SESSIONS": "5"}, 5, False), ({"MAMEMCP_MAX_SESSIONS": " 1 "}, 1, False), ({"MAMEMCP_MAX_SESSIONS": ""}, 3, False),
    ({"MAMEMCP_MAX_SESSIONS": "0"}, 3, True), ({"MAMEMCP_MAX_SESSIONS": "17"}, 3, True), ({"MAMEMCP_MAX_SESSIONS": "abc"}, 3, True),
    ({"MAMEMCP_MAX_SESSIONS": "-2"}, 3, True), ({"MAMEMCP_MAX_SESSIONS": "2.5"}, 3, True), ({"MAMEMCP_MAX_SESSIONS": "1e1"}, 3, True)])
def test_max_sessions_setting_is_validated(env, expected, error):
    cfg = SessionConfig.from_env({"MAMEMCP_MAME_DIR": "D:/x/mame", **env})
    assert cfg.max_sessions == expected and (cfg.config_error is not None) is error
    assert cfg.public()["max_sessions"] == expected and ("config_error" in cfg.public()) is error


@pytest.mark.parametrize("bad", [0, -1, 17, True, "3", 2.5, None])
def test_config_object_rejects_a_bad_limit(bad):
    with pytest.raises(ValueError):
        SessionConfig(mame_dir=None, rompaths=[], max_sessions=bad)


def test_an_invalid_limit_setting_refuses_to_launch_anything(tmp_path):
    r = MRig(tmp_path)
    r.cfg.config_error = "MAMEMCP_MAX_SESSIONS must be an integer 1..16, got 'abc'"
    with pytest.raises(BridgeError) as e:
        r.start()
    assert e.value.code == "bad_configuration" and "MAMEMCP_MAX_SESSIONS" in e.value.message and r.launches == [] and r.mgr.list_sessions()["sessions"] == []


# ---------------------------------------------------------------------------------------------- ports and identity
def test_a_port_held_by_another_session_is_never_picked_and_picking_is_bounded(rig):
    a = rig.start()
    seq = iter([a["port"], a["port"], 6001])
    rig.mgr._port_picker = lambda: next(seq)
    b = rig.start()
    assert b["port"] == 6001 and [kw["port"] for kw in rig.launches] == [a["port"], 6001]
    rig.mgr._port_picker = lambda: a["port"]  # always colliding: bounded, nothing launched
    with pytest.raises(BridgeError) as e:
        rig.start()
    assert e.value.code == "port_unavailable" and len(rig.launches) == 2 and rig.mgr.list_sessions()["live_count"] == 2
    rig.mgr._extra_reserved = frozenset({6002})  # the external attachment's port is reserved too
    seq2 = iter([6002, 6003])
    rig.mgr._port_picker = lambda: next(seq2)
    rig.cfg.max_sessions = 3
    assert rig.start()["port"] == 6003


def test_a_port_collision_after_launch_is_retried_on_a_new_port_and_never_attached(rig):
    a = rig.start()
    n = {"n": 0}
    orig = rig._launch

    def squat_first(**kw):
        p, info = orig(**kw)
        n["n"] += 1
        if n["n"] == 1:
            rig.ident.owners[kw["port"]] = {99999}  # another process answers on the chosen port
        return p, info

    rig.mgr._launch = squat_first
    b = rig.start()
    assert b["start_attempts"] == 2 and b["state"] == READY and len({kw["port"] for kw in rig.launches}) == 3
    assert b["port"] not in (a["port"], rig.launches[1]["port"])
    assert rig.procs[1].calls == ["terminate"] and rig.procs[2].calls == [] and rig.proc_of(a).calls == []
    assert write(rig, b)["client"] == b["session_id"] and write(rig, a)["client"] == a["session_id"]


def test_persistent_port_collisions_stop_after_the_attempt_bound_and_leave_other_sessions_alone(rig):
    a = rig.start()
    orig = rig._launch

    def squat_always(**kw):
        p, info = orig(**kw)
        rig.ident.owners[kw["port"]] = {99999}
        return p, info

    rig.mgr._launch = squat_always
    with pytest.raises(BridgeError) as e:
        rig.start()
    assert e.value.code == "bridge_owner_mismatch" and len(rig.launches) == 1 + session.MAX_START_ATTEMPTS
    assert all(p.calls == ["terminate"] for p in rig.procs[1:])  # every attempt's own process was stopped, none was attached
    assert rig.proc_of(a).calls == [] and write(rig, a)["client"] == a["session_id"]
    failed = [s for s in rig.mgr.list_sessions()["sessions"] if s["state"] == FAILED]
    assert len(failed) == 1 and failed[0]["failure"]["start_attempts"] == session.MAX_START_ATTEMPTS


def test_a_bridge_answering_with_another_sessions_id_is_refused(rig):
    a = rig.start()
    rig.unique_ids = False
    rig.client_kw["sid"] = a["session_id"]  # the new MAME's bridge reports session A's id
    with pytest.raises(BridgeError) as e:
        rig.start()
    assert e.value.code == "duplicate_session_id" and len(rig.launches) == 1 + session.MAX_START_ATTEMPTS
    assert rig.proc_of(a).calls == [] and rig.client_of(a).log.count("close") == 0
    assert write(rig, a)["client"] == a["session_id"]


# ---------------------------------------------------------------------------------------------- failure isolation and stop
def test_a_failed_start_leaves_existing_sessions_untouched(rig):
    a = rig.start()
    rig.client_kw["connect_errors"] = 10**6
    with pytest.raises(BridgeError) as e:
        rig.start(ready_timeout=0.2)
    assert e.value.code == "startup_timeout"
    rig.client_kw["connect_errors"] = 0
    assert rig.proc_of(a).calls == [] and write(rig)["client"] == a["session_id"]  # omission still selects the one live session
    assert rig.procs[1].calls == ["terminate"]
    assert [s["state"] for s in rig.mgr.list_sessions()["sessions"]].count(FAILED) == 1
    assert rig.start()["state"] == READY  # and a later start works


def test_stop_session_stops_only_the_resolved_session(rig):
    a, b, c = rig.start(), rig.start(), rig.start()
    rep = rig.mgr.stop(b["session_id"])
    assert rep["session_id"] == b["session_id"] and rep["process"]["terminated"] is True and rep["cleanup_failures"] == []
    assert [s["session_id"] for s in rep["remaining_sessions"]] == [a["session_id"], c["session_id"]]
    assert rig.proc_of(b).calls == ["terminate"] and rig.proc_of(a).calls == [] and rig.proc_of(c).calls == []
    assert rig.client_of(a).log.count("close") == 0 and rig.client_of(c).log.count("close") == 0
    assert write(rig, a)["client"] == a["session_id"] and write(rig, c)["client"] == c["session_id"]


def test_one_sessions_cleanup_failure_does_not_stop_the_others(rig):
    a, b = rig.start(), rig.start()
    rig.handlers[a["session_id"]] = {"input_cancel": BridgeError("mame_disconnected", "gone")}
    rep = rig.mgr.stop(a["session_id"])
    assert rep["input_cleanup"]["released"] is False and rep["process"]["terminated"] is True
    assert write(rig, b)["client"] == b["session_id"] and rig.proc_of(b).calls == []


# ---------------------------------------------------------------------------------------------- locking
def test_stop_of_one_session_does_not_wait_for_another_sessions_in_flight_call(rig):
    a, b = rig.start(), rig.start()
    entered, release = threading.Event(), threading.Event()

    def hold():
        with rig.mgr.use_client(None, b["session_id"]):
            entered.set()
            release.wait(5)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert entered.wait(2)
    t0 = time.monotonic()
    assert rig.mgr.stop(a["session_id"])["drained_in_flight_calls"] is True  # B's call is irrelevant to A's stop
    assert time.monotonic() - t0 < 1.0 and rig.proc_of(b).calls == []
    release.set()
    t.join(5)


def test_stopping_one_session_blocks_only_that_session(rig):
    a, b = rig.start(), rig.start()
    entered, release, done = threading.Event(), threading.Event(), threading.Event()

    def hold():
        with rig.mgr.use_client(None, a["session_id"]):
            entered.set()
            release.wait(5)

    threading.Thread(target=hold, daemon=True).start()
    assert entered.wait(2)
    threading.Thread(target=lambda: (rig.mgr.stop(a["session_id"]), done.set()), daemon=True).start()
    time.sleep(0.2)
    assert not done.is_set()  # A's stop waits for A's own call...
    t0 = time.monotonic()
    assert write(rig, b)["client"] == b["session_id"] and rig.mgr.list_sessions()["live_count"] == 2  # ...while B and the registry stay responsive
    assert rig.mgr.snapshot(b["session_id"])["state"] == READY and time.monotonic() - t0 < 1.0
    with pytest.raises(BridgeError) as e:  # new calls to the session being stopped are refused, not queued
        write(rig, a)
    assert e.value.code == "session_stopping"
    release.set()
    assert done.wait(3)


def test_state_changing_operations_are_serialised_per_session_not_globally(rig, monkeypatch):
    monkeypatch.setattr(operations, "EXCLUSIVE_WAIT_S", 0.2)
    a, b = rig.start(), rig.start()
    entered, release = threading.Event(), threading.Event()

    def slow(args):
        entered.set()
        release.wait(5)
        return {"client": a["session_id"], "op": "write_memory"}

    rig.handlers[a["session_id"]] = {"write_memory": slow}
    t = threading.Thread(target=lambda: write(rig, a), daemon=True)
    t.start()
    assert entered.wait(2)
    with pytest.raises(BridgeError) as e:  # same session: refused (serialised)
        write(rig, a)
    assert e.value.code == "operation_in_progress" and "write_memory" in e.value.message and a["session_id"] in e.value.message
    t0 = time.monotonic()
    assert write(rig, b)["client"] == b["session_id"]  # another session proceeds at once
    assert time.monotonic() - t0 < 0.15
    release.set()
    t.join(5)
    assert write(rig, a)["client"] == a["session_id"]


def test_no_global_lock_is_held_while_a_session_is_starting(rig):
    a = rig.start()
    entered, release = threading.Event(), threading.Event()

    class Slow(RecClient):
        def connect(self, timeout=15.0):
            entered.set()
            release.wait(5)
            return super().connect(timeout)

    Slow.rig = rig
    rig.client_cls = Slow
    out = {}
    t = threading.Thread(target=lambda: out.update(b=rig.start()), daemon=True)
    t.start()
    assert entered.wait(2)  # B is blocked in its startup wait
    t0 = time.monotonic()
    states = {s["state"] for s in rig.mgr.list_sessions()["sessions"]}
    assert states == {READY, "starting"} and write(rig, a)["client"] == a["session_id"]
    with pytest.raises(BridgeError) as e:
        write(rig)  # two live sessions (one still starting): ambiguous, immediately
    assert e.value.code == "session_ambiguous"
    assert rig.mgr.stop(a["session_id"])["stopped"] is True and time.monotonic() - t0 < 1.0
    release.set()
    t.join(5)
    assert out["b"]["state"] == READY


# ---------------------------------------------------------------------------------------------- shutdown
def test_shutdown_cleans_up_every_session_even_when_one_fails(rig):
    a, b, c = rig.start(), rig.start(), rig.start()
    rig.handlers[b["session_id"]] = {"input_cancel": RuntimeError("boom")}  # an unexpected error inside one session's cleanup
    rep = rig.mgr.shutdown()
    assert set(rep["sessions"]) == {a["run_id"], b["run_id"], c["run_id"]}
    assert all(rig.proc_of(x).calls == ["terminate"] for x in (a, b, c))  # B's process was still stopped
    assert any("boom" in f and b["session_id"] in f for f in rep["cleanup_failures"])
    assert rep["sessions"][a["run_id"]]["cleanup_failures"] == [] and rep["sessions"][c["run_id"]]["cleanup_failures"] == []
    assert rig.mgr.list_sessions()["sessions"] == [] and rig.mgr.shutdown() is None
    with pytest.raises(BridgeError) as e:
        rig.start()
    assert e.value.code == "shutting_down"


def test_shutdown_is_bounded_when_one_session_hangs(rig, monkeypatch):
    monkeypatch.setattr(session, "SHUTDOWN_BOUND_S", 0.5)
    a, b = rig.start(), rig.start()
    hang = threading.Event()
    rig.handlers[a["session_id"]] = {"input_cancel": lambda args: hang.wait(5) and {"state": "none", "was_holding": False, "release_errors": []}}
    t0 = time.monotonic()
    rep = rig.mgr.shutdown()
    assert time.monotonic() - t0 < 3.0
    assert rep["sessions"][a["run_id"]].get("timed_out") is True and rep["sessions"][b["run_id"]]["process"]["terminated"] is True
    assert rig.proc_of(b).calls == ["terminate"] and rig.mgr.list_sessions()["sessions"] == []
    hang.set()


# ---------------------------------------------------------------------------------------------- experiments and checkpoints
def test_experiment_ids_name_their_session():
    sid = "s1700000001-00000a"
    protocol.check_experiment_owner(f"exp-{sid}-001", sid)
    protocol.check_experiment_owner(None, sid)  # no id: nothing to compare (the latest experiment of the routed session)
    protocol.check_experiment_owner(f"exp-{sid}-001", None)  # session not known yet: the plugin decides
    protocol.check_experiment_owner("not-an-experiment-id", sid)  # unrecognised shape: left to the plugin
    with pytest.raises(BridgeError) as e:
        protocol.check_experiment_owner(f"exp-{sid}-001", "s1700000002-00000b")
    assert e.value.code == "experiment_from_other_session" and "Nothing was sent" in e.value.message


def test_a_checkpoint_id_never_selects_the_target_session(rig):
    a, b = rig.start(), rig.start()
    cid, src = make_earlier_session(rig)
    other_system, _ = make_earlier_session(rig, system="1942", run="1942-20250101-010101-abcdef")
    old_version, _ = make_earlier_session(rig, mame="0.288", run="shinobi-20250101-010101-abcdee")
    with pytest.raises(BridgeError) as e:
        rig.mgr.load_checkpoint(cid)  # even though exactly one session could take it, the id picks nobody
    assert e.value.code == "session_ambiguous"
    for bad, code in ((other_system, "checkpoint_incompatible"), (old_version, "checkpoint_incompatible")):
        with pytest.raises(BridgeError) as inc:
            rig.mgr.load_checkpoint(bad, session_id=b["session_id"])
        assert inc.value.code == code
    assert all(c.n("checkpoint_load") == 0 for c in rig.clients)
    rig.handlers[b["session_id"]] = {
        "checkpoint_load": load_ok,
        "session_info": {"bridge": {"session_id": b["session_id"]}, "state_epoch": 2, "execution": {"state": "debugger_stopped"},
                         "active_experiment": None, "unresolved_instrumentation": []}}
    out = rig.mgr.load_checkpoint(cid, session_id=b["session_id"])
    assert out["session_id"] == b["session_id"] and out["target_run_id"] == b["run_id"] and out["imported_from_earlier_session"] is True
    assert rig.client_of(b).n("checkpoint_load") == 1 and rig.client_of(a).n("checkpoint_load") == 0
    b_dest = Path(b["run_dir"]) / "home" / "sta" / "shinobi" / f"{cid}.sta"
    assert b_dest.is_file() and b_dest.read_bytes() == src.read_bytes()
    assert not (Path(a["run_dir"]) / "home" / "sta" / "shinobi" / f"{cid}.sta").exists()
    assert cid in rig.mgr.snapshot(b["session_id"])["checkpoints"] and cid not in rig.mgr.snapshot(a["session_id"])["checkpoints"]


def test_list_checkpoints_needs_no_selection_and_reports_compatibility_for_an_explicit_target(rig):
    a, b = rig.start(), rig.start()
    ok_id, _ = make_earlier_session(rig)
    bad_id, _ = make_earlier_session(rig, system="1942", run="1942-20250101-010101-abcdef")
    allc = rig.mgr.list_checkpoints()  # ambiguity-free: metadata only
    assert {c["checkpoint_id"] for c in allc["checkpoints"]} == {ok_id, bad_id} and allc["target_run_id"] is None
    assert set(allc["live_run_ids"]) == {a["run_id"], b["run_id"]}
    rel = {c["checkpoint_id"]: c["compatible"] for c in rig.mgr.list_checkpoints(session_id=b["session_id"])["checkpoints"]}
    assert rel == {ok_id: True, bad_id: False}
    with pytest.raises(BridgeError) as e:
        rig.mgr.list_checkpoints(session_id="s1-nonexistent")
    assert e.value.code == "unknown_session"


# ---------------------------------------------------------------------------------------------- advertised surface
def test_every_session_bound_tool_takes_an_optional_session_id():
    from mame_mcp import server
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    unbound = {"start_session", "list_sessions"}
    assert "list_sessions" in tools and set(tools) >= unbound
    for name, t in tools.items():
        props, required = t.input_schema["properties"], t.input_schema.get("required", [])
        if name in unbound:
            assert "session_id" not in props, name
        else:
            assert "session_id" in props and "session_id" not in required, name
