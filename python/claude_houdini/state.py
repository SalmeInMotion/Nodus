"""Shared mutable state across the panel and the HTTP server.

`auto_mode` controls whether destructive tools require a confirmation modal.
When True, the server applies destructive operations immediately and the
panel only logs them in the chat history.

Persisted to a small JSON file under SESSIONS_DIR so the preference
survives Houdini restarts.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from . import config

_lock = threading.Lock()
_STATE_FILE: Path = config.SESSIONS_DIR / "state.json"

_state: dict = {
    "auto_mode": False,
    "dev_mode": False,
    # "claude" (agentic, sees the scene) or "local" (Ollama, chat only)
    "backend": "claude",
    "anthropic_model": "claude-opus-5",
    "local_model": "",
}


def _load() -> None:
    if _STATE_FILE.is_file():
        try:
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _state.update({k: v for k, v in data.items() if k in _state})
        except Exception:
            pass


def _save() -> None:
    try:
        _STATE_FILE.write_text(json.dumps(_state, indent=2), encoding="utf-8")
    except Exception:
        pass


_load()


def auto_mode() -> bool:
    with _lock:
        return bool(_state.get("auto_mode", False))


def set_auto_mode(enabled: bool) -> None:
    with _lock:
        _state["auto_mode"] = bool(enabled)
        _save()


def dev_mode() -> bool:
    with _lock:
        return bool(_state.get("dev_mode", False))


def set_dev_mode(enabled: bool) -> None:
    with _lock:
        _state["dev_mode"] = bool(enabled)
        _save()


def backend() -> str:
    with _lock:
        return str(_state.get("backend", "claude"))


def set_backend(name: str) -> None:
    with _lock:
        _state["backend"] = str(name)
        _save()


def anthropic_model() -> str:
    with _lock:
        return str(_state.get("anthropic_model", "claude-opus-5"))


def set_anthropic_model(name: str) -> None:
    with _lock:
        _state["anthropic_model"] = str(name)
        _save()


def local_model() -> str:
    with _lock:
        return str(_state.get("local_model", ""))


def set_local_model(name: str) -> None:
    with _lock:
        _state["local_model"] = str(name)
        _save()
