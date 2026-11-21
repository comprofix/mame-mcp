"""State-changing operations on a managed MAME session: checkpoints, held inputs and RAM writes.

Mixed into SessionManager (session.py). Everything here is for a session this server OWNS: nothing is done to an
externally attached MAME, because ownership is what makes cleanup and session-owned file paths meaningful.

Rules shared by all of it:
  * One state-changing operation at a time (`exclusive`), so checkpoints, input presses/releases and RAM writes are
    serialised against each other. start/stop are serialised by the existing in-flight accounting (`use_managed`): a
    stop never interleaves with a bridge call, and every poll below re-checks that the session is still READY.
  * The plugin is the authority on whether an operation is allowed (execution state, active capture, unresolved
    instrumentation, held input, pending checkpoint); this module validates arguments first and never assumes an
    operation completed: it returns only what the plugin reports after completion.
  * Checkpoint files live in the session's own run directory (<run_dir>/home/sta/<system>/<id>.sta, MAME's
    -state_directory) with a metadata sidecar in <run_dir>/checkpoints/<id>.json. The id is
    <run_id>-cpNNN, so it names its owning session; loading an id from an earlier session copies the file into the
    current session after validating it. Nothing outside the configured runs directory is ever read.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from . import png, protocol
from .bridge import BridgeClient
from .protocol import BridgeError

if TYPE_CHECKING:  # pragma: no cover
    from .session import Session, SessionConfig

POLL_S = 0.1                 # polling interval for pending checkpoint / held-input waits
CHECKPOINT_WAIT_S = 20.0     # the plugin bounds a deferred operation at 10 s host / 90 emulated frames
EXCLUSIVE_WAIT_S = 30.0
MAX_LISTED_CHECKPOINTS = 100
MAX_SCANNED_SIDECARS = 400
SCREENSHOT_TIMEOUT_S = 20.0
MAX_SCREENSHOT_BYTES = 8 << 20
SCREENSHOT_DIRNAME = "screenshots"  # <evidence_dir>/screenshots; the plugin creates it and chooses every file name
_HEADER_BYTES = 0x20


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _hash_file(path: Path) -> tuple[int, str]:
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
            n += len(chunk)
    return n, h.hexdigest()


def _error_from(err: dict[str, Any], *, uncertain: bool = False) -> BridgeError:
    return BridgeError(err.get("code", "checkpoint_failed"), err.get("message", "checkpoint operation failed"),
                       detail=err.get("detail"), uncertain=uncertain)


class StateOperations:
    cfg: "SessionConfig"
    _cond: threading.Condition
    _shutting_down: bool

    def _init_operations(self) -> None:
        self._op_lock = threading.Lock()
        self._op_name: str | None = None

    # ------------------------------------------------------------------ serialisation
    @contextlib.contextmanager
    def exclusive(self, name: str, timeout: float | None = None) -> Iterator[None]:
        """One state-changing operation at a time. A second caller waits (bounded) and is then refused with a clear
        error rather than interleaving with the first."""
        if not self._op_lock.acquire(timeout=EXCLUSIVE_WAIT_S if timeout is None else timeout):
            raise BridgeError("operation_in_progress", f"{self._op_name or 'another state-changing operation'} is still running; "
                              f"{name} was not started. Retry when it has finished.")
        self._op_name = name
        try:
            yield
        finally:
            self._op_name = None
            self._op_lock.release()

    def _assert_ready(self, s: "Session") -> None:
        """Raised between polls so a stop_session / shutdown / MAME exit ends a wait promptly."""
        with self._cond:
            self._refresh_locked()  # type: ignore[attr-defined]
            if s.state == "exited":
                raise BridgeError("session_exited", f"the managed MAME exited ({s.exit_reason}); the operation was abandoned")
            if s.state != "ready" or self._shutting_down:
                raise BridgeError("session_stopping", f"the session is no longer ready ({s.state}); the operation was abandoned")

    # ------------------------------------------------------------------ checkpoints
    def _state_file(self, s: "Session", cid: str) -> Path:
        return s.run_dir / "home" / "sta" / s.system / f"{cid}.sta"

    @staticmethod
    def _sidecar(run_dir: Path, cid: str) -> Path:
        return run_dir / "checkpoints" / f"{cid}.json"

    def _ckpt_run(self, client: BridgeClient, s: "Session", op: str, args: dict[str, Any]) -> dict[str, Any]:
        """Start a checkpoint save/load and wait until the plugin reports it finished. The result is returned only
        after completion; a failed or timed-out operation raises."""
        res = client.request(op, args, timeout=20.0, mutating=True)
        if res.get("state") != "pending":
            return res
        deadline = time.monotonic() + CHECKPOINT_WAIT_S
        while True:
            time.sleep(POLL_S)
            self._assert_ready(s)
            st = client.request("checkpoint_status", {}, retry_safe=True)
            state = st.get("state")
            if state == "completed":
                return st
            if state == "failed":
                raise _error_from(st.get("error") or {}, uncertain=True)
            if state != "pending":
                raise BridgeError("checkpoint_failed", f"checkpoint status is '{state}' after a {op} was started", detail=st, uncertain=True)
            if time.monotonic() > deadline:
                raise BridgeError("checkpoint_timeout", f"{op} did not finish within {CHECKPOINT_WAIT_S:g} s; MAME may be hung. "
                                  "Check session_info.checkpoint before doing anything else.", detail=st, uncertain=True)

    def save_checkpoint(self, label: str | None = None) -> dict[str, Any]:
        label = protocol.check_label(label)
        with self.exclusive("save_checkpoint"), self.use_managed() as (client, s):
            s.checkpoint_seq += 1  # an id is consumed even if the attempt fails: a file may exist for it
            cid = f"{s.run_id}-cp{s.checkpoint_seq:03d}"
            res = self._ckpt_run(client, s, "checkpoint_save", {"id": cid})
            if res.get("id") != cid or res.get("kind") != "save":
                raise BridgeError("checkpoint_failed", f"plugin reported {res.get('kind')}/{res.get('id')} for save {cid}", detail=res, uncertain=True)
            path = self._owned_path(s, res.get("path"))
            size, digest = _hash_file(path)
            if res.get("size") not in (None, size):
                raise BridgeError("checkpoint_failed", f"state file is {size} bytes on disk but MAME reported {res.get('size')}", detail={"path": str(path)}, uncertain=True)
            meta = {
                "id": cid, "label": label, "system": s.system, "run_id": s.run_id, "session_id": s.session_id,
                "created_utc": _utc(), "mame_version": res.get("mame_version") or s.mame_version,
                "frame": res.get("frame"), "emu_time_s": res.get("emu_time_s"),
                "execution_at_save": res.get("execution_before"), "mode": res.get("mode"),
                "fingerprint": res.get("fingerprint"), "fingerprint_coverage": res.get("fingerprint_coverage"), "header": res.get("header"),
                "file": {"name": path.name, "size": size, "sha256": digest},
            }
            side = self._sidecar(s.run_dir, cid)
            try:
                side.parent.mkdir(parents=True, exist_ok=True)
                side.write_text(json.dumps(meta, indent=2))
            except OSError as e:
                raise BridgeError("checkpoint_metadata_failed", f"the state file was written but its metadata could not be: {e}",
                                  detail={"path": str(path)}) from e
            s.checkpoints[cid] = meta
            out = self._public(meta, s)
            out.update(state="completed", mode=res.get("mode"), execution_before=res.get("execution_before"), execution=res.get("execution"),
                       frames_run_during_operation=res.get("frames_run_during_operation"),
                       note=("saved immediately while MAME was stopped; execution state unchanged" if res.get("mode") == "immediate"
                             else "MAME could not save at that instant, so emulation ran briefly until it did and the previous state was restored; "
                                  "the recorded frame is a range and a later load can only be verified by frame window"))
            return out

    def _owned_path(self, s: "Session", reported: Any) -> Path:
        if not isinstance(reported, str):
            raise BridgeError("checkpoint_failed", "the plugin reported no state file path", uncertain=True)
        p = Path(reported).resolve()
        if not p.is_relative_to(s.run_dir.resolve()):
            raise BridgeError("checkpoint_path_outside_session", f"state file {p} is outside this session's run directory", uncertain=True)
        if not p.is_file():
            raise BridgeError("checkpoint_failed", f"MAME reported a state file that does not exist: {p}", uncertain=True)
        return p

    @staticmethod
    def _public(meta: dict[str, Any], s: "Session | None" = None, state_file: Path | None = None) -> dict[str, Any]:
        fp = meta.get("fingerprint") or {}
        out = {
            "checkpoint_id": meta["id"], "label": meta.get("label"), "system": meta.get("system"), "run_id": meta.get("run_id"),
            "created_utc": meta.get("created_utc"), "frame": meta.get("frame"), "emu_time_s": meta.get("emu_time_s"),
            "execution_at_save": meta.get("execution_at_save"), "size_bytes": (meta.get("file") or {}).get("size"),
            "sha256": (meta.get("file") or {}).get("sha256"), "mame_version": meta.get("mame_version"),
            "fingerprint_exact": fp.get("exact", True), "fingerprint_coverage": meta.get("fingerprint_coverage"),
            "current_session": bool(s is not None and meta.get("run_id") == s.run_id),
        }
        if meta.get("imported_from"):
            out["imported_from"] = meta["imported_from"]
        if state_file is not None:
            out["path"] = str(state_file)
        elif s is not None and meta.get("run_id") == s.run_id:
            out["path"] = str(s.run_dir / "home" / "sta" / s.system / meta["file"]["name"])
        return out

    def _locate(self, s: "Session", cid: str, run: str) -> tuple[dict[str, Any], Path | None]:
        """Metadata for `cid` and, when it belongs to an earlier session, the source state file to import."""
        if cid in s.checkpoints:
            return s.checkpoints[cid], None
        if run == s.run_id:
            raise BridgeError("checkpoint_not_found", f"this session has no checkpoint {cid}; list_checkpoints shows what exists")
        runs = self.cfg.runs_dir.resolve()
        origin = (runs / run).resolve()
        if origin.parent != runs:  # the id regex already forbids separators; this is the belt to that braces
            raise BridgeError("bad_argument", "checkpoint id does not name a session directory under the runs directory")
        side = self._sidecar(origin, cid)
        try:
            meta = json.loads(side.read_text())
        except (OSError, ValueError) as e:
            raise BridgeError("checkpoint_not_found", f"no readable checkpoint metadata for {cid} ({type(e).__name__}); list_checkpoints shows what exists") from e
        if not isinstance(meta, dict) or meta.get("id") != cid or meta.get("run_id") != run or not isinstance(meta.get("file"), dict) \
                or not isinstance(meta.get("fingerprint"), dict) or not isinstance(meta.get("system"), str):
            raise BridgeError("checkpoint_incompatible", f"metadata for {cid} is malformed or does not match its id")
        src = origin / "home" / "sta" / meta["system"] / f"{cid}.sta"
        if not src.resolve().is_relative_to(origin):
            raise BridgeError("checkpoint_incompatible", f"state file path for {cid} escapes its session directory")
        if not src.is_file():
            raise BridgeError("checkpoint_not_found", f"the state file for {cid} is missing: {src}")
        return meta, src

    def load_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        m = protocol.check_checkpoint_id(checkpoint_id)
        cid, run = checkpoint_id, m.group("run")
        with self.exclusive("load_checkpoint"), self.use_managed() as (client, s):
            meta, src = self._locate(s, cid, run)
            if meta.get("system") != s.system:
                raise BridgeError("checkpoint_incompatible", f"checkpoint {cid} is for machine '{meta.get('system')}', this session runs '{s.system}'")
            if meta.get("mame_version") and s.mame_version and meta["mame_version"] != s.mame_version:
                raise BridgeError("checkpoint_incompatible", f"checkpoint {cid} was saved by MAME {meta['mame_version']}, this session runs {s.mame_version}; "
                                  "state files are not portable across versions")
            dest = self._state_file(s, cid)
            imported = src is not None
            if imported:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
            if not dest.is_file():
                raise BridgeError("checkpoint_not_found", f"the state file for {cid} is missing from this session: {dest}")
            size, digest = _hash_file(dest)
            want = meta["file"]
            if size != want.get("size") or digest != want.get("sha256"):
                if imported:
                    with contextlib.suppress(OSError):
                        dest.unlink()
                raise BridgeError("checkpoint_corrupt", f"state file for {cid} does not match its recorded size/SHA-256; refusing to load it",
                                  detail={"expected": {"size": want.get("size"), "sha256": want.get("sha256")}, "actual": {"size": size, "sha256": digest}})
            if imported:
                s.checkpoints[cid] = {**meta, "imported_from": str(src)}
            res = self._ckpt_run(client, s, "checkpoint_load", {"id": cid, "expect": meta["fingerprint"]})
            if res.get("id") != cid or res.get("kind") != "load":
                raise BridgeError("checkpoint_failed", f"plugin reported {res.get('kind')}/{res.get('id')} for load {cid}", detail=res, uncertain=True)
            # Identity refresh: the load happens inside the same MAME process, so the bridge session must be unchanged.
            info = client.request("session_info", {}, retry_safe=True)
            sid = (info.get("bridge") or {}).get("session_id")
            if sid != s.session_id:
                raise BridgeError("session_changed", f"MAME session changed from {s.session_id} to {sid} during the load", uncertain=True)
            out = {
                "state": "completed", "checkpoint_id": cid, "label": meta.get("label"), "imported_from_earlier_session": imported,
                "session_id": sid, "state_epoch": info.get("state_epoch"), "mode": res.get("mode"),
                "verification": res.get("verification"), "execution_before": res.get("execution_before"),
                "execution": info.get("execution"), "frames_run_during_operation": res.get("frames_run_during_operation"),
                "invalidated_experiments": res.get("invalidated_experiments") or [],
                "active_experiment": info.get("active_experiment"),
                "note": "the emulated state was replaced; experiment ids from before this load are refused as stale (their evidence files remain on disk)",
            }
            return out

    def list_checkpoints(self, include_previous_sessions: bool = True) -> dict[str, Any]:
        """Checkpoints of the current session (when there is one) and, optionally, of earlier sessions under the runs
        directory. Reads metadata only; works with no MAME running."""
        with self._cond:
            s = self._current  # type: ignore[attr-defined]
            current = s.run_id if s is not None else None
            mine = [self._public(m, s) for m in (s.checkpoints.values() if s is not None else [])]
        out: list[dict[str, Any]] = sorted(mine, key=lambda r: r["created_utc"] or "", reverse=True)
        scanned = 0
        if include_previous_sessions:
            runs = self.cfg.runs_dir
            files: list[Path] = []
            try:
                files = sorted(runs.glob("*/checkpoints/*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_SCANNED_SIDECARS]
            except OSError:
                pass
            for f in files:
                scanned += 1
                if f.parent.parent.name == current:
                    continue
                try:
                    meta = json.loads(f.read_text())
                    if protocol.CHECKPOINT_ID.fullmatch(meta["id"]) and meta["id"] == f.stem and meta["run_id"] == f.parent.parent.name:
                        out.append(self._public(meta))
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        return {"current_run_id": current, "checkpoints": out[:MAX_LISTED_CHECKPOINTS], "truncated": len(out) > MAX_LISTED_CHECKPOINTS,
                "sidecars_scanned": scanned, "note": "load_checkpoint accepts any listed id whose machine and MAME version match the running session"}

    # ------------------------------------------------------------------ inputs
    def press_inputs(self, inputs: Any, frames: Any, wait: bool = True, max_wait_s: float | None = None) -> dict[str, Any]:
        args = protocol.press_args(inputs, frames)
        if not isinstance(wait, bool):
            raise BridgeError("bad_argument", "wait must be true or false")
        if max_wait_s is None:
            max_wait_s = min(60.0, args["frames"] / 30.0 + 5.0)
        if isinstance(max_wait_s, bool) or not isinstance(max_wait_s, (int, float)) or not 0 < max_wait_s <= 120:
            raise BridgeError("bad_argument", "max_wait_s must be a number in (0, 120]")
        with self.exclusive("press_inputs"), self.use_managed() as (client, _s):
            res = client.request("input_press", args, mutating=True)
        if not wait or res.get("state") != "holding" or res.get("waiting_for_emulation"):
            res["waited"] = False
            if res.get("waiting_for_emulation"):
                res["note"] = ("execution is stopped, so no frame can elapse: the inputs are held and the frame count starts when emulation runs "
                               "(execution_control resume); they are released after the frames, by release_inputs, or by stop_session")
            return res
        return self._wait_input(time.monotonic() + max_wait_s)

    def _wait_input(self, deadline: float) -> dict[str, Any]:
        while True:
            time.sleep(POLL_S)
            try:
                with self.use_managed() as (client, _s):  # in-flight only per poll, so stop_session is never held up by a wait
                    st = client.request("input_status", {}, retry_safe=True)
            except BridgeError:
                self._cancel_quietly()  # an error while the inputs are held must not leave them held
                raise
            if st.get("state") != "holding":
                st["waited"] = True
                return st
            if (st.get("execution") or {}).get("state") != "running":
                st.update(waited=True, note="execution stopped while the inputs were held: no frame elapses until emulation runs; they remain held (bounded) until released")
                return st
            if time.monotonic() > deadline:
                st.update(waited=True, note="still holding after max_wait_s (emulation slower than expected); the plugin releases after the requested frames, or call release_inputs")
                return st

    def _cancel_quietly(self) -> None:
        try:
            with self.use_managed() as (client, _s):
                client.request("input_cancel", {}, timeout=5.0, mutating=True)
        except BridgeError:
            pass  # session ended or stopping: teardown / the plugin's own disconnect handling releases

    def release_inputs(self) -> dict[str, Any]:
        with self.exclusive("release_inputs"), self.use_managed() as (client, _s):
            return client.request("input_cancel", {}, mutating=True)

    @staticmethod
    def _release_inputs(client: BridgeClient, timeout: float) -> dict[str, Any]:
        """Teardown policy for held inputs (stop_session and server shutdown): cancel, never resume or pause anything."""
        out: dict[str, Any] = {"released": False}
        try:
            r = client.request("input_cancel", {}, timeout=timeout, mutating=True)
            out.update(was_holding=r.get("was_holding"), state=r.get("state"), end_reason=r.get("end_reason"),
                       release_errors=r.get("release_errors") or [])
            out["released"] = not out["release_errors"]
        except BridgeError as e:
            out["error"] = e.to_dict()
        return out

    # ------------------------------------------------------------------ RAM writes
    def write_memory(self, cpu: str, space: str, address: Any, value: Any, width: Any) -> dict[str, Any]:
        args = protocol.write_memory_args(cpu, space, address, value, width)
        with self.exclusive("write_memory"), self.use_managed() as (client, _s):
            return client.request("write_memory", args, mutating=True)

    # ------------------------------------------------------------------ screenshots
    def capture_screenshot(self, screen: Any = None) -> tuple[dict[str, Any], bytes]:
        """Ask the plugin to write one PNG of the emulated screen into <evidence>/screenshots, then validate it. Returns
        (metadata, png_bytes). Runs under the same one-at-a-time lock as checkpoints, input presses and RAM writes (so a
        capture never overlaps a checkpoint load) and as an in-flight call, so stop_session waits for it. It never pauses,
        resumes or steps emulation: it reports the state it found and whether it was unchanged afterwards."""
        args = protocol.screenshot_args(screen)
        with self.exclusive("capture_screenshot"), self.use_managed() as (client, s):
            res = client.request("screenshot", args, timeout=SCREENSHOT_TIMEOUT_S, mutating=True)
            path = self._owned_screenshot(s, res.get("path"))
            data = path.read_bytes()
            if len(data) > MAX_SCREENSHOT_BYTES:
                raise BridgeError("screenshot_invalid", f"{path.name} is {len(data)} bytes, above the {MAX_SCREENSHOT_BYTES} limit",
                                  detail={"path": str(path)})
            try:
                info = png.inspect_png(data)
            except png.PngError as e:
                raise BridgeError("screenshot_invalid", f"MAME wrote {path.name} but it is not a valid PNG: {e}", detail={"path": str(path)}) from e
            try:
                pixels = png.summarise_pixels(data)
            except png.PngError as e:  # structurally valid but not decodable here: still returned, flagged
                pixels = {"pixel_summary_unavailable": str(e)}
            meta = self._screenshot_meta(s, res, path, data, info, pixels)
            side = path.with_suffix(".json")
            try:
                side.write_text(json.dumps(meta, indent=2))
                meta["sidecar_path"] = str(side)
            except OSError as e:
                meta["sidecar_error"] = f"metadata sidecar could not be written: {e}"
            return meta, data

    @staticmethod
    def _owned_screenshot(s: "Session", reported: Any) -> Path:
        if not isinstance(reported, str):
            raise BridgeError("screenshot_failed", "the plugin reported no screenshot path", uncertain=True)
        folder = (s.evidence_dir / SCREENSHOT_DIRNAME).resolve()
        p = Path(reported).resolve()
        if p.parent != folder or p.suffix.lower() != ".png":
            raise BridgeError("screenshot_path_outside_session",
                              f"screenshot {p} is not a PNG directly inside this session's screenshot directory {folder}", uncertain=True)
        if not p.is_file():
            raise BridgeError("screenshot_failed", f"MAME reported a screenshot that does not exist: {p}", uncertain=True)
        return p

    @staticmethod
    def _screenshot_meta(s: "Session", res: dict[str, Any], path: Path, data: bytes, info: "png.PngInfo",
                         pixels: dict[str, Any]) -> dict[str, Any]:
        nw, nh, rot = res.get("native_width"), res.get("native_height"), res.get("orientation_degrees")
        expect = (nh, nw) if rot in (90, 270) else (nw, nh)
        matches = None if None in (nw, nh, rot) else (info.width, info.height) == expect
        before, after = res.get("execution") or {}, res.get("execution_after") or {}
        unchanged = (res.get("frame_after_capture") == res.get("current_frame") and before.get("state") == after.get("state")) \
            if before and after else None
        return {
            "state": "captured", "system": s.system, "session_id": s.session_id, "run_id": s.run_id, "screen": res.get("screen"),
            "evidence_path": str(path), "file_name": path.name, "file_size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "mime_type": "image/png", "sequence": res.get("sequence"), "captured_utc": _utc(),
            "image": {**info.summary(), **pixels},
            "geometry": {
                "native_width": nw, "native_height": nh, "orientation_degrees": rot,
                "image_matches_native_after_rotation": matches,
                "rotation": "the image is rotated to the displayed orientation: a screen MAME rotates 90 or 270 degrees is a portrait image "
                            "(width and height swapped relative to native_*), not the raw landscape frame buffer",
                "scaling": "none: one image pixel per emulated screen pixel (no aspect-ratio correction, filtering or zoom)",
                "cropping": "none beyond MAME's visible area for the screen; no bezel, artwork, overlay, debugger or desktop is included",
            },
            "frame": {
                "image_frame": res.get("image_frame"), "current_frame": res.get("current_frame"), "frames_behind": res.get("frames_behind"),
                "freshness": res.get("freshness"), "freshness_note": res.get("freshness_note"), "frames_since_load": res.get("frames_since_load"),
                "meaning": "image_frame is the screen's frame counter when MAME last finished rendering the screen; current_frame is the counter "
                           "at capture time. The image shows what the screen displayed; it does not by itself establish what the game is doing.",
            },
            "execution": before, "execution_after": after, "clock": res.get("clock"),
            "emulation_unchanged_by_capture": unchanged, "state_epoch": res.get("state_epoch"), "frameskip": res.get("frameskip"),
        }
