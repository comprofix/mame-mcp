"""Blocking client for the mamemcp Lua plugin (newline-delimited JSON over loopback TCP)."""
from __future__ import annotations

import itertools
import json
import socket
import threading
import time
from typing import Any

from .protocol import PROTOCOL_VERSION, BridgeError

LOOPBACK = "127.0.0.1"
RETRY_SAFE_OPS = frozenset({"session_info", "read_registers", "experiment_status", "input_status", "checkpoint_status", "list_inputs"})


class BridgeClient:
    """One connection to one MAME session.

    * Requests are serialised by a lock (a single in-flight request).
    * Every request has a unique id; responses with another id are stale and are counted and dropped.
    * The session id learned from the handshake is sent with every later request, so a
      restarted MAME (new session) is rejected rather than silently accepted.
    * Nothing is retried unless the caller marks the request `retry_safe` (see RETRY_SAFE_OPS). Only
      operations that are side-effect free and idempotent qualify: session_info, read_registers, input_status, checkpoint_status, list_inputs,
      experiment_status. State-changing ops and memory reads are never retried automatically.
      A retry happens at most once, only after request_timeout, only on the same connection and
      session; a connection loss or session change is raised, never retried. The first attempt's late
      reply (different request id) is discarded as stale.
    * `transport_events` records every timeout and retry (utc time, op, request ids) for provenance.
    """

    def __init__(self, port: int = 9411, host: str = LOOPBACK, default_timeout: float = 15.0, wake=None):
        if host not in (LOOPBACK, "localhost"):
            raise ValueError("bridge endpoint must be loopback only")
        self.host, self.port = host, port
        self.default_timeout = default_timeout
        self.read_retries = 0
        self.wake = wake  # optional callable(): nudge a debugger-stopped MAME (see winwake.py); returns posts made
        self.wake_posts = 0
        self.timeouts = 0
        self.last_request_id: str | None = None
        self.transport_events: list[dict[str, Any]] = []
        self.session_id: str | None = None
        self.stale_discarded = 0
        self._sock: socket.socket | None = None
        self._rbuf = b""
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self._client_tag = f"c{int(time.time()) & 0xFFFFFF:x}"

    # -- connection ---------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self, timeout: float = 15.0) -> dict[str, Any]:
        """Connect and perform a session_info handshake. Returns the session_info result.

        MAME's socket accepts one client at a time and only re-listens after the plugin has noticed the
        previous client leave, so a TCP connect can succeed without anyone serving it. The handshake
        is therefore retried on a *reset* connection, but a silent connection is waited on (abandoning
        it would hand the plugin's single accept slot to a dead socket again and again).
        """
        with self._lock:
            self.close(graceful=False)
            deadline = time.monotonic() + timeout
            last: BridgeError | OSError | None = None
            old = self.session_id
            while time.monotonic() < deadline:
                try:
                    self._sock = socket.create_connection((self.host, self.port), timeout=1.0)
                except OSError as e:
                    last = e
                    time.sleep(0.2)
                    continue
                self._rbuf = b""
                self.session_id = None  # accept whichever session answers the handshake
                try:
                    info = self.request("session_info", {}, timeout=max(0.2, deadline - time.monotonic()))
                except BridgeError as e:
                    last = e
                    self.close(graceful=False)
                    if e.code == "mame_disconnected":
                        # connection was reset while the plugin re-armed its listener: try again
                        time.sleep(0.2)
                        continue
                    if e.code == "request_timeout":
                        break
                    raise
                if old and old != self.session_id:
                    info["session_changed_from"] = old
                return info
            raise BridgeError("connect_failed", f"cannot establish a bridge session on {self.host}:{self.port}: {last}")

    def close(self, graceful: bool = True) -> None:
        """Close the connection. A graceful close tells the plugin to listen for a new client."""
        with self._lock:
            if self._sock is not None:
                if graceful:
                    try:
                        self.request("goodbye", {}, timeout=1.0)
                    except BridgeError:
                        pass
                try:
                    self._sock.close()
                except OSError:
                    pass
            self._sock = None
            self._rbuf = b""

    # -- request/response ---------------------------------------------------
    def _note(self, event: str, op: str, **kw: Any) -> None:
        self.transport_events.append({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, "op": op, **kw})
        del self.transport_events[:-200]

    def request(self, op: str, args: dict[str, Any] | None = None, *, timeout: float | None = None,
                mutating: bool = False, retry_safe: bool = False) -> dict[str, Any]:
        if retry_safe and (mutating or op not in RETRY_SAFE_OPS):
            raise ValueError(f"{op} is not a retry-safe operation")
        sock, sid = self._sock, self.session_id
        try:
            return self._request_once(op, args, timeout=timeout, mutating=mutating)
        except BridgeError as e:
            if e.code != "request_timeout":
                raise
            self.timeouts += 1
            self._note("timeout", op, retried=retry_safe, last_request_id=self.last_request_id)
            with self._lock:
                if not retry_safe or self._sock is None or self._sock is not sock or self.session_id != sid:
                    raise
                self.read_retries += 1
                self._note("retry", op, first_request_id=self.last_request_id, session_id=sid)
                return self._request_once(op, args, timeout=timeout, mutating=mutating)

    def _request_once(self, op: str, args: dict[str, Any] | None = None, *, timeout: float | None = None,
                      mutating: bool = False) -> dict[str, Any]:
        timeout = self.default_timeout if timeout is None else timeout
        with self._lock:
            if self._sock is None:
                raise BridgeError("not_connected", "no MAME connection; call connect first")
            req_id = f"{self._client_tag}-{next(self._ids)}"
            self.last_request_id = req_id
            msg: dict[str, Any] = {"v": PROTOCOL_VERSION, "id": req_id, "op": op, "args": args or {}}
            if self.session_id:
                msg["session_id"] = self.session_id
            data = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
            try:
                self._sock.sendall(data)
            except OSError as e:
                self.close(graceful=False)
                raise BridgeError("mame_disconnected", f"send failed ({e}); MAME probably exited",
                                  uncertain=mutating) from e
            deadline = time.monotonic() + timeout
            while True:
                line = self._read_line(deadline, op, mutating)
                if not line.strip():
                    continue  # plugin heartbeat
                try:
                    resp = json.loads(line)
                except ValueError:
                    self.stale_discarded += 1
                    continue
                if resp.get("id") != req_id:
                    self.stale_discarded += 1  # late answer to an earlier timed-out request, or noise
                    continue
                sid = resp.get("session_id")
                if self.session_id is None:
                    self.session_id = sid
                elif sid != self.session_id:
                    self.close(graceful=False)
                    raise BridgeError("session_mismatch",
                                      f"response from session {sid}, expected {self.session_id}",
                                      uncertain=mutating)
                if resp.get("ok"):
                    return resp["result"]
                err = resp.get("error") or {}
                raise BridgeError(err.get("code", "bridge_error"), err.get("message", "unknown error"),
                                  detail=err.get("detail"))

    def _read_line(self, deadline: float, op: str, mutating: bool) -> bytes:
        assert self._sock is not None
        t_wait, last_wake = time.monotonic(), 0.0
        while b"\n" not in self._rbuf:
            now = time.monotonic()
            if self.wake is not None and now - t_wait > 0.3 and now - last_wake > 0.25:
                # No answer yet: MAME's Windows debugger front-end may be blocked in GetMessage while stopped.
                last_wake = now
                try:
                    n = self.wake() or 0
                except Exception:  # a failing nudge must never break the request
                    n = 0
                if n and self.wake_posts == 0:
                    self._note("wake", op, request_id=self.last_request_id)
                self.wake_posts += n
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # The connection stays usable; a late reply will be discarded as stale by id.
                raise BridgeError(
                    "request_timeout",
                    f"no response to '{op}' in time; MAME may be busy, hung or not servicing callbacks",
                    uncertain=mutating,
                )
            self._sock.settimeout(min(remaining, 0.25))
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                continue
            except OSError as e:
                self.close(graceful=False)
                raise BridgeError("mame_disconnected", f"connection lost ({e}); MAME exited or crashed",
                                  uncertain=mutating) from e
            if not chunk:
                self.close(graceful=False)
                raise BridgeError("mame_disconnected", "MAME closed the connection (exited or crashed)",
                                  uncertain=mutating)
            self._rbuf += chunk
        line, self._rbuf = self._rbuf.split(b"\n", 1)
        return line

    # -- typed wrappers -----------------------------------------------------
    def session_info(self) -> dict[str, Any]:
        return self.request("session_info", retry_safe=True)

    def read_memory(self, args: dict[str, Any]) -> dict[str, Any]:
        # never auto-retried: even a side-effect-suppressed read may reach handlers that ignore the flag
        return self.request("read_memory", args)

    def read_registers(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.request("read_registers", args, retry_safe=True)

    def capture_breakpoint(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.request("capture_breakpoint", args, mutating=True)

    def experiment_status(self, experiment_id: str | None = None) -> dict[str, Any]:
        return self.request("experiment_status", {"experiment_id": experiment_id} if experiment_id else {}, retry_safe=True)

    def cancel_experiment(self, experiment_id: str) -> dict[str, Any]:
        return self.request("cancel_experiment", {"experiment_id": experiment_id}, mutating=True)

    def execution_control(self, action: str) -> dict[str, Any]:
        return self.request("execution_control", {"action": action}, mutating=True)

    def list_inputs(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.request("list_inputs", args, retry_safe=True)

    def write_memory(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.request("write_memory", args, mutating=True)
