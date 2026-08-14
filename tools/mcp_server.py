"""MCP adapter for the Nodus bridge.

Exposes the HTTP bridge of a running Houdini as standard MCP tools, so ANY
MCP-capable client — Claude Code, Gemini CLI, local agent frameworks — can see
and drive the scene without knowing the bridge's HTTP details or token
handshake. The adapter is a thin proxy: discovery, auth and transport come from
`hbridge.py`; every tool is one HTTP call.

Run it with the venv next to it (see README "MCP" section):

    .venv-mcp/Scripts/python.exe tools/mcp_server.py

Multi-session: with one Houdini running, tools connect automatically. With
several, pass `port` (list them with houdini_sessions). Mutating tools still go
through the in-Houdini confirmation modal unless auto mode is enabled there —
the adapter adds no privileges, it only translates.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hbridge  # noqa: E402  (same directory)

# Requires the MCP SDK 2.x (`pip install "mcp>=2,<3"`). In 1.x this class was
# mcp.server.fastmcp.FastMCP, so an unpinned install can break the import.
from mcp.server import MCPServer  # noqa: E402

mcp = MCPServer(
    "houdini-bridge",
    instructions=(
        "Live control of running Houdini sessions. Call houdini_sessions "
        "first when several Houdinis may be open, verify with "
        "houdini_identity, then pass that port to every other tool."),
)


def _call(port: int, api_path: str, params: dict | None = None,
          body: dict | None = None) -> Any:
    url, token = hbridge.resolve(None, None, None, port or None)
    return hbridge._unwrap(hbridge.request(url, token, api_path,
                                           body=body, params=params))


@mcp.tool()
def houdini_sessions() -> list[dict]:
    """List every running Houdini bridge session (port, pid, hip file, build,
    label, alive). Call this first when unsure which Houdini to talk to; pass
    the chosen port to the other tools."""
    rows = hbridge.list_sessions(None)
    for row in rows:
        row.pop("token", None)  # clients need the port, never the secret
    return rows


@mcp.tool()
def houdini_identity(port: int = 0) -> dict:
    """Who a session is: pid, port, hip file, Houdini build, label. Verify you
    are talking to the intended Houdini before creating or deleting anything."""
    return _call(port, "/api/identity")


@mcp.tool()
def houdini_scene(port: int = 0) -> dict:
    """Scene info: hip file path, fps, frame range, current frame."""
    return _call(port, "/api/scene")


@mcp.tool()
def houdini_nodes(path: str = "/obj", recursive: bool = False,
                  max_items: int = 500, port: int = 0) -> dict:
    """List child nodes under a path (type, name, path per node)."""
    return _call(port, "/api/nodes", params={
        "path": path, "recursive": str(recursive).lower(),
        "max_items": str(max_items)})


@mcp.tool()
def houdini_node(path: str, port: int = 0) -> dict:
    """Full detail of one node: type, parameters, inputs/outputs, flags."""
    return _call(port, "/api/node", params={"path": path})


@mcp.tool()
def houdini_parm(path: str, parm: str, port: int = 0) -> dict:
    """One parameter's value and expression (if any)."""
    return _call(port, "/api/parm", params={"path": path, "parm": parm})


@mcp.tool()
def houdini_selected(port: int = 0) -> dict:
    """Nodes currently selected in the Houdini UI."""
    return _call(port, "/api/selected")


@mcp.tool()
def houdini_cook_errors(path: str = "/obj", recursive: bool = True,
                        port: int = 0) -> dict:
    """Cook errors/warnings under a path — check after building a network."""
    return _call(port, "/api/cook_errors", params={
        "path": path, "recursive": str(recursive).lower()})


@mcp.tool()
def houdini_screenshot(what: str = "viewport", node_path: str = "",
                       width: int = 1400, port: int = 0) -> dict:
    """Capture a PNG to disk and return its file path. `what` is one of
    "viewport" (clean 3D view), "houdini" (whole window), "network" (node
    editor; honors node_path). Read the returned file to actually see it."""
    params = {"what": what, "width": str(width)}
    if node_path:
        params["path"] = node_path
    return _call(port, "/api/screenshot", params=params)


@mcp.tool()
def houdini_create_node(parent: str, node_type: str, name: str = "",
                        set_parms: dict | None = None,
                        port: int = 0) -> dict:
    """Create a node (e.g. parent="/obj", node_type="geo"). Optionally set
    parameters in the same call via set_parms {parm: value}."""
    body: dict = {"parent": parent, "type": node_type}
    if name:
        body["name"] = name
    if set_parms:
        body["set_parms"] = set_parms
    return _call(port, "/api/create_node", body=body)


@mcp.tool()
def houdini_set_parm(path: str, parm: str, value: Any,
                     as_expression: bool = False, port: int = 0) -> dict:
    """Set one parameter. as_expression=True stores the value as an hscript
    expression instead of a literal."""
    return _call(port, "/api/set_parm", body={
        "path": path, "parm": parm, "value": value,
        "as_expression": as_expression})


@mcp.tool()
def houdini_connect(from_path: str, to_path: str, from_output: int = 0,
                    to_input: int = 0, port: int = 0) -> dict:
    """Wire from_path's output into to_path's input."""
    return _call(port, "/api/connect", body={
        "from_path": from_path, "from_output": from_output,
        "to_path": to_path, "to_input": to_input})


@mcp.tool()
def houdini_delete_node(path: str, port: int = 0) -> dict:
    """Delete a node. Confirmed by the user inside Houdini unless auto mode."""
    return _call(port, "/api/delete_node", body={"path": path})


@mcp.tool()
def houdini_layout(path: str, port: int = 0) -> dict:
    """Tidy the layout of a network's children (cosmetic, no confirmation)."""
    return _call(port, "/api/layout", body={"path": path})


@mcp.tool()
def houdini_run_python(code: str, port: int = 0) -> dict:
    """Escape hatch: run arbitrary Python inside the Houdini session (full hou
    access, main thread). Returns {stdout, result, error}. Use the typed tools
    when one fits; use this for everything they do not cover."""
    return _call(port, "/api/run_python", body={"code": code})


if __name__ == "__main__":
    mcp.run()
