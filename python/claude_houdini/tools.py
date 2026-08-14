"""Houdini-side tool implementations.

Every function in this module MUST be called from Houdini's main thread.
The HTTP server (`server.py`) marshals calls here via `hdefereval`.
"""

from __future__ import annotations

import io
import os
import textwrap
from typing import Any, Iterable

import hou


# ---------- Read-only ----------

def scene_info() -> dict:
    return {
        "hip": hou.hipFile.path(),
        "name": hou.hipFile.basename(),
        "fps": hou.fps(),
        "frame_range": [hou.playbar.playbackRange()[0], hou.playbar.playbackRange()[1]],
        "current_frame": hou.frame(),
        "houdini_version": hou.applicationVersionString(),
    }


def list_nodes(path: str = "/obj", recursive: bool = False, max_items: int = 500) -> dict:
    node = hou.node(path)
    if node is None:
        raise ValueError(f"No node at '{path}'")

    out: list[dict] = []
    if recursive:
        it: Iterable[hou.Node] = node.allSubChildren()
    else:
        it = node.children()

    for n in it:
        out.append(_node_brief(n))
        if len(out) >= max_items:
            break

    return {"parent": path, "count": len(out), "nodes": out}


def get_node(path: str, include_parms: bool = True, parm_limit: int = 80) -> dict:
    node = hou.node(path)
    if node is None:
        raise ValueError(f"No node at '{path}'")

    info = _node_brief(node)
    info["inputs"] = [i.path() if i else None for i in node.inputs()]
    info["outputs"] = [[c.path() for c in conn] for conn in _outputs_grouped(node)]
    info["position"] = list(node.position())
    info["is_bypassed"] = bool(node.isBypassed()) if hasattr(node, "isBypassed") else False
    info["display_flag"] = bool(node.isDisplayFlagSet()) if hasattr(node, "isDisplayFlagSet") else None
    info["render_flag"] = bool(node.isRenderFlagSet()) if hasattr(node, "isRenderFlagSet") else None

    if include_parms:
        parms = []
        for p in node.parms()[:parm_limit]:
            try:
                val = p.eval()
                if isinstance(val, (tuple, list)):
                    val = list(val)
            except Exception:
                val = None
            raw = None
            try:
                raw = p.unexpandedString()
            except hou.OperationFailed:
                raw = None
            parms.append({
                "name": p.name(),
                "label": p.parmTemplate().label(),
                "value": val,
                "expression": raw if raw != str(val) else None,
            })
        info["parms"] = parms
        info["parm_total"] = len(node.parms())

    return info


def get_parm(path: str, parm: str) -> dict:
    p = _get_parm_or_raise(path, parm)
    try:
        raw = p.unexpandedString()
    except hou.OperationFailed:
        raw = None
    return {
        "path": path,
        "parm": parm,
        "value": p.eval(),
        "expression": raw,
    }


def selected_nodes() -> dict:
    sel = hou.selectedNodes()
    return {"count": len(sel), "nodes": [_node_brief(n) for n in sel]}


def screenshot(what: str = "viewport", width: int = 1400,
               node_path: str | None = None) -> dict:
    """Capture the session to a PNG on disk and return its path.

    Returns a path rather than inline base64 on purpose: the caller can open it
    with the `Read` tool, which renders images, whereas base64 in JSON is text
    the model cannot see. See capture.py.
    """
    from . import capture

    if what == "network" and node_path:
        pane = _pane_of_type(hou.paneTabType.NetworkEditor)
        target = hou.node(node_path)
        if target is None:
            raise ValueError(f"No node at '{node_path}'")
        if pane is not None:
            pane.setPwd(target)
            if hou.selectedNodes():
                pane.homeToSelection()
            else:
                pane.frameAll()

    return capture.capture(what=what, width=width)


def cook_errors(path: str = "/obj", recursive: bool = True,
                max_items: int = 50) -> dict:
    """Collect cook errors and warnings under `path`.

    Saves a round-trip: this used to require writing an ad-hoc script through
    run_python every time something failed to cook.
    """
    root = hou.node(path)
    if root is None:
        raise ValueError(f"No node at '{path}'")

    nodes = list(root.allSubChildren()) if recursive else list(root.children())
    nodes.insert(0, root)

    problems: list[dict] = []
    for n in nodes:
        try:
            errs = list(n.errors())
            warns = list(n.warnings())
        except Exception:
            continue
        if not errs and not warns:
            continue
        problems.append({
            "path": n.path(),
            "type": n.type().name(),
            "errors": errs,
            "warnings": warns,
        })
        if len(problems) >= max_items:
            break

    return {
        "root": path,
        "checked": len(nodes),
        "with_problems": len(problems),
        "problems": problems,
    }


# ---------- Mutating (server gates with confirmation) ----------

def create_node(parent: str, type_name: str, name: str | None = None,
                set_parms: dict | None = None, layout: bool = True) -> dict:
    parent_node = hou.node(parent)
    if parent_node is None:
        raise ValueError(f"Parent does not exist: '{parent}'")
    new_node = parent_node.createNode(type_name, node_name=name)
    if set_parms:
        for k, v in set_parms.items():
            p = new_node.parm(k) or new_node.parmTuple(k)
            if p is None:
                continue
            p.set(v)
    if layout:
        new_node.moveToGoodPosition()
    return _node_brief(new_node)


def set_parm(path: str, parm: str, value: Any, as_expression: bool = False) -> dict:
    p = _get_parm_or_raise(path, parm)
    if as_expression:
        if not isinstance(value, str):
            raise ValueError("as_expression=True requires a string value.")
        p.setExpression(value)
    else:
        p.set(value)
    return {"path": path, "parm": parm, "value": p.eval()}


def connect_nodes(from_path: str, from_output: int,
                  to_path: str, to_input: int) -> dict:
    src = hou.node(from_path)
    dst = hou.node(to_path)
    if src is None or dst is None:
        raise ValueError(f"Invalid path: src={from_path} dst={to_path}")
    dst.setInput(to_input, src, from_output)
    return {"from": from_path, "to": to_path, "from_output": from_output, "to_input": to_input}


def delete_node(path: str) -> dict:
    n = hou.node(path)
    if n is None:
        raise ValueError(f"No node at '{path}'")
    n.destroy()
    return {"deleted": path}


def run_python(code: str) -> dict:
    """Execute Python in Houdini's main thread. Returns stdout/result/error."""
    import contextlib
    import sys
    import traceback

    stdout = io.StringIO()
    g = {"hou": hou, "__builtins__": __builtins__}
    err = None
    result_repr = None

    code = textwrap.dedent(code)
    with contextlib.redirect_stdout(stdout):
        # Try compiling as expression first; if it's multi-statement we get a
        # SyntaxError — swallow it silently and fall through to exec. Only the
        # real runtime exception (if any) gets reported.
        is_expression = False
        try:
            compiled_eval = compile(code, "<claude>", "eval")
            is_expression = True
        except SyntaxError:
            compiled_eval = None

        try:
            if is_expression:
                value = eval(compiled_eval, g)
                if value is not None:
                    result_repr = repr(value)
            else:
                exec(compile(code, "<claude>", "exec"), g)
        except Exception:
            err = traceback.format_exc()

    return {
        "stdout": stdout.getvalue(),
        "result": result_repr,
        "error": err,
    }


# ---------- Undo grouping ----------
#
# A single answer can create a dozen nodes. Without grouping, undoing it means
# hammering Ctrl+Z blindly — enough of a hazard that users end up versioning
# the .hip before every request. `hou.undos.group()` is a context manager, and
# Houdini's undo stack is session state, so entering it on one main-thread call
# and exiting on a later one collapses the whole turn into one undo entry
# (verified in H22).

_undo_group = None


def undo_begin(label: str = "Claude") -> dict:
    global _undo_group
    if _undo_group is not None:
        return {"undo_group": "already_open"}
    if os.environ.get("CLAUDE_HOUDINI_UNDO_GROUP", "1") == "0":
        return {"undo_group": "disabled"}
    try:
        g = hou.undos.group(label)
        g.__enter__()
        _undo_group = g
        return {"undo_group": "open", "label": label}
    except Exception as e:
        _undo_group = None
        return {"undo_group": "failed", "error": str(e)}


def undo_end() -> dict:
    global _undo_group
    g, _undo_group = _undo_group, None
    if g is None:
        return {"undo_group": "not_open"}
    try:
        g.__exit__(None, None, None)
        return {"undo_group": "closed"}
    except Exception as e:
        return {"undo_group": "failed_to_close", "error": str(e)}


def layout_children(path: str) -> dict:
    n = hou.node(path)
    if n is None:
        raise ValueError(f"No node at '{path}'")
    n.layoutChildren()
    return {"laid_out": path}


# ---------- Helpers ----------

def _pane_of_type(pane_type):
    try:
        tabs = hou.ui.paneTabs()
    except Exception:
        return None
    for tab in tabs:
        if tab.type() == pane_type:
            return tab
    return None


def _node_brief(n: hou.Node) -> dict:
    return {
        "path": n.path(),
        "name": n.name(),
        "type": n.type().name(),
        "category": n.type().category().name(),
    }


def _get_parm_or_raise(path: str, parm: str) -> hou.Parm:
    n = hou.node(path)
    if n is None:
        raise ValueError(f"No node at '{path}'")
    p = n.parm(parm)
    if p is None:
        pt = n.parmTuple(parm)
        if pt is not None and len(pt) == 1:
            p = pt[0]
    if p is None:
        raise ValueError(f"Node '{path}' has no parm '{parm}'")
    return p


def _outputs_grouped(node: hou.Node) -> list[list[hou.Node]]:
    """Outputs grouped by output index. Empty if not applicable."""
    try:
        num = len(node.outputConnectors())
    except Exception:
        return []
    grouped: list[list[hou.Node]] = [[] for _ in range(num)]
    for c in node.outputConnections():
        try:
            grouped[c.outputIndex()].append(c.inputNode())
        except Exception:
            pass
    return grouped
