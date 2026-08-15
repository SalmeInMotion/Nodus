"""Local-model backend (Ollama), exposing the same Qt contract as ClaudeWorker.

Deliberately *chat only*: no tools, no HTTP bridge, no access to the scene. A
local 32-36B model is good for "what does this VEX do", "give me a wrangle that
…", "which SOP handles X" — and hopeless at multi-step tool orchestration. The
backend is picked explicitly in the panel, so no auto-router has to guess.

Costs no tokens and works offline.
"""

from __future__ import annotations

import json
import os
import re
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


# ---------- model discovery (ported from VEXgraph's providers.py) ----------

# Below ~4B parameters a model answers confidently wrong about Houdini —
# invented VEX functions, answers in the wrong language. Not offered at all.
_MINIMUM_BILLIONS = 4.0


def _ollama_answers(timeout: int = 2) -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=timeout):
            return True
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def start_ollama(wait: float = 25.0) -> tuple[bool, str]:
    """Start Ollama if it is not answering. Detached: it outlives this request."""
    import shutil
    import subprocess
    import time

    if _ollama_answers():
        return True, ""
    executable = shutil.which("ollama")
    if executable is None:
        return False, ("Ollama is not installed (no `ollama` on PATH). "
                       "Install it from ollama.com, or use a Claude backend.")
    options: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        options["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                    | getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        options["start_new_session"] = True
    try:
        subprocess.Popen([executable, "serve"], **options)
    except OSError as exc:
        return False, f"Could not start Ollama: {exc}"
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if _ollama_answers():
            return True, ""
        time.sleep(0.4)
    return False, f"Ollama started but did not answer within {wait:.0f}s."


def _billions(parameters: str) -> float:
    m = re.match(r"\s*([\d.]+)\s*([BM])", str(parameters), re.IGNORECASE)
    if not m:
        return 0.0
    size = float(m.group(1))
    return size / 1000 if m.group(2).upper() == "M" else size


def _can_chat(name: str, timeout: int = 2) -> bool:
    """Ollama lists embedding models (bge-m3…) next to chat models; sending
    those a conversation is a bare 400. /api/show tells them apart."""
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/show",
        data=json.dumps({"model": name}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            caps = json.loads(resp.read().decode("utf-8")).get("capabilities")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return True   # can't tell -> keep it
    return not caps or "completion" in caps


def installed_local_models(timeout: int = 2) -> list[dict]:
    """Chat-capable, trustworthy-sized models Ollama has pulled, best-effort.

    Returns [] silently when Ollama is down: this feeds a dropdown at panel
    startup and must never block or spawn processes just to fill a menu.
    """
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=timeout) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return []
    out = []
    for model in tags.get("models", ()):
        name = model.get("name")
        if not name or not _can_chat(name):
            continue
        params = (model.get("details") or {}).get("parameter_size", "")
        if _billions(params) and _billions(params) < _MINIMUM_BILLIONS:
            continue
        out.append({"name": name,
                    "gb": round(int(model.get("size", 0) or 0) / 1e9, 1),
                    "parameters": params})
    return sorted(out, key=lambda m: -m["gb"])


def _system_prompt() -> str:
    """Built per call so a change of language/name needs no Houdini restart."""
    from .system_prompt import LANGUAGE_ENV

    language = os.environ.get(LANGUAGE_ENV, "").strip()
    lang_line = (f"Always reply in {language}."
                 if language else
                 "Reply in the same language the user writes to you in.")

    return f"""\
You are a technical Houdini assistant for an experienced VFX artist/TD.
You answer knowledge questions: VEX, SOP/LOP/DOP nodes, expressions, Houdini
Python (`hou`), formats (Alembic, USD) and workflows.

IMPORTANT: in this mode you have NO access to the Houdini scene. You cannot
read or modify nodes. If a question requires inspecting or touching the scene,
say so plainly and suggest switching to one of the Anthropic models in the
dropdown.

{lang_line} Be concise and to the point. Give the code directly. Do not invent
node names or VEX functions: if you are not sure, say so.
"""


def local_model() -> str:
    return os.environ.get(LOCAL_MODEL_ENV) or LOCAL_MODEL_DEFAULT


def _docs_context(prompt: str) -> str:
    """Documentation sections relevant to the prompt, or "" when none/absent."""
    try:
        from . import docs_corpus
        if not docs_corpus.available():
            return ""
        hits = docs_corpus.search(prompt, max_sections=3, max_chars=6000)
        if not hits:
            return ""
        blocks = "\n\n---\n\n".join(h["text"] for h in hits)
        return ("Reference sections from the official Houdini documentation "
                "installed on this machine. Ground your answer in these when "
                "they are relevant; say so when they are not:\n\n" + blocks)
    except Exception:
        return ""   # grounding must never break the chat


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
                "message": f"Could not reach Ollama at {OLLAMA_URL} ({e.reason}). "
                           "Is it running? Check with: ollama list"
            }))
            self.finished.emit(False, "ollama-unreachable")
        except Exception as e:
            self.event.emit(StreamEvent("error", {"message": f"Exception: {e!r}"}))
            self.finished.emit(False, str(e))
        finally:
            self._active = False

    # ---------- implementation ----------

    def _chat(self, prompt: str) -> None:
        model = local_model()

        # "Ollama is not running — go start it" is an errand the tool can run
        # itself (one detached process, ~a second).
        if not _ollama_answers():
            self.event.emit(StreamEvent("info", {"message": "starting Ollama…"}))
            ok, why = start_ollama()
            if not ok:
                raise RuntimeError(why)

        self._history.append({"role": "user", "content": prompt})

        # Ground the local model in the offline docs corpus when there is one.
        # A 30B model without tools invents VEX functions; three whole doc
        # sections in context is the cheapest cure. Per-call, never stored in
        # history — grounding one answer must not bloat the rest of the chat.
        messages = [{"role": "system", "content": _system_prompt()}]
        grounding = _docs_context(prompt)
        if grounding:
            messages.append({"role": "system", "content": grounding})
        messages += self._history

        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "think": False,
        }
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        self.event.emit(StreamEvent("info", {"message": f"local model: {model}"}))

        answer: list[str] = []
        thinking = _ThinkFilter()

        with urllib.request.urlopen(req, timeout=600) as resp:
            for raw in resp:
                if self._cancelled.is_set():
                    self.event.emit(StreamEvent("info", {"message": "cancelled"}))
                    self.finished.emit(False, "cancelled")
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
