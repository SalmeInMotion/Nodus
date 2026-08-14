"""Lightweight HTTP server exposing Houdini operations to the Claude CLI.

- Listens on 127.0.0.1 only.
- Requires bearer token (`Authorization: Bearer <token>`).
- All `hou.*` calls are marshaled into Houdini's main thread via hdefereval.
- Destructive endpoints prompt the user in Houdini before executing.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import hdefereval

from . import config, state, tools
from .confirmation import confirm

_server: ThreadingHTTPServer | None = None
_server_thread: threading.Thread | None = None
_token: str | None = None
_port: int | None = None          # the port actually bound (fallback may move it)
_started_at: str | None = None
_lock = threading.Lock()

# When the default port is taken (another Houdini got there first), these are
# tried in order. 8743-8751 are deliberately absent: they are reserved for
# pinned special instances (8743/8744 = the SuperCache test sandboxes).
_FALLBACK_PORTS = (8752, 8753, 8754, 8755, 8756, 8757, 8758, 8759)


def get_token() -> str:
    """Session token. A fixed token can be pinned via CLAUDE_HOUDINI_TOKEN."""
    global _token
    if _token is None:
        _token = os.environ.get(config.AUTH_TOKEN_ENV) or secrets.token_urlsafe(24)
    return _token


def port() -> int:
    return _port if _port is not None else config.HTTP_PORT


def base_url() -> str:
    return f"http://{config.HTTP_HOST}:{port()}"


def is_running() -> bool:
    return _server is not None


def session_label() -> str:
    """Human-readable tag distinguishing this session from siblings.

    CLAUDE_HOUDINI_LABEL wins; the SuperCache test marker is recognized so the
    sandbox announces itself without extra configuration.
    """
    label = os.environ.get("CLAUDE_HOUDINI_LABEL", "").strip()
    if label:
        return label
    if os.environ.get("SUPERCACHE_TEST_SESSION") == "1":
        return "supercache-test"
    return ""


def identity() -> dict:
    """Who this session is. Cheap enough to call before every mutating batch."""
    try:
        import hou
        hip = hou.hipFile.path()
        build = hou.applicationVersionString()
    except Exception:
        hip, build = None, None
    return {
        "pid": os.getpid(),
        "port": port(),
        "hip": hip,
        "build": build,
        "label": session_label(),
        "started": _started_at,
    }


def session_file() -> Path:
    """Legacy single-session file. Kept for old consumers; last writer wins."""
    return config.SESSIONS_DIR / "session.json"


def _port_session_file(p: int) -> Path:
    return config.SESSIONS_DIR / f"session_{p}.json"


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return True  # cannot tell -> keep the file
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _prune_stale_sessions() -> None:
    """Drop per-port session files whose Houdini process is gone."""
    try:
        for f in config.SESSIONS_DIR.glob("session_*.json"):
            try:
                pid = int(json.loads(f.read_text(encoding="utf-8")).get("pid", 0))
            except (OSError, ValueError):
                pid = 0
            if not pid or not _pid_alive(pid):
                f.unlink(missing_ok=True)
    except Exception:
        pass


def _publish_session(token: str) -> None:
    """Write url/token to disk so external CLIs connect without a manual handshake.

    One file per port (session_<port>.json) so several Houdinis can publish
    side by side, plus the legacy session.json for old consumers. Local-only
    files (the server listens on 127.0.0.1), removed on stop().
    """
    try:
        payload = dict(identity())
        payload.update({
            "url": base_url(),
            "token": token,
        })
        text = json.dumps(payload, indent=2)
        _prune_stale_sessions()
        _port_session_file(port()).write_text(text, encoding="utf-8")
        session_file().write_text(text, encoding="utf-8")
    except Exception:  # never block startup over a bookkeeping file
        pass


def _unpublish_session() -> None:
    try:
        _port_session_file(port()).unlink(missing_ok=True)
    except Exception:
        pass
    try:
        # Remove the legacy file only if it is ours; a sibling session may
        # have overwritten it since we started.
        data = json.loads(session_file().read_text(encoding="utf-8"))
        if int(data.get("pid", -1)) == os.getpid():
            session_file().unlink(missing_ok=True)
    except Exception:
        pass


def _bind() -> ThreadingHTTPServer:
    """Bind the configured port, falling back to the shared range if taken.

    An explicitly pinned port (CLAUDE_HOUDINI_PORT set) never falls back:
    whoever pinned it wants THAT port, and a silent move would defeat the
    point of pinning.
    """
    pinned = os.environ.get("CLAUDE_HOUDINI_PORT") is not None
    candidates = [config.HTTP_PORT] if pinned else [config.HTTP_PORT, *_FALLBACK_PORTS]
    last_error: OSError | None = None
    for candidate in candidates:
        try:
            server = ThreadingHTTPServer((config.HTTP_HOST, candidate), _Handler)
        except OSError as exc:
            last_error = exc
            continue
        global _port
        _port = candidate
        return server
    raise OSError(
        f"claude-houdini: no free port among {candidates} ({last_error}). "
        "Another session may be using them; set CLAUDE_HOUDINI_PORT to pick one."
    )


def start() -> tuple[str, str]:
    """Start the server if not already running. Returns (base_url, token)."""
    global _server, _server_thread, _started_at

    with _lock:
        if _server is not None:
            return base_url(), get_token()

        token = get_token()
        _server = _bind()
        _server.daemon_threads = True
        _server_thread = threading.Thread(
            target=_server.serve_forever, name="claude-houdini-http", daemon=True
        )
        _server_thread.start()
        _started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        _publish_session(token)
        return base_url(), token


def stop() -> None:
    global _server, _server_thread
    with _lock:
        if _server is not None:
            _server.shutdown()
            _server.server_close()
            _server = None
            _server_thread = None
            _unpublish_session()


# ---------- Route table ----------

def _route_read() -> dict[str, Callable[[dict], Any]]:
    return {
        "/api/identity": lambda q: identity(),
        "/api/scene": lambda q: tools.scene_info(),
        "/api/nodes": lambda q: tools.list_nodes(
            path=q.get("path", "/obj"),
            recursive=q.get("recursive", "false").lower() == "true",
            max_items=int(q.get("max_items", "500")),
        ),
        "/api/node": lambda q: tools.get_node(
            path=q["path"],
            include_parms=q.get("include_parms", "true").lower() == "true",
            parm_limit=int(q.get("parm_limit", "80")),
        ),
        "/api/parm": lambda q: tools.get_parm(path=q["path"], parm=q["parm"]),
        "/api/selected": lambda q: tools.selected_nodes(),
        "/api/screenshot": lambda q: tools.screenshot(
            what=q.get("what", "viewport"),
            width=int(q.get("width", "1400")),
            node_path=q.get("path"),
        ),
        # Back-compat: same capture, always the network editor.
        "/api/network_screenshot": lambda q: tools.screenshot(
            what="network",
            width=int(q.get("width", "1400")),
            node_path=q.get("path"),
        ),
        "/api/cook_errors": lambda q: tools.cook_errors(
            path=q.get("path", "/obj"),
            recursive=q.get("recursive", "true").lower() == "true",
            max_items=int(q.get("max_items", "50")),
        ),
    }


def _route_write() -> dict[str, tuple[Callable[[dict], Any], Callable[[dict], tuple[str, str | None]]]]:
    """POST routes. Value is (handler, confirmation_summarizer)."""
    return {
        "/api/create_node": (
            lambda b: tools.create_node(
                parent=b["parent"],
                type_name=b["type"],
                name=b.get("name"),
                set_parms=b.get("set_parms"),
                layout=bool(b.get("layout", True)),
            ),
            lambda b: (
                f"Crear nodo <b>{b['type']}</b> en <code>{b['parent']}</code>"
                + (f" con nombre <code>{b['name']}</code>" if b.get("name") else ""),
                json.dumps(b.get("set_parms"), indent=2) if b.get("set_parms") else None,
            ),
        ),
        "/api/set_parm": (
            lambda b: tools.set_parm(
                path=b["path"], parm=b["parm"], value=b["value"],
                as_expression=bool(b.get("as_expression", False)),
            ),
            lambda b: (
                f"Setear <code>{b['path']}.{b['parm']}</code> = <code>{_clip(repr(b['value']))}</code>"
                + (" (expresión)" if b.get("as_expression") else ""),
                None,
            ),
        ),
        "/api/connect": (
            lambda b: tools.connect_nodes(
                from_path=b["from_path"], from_output=int(b.get("from_output", 0)),
                to_path=b["to_path"], to_input=int(b.get("to_input", 0)),
            ),
            lambda b: (
                f"Conectar <code>{b['from_path']}</code>[{b.get('from_output', 0)}] → "
                f"<code>{b['to_path']}</code>[{b.get('to_input', 0)}]",
                None,
            ),
        ),
        "/api/delete_node": (
            lambda b: tools.delete_node(path=b["path"]),
            lambda b: (f"Borrar nodo <code>{b['path']}</code>", None),
        ),
        "/api/run_python": (
            lambda b: tools.run_python(code=b["code"]),
            lambda b: ("Ejecutar Python en la sesión de Houdini", b["code"]),
        ),
        "/api/layout": (
            lambda b: tools.layout_children(path=b["path"]),
            lambda b: (f"Layout de hijos de <code>{b['path']}</code>", None),
        ),
    }


_READ_ROUTES = _route_read()
_WRITE_ROUTES = _route_write()
_NON_CONFIRMABLE_WRITES = {"/api/layout"}


# ---------- Handler ----------

class _Handler(BaseHTTPRequestHandler):
    server_version = "ClaudeHoudini/0.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        pass  # silence default stderr logging

    # ----- auth + dispatch -----

    def _check_auth(self) -> bool:
        token = get_token()
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return secrets.compare_digest(header[7:], token)
        # Also accept ?token= for browser/curl convenience
        q = parse_qs(urlparse(self.path).query)
        if "token" in q and secrets.compare_digest(q["token"][0], token):
            return True
        return False

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        # /api/ping is intentionally unauthenticated for diagnostics from
        # the Houdini Python Shell or external curl.
        if parsed.path == "/api/ping":
            self._send_json(200, {"ok": True, "service": "claude-houdini"})
            return

        if not self._check_auth():
            self._send_json(401, {"error": "unauthorized"})
            return

        handler = _READ_ROUTES.get(parsed.path)
        if handler is None:
            self._send_json(404, {"error": f"unknown route: {parsed.path}"})
            return

        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            result = hdefereval.executeInMainThreadWithResult(handler, q)
            self._send_json(200, {"ok": True, "result": result})
        except KeyError as e:
            self._send_json(400, {"error": f"missing parameter: {e.args[0]}"})
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception:
            self._send_json(500, {"error": "exception", "traceback": traceback.format_exc()})

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_auth():
            self._send_json(401, {"error": "unauthorized"})
            return

        parsed = urlparse(self.path)

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""

        # Raw-text variant of run_python: the body IS the code, no JSON
        # envelope. Lets callers pipe a heredoc through `curl --data-binary @-`
        # instead of escaping newlines and quotes into JSON — the escaping pain
        # is what pushed earlier sessions into writing scratch .py files.
        if parsed.path == "/api/run_python_raw":
            code = raw.decode("utf-8", errors="replace")
            if not code.strip():
                self._send_json(400, {"error": "empty body: send the Python code as the request body"})
                return
            body = {"code": code}
            handler, summarizer = _WRITE_ROUTES["/api/run_python"]
        else:
            entry = _WRITE_ROUTES.get(parsed.path)
            if entry is None:
                self._send_json(404, {"error": f"unknown route: {parsed.path}"})
                return
            handler, summarizer = entry
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError as e:
                self._send_json(400, {"error": f"invalid JSON: {e}"})
                return

        # Confirmation gate (in main thread). Auto mode skips the modal.
        if parsed.path not in _NON_CONFIRMABLE_WRITES and not state.auto_mode():
            try:
                summary, details = summarizer(body)
            except Exception as e:
                self._send_json(400, {"error": f"missing fields: {e}"})
                return
            tool_name = parsed.path.replace("/api/", "")
            approved = confirm(tool_name, summary, details)
            if not approved:
                self._send_json(403, {"ok": False, "error": "user_denied",
                                       "message": "El usuario canceló esta acción."})
                return

        try:
            result = hdefereval.executeInMainThreadWithResult(handler, body)
            self._send_json(200, {"ok": True, "result": result})
        except KeyError as e:
            self._send_json(400, {"error": f"missing field: {e.args[0]}"})
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception:
            self._send_json(500, {"error": "exception", "traceback": traceback.format_exc()})


def _clip(s: str, n: int = 120) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"
