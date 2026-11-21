"""Unit tests for the clean-exit request in managed-session teardown (fake processes, fake bridge clients, fake process table).
They prove the decision logic only: ordering, the single never-retried request, bounded waiting, identity rechecks before each
fallback action, and that an external attachment is never sent the request. Real MAME behaviour is covered by
scripts/mcp_exit_e2e.py (stop_session in three execution states) and scripts/mcp_shutdown_e2e.py (server shutdown)."""
import time
from pathlib import Path

import pytest

from mame_mcp import session
from mame_mcp.protocol import BridgeError
from test_multisession import FakeExternal, MRig
from test_session import _fast  # noqa: F401  (autouse fixture re-used)

ACCEPT = {"accepted": True}


@pytest.fixture
def rig(tmp_path):
    return MRig(tmp_path)


def exits(proc, reply=ACCEPT, rc=0):
    """A bridge `shutdown` handler that behaves like MAME: the reply is returned, then the process goes."""
    def h(args):
        proc.rc = rc
        return reply
    return h


def start(rig):
    info = rig.start()
    return info, rig.client_of(info), rig.proc_of(info)


def test_clean_exit_is_requested_after_inputs_and_captures_and_before_the_connection_closes(rig):
    info, c, p = start(rig)
    rig.handlers[info["session_id"]] = {"shutdown": exits(p)}
    r = rig.mgr.stop(info["session_id"])
    pr = r["process"]
    assert pr["exit_method"] == "clean_exit_request" and pr["exit_request"]["reply"] == "accepted" and pr["exit_request"]["exited"] is True
    assert pr["terminated"] is True and pr["exit_code"] == 0 and r["exit_code"] == 0 and "escalated_to_terminate" not in pr
    assert p.calls == [] and r["cleanup_failures"] == [] and "refused" not in pr  # no signal of any kind was needed
    log = c.log
    assert log.index("input_cancel") < log.index("shutdown") < log.index("close") and c.n("shutdown") == 1
    assert [a for o, a in c.calls if o == "shutdown"] == [{}]  # a fixed request: no arguments, no client-chosen content


def test_capture_is_finalised_before_the_exit_request(tmp_path):
    rig = MRig(tmp_path)
    rig.client_kw["active"] = "exp-1"
    info, c, p = start(rig)
    rig.handlers[info["session_id"]] = {"shutdown": exits(p)}
    r = rig.mgr.stop(info["session_id"])
    assert r["capture_cleanup"]["verified"] is True
    assert c.log.index("cancel_experiment") < c.log.index("shutdown") < c.log.index("close")


def test_connection_lost_during_the_exit_is_not_a_failure_and_the_request_is_not_repeated(rig):
    info, c, p = start(rig)

    def lost(args):
        p.rc = 0  # MAME exited and took the connection with it before the reply was read
        return BridgeError("mame_disconnected", "MAME closed the connection", uncertain=True)

    rig.handlers[info["session_id"]] = {"shutdown": lambda a: lost(a)}
    r = rig.mgr.stop(info["session_id"])
    pr = r["process"]
    assert pr["exit_method"] == "clean_exit_request" and pr["exit_request"]["reply"] == "connection_lost"
    assert pr["exit_request"]["error"]["code"] == "mame_disconnected" and p.calls == [] and c.n("shutdown") == 1
    assert r["cleanup_failures"] == [] and pr["terminated"] is True


def test_connection_lost_but_process_survives_falls_back_once_without_repeating_the_request(rig):
    info, c, p = start(rig)
    rig.handlers[info["session_id"]] = {"shutdown": BridgeError("mame_disconnected", "gone", uncertain=True)}
    r = rig.mgr.stop(info["session_id"])
    assert r["process"]["exit_request"]["reply"] == "connection_lost" and r["process"]["exit_method"] == "terminate"
    assert p.calls == ["terminate"] and c.n("shutdown") == 1 and r["cleanup_failures"] == []


def test_a_timeout_waits_a_bounded_time_then_falls_back_to_terminate(rig):
    info, c, p = start(rig)
    rig.handlers[info["session_id"]] = {"shutdown": BridgeError("request_timeout", "no reply", uncertain=True)}
    r = rig.mgr.stop(info["session_id"])
    pr = r["process"]
    assert pr["exit_request"]["reply"] == "timeout" and pr["exit_request"]["exited"] is False
    assert pr["exit_method"] == "terminate" and pr["escalated_to_terminate"] is True and p.calls == ["terminate"] and c.n("shutdown") == 1


def test_a_refused_request_falls_back_at_once_without_waiting(rig, monkeypatch):
    monkeypatch.setattr(session, "EXIT_WAIT", 30.0)
    info, c, p = start(rig)
    rig.handlers[info["session_id"]] = {"shutdown": BridgeError("internal_error", "boom")}
    t0 = time.monotonic()
    r = rig.mgr.stop(info["session_id"])
    assert time.monotonic() - t0 < 5.0  # the 30 s wait was not taken
    assert r["process"]["exit_request"]["reply"] == "error" and r["process"]["exit_method"] == "terminate" and p.calls == ["terminate"]
    assert r["cleanup_failures"] == []  # the process was stopped; the refused request is reported, not a cleanup failure


def test_an_unexpected_exception_in_the_request_never_prevents_stopping_the_process(rig):
    info, c, p = start(rig)
    rig.handlers[info["session_id"]] = {"shutdown": RuntimeError("unexpected")}
    r = rig.mgr.stop(info["session_id"])
    assert r["process"]["exit_request"]["reply"] == "error" and r["process"]["exit_method"] == "terminate" and p.calls == ["terminate"]


def test_accepted_but_ignored_escalates_terminate_then_kill_and_reports_it(rig, monkeypatch):
    monkeypatch.setattr(session, "TERMINATE_WAIT", 0.05)
    rig.proc_kw["exit_on_terminate"] = False
    info, c, p = start(rig)
    r = rig.mgr.stop(info["session_id"])
    pr = r["process"]
    assert pr["exit_request"]["reply"] == "accepted" and pr["exit_request"]["exited"] is False
    assert p.calls == ["terminate", "kill"] and pr["exit_method"] == "kill"
    assert pr["escalated_to_terminate"] is True and pr["escalated_to_kill"] is True and pr["still_running"] is False


def test_a_process_that_never_dies_is_reported_as_a_cleanup_failure(rig, monkeypatch):
    monkeypatch.setattr(session, "TERMINATE_WAIT", 0.05)
    monkeypatch.setattr(session, "KILL_WAIT", 0.05)
    info, c, p = start(rig)
    p.kill = lambda: p.calls.append("kill")  # kill has no effect
    p.exit_on_terminate = False
    r = rig.mgr.stop(info["session_id"])
    assert r["process"]["still_running"] is True and r["process"]["exit_method"] is None
    assert any("did not exit after kill" in f for f in r["cleanup_failures"])


@pytest.mark.parametrize("mutate,why", [
    (lambda rig, p: rig.ident.images.__setitem__(p.pid, "/bin/sleep"), "not the expected MAME runtime"),
    (lambda rig, p: rig.ident.created_map.__setitem__(p.pid, 1), "recycled pid"),
    (lambda rig, p: rig.ident.owners.__setitem__(5555, {1}), "bridge port"),
])
def test_identity_refusal_means_no_exit_request_and_no_signal(rig, mutate, why):
    info, c, p = start(rig)
    mutate(rig, p)
    r = rig.mgr.stop(info["session_id"])
    assert why in r["process"]["refused"] and "shutdown" not in c.log and c.n("shutdown") == 0 and p.calls == []
    assert "exit_request" not in r["process"] and any("NOT stopped" in f for f in r["cleanup_failures"])


def test_identity_is_rechecked_before_terminate_and_a_changed_process_is_left_alone(rig):
    info, c, p = start(rig)

    def swap_then_ignore(args):  # the pid is recycled while MAME ignores the request
        rig.ident.created_map[p.pid] = 12345
        return ACCEPT

    rig.handlers[info["session_id"]] = {"shutdown": swap_then_ignore}
    r = rig.mgr.stop(info["session_id"])
    assert "identity changed between verification and termination" in r["process"]["refused"] and p.calls == []
    assert any("NOT stopped" in f for f in r["cleanup_failures"])


def test_identity_is_rechecked_again_before_kill(rig, monkeypatch):
    monkeypatch.setattr(session, "TERMINATE_WAIT", 0.05)
    rig.proc_kw["exit_on_terminate"] = False
    info, c, p = start(rig)
    real = p.terminate

    def terminate_then_recycle():
        real()
        rig.ident.created_map[p.pid] = 999  # changes after terminate was sent, before kill

    p.terminate = terminate_then_recycle
    r = rig.mgr.stop(info["session_id"])
    assert p.calls == ["terminate"] and "identity changed before kill" in r["process"]["refused"]
    assert "escalated_to_kill" not in r["process"]


def test_a_process_that_exits_while_identity_is_rechecked_is_exited_not_a_failure(rig):
    info, c, p = start(rig)

    def refuse_and_vanish(args):
        p.rc = 3
        rig.ident.images.pop(p.pid)  # the image becomes unreadable because the process is gone
        return BridgeError("internal_error", "no")

    rig.handlers[info["session_id"]] = {"shutdown": refuse_and_vanish}
    r = rig.mgr.stop(info["session_id"])
    pr = r["process"]
    assert pr["exited_before_termination"] is True and pr["exit_method"] == "exited_without_our_action" and "refused" not in pr
    assert p.calls == [] and r["cleanup_failures"] == [] and pr["exit_code"] == 3


def test_no_exit_request_is_made_for_a_process_that_already_ended(rig):
    info, c, p = start(rig)
    p.rc = 9
    r = rig.mgr.stop(info["session_id"])
    assert "shutdown" not in c.log and c.n("shutdown") == 0 and p.calls == [] and r["cleanup_failures"] == []


def test_exit_request_applies_to_the_named_session_only(rig):
    a, ca, pa = start(rig)
    b, cb, pb = start(rig)
    rig.handlers[a["session_id"]] = {"shutdown": exits(pa)}
    rig.mgr.stop(a["session_id"])
    assert ca.n("shutdown") == 1 and cb.n("shutdown") == 0 and pb.rc is None and pb.calls == []


def test_shutdown_cleans_every_session_independently_with_mixed_outcomes(rig):
    a, ca, pa = start(rig)
    b, cb, pb = start(rig)
    c_, cc, pc = start(rig)
    rig.handlers[a["session_id"]] = {"shutdown": exits(pa)}
    rig.handlers[b["session_id"]] = {"shutdown": RuntimeError("bridge exploded")}
    rig.handlers[c_["session_id"]] = {"shutdown": BridgeError("request_timeout", "no reply", uncertain=True)}
    rep = rig.mgr.shutdown()["sessions"]
    assert rep[a["run_id"]]["process"]["exit_method"] == "clean_exit_request" and pa.calls == []
    assert rep[b["run_id"]]["process"]["exit_method"] == "terminate" and pb.calls == ["terminate"]
    assert rep[c_["run_id"]]["process"]["exit_method"] == "terminate" and pc.calls == ["terminate"]
    assert all(r["cleanup_failures"] == [] for r in rep.values()) and rig.mgr.list_sessions()["sessions"] == []


def test_an_external_attachment_is_never_sent_the_exit_request(rig):
    ext = FakeExternal()
    ext.connect()
    seen = []
    ext.request = lambda op, *a, **k: seen.append(op)  # would record any request a teardown made
    info, c, p = start(rig)
    with rig.mgr.use_client(ext, "external") as (client, managed):  # explicit use of the attachment is possible ...
        assert client is ext and managed is False
    with pytest.raises(BridgeError) as e:  # ... but it can never be stopped
        rig.mgr.stop("external")
    assert e.value.code == "external_not_supported"
    rig.handlers[info["session_id"]] = {"shutdown": exits(p)}
    rig.mgr.stop(info["session_id"])
    rig.mgr.shutdown()
    assert seen == [] and ext.calls == [] and ext.connected is True  # never asked to exit, never closed by a teardown


def test_teardown_reaches_only_launched_sessions_so_an_attachment_has_no_path_to_the_request():
    import inspect
    src = inspect.getsource(session.SessionManager._request_exit)
    assert "s.client.request(\"shutdown\"" in src and "external" not in src.replace("external attachment", "")


def test_the_exit_operation_is_internal_only():
    import asyncio
    from mame_mcp import server
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert not ({"shutdown", "exit", "quit", "stop_mame", "exit_mame"} & names)
    lua = (Path(session.__file__).parents[2] / "plugin" / "mamemcp" / "init.lua").read_text()
    assert lua.count("manager.machine:exit()") == 1 and "function ops.shutdown(args)" in lua
    body = lua.split("function ops.shutdown(args)")[1].split("end", 1)[0]
    assert "args" not in body.replace("(args)", "") and "load(" not in body and "dofile" not in body  # nothing client-controlled is used
    assert '"input_sequence", "shutdown" }' in lua  # advertised in the bridge capabilities


def test_the_lua_exit_waits_for_the_reply_to_be_flushed():
    lua = (Path(session.__file__).parents[2] / "plugin" / "mamemcp" / "init.lua").read_text()
    block = lua.split("if exit_after_flush and #outbuf == 0 then")[1].split("return\n\t\tend", 1)[0]
    assert "manager.machine:exit()" in block
    before = lua.split("if exit_after_flush and #outbuf == 0 then")[0]
    assert before.rstrip().endswith("end") and "outbuf = outbuf:sub(n + 1)" in before[-400:]  # the flush of the reply comes first
