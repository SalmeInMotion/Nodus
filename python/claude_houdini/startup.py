"""Startup registration, shared by scripts/123.py and scripts/456.py.

Kept version-agnostic on purpose: the code lives in `<root>/python`, not in a
`python3.NNlibs` folder, so a new Houdini release (which bumps the bundled
Python) needs no directory rename. H21 ships Python 3.11, H22 ships 3.13.
"""

from __future__ import annotations

import os
import sys

PANEL_NAME = "claude_chat"


def bootstrap_syspath() -> None:
    """Ensure `<CLAUDE_HOUDINI_ROOT>/python` is importable.

    Callers run before this module is importable, so they inline the same
    logic; this stays here for external callers (hython, tools/hbridge.py).
    """
    root = os.environ.get("CLAUDE_HOUDINI_ROOT")
    if not root:
        return
    libs = os.path.join(root, "python")
    if os.path.isdir(libs) and libs not in sys.path:
        sys.path.insert(0, libs)


def register_panel() -> None:
    """Install the .pypanel and add it to the pane-tab menu. Idempotent."""
    import hou

    if not hou.isUIAvailable():
        return

    root = os.environ.get("CLAUDE_HOUDINI_ROOT")
    if root and PANEL_NAME not in hou.pypanel.interfaces():
        pypanel = os.path.join(root, "python_panels", "claude_chat.pypanel")
        if os.path.isfile(pypanel):
            try:
                hou.pypanel.installFile(pypanel)
            except Exception as e:
                sys.stderr.write(f"[claude_houdini] installFile failed: {e}\n")

    current = list(hou.pypanel.menuInterfaces())
    if PANEL_NAME not in current:
        current.append(PANEL_NAME)
        hou.pypanel.setMenuInterfaces(tuple(current))


def register_panel_deferred() -> None:
    """Queue registration for after the UI finishes building."""
    try:
        import hou
        if not hou.isUIAvailable():
            return
        import hdefereval
        hdefereval.executeDeferred(_safe_register)
    except Exception as e:
        sys.stderr.write(f"[claude_houdini] deferred register failed: {e}\n")


def _safe_register() -> None:
    try:
        register_panel()
    except Exception as e:
        sys.stderr.write(f"[claude_houdini] panel auto-register failed: {e}\n")
    autostart_server()


def autostart_server() -> None:
    """Bring the bridge up at startup unless CLAUDE_HOUDINI_AUTOSTART=0.

    This is what the README always promised; until now the server only started
    when the chat panel was opened, so external tooling found a dead port.
    With per-port session files and port fallback, every session announcing
    itself is cheap and unambiguous.
    """
    if os.environ.get("CLAUDE_HOUDINI_AUTOSTART", "1") == "0":
        return
    try:
        from . import server
        url, _ = server.start()
        sys.stderr.write(f"[claude_houdini] bridge listening on {url} "
                         f"as {server.session_id()}\n")
        _tag_window(server)
    except Exception as e:
        sys.stderr.write(f"[claude_houdini] bridge autostart failed: {e}\n")


def _tag_window(server) -> None:
    """Put the bridge id in the window title.

    With several Houdinis open, "which window is port 8756?" is otherwise
    unanswerable from the taskbar. Houdini rewrites this title whenever the
    scene changes, so this is a best-effort stamp, not a guarantee.
    """
    try:
        import hou
        if not hou.isUIAvailable():
            return
        window = None
        for getter in (lambda: hou.qt.mainWindow(),
                       lambda: hou.ui.mainQtWindow()):
            try:
                window = getter()
                if window is not None:
                    break
            except Exception:
                continue
        if window is None:
            return
        tag = f"  [Nodus {server.session_id()} :{server.port()}]"
        if tag.strip() not in window.windowTitle():
            window.setWindowTitle(window.windowTitle() + tag)
    except Exception as e:
        sys.stderr.write(f"[claude_houdini] could not tag the window: {e}\n")
