"""Unit tests for BridgeClient lifecycle logic using a fake line-oriented server on loopback.

These cover timeout / stale-response / disconnect / session-mismatch / no-retry behaviour only.
They do not prove anything about MAME: see scripts/validate.py for that.
"""
import json
import socket
import threading
import time

import pytest

from mame_mcp.bridge import BridgeClient
from mame_mcp.protocol import BridgeError


class FakeBridge:
    """Replies per a script: each handler gets the request dict and returns list[str] lines to send (or None to stay silent)."""

    def __init__(self, handler, port=0):
        self.handler = handler
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(2)
        self.port = self.srv.getsockname()[1]
        self.received = []
        self.conn = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            self.conn = conn
            buf = b""
            while True:
                try:
                    d = conn.recv(4096)
                except OSError:
                    break
                if not d:
                    break
                buf += d
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    req = json.loads(line)
                    self.received.append(req)
                    for out in self.handler(req) or []:
                        conn.sendall(out.encode() + b"\n")

    def close(self):
        self.srv.close()
        if self.conn:
            self.conn.close()


def ok(req, sid="S1", result=None):
    return json.dumps({"v": 1, "id": req["id"], "session_id": sid, "ok": True, "result": result or {"x": 1}})


def test_handshake_and_session_pinning():
    fb = FakeBridge(lambda r: [ok(r, "S1")])
    c = BridgeClient(port=fb.port)
    c.connect(timeout=3)
    assert c.session_id == "S1"
    c.request("read_memory", {})
    assert fb.received[-1]["session_id"] == "S1"  # later requests carry the session id
    c.close(graceful=False)


def test_session_mismatch_detected_when_mame_restarted():
    sids = iter(["S1", "S2"])
    fb = FakeBridge(lambda r: [ok(r, next(sids))])
    c = BridgeClient(port=fb.port)
    c.connect(timeout=3)
    with pytest.raises(BridgeError) as e:
        c.request("session_info", mutating=True)
    assert e.value.code == "session_mismatch" and e.value.uncertain is True
    assert not c.connected


def test_timeout_then_stale_response_is_discarded_and_not_retried():
    state = {"hold": None}

    def handler(r):
        if r["op"] == "slow":
            state["hold"] = r
            return None  # no reply yet
        out = [ok(state["hold"], "S1", {"late": True})] if state["hold"] else []
        state["hold"] = None
        return out + [ok(r, "S1", {"fresh": True})]

    fb = FakeBridge(handler)
    c = BridgeClient(port=fb.port)
    # handshake needs a reply: use a first handler call that is not 'slow'
    c.connect(timeout=3)
    with pytest.raises(BridgeError) as e:
        c.request("slow", timeout=0.3, mutating=True)
    assert e.value.code == "request_timeout" and e.value.uncertain is True
    n_slow = sum(1 for r in fb.received if r["op"] == "slow")
    res = c.request("other")
    assert res == {"fresh": True}
    assert c.stale_discarded == 1
    assert n_slow == 1 and sum(1 for r in fb.received if r["op"] == "slow") == 1  # never re-sent


def test_disconnect_reports_mame_disconnected():
    fb = FakeBridge(lambda r: [ok(r)])
    c = BridgeClient(port=fb.port)
    c.connect(timeout=3)
    fb.conn.close()
    time.sleep(0.2)
    with pytest.raises(BridgeError) as e:
        c.request("session_info")
    assert e.value.code == "mame_disconnected"
    with pytest.raises(BridgeError) as e2:
        c.request("session_info")
    assert e2.value.code == "not_connected"


def test_error_response_maps_to_bridge_error_without_uncertainty():
    def handler(r):
        if r["op"] == "boom":
            return [json.dumps({"id": r["id"], "session_id": "S1", "ok": False, "error": {"code": "too_large", "message": "m"}})]
        return [ok(r)]

    fb = FakeBridge(handler)
    c = BridgeClient(port=fb.port)
    c.connect(timeout=3)
    with pytest.raises(BridgeError) as e:
        c.request("boom", mutating=True)
    assert (e.value.code, e.value.uncertain) == ("too_large", False)


def test_blank_heartbeat_lines_ignored():
    fb = FakeBridge(lambda r: ["", "  ", ok(r)])
    c = BridgeClient(port=fb.port)
    c.connect(timeout=3)
    assert c.request("x") == {"x": 1}


def test_non_loopback_rejected():
    with pytest.raises(ValueError):
        BridgeClient(host="0.0.0.0")


# ---- retry policy ---------------------------------------------------------------------------------
def _slow_first(op_name):
    """Handler that ignores the first `op_name` request until the second arrives, then answers both."""
    st = {"held": []}

    def handler(r):
        if r["op"] != op_name:
            return [ok(r)]
        st["held"].append(r)
        if len(st["held"]) == 1:
            return None
        first, second = st["held"][0], st["held"][1]
        return [ok(first, "S1", {"which": "first"}), ok(second, "S1", {"which": "second"})]
    return handler, st


def test_retry_safe_read_retries_once_and_late_first_reply_is_stale():
    handler, st = _slow_first("session_info")
    fb = FakeBridge(lambda r: [ok(r)] if len(fb_state) == 0 else handler(r))
    fb_state = []
    c = BridgeClient(port=fb.port)
    c.connect(timeout=3)
    fb_state.append(1)
    res = c.request("session_info", timeout=0.3, retry_safe=True)
    assert res == {"which": "second"}  # the answer to the retry, not the late first reply
    assert c.read_retries == 1 and c.timeouts == 1 and c.stale_discarded == 1
    assert [e["event"] for e in c.transport_events] == ["timeout", "retry"]
    ids = [r["id"] for r in fb.received if r["op"] == "session_info"][-2:]
    assert ids[0] != ids[1]


def test_mutating_and_memory_ops_are_never_retried():
    fb = FakeBridge(lambda r: [ok(r)] if r["op"] == "session_info" else None)
    c = BridgeClient(port=fb.port)
    c.connect(timeout=3)
    for op, kw in (("capture_breakpoint", {"mutating": True}), ("read_memory", {})):
        with pytest.raises(BridgeError) as e:
            c.request(op, timeout=0.2, **kw)
        assert e.value.code == "request_timeout"
        assert sum(1 for r in fb.received if r["op"] == op) == 1
    assert c.read_retries == 0
    with pytest.raises(ValueError):
        c.request("read_memory", retry_safe=True)
    with pytest.raises(ValueError):
        c.request("cancel_experiment", mutating=True, retry_safe=True)


def test_retry_does_not_cross_a_session_change():
    sids = {"n": 0}

    def handler(r):
        sids["n"] += 1
        if sids["n"] == 1:
            return [ok(r, "S1")]            # handshake
        if sids["n"] == 2:
            return None                      # first attempt: silence
        return [ok(r, "S2")]                 # retry answered by a different session
    fb = FakeBridge(handler)
    c = BridgeClient(port=fb.port)
    c.connect(timeout=3)
    with pytest.raises(BridgeError) as e:
        c.request("session_info", timeout=0.3, retry_safe=True)
    assert e.value.code == "session_mismatch" and not c.connected
    assert c.read_retries == 1


def test_retry_not_attempted_if_connection_lost_during_wait():
    fb = FakeBridge(lambda r: [ok(r)] if r["op"] == "session_info" and len(fb.received) == 1 else None)
    c = BridgeClient(port=fb.port)
    c.connect(timeout=3)
    threading.Timer(0.1, lambda: fb.conn.close()).start()
    with pytest.raises(BridgeError) as e:
        c.request("session_info", timeout=2, retry_safe=True)
    assert e.value.code == "mame_disconnected" and c.read_retries == 0


def test_wake_hook_runs_only_while_waiting_and_failures_are_contained():
    state = {"held": None, "calls": 0, "armed": False}

    def handler(r):
        if state["armed"] and r["op"] == "session_info":
            state["held"] = r  # stay silent until the wake hook has run twice
            return None
        return [ok(r)]

    def wake():
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("nudge failed")  # must not break the request
        if state["calls"] == 2 and state["held"]:
            fb.conn.sendall(ok(state["held"]).encode() + bytes([10]))
        return 1

    fb = FakeBridge(handler)
    c = BridgeClient(port=fb.port, wake=wake)
    c.connect(timeout=3)
    assert state["calls"] == 0  # fast replies never trigger a nudge
    state["armed"] = True
    assert c.request("session_info", timeout=5) == {"x": 1}
    assert state["calls"] >= 2 and c.wake_posts >= 1

# --- reconnect to a MAME whose plugin has not re-listened yet (debugger-stopped: it runs only when nudged) -----------------------
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_connect_nudges_a_stopped_mame_until_it_listens_and_sends_only_the_handshake():
    port, state = _free_port(), {"wakes": 0, "bridge": None}

    def wake():  # the plugin re-listens from a periodic callback, which runs only after the Nth nudge
        state["wakes"] += 1
        if state["wakes"] == 3 and state["bridge"] is None:
            state["bridge"] = FakeBridge(lambda r: [ok(r, "S1")], port=port)
        return 1

    c = BridgeClient(port=port, wake=wake)
    t0 = time.monotonic()
    info = c.connect(timeout=8)
    assert info == {"x": 1} and c.session_id == "S1" and state["wakes"] == 3 and c.wake_posts == 3
    assert time.monotonic() - t0 < 4
    assert [r["op"] for r in state["bridge"].received] == ["session_info"]  # nothing but the read-only handshake was sent
    c.close(graceful=False)


def test_connect_without_a_wake_hook_stays_bounded_and_reports_connect_failed():
    c = BridgeClient(port=_free_port())
    t0 = time.monotonic()
    with pytest.raises(BridgeError) as e:
        c.connect(timeout=1.0)
    assert e.value.code == "connect_failed" and time.monotonic() - t0 < 3 and c.wake_posts == 0 and not c.connected


def test_connect_nudges_are_rate_limited_bounded_by_the_deadline_and_failures_are_contained():
    calls = []

    def wake():
        calls.append(time.monotonic())
        raise RuntimeError("a failing nudge must never break connect")

    c = BridgeClient(port=_free_port(), wake=wake)
    t0 = time.monotonic()
    with pytest.raises(BridgeError) as e:
        c.connect(timeout=1.6)
    assert e.value.code == "connect_failed" and time.monotonic() - t0 < 4
    assert 2 <= len(calls) <= 7 and all(b - a >= 0.24 for a, b in zip(calls, calls[1:]))  # about 4 per second, none while it was answered


def test_connect_does_not_nudge_a_mame_that_answers_at_once():
    fb = FakeBridge(lambda r: [ok(r, "S1")])
    woken = []
    c = BridgeClient(port=fb.port, wake=lambda: woken.append(1) or 1)
    c.connect(timeout=3)
    assert woken == [] and c.wake_posts == 0
    c.close(graceful=False)
