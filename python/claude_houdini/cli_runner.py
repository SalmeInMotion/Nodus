"""Wrapper around the `claude` CLI subprocess.

One process stays alive for the whole panel session. Prompts go in as
stream-json lines on stdin; events come back on stdout and are re-emitted as Qt
signals for the panel.

Why persistent: the previous design spawned `claude --print` per prompt, paying
process boot plus a full system-prompt round trip every turn, which was
noticeable on every single answer. With `--input-format stream-json` the CLI
keeps reading turns off stdin, so the conversation state lives in the process
and there is no `--resume` dance.

`--include-partial-messages` adds `stream_event` frames carrying `text_delta`
chunks, which is what makes the answer appear as it is written.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Optional

from PySide6 import QtCore

from . import config


@dataclass
class StreamEvent:
    # "text_delta" | "text" | "tool_use" | "tool_result" | "info" | "error"
    kind: str
    payload: dict


# Houdini exports these so its own embedded interpreter finds its stdlib. If the
# `claude` subprocess inherits them, any `python` it launches from the Bash tool
# loads Houdini's stdlib against the system binary and dies with
# "AssertionError: SRE module mismatch". Strip them at the boundary.
_PYTHON_ENV_LEAKS = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONEXECUTABLE",
    "PYTHONNOUSERSITE",
)

# Warn (do not kill) if a turn produces nothing for this long.
_WATCHDOG_S = 180


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    for var in _PYTHON_ENV_LEAKS:
        env.pop(var, None)
    env.setdefault("CI", "1")
    return env


class ClaudeWorker(QtCore.QObject):
    """Owns the persistent `claude` process. Emits events to the panel."""

    event = QtCore.Signal(object)         # StreamEvent
    finished = QtCore.Signal(bool, str)   # ok, final_text_or_error

    def __init__(self, system_prompt: str, parent: QtCore.QObject | None = None):
        super().__init__(parent)
        self._system_prompt = system_prompt
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._session_id: Optional[str] = None
        self._turn_active = False
        self._got_delta = False
        self._watchdog: Optional[threading.Timer] = None

    # ---------- public API (called from the panel) ----------

    def session_id(self) -> Optional[str]:
        return self._session_id

    def set_system_prompt(self, prompt: str) -> None:
        """Swap the system prompt; forces a fresh process on the next turn."""
        with self._lock:
            if prompt != self._system_prompt:
                self._system_prompt = prompt
                self._kill_process()

    def reset_session(self) -> None:
        """Drop all conversation state by restarting the process."""
        with self._lock:
            self._kill_process()
            self._session_id = None

    def cancel(self) -> None:
        """Abort the current turn. The next prompt starts a new process."""
        with self._lock:
            was_active = self._turn_active
            self._kill_process()
        if was_active:
            self.event.emit(StreamEvent("info", {"message": "cancelled"}))
            self._end_turn(False, "cancelled")

    def shutdown(self) -> None:
        with self._lock:
            self._kill_process()

    @QtCore.Slot(str)
    def send(self, prompt: str) -> None:
        try:
            with self._lock:
                self._ensure_process()
                proc = self._proc
                assert proc is not None and proc.stdin is not None
                self._turn_active = True
                self._got_delta = False
                msg = {"type": "user",
                       "message": {"role": "user", "content": prompt}}
                proc.stdin.write(json.dumps(msg) + "\n")
                proc.stdin.flush()
            self._arm_watchdog()
        except (BrokenPipeError, OSError) as e:
            # Process died between turns (crash, or the user killed it).
            self.event.emit(StreamEvent("error", {
                "message": f"The `claude` process exited ({e}). Try again - it will relaunch itself."
            }))
            with self._lock:
                self._kill_process()
            self._end_turn(False, "process died")
        except FileNotFoundError:
            self.event.emit(StreamEvent("error", {
                "message": "Could not find the `claude` binary. It must be on the PATH of the "
                           f"Houdini process, or set {config.CLAUDE_CLI_ENV} to its full path."
            }))
            self._end_turn(False, "binary-not-found")
        except Exception as e:
            self.event.emit(StreamEvent("error", {"message": f"Exception: {e!r}"}))
            self._end_turn(False, str(e))

    # ---------- process lifecycle ----------

    def _ensure_process(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return

        cli = os.environ.get(config.CLAUDE_CLI_ENV, config.CLAUDE_CLI_DEFAULT)
        cmd = [
            cli,
            "--print",
            "--model", config.model(),
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",                 # required by the CLI for stream-json
            "--include-partial-messages",
            "--append-system-prompt", self._system_prompt,
            "--allowed-tools", "Bash,Read,Grep,Glob,WebSearch,WebFetch",
            "--permission-mode", "bypassPermissions",
        ]

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=_clean_env(),
            cwd=str(config.WORKSPACE_DIR),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        self._reader = threading.Thread(
            target=self._read_stdout, args=(self._proc,),
            name="claude-stdout", daemon=True)
        self._reader.start()
        threading.Thread(
            target=self._drain_stderr, args=(self._proc,),
            name="claude-stderr", daemon=True).start()

        self.event.emit(StreamEvent("info", {
            "message": f"claude process started ({config.model()})"
        }))

    def _kill_process(self) -> None:
        proc, self._proc = self._proc, None
        self._reader = None
        self._cancel_watchdog()
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.close()
        except Exception:
            pass
        # terminate() only kills the CLI itself; it spawns children, so on
        # Windows take the whole tree down.
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                proc.terminate()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ---------- watchdog ----------

    def _arm_watchdog(self) -> None:
        self._cancel_watchdog()
        t = threading.Timer(_WATCHDOG_S, self._on_watchdog)
        t.daemon = True
        self._watchdog = t
        t.start()

    def _cancel_watchdog(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    def _on_watchdog(self) -> None:
        if self._turn_active:
            self.event.emit(StreamEvent("info", {
                "message": f"still working ({_WATCHDOG_S}s without finishing) - "
                           "cancel it if it looks stuck"
            }))

    # ---------- reader threads ----------

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.strip()
            if line:
                self.event.emit(StreamEvent("info", {"message": f"[stderr] {line}"}))

    def _read_stdout(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                self.event.emit(StreamEvent("info", {"message": f"[raw] {line[:200]}"}))
                continue
            try:
                self._dispatch(evt)
            except Exception as e:
                self.event.emit(StreamEvent("error", {"message": f"dispatch: {e!r}"}))

        # stdout closed => process gone.
        if self._turn_active:
            self.event.emit(StreamEvent("error", {
                "message": "The `claude` process exited unexpectedly."
            }))
            self._end_turn(False, "proceso terminado")

    # ---------- event dispatch ----------

    def _dispatch(self, evt: dict) -> None:
        t = evt.get("type")

        if t == "stream_event":
            self._dispatch_partial(evt.get("event") or {})
            return

        if t == "system":
            if evt.get("subtype") == "init":
                sid = evt.get("session_id")
                if sid:
                    self._session_id = sid
                    self.event.emit(StreamEvent("info", {"message": f"session: {sid}"}))
            return

        if t == "assistant":
            msg = evt.get("message") or {}
            for block in msg.get("content", []):
                bt = block.get("type")
                if bt == "text":
                    # Text already arrived as deltas; only emit it whole if
                    # partial messages were unavailable for this turn.
                    if not self._got_delta:
                        self.event.emit(StreamEvent("text", {"text": block.get("text", "")}))
                elif bt == "tool_use":
                    self.event.emit(StreamEvent("tool_use", {
                        "name": block.get("name"),
                        "input": block.get("input"),
                    }))
            return

        if t == "user":
            msg = evt.get("message") or {}
            for block in msg.get("content", []):
                if block.get("type") == "tool_result":
                    content = block.get("content")
                    if isinstance(content, list):
                        content = "\n".join(
                            c.get("text", "") for c in content if c.get("type") == "text"
                        )
                    self.event.emit(StreamEvent("tool_result", {
                        "content": content,
                        "is_error": block.get("is_error", False),
                    }))
            return

        if t == "result":
            self._end_turn(not evt.get("is_error", False), evt.get("result") or "")
            return

        if t == "rate_limit_event":
            info = evt.get("rate_limit_info") or {}
            self.event.emit(StreamEvent("info", {
                "message": f"[rate limit] {info.get('status')} "
                           f"uso={info.get('utilization')}"
            }))
            return

        self.event.emit(StreamEvent("info", {"message": f"[evt {t}]"}))

    def _dispatch_partial(self, inner: dict) -> None:
        it = inner.get("type")

        if it == "message_start":
            self._got_delta = False
            return

        if it == "content_block_delta":
            delta = inner.get("delta") or {}
            if delta.get("type") == "text_delta":
                text = delta.get("text", "")
                if text:
                    self._got_delta = True
                    self.event.emit(StreamEvent("text_delta", {"text": text}))
            return

        if it == "content_block_stop":
            if self._got_delta:
                self.event.emit(StreamEvent("text_done", {}))
            return

    # ---------- turn bookkeeping ----------

    def _end_turn(self, ok: bool, final: str) -> None:
        self._cancel_watchdog()
        if not self._turn_active:
            return
        self._turn_active = False
        if self._got_delta:
            self.event.emit(StreamEvent("text_done", {}))
        self.finished.emit(ok, final)
