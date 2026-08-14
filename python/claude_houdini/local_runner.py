"""Local-model backend (Ollama), exposing the same Qt contract as ClaudeWorker.

Deliberately *chat only*: no tools, no HTTP bridge, no access to the scene. A
local 32-36B model is good for "what does this VEX do", "give me a wrangle that
…", "which SOP handles X" — and hopeless at multi-step tool orchestration. Ivan
picks the backend from the panel, so there is no auto-router guessing for him.

Costs no tokens and works offline.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from typing import Optional

from PySide6 import QtCore

from .cli_runner import StreamEvent

# "localhost" resolves to IPv6 first on Windows while Ollama listens on IPv4
# only, costing ~2s per request before it falls back. Always dial 127.0.0.1.
OLLAMA_URL = os.environ.get("CLAUDE_HOUDINI_OLLAMA", "http://127.0.0.1:11434")
LOCAL_MODEL_ENV = "CLAUDE_HOUDINI_LOCAL_MODEL"
LOCAL_MODEL_DEFAULT = "qwen3.6:latest"

SYSTEM_PROMPT = """\
Eres un asistente técnico de Houdini para Ivan, artista/TD de VFX (es-ES).
Respondes preguntas de conocimiento: VEX, nodos SOP/LOP/DOP, expresiones,
Python de Houdini (hou.*), formatos (Alembic, USD), y flujos de trabajo.

IMPORTANTE: en este modo NO tienes acceso a la escena de Houdini. No puedes
leer ni modificar nodos. Si la pregunta requiere inspeccionar o tocar la escena,
dilo claramente y sugiere cambiar al modelo de Anthropic en el desplegable.

Responde en español, conciso y al grano. Da el código directamente. No inventes
nombres de nodos ni de funciones VEX: si no estás seguro, dilo.
"""


def local_model() -> str:
    return os.environ.get(LOCAL_MODEL_ENV) or LOCAL_MODEL_DEFAULT


class LocalWorker(QtCore.QObject):
    """Mirror of ClaudeWorker's signals so the panel can swap backends."""

    event = QtCore.Signal(object)
    finished = QtCore.Signal(bool, str)

    def __init__(self, parent: QtCore.QObject | None = None):
        super().__init__(parent)
        self._history: list[dict] = []
        self._cancelled = threading.Event()
        self._active = False

    # ---------- same API surface as ClaudeWorker ----------

    def session_id(self) -> Optional[str]:
        return None

    def set_system_prompt(self, prompt: str) -> None:
        pass  # the local mode has its own, scene-less prompt

    def reset_session(self) -> None:
        self._history.clear()

    def cancel(self) -> None:
        if self._active:
            self._cancelled.set()
        # Ollama keeps a model resident for `keep_alive` (default 5 min) after
        # its last request, which is what left ~31GB of VRAM sitting there
        # after "cancel" — that button expects "stop and let go", not "stop
        # and hold the GPU hostage". keep_alive:0 requests immediate unload.
        self._unload_model()

    def shutdown(self) -> None:
        self.cancel()

    def _unload_model(self) -> None:
        model = local_model()

        def _do() -> None:
            try:
                # Documented unload pattern: /api/generate, no prompt,
                # keep_alive: 0. (/api/chat needs a non-empty `messages`.)
                req = urllib.request.Request(
                    f"{OLLAMA_URL}/api/generate",
                    data=json.dumps({"model": model, "keep_alive": 0}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=15).read()
            except Exception:
                pass  # best-effort; nothing the user needs to see if it fails

        threading.Thread(target=_do, name="ollama-unload", daemon=True).start()

    @QtCore.Slot(str)
    def send(self, prompt: str) -> None:
        self._cancelled.clear()
        self._active = True
        try:
            self._chat(prompt)
        except urllib.error.URLError as e:
            self.event.emit(StreamEvent("error", {
                "message": f"No pude hablar con Ollama en {OLLAMA_URL} ({e.reason}). "
                           "¿Está arrancado? Pruébalo con: ollama list"
            }))
            self.finished.emit(False, "ollama-unreachable")
        except Exception as e:
            self.event.emit(StreamEvent("error", {"message": f"Excepción: {e!r}"}))
            self.finished.emit(False, str(e))
        finally:
            self._active = False

    # ---------- implementation ----------

    def _chat(self, prompt: str) -> None:
        model = local_model()
        self._history.append({"role": "user", "content": prompt})

        payload = {
            "model": model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + self._history,
            "stream": True,
            "think": False,
        }
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        self.event.emit(StreamEvent("info", {"message": f"modelo local: {model}"}))

        answer: list[str] = []
        thinking = _ThinkFilter()

        with urllib.request.urlopen(req, timeout=600) as resp:
            for raw in resp:
                if self._cancelled.is_set():
                    self.event.emit(StreamEvent("info", {"message": "cancelado"}))
                    self.finished.emit(False, "cancelado")
                    return
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if chunk.get("error"):
                    self.event.emit(StreamEvent("error", {"message": str(chunk["error"])}))
                    self.finished.emit(False, str(chunk["error"]))
                    return

                piece = (chunk.get("message") or {}).get("content", "")
                if piece:
                    visible = thinking.feed(piece)
                    if visible:
                        answer.append(visible)
                        self.event.emit(StreamEvent("text_delta", {"text": visible}))

                if chunk.get("done"):
                    break

        tail = thinking.flush()
        if tail:
            answer.append(tail)
            self.event.emit(StreamEvent("text_delta", {"text": tail}))

        text = "".join(answer).strip()
        self._history.append({"role": "assistant", "content": text})
        self.event.emit(StreamEvent("text_done", {}))
        self.finished.emit(True, text)


class _ThinkFilter:
    """Drop <think>…</think> spans from a token stream.

    Qwen emits reasoning in those tags. `think: False` usually suppresses them,
    but not every build honours it, and a tag can arrive split across chunks —
    hence filtering the stream rather than the finished string.

    Call `flush()` when the stream ends: mid-stream the filter holds back any
    suffix that could still turn out to be the start of a tag, and that tail
    has to be released at the end or the answer loses its last few characters.
    """

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self) -> None:
        self._buf = ""
        self._inside = False

    def feed(self, chunk: str) -> str:
        self._buf += chunk
        return self._consume(final=False)

    def flush(self) -> str:
        return self._consume(final=True)

    def _consume(self, final: bool) -> str:
        out: list[str] = []
        while True:
            if self._inside:
                end = self._buf.find(self.CLOSE)
                if end == -1:
                    # Discard reasoning, but keep a possible partial close tag.
                    self._buf = "" if final else self._keep_suffix(self.CLOSE)
                    break
                self._buf = self._buf[end + len(self.CLOSE):]
                self._inside = False
                continue

            start = self._buf.find(self.OPEN)
            if start != -1:
                out.append(self._buf[:start])
                self._buf = self._buf[start + len(self.OPEN):]
                self._inside = True
                continue

            if final:
                out.append(self._buf)
                self._buf = ""
                break

            held = self._keep_suffix(self.OPEN)
            if held:
                out.append(self._buf[: len(self._buf) - len(held)])
            else:
                out.append(self._buf)
            self._buf = held
            break

        return "".join(out)

    def _keep_suffix(self, tag: str) -> str:
        """Longest suffix of the buffer that is a prefix of `tag`."""
        for i in range(min(len(tag) - 1, len(self._buf)), 0, -1):
            if self._buf.endswith(tag[:i]):
                return self._buf[-i:]
        return ""
