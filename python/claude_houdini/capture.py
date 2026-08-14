"""Screen captures written to disk so Claude can actually *see* them.

Why files and not base64: the CLI's `Read` tool renders PNG/JPG as real image
blocks, while a base64 blob inside a JSON response is just an expensive wall of
text the model cannot look at. So every capture lands in `.workspace/captures/`
and the endpoint returns the path; the system prompt tells Claude to `Read` it.

Qt-based grabs go through `hou.qt` / `hou.ui`, which only exist in a graphical
Houdini and have moved across releases — hence the layered fallbacks and the
runtime probing instead of hard-coded API calls.
"""

from __future__ import annotations

import glob
import os
import time
from pathlib import Path

import hou

from . import config

CAPTURES_DIR = config.WORKSPACE_DIR / "captures"
KEEP_LAST = 20

VALID_TARGETS = ("viewport", "houdini", "network")


# ---------- public ----------

def capture(what: str = "viewport", width: int = 1400) -> dict:
    """Capture `what` to a PNG and return its path.

    what:
      viewport — the 3D view only (clean, no UI chrome), via flipbook
      houdini  — the whole Houdini window, i.e. literally what the user sees
      network  — the network editor pane, falling back to the full window
    """
    if what not in VALID_TARGETS:
        raise ValueError(f"'what' must be one of {VALID_TARGETS}, not {what!r}")

    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    _prune_old()

    stamp = time.strftime("%H%M%S")
    path = CAPTURES_DIR / f"{what}_{stamp}.png"

    if what == "viewport":
        result = _capture_viewport(path, width)
    elif what == "houdini":
        result = _capture_widget(_main_window(), path, width)
    else:
        widget = _network_widget()
        if widget is None:
            result = _capture_widget(_main_window(), path, width)
            result["note"] = ("Could not isolate the Network Editor in this version; "
                              "this is the whole Houdini window.")
        else:
            result = _capture_widget(widget, path, width)

    result["what"] = what
    result["hint"] = f"Open {result['path']} with the Read tool to actually see it."
    return result


# ---------- viewport (flipbook) ----------

def _capture_viewport(path: Path, width: int) -> dict:
    viewer = _scene_viewer()
    if viewer is None:
        raise RuntimeError("No Scene Viewer open in this session.")

    settings = viewer.flipbookSettings().stash()
    frame = hou.frame()
    settings.frameRange((frame, frame))
    settings.output(str(path))
    settings.outputToMPlay(False)

    # Keep the aspect of the current viewport, just bound the width.
    try:
        viewport = viewer.curViewport()
        vw, vh = viewport.size()[2], viewport.size()[3]
        if vw and vh and width:
            height = max(1, int(round(width * vh / float(vw))))
            settings.useResolution(True)
            settings.resolution((width, height))
    except Exception:
        pass  # default resolution is fine

    viewer.flipbook(viewer.curViewport(), settings)

    written = _resolve_written_file(path)
    if written is None:
        raise RuntimeError(f"The flipbook wrote no file into {path.parent}")
    return {"path": str(written).replace("\\", "/"), "frame": frame}


def _resolve_written_file(path: Path) -> Path | None:
    """Flipbook may append a frame number; find whatever it actually wrote."""
    if path.is_file():
        return path
    matches = sorted(glob.glob(str(path.with_suffix("")) + "*"), key=os.path.getmtime)
    return Path(matches[-1]) if matches else None


# ---------- Qt widget grabs ----------

def _capture_widget(widget, path: Path, width: int) -> dict:
    if widget is None:
        raise RuntimeError("Could not get the Houdini window (headless session?).")

    from PySide6 import QtCore

    pixmap = widget.grab()
    if width and pixmap.width() > width:
        pixmap = pixmap.scaledToWidth(width, QtCore.Qt.SmoothTransformation)
    if not pixmap.save(str(path), "PNG"):
        raise RuntimeError(f"Could not write the PNG to {path}")
    return {"path": str(path).replace("\\", "/"),
            "width": pixmap.width(), "height": pixmap.height()}


def _main_window():
    """Houdini's main Qt window, across the APIs that have existed."""
    for getter in (
        lambda: hou.qt.mainWindow(),
        lambda: hou.ui.mainQtWindow(),
    ):
        try:
            w = getter()
            if w is not None:
                return w
        except Exception:
            continue
    return None


def _network_widget():
    """Best-effort handle on the Network Editor pane widget.

    `PaneTab.qtWindow()` existed in some releases and is gone in H22, so this
    probes instead of assuming, and returns None when it cannot isolate it.
    """
    pane = _pane_of_type(hou.paneTabType.NetworkEditor)
    if pane is None:
        return None
    for attr in ("qtWindow", "qtWidget"):
        getter = getattr(pane, attr, None)
        if callable(getter):
            try:
                w = getter()
                if w is not None:
                    return w
            except Exception:
                pass
    return None


# ---------- helpers ----------

def _scene_viewer():
    return _pane_of_type(hou.paneTabType.SceneViewer)


def _pane_of_type(pane_type):
    try:
        tabs = hou.ui.paneTabs()
    except Exception:
        return None
    # Prefer a visible tab; a hidden one may not render.
    found = None
    for tab in tabs:
        if tab.type() != pane_type:
            continue
        if getattr(tab, "isCurrentTab", lambda: True)():
            return tab
        found = found or tab
    return found


def _prune_old() -> None:
    try:
        files = sorted(CAPTURES_DIR.glob("*.png"), key=os.path.getmtime)
        for old in files[:-KEEP_LAST]:
            old.unlink(missing_ok=True)
    except Exception:
        pass
