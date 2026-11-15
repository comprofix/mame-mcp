"""Helpers shared by the hardening validation scripts (test-only)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class Fixture:
    """Client for the TEST-ONLY mamemcp_fixture Lua plugin (file mailbox)."""

    def __init__(self, directory: str | Path, wake=None):
        self.wake = wake
        self.dir = Path(directory)
        self._n = 0

    def call(self, op: str, timeout: float = 15.0, **args: Any) -> dict[str, Any]:
        self._n += 1
        reply = self.dir / "reply.json"
        try:
            reply.unlink()
        except OSError:
            pass
        tmp = self.dir / f"cmd.{self._n}.tmp"
        tmp.write_text(json.dumps({"id": self._n, "op": op, "args": args}))
        os.replace(tmp, self.dir / "cmd.json")
        end = time.time() + timeout
        t0 = last_wake = time.time()
        while time.time() < end:
            if self.wake is not None and time.time() - t0 > 0.3 and time.time() - last_wake > 0.25:
                last_wake = time.time()
                self.wake()
            try:
                present = reply.exists()
            except OSError:
                present = False
            if present:
                try:
                    data = json.loads(reply.read_text())
                except (ValueError, OSError):
                    time.sleep(0.02)
                    continue
                if data.get("id") == self._n:
                    if not data.get("ok"):
                        raise RuntimeError(f"fixture op {op} failed: {data.get('error')}")
                    return data["result"]
            time.sleep(0.02)
        raise TimeoutError(f"fixture op {op} not answered in {timeout}s")


class Results:
    def __init__(self, name: str):
        self.name = name
        self.items: list[dict] = []

    def check(self, label: str, ok: bool, **evidence) -> bool:
        self.items.append({"check": label, "status": "PASS" if ok else "FAIL", "evidence": evidence})
        print(f"[{'PASS' if ok else 'FAIL'}] {label}  {json.dumps(evidence, default=str)[:320]}", flush=True)
        return ok

    def note(self, label: str, **evidence) -> None:
        self.items.append({"check": label, "status": "INFO", "evidence": evidence})
        print(f"[INFO] {label}  {json.dumps(evidence, default=str)[:320]}", flush=True)

    def save(self, path: Path, **extra) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**extra, "results": self.items}, indent=2, default=str))
        fails = [r for r in self.items if r["status"] == "FAIL"]
        passed = sum(1 for r in self.items if r["status"] == "PASS")
        print(f"\n{passed} passed, {len(fails)} failed -> {path}", flush=True)
        return 1 if fails else 0


def body_of(res) -> dict:
    """Result of an MCP tool call as a dict; a tool error becomes {'_error': {...}} (code, message, uncertain_state, detail)."""
    import re
    if res.is_error:
        m = re.search(r"\{.*\}", res.content[0].text, re.S)
        try:
            return {"_error": json.loads(m.group(0))} if m else {"_error": {"code": "?", "message": res.content[0].text}}
        except ValueError:
            return {"_error": {"code": "?", "message": res.content[0].text}}
    if res.structured_content is not None:
        return res.structured_content
    return json.loads(res.content[0].text)


def code_of(b: dict) -> str | None:
    return (b.get("_error") or {}).get("code")
