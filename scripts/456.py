"""Houdini scene-load hook — safety net in case 123.py did not run.

Same content as scripts/123.py; registration is idempotent so running both is
harmless. Houdini exec()s this file, so `__file__` is NOT defined here.
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
    sys.stderr.write(f"[claude_houdini] 456.py failed: {_e}\n")
