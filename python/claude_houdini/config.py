"""Configuration constants for the Claude Houdini integration."""

import os
from pathlib import Path

HTTP_HOST = "127.0.0.1"
HTTP_PORT_DEFAULT = 8742
HTTP_PORT = int(os.environ.get("CLAUDE_HOUDINI_PORT", HTTP_PORT_DEFAULT))

AUTH_TOKEN_ENV = "CLAUDE_HOUDINI_TOKEN"

CLAUDE_CLI_ENV = "CLAUDE_HOUDINI_CLI"
CLAUDE_CLI_DEFAULT = "claude"

MODEL_ENV = "CLAUDE_HOUDINI_MODEL"
MODEL_DEFAULT = "claude-opus-5"

# Reasoning effort passed to the CLI (low | medium | high | xhigh | max).
EFFORT_ENV = "CLAUDE_HOUDINI_EFFORT"
EFFORT_DEFAULT = "high"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

ROOT = Path(os.environ.get("CLAUDE_HOUDINI_ROOT", Path(__file__).resolve().parents[2]))

SESSIONS_DIR = ROOT / ".sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# cwd handed to the `claude` subprocess. Without this the CLI inherits
# Houdini's cwd (often Program Files) and scratch files land in the user's
# home — a real symptom seen in early sessions.
WORKSPACE_DIR = ROOT / ".workspace"
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

# Offline documentation corpus: a folder of consolidated markdown volumes
# generated from Houdini's own installed help (see docs_corpus.py). Not
# distributed with Nodus — the text is SideFX's.
DOCS_DIR = Path(os.environ.get("CLAUDE_HOUDINI_DOCS") or ROOT / ".docs")


def model() -> str:
    return os.environ.get(MODEL_ENV) or MODEL_DEFAULT


def effort() -> str:
    value = (os.environ.get(EFFORT_ENV) or EFFORT_DEFAULT).lower()
    return value if value in EFFORT_LEVELS else EFFORT_DEFAULT

REQUEST_TIMEOUT_S = 600
CONFIRM_TIMEOUT_S = 120
