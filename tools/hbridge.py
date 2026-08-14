"""Command-line client for the claude-houdini bridge.

Resolves the bridge url/token automatically from the session file that the
server publishes on startup (.sessions/session.json), so no manual token
handshake is needed. Standard library only -- runs on any Python 3.9+.

Examples:
    python hbridge.py ping
    python hbridge.py scene
    python hbridge.py nodes /obj --recursive
    python hbridge.py node /obj/geo1
    python hbridge.py run setup.py
    echo "print(hou.hipFile.path())" | python hbridge.py run -
    python hbridge.py get /api/parm path=/obj/geo1 parm=tx
    python hbridge.py post /api/create_node '{"parent": "/obj", "type": "geo"}'
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_PORT = 8742
TIMEOUT_S = 600


class BridgeError(RuntimeError):
    pass


# ---------- connection discovery ----------

def _root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("CLAUDE_HOUDINI_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1]


def _sessions_dir(root: str | None) -> Path:
    return _root(root) / ".sessions"


def list_sessions(root: str | None) -> list[dict]:
    """Every published session, liveness-checked against /api/identity.

    Per-port files (session_<port>.json) are the source of truth; the legacy
    session.json is read only when no per-port files exist (a pre-multi-session
    server). Dead entries are reported with alive=False rather than hidden, so
    `sessions` output explains itself.
    """
    directory = _sessions_dir(root)
    files = sorted(directory.glob("session_*.json"))
    if not files and (directory / "session.json").is_file():
        files = [directory / "session.json"]

    out: list[dict] = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entry = {
            "file": f.name,
            "url": data.get("url") or f"http://127.0.0.1:{data.get('port', DEFAULT_PORT)}",
            "port": data.get("port"),
            "token": data.get("token", ""),
            "pid": data.get("pid"),
            "hip": data.get("hip"),
            "build": data.get("build"),
            "label": data.get("label", ""),
            "alive": False,
        }
        try:
            ident = request(entry["url"], entry["token"], "/api/identity")
            live = ident.get("result", {})
            entry.update({k: live[k] for k in ("hip", "build", "label", "pid") if live.get(k) is not None})
            entry["alive"] = True
        except BridgeError:
            pass
        out.append(entry)
    return out


def resolve(root: str | None, url: str | None, token: str | None,
            port: int | None = None) -> tuple[str, str]:
    """Return (url, token): explicit args, then env, then published sessions.

    With several Houdinis running, one of --port / --url / CLAUDE_HOUDINI_PORT
    must disambiguate; connecting to "whichever session wrote a file last"
    is exactly the bug this replaces.
    """
    url = url or os.environ.get("CLAUDE_HOUDINI_URL")
    token = token or os.environ.get("CLAUDE_HOUDINI_TOKEN")
    if url and token:
        return url.rstrip("/"), token

    env_port = os.environ.get("CLAUDE_HOUDINI_PORT")
    port = port or (int(env_port) if env_port else None)

    sessions = [s for s in list_sessions(root) if s["alive"]]
    if port is not None:
        match = [s for s in sessions if s["port"] == port]
        if not match:
            raise BridgeError(
                f"No live bridge on port {port}. Run `hbridge.py sessions` to see "
                "what is available.")
        chosen = match[0]
    elif len(sessions) == 1:
        chosen = sessions[0]
    elif not sessions:
        raise BridgeError(
            f"No live bridge found (looked in {_sessions_dir(root)}).\n"
            "Is Houdini running with the claude-houdini package loaded?\n"
            "Start the bridge manually from the Houdini Python Shell with:\n"
            "    from claude_houdini import server; server.start()"
        )
    else:
        lines = "\n".join(
            f"  --port {s['port']}  pid {s['pid']}  {s['label'] or ''}  {s['hip'] or ''}"
            for s in sessions)
        raise BridgeError(
            f"{len(sessions)} Houdini sessions are live - pick one with --port:\n{lines}")

    return chosen["url"].rstrip("/"), token or chosen["token"]


# ---------- transport ----------

def request(url: str, token: str, path: str, body: dict | None = None,
            params: dict | None = None) -> dict:
    target = url + path
    if params:
        target += "?" + urllib.parse.urlencode(params)

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(target, data=data, method="POST" if data else "GET")
    req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        if e.code == 401:
            raise BridgeError(
                "401 unauthorized: the session file token is stale.\n"
                "Houdini was probably restarted -- it rewrites the token on startup."
            ) from e
        raise BridgeError(f"HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise BridgeError(
            f"Cannot reach the bridge at {url} ({e.reason}). Is Houdini running?"
        ) from e


def _unwrap(payload: dict):
    """Bridge responses are {ok, result} or {ok: false, error}."""
    if not payload.get("ok", True):
        raise BridgeError(payload.get("error", "unknown bridge error"))
    return payload.get("result", payload)


# ---------- commands ----------

def cmd_run(url: str, token: str, source: str) -> int:
    code = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    result = _unwrap(request(url, token, "/api/run_python", body={"code": code}))

    stdout = result.get("stdout") or ""
    if stdout:
        sys.stdout.write(stdout if stdout.endswith("\n") else stdout + "\n")
    if result.get("result") is not None:
        print(result["result"])
    if result.get("error"):
        sys.stderr.write(result["error"])
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Talk to a running Houdini through the claude-houdini bridge.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--root", help="claude-houdini install dir (finds the session files)")
    parser.add_argument("--url", help="override the bridge url")
    parser.add_argument("--token", help="override the bearer token")
    parser.add_argument("--port", type=int,
                        help="pick a session by port when several Houdinis are live")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sessions", help="list every live Houdini bridge (port, pid, hip, label)")
    sub.add_parser("ping", help="check the bridge is alive")
    sub.add_parser("identity", help="who this session is (pid, port, hip, build, label)")
    sub.add_parser("scene", help="scene info (hip file, fps, frame range)")
    sub.add_parser("selected", help="currently selected nodes")

    p = sub.add_parser("nodes", help="list nodes under a path")
    p.add_argument("path", nargs="?", default="/obj")
    p.add_argument("-r", "--recursive", action="store_true")

    p = sub.add_parser("node", help="node detail including parameters")
    p.add_argument("path")

    p = sub.add_parser("run", help="run Python inside Houdini (file path, or - for stdin)")
    p.add_argument("source")

    p = sub.add_parser("get", help="raw GET against any read endpoint")
    p.add_argument("path")
    p.add_argument("params", nargs="*", metavar="key=value")

    p = sub.add_parser("post", help="raw POST against any write endpoint")
    p.add_argument("path")
    p.add_argument("body", help="JSON object")

    args = parser.parse_args(argv)

    try:
        if args.command == "sessions":
            rows = list_sessions(args.root)
            if not rows:
                print("No published sessions.")
                return 0
            for s in rows:
                status = "LIVE" if s["alive"] else "dead"
                label = f"  [{s['label']}]" if s["label"] else ""
                print(f"{status}  port {s['port']}  pid {s['pid']}  "
                      f"{s['build'] or '?'}{label}  {s['hip'] or ''}")
            return 0

        url, token = resolve(args.root, args.url, args.token, args.port)

        if args.command == "ping":
            print(json.dumps(request(url, token, "/api/ping"), indent=2))
        elif args.command == "identity":
            print(json.dumps(_unwrap(request(url, token, "/api/identity")), indent=2))
        elif args.command == "scene":
            print(json.dumps(_unwrap(request(url, token, "/api/scene")), indent=2))
        elif args.command == "selected":
            print(json.dumps(_unwrap(request(url, token, "/api/selected")), indent=2))
        elif args.command == "nodes":
            params = {"path": args.path, "recursive": str(args.recursive).lower()}
            print(json.dumps(_unwrap(request(url, token, "/api/nodes", params=params)), indent=2))
        elif args.command == "node":
            params = {"path": args.path}
            print(json.dumps(_unwrap(request(url, token, "/api/node", params=params)), indent=2))
        elif args.command == "run":
            return cmd_run(url, token, args.source)
        elif args.command == "get":
            params = dict(kv.split("=", 1) for kv in args.params)
            print(json.dumps(_unwrap(request(url, token, args.path, params=params)), indent=2))
        elif args.command == "post":
            body = json.loads(args.body)
            print(json.dumps(_unwrap(request(url, token, args.path, body=body)), indent=2))
    except BridgeError as e:
        sys.stderr.write(f"error: {e}\n")
        return 2
    except ValueError as e:
        sys.stderr.write(f"error: bad argument ({e})\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
