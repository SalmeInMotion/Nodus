"""Houdini startup hook — runs once when Houdini starts, in every version.

Houdini exec()s this file, so `__file__` is NOT defined here; the project root
comes from CLAUDE_HOUDINI_ROOT, set by the package descriptor.
"""

import os
import sys

_root = os.environ.get("CLAUDE_HOUDINI_ROOT")
if _root:
    _libs = os.path.join(_root, "python")
    if os.path.isdir(_libs) and _libs not in sys.path:
        sys.path.insert(0, _libs)

try:
    from claude_houdini import startup
    startup.register_panel_deferred()
except Exception as _e:
    sys.stderr.write(f"[claude_houdini] 123.py failed: {_e}\n")
