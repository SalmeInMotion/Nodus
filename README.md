# Nodus

A chat panel embedded in Houdini, plus a local HTTP bridge that lets Claude —
or any MCP client — see and drive your scene.

No API key required: the panel launches the **Claude Code CLI** as a
subprocess, so it uses the session you are already logged into (a Pro/Max
subscription works as-is). A local Ollama backend is also available for
offline, zero-cost questions.

What it can do:

- Read your scene: nodes, parameters, selection, cook errors
- **See** it: viewport, node network, or the whole Houdini window
- Create / modify / delete nodes, set parameters, wire them up
- Run arbitrary Python inside the running Houdini session

Every destructive action goes through a confirmation modal, unless you turn
autonomous mode on. Each turn is wrapped in a single undo group, so one Ctrl+Z
reverts a whole answer.

## Requirements

- **Houdini 21 or 22** (Python 3.11 / 3.13 + PySide6 — both ship with Houdini)
- **[Claude Code CLI](https://claude.com/code)** installed, with `claude` on
  the PATH and logged in (run `claude` once)
- Optional, for the local backend: [Ollama](https://ollama.com) with a model
  pulled (default `qwen3.6:latest`)
- Windows (tested on Win 11; macOS/Linux would need path adjustments)

## Install

```powershell
git clone <this-repo> nodus
cd nodus
.\install.ps1
```

The installer finds every `houdini<version>` folder in your Documents
(following the OneDrive redirection Windows often applies) and writes
`packages/claude_houdini.json` into each one.

```powershell
.\install.ps1 -Versions 22.0                  # one version only
.\install.ps1 -Language "Spanish (es-ES)"     # always answer in this language
.\install.ps1 -UserName "Alex"                # let the assistant know your name
```

Both preference flags are optional; without them the assistant replies in
whatever language you write to it in.

Restart Houdini, then open the panel:

- **Shelf**: `Claude` tab → **Nodus** button
- Or the **"+"** menu of any pane tab → **Nodus**

## How it works

```
┌─────────────────── Houdini process ─────────────────────┐
│                                                          │
│  PySide6 panel (your input + chat history)              │
│       ↓ submit                                           │
│  Worker thread                                           │
│       ↓ stdin (stream-json)                              │
│  persistent subprocess: claude --print                   │
│       ← stdout (stream of JSON events)                   │
│                                                          │
│  HTTP server on 127.0.0.1:8742  ──┐                     │
│  (bearer token auth)              │                     │
│       ↑ curl                      │                     │
│  the CLI's Bash tool ─────────────┘                     │
│       ↓                                                  │
│  /api/* endpoints → hou.* (on the main thread)          │
│       ↓                                                  │
│  destructive ops → Qt modal → accept/deny               │
└──────────────────────────────────────────────────────────┘
```

One `claude` process stays alive for the whole panel session, so there is no
process boot or system-prompt round trip per turn, and answers stream in
token by token.

## Configuration

All optional environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_HOUDINI_PORT` | `8742` | HTTP server port. When set, it is exact — no fallback |
| `CLAUDE_HOUDINI_CLI` | `claude` | Path to the `claude` binary if it is not on PATH |
| `CLAUDE_HOUDINI_MODEL` | `claude-opus-5` | Anthropic model for the panel |
| `CLAUDE_HOUDINI_EFFORT` | `high` | Reasoning effort: low, medium, high, xhigh, max |
| `CLAUDE_HOUDINI_LOCAL_MODEL` | `qwen3.6:latest` | Ollama model for the local backend |
| `CLAUDE_HOUDINI_DOCS` | `<project>/.docs` | Offline docs corpus folder (see "Offline docs") |
| `CLAUDE_HOUDINI_OLLAMA` | `http://127.0.0.1:11434` | Ollama endpoint |
| `CLAUDE_HOUDINI_LANGUAGE` | — | Force replies into one language, e.g. `"Spanish (es-ES)"` |
| `CLAUDE_HOUDINI_USER_NAME` | — | Your name, for the assistant to address you |
| `CLAUDE_HOUDINI_AUTOSTART` | `1` | Set to `0` to not start the bridge when Houdini opens |
| `CLAUDE_HOUDINI_UNDO_GROUP` | `1` | Set to `0` to disable per-turn undo grouping |
| `CLAUDE_HOUDINI_TOKEN` | — | Pin the bearer token instead of generating one per session |
| `CLAUDE_HOUDINI_LABEL` | — | Label this session, so tooling can tell your Houdinis apart |
| `CLAUDE_HOUDINI_SESSION` | — | Pin this client to one instance by id; other instances answer 409 |

## Backends

Pick one from the dropdown in the panel:

| Option | What it is |
|---|---|
| **Opus 5** | Agentic. Sees and modifies the scene through the HTTP API. Default. |
| **Fable 5** | Mythos-class tier, above Opus in capability. |
| **Sonnet 5** | Faster and cheaper — good for mechanical work. |
| **Local models** | Every chat-capable model Ollama has pulled (≥4B parameters) appears in the dropdown with its VRAM cost. Free and offline, but **no scene access**: VEX, node and syntax questions only. |

Local entries are discovered live from Ollama; embedding models and models too
small to answer Houdini questions reliably are filtered out. If Ollama is not
running, the first local request starts it.

A separate dropdown sets **reasoning effort** (low → max) for the Anthropic
models; local models ignore it.

Switching backend or effort restarts the conversation — both are read when the
`claude` process spawns. Choices persist in `.sessions/state.json`.

Cancelling a local answer also asks Ollama to unload the model
(`keep_alive: 0`) instead of leaving tens of GB of VRAM occupied.

## Offline docs

Point `CLAUDE_HOUDINI_DOCS` (default: `<project>/.docs/`) at a folder of
consolidated markdown volumes generated from Houdini's installed help, where
each node/topic is a `## Name (context/internalname)` header. When present:

- the **panel's Claude** is instructed to grep/read it before asserting any
  node or parameter name (it matches your installed Houdini exactly)
- **local models** get the most relevant documentation sections injected into
  their context automatically — grounding without tool use
- the **MCP server** exposes `houdini_docs_node` and `houdini_docs_search` to
  any client

The corpus is not distributed with Nodus — the text belongs to SideFX. A
generator that builds it from your own Houdini install is on the roadmap.

## Vision

`/api/screenshot?what=viewport|houdini|network` writes a PNG to
`.workspace/captures/` and returns its path, which the model then opens with
its `Read` tool. Returning base64 inside the JSON does not work: a model cannot
*look at* a blob of text, and it costs a fortune in tokens.

- `viewport` — the 3D view only, clean (via flipbook)
- `houdini` — the whole window: literally what you are looking at
- `network` — the node editor (accepts `&path=/obj/geo1`)

## Multiple sessions

Every Houdini publishes its own `.sessions/session_<port>.json`. If 8742 is
taken, the server moves to 8752–8759 (8743–8751 are left free for pinned
instances). Setting `CLAUDE_HOUDINI_PORT` disables the fallback: a pinned port
is exact.

Each instance also mints a **session id** (`h22-54024`: Houdini major version +
PID) that is stable for the life of that process and independent of the port.
It is stamped into the window title, so the taskbar answers "which window is
this?", and `GET /api/identity` reports `{id, pid, port, hip, build, label,
busy}`.

**Why an id and not just the port**: a port is an address, not an identity.
Close Houdini and the next one may bind the same number, so an agent that
remembered "port 8742" would happily write into a different scene. Send the id
in an `X-Nodus-Session` header (or set `CLAUDE_HOUDINI_SESSION`) and the bridge
answers **409 wrong_session** instead of acting, which turns a silent
catastrophe into an error message.

```powershell
$env:CLAUDE_HOUDINI_SESSION = "h22-54024"   # this shell now only talks to that Houdini
python tools/hbridge.py scene               # 409 if it is gone
```

`/api/identity` never touches Houdini's main thread — it answers from cached
facts. A Houdini in the middle of a simulation still says who it is instead of
timing out and stalling discovery for everyone else. When the main thread has
been unresponsive for a while, identity reports `busy: true`.

## Driving it from an external terminal (`hbridge.py`)

Besides the panel, you can drive Houdini from a `claude` (or yourself) running
**outside** Houdini. The server publishes url + token on startup, so there is
no token to copy by hand:

```powershell
python tools/hbridge.py sessions            # which Houdinis are alive
python tools/hbridge.py identity            # who the resolved session is
python tools/hbridge.py scene
python tools/hbridge.py nodes /obj -r
python tools/hbridge.py run my_setup.py     # "-" reads from stdin
python tools/hbridge.py get /api/parm path=/obj/geo1 parm=tx
python tools/hbridge.py post /api/create_node '{\"parent\":\"/obj\",\"type\":\"geo\"}'
```

With one session alive it resolves automatically. With several it requires
`--port N` rather than connecting blind. Standard library only, any Python 3.9+.

> From **Git Bash**, prefix commands with `MSYS_NO_PATHCONV=1` or it rewrites
> `/obj` into `C:/Program Files/Git/obj` and the bridge answers 400. PowerShell
> is unaffected.

## MCP adapter — any AI, not just Claude

`tools/mcp_server.py` exposes the bridge as a standard **MCP server** (stdio),
so any MCP client can see and drive the scene: Claude Code, Gemini CLI, local
agent frameworks. It is a thin proxy over `hbridge.py`: same multi-session
discovery, same in-Houdini confirmation modals. It adds no privileges.

15 tools: `houdini_sessions`, `houdini_identity`, `houdini_scene`,
`houdini_nodes`, `houdini_node`, `houdini_parm`, `houdini_selected`,
`houdini_cook_errors`, `houdini_screenshot`, `houdini_create_node`,
`houdini_set_parm`, `houdini_connect`, `houdini_delete_node`,
`houdini_layout`, `houdini_run_python`.

It needs its own venv (once):

```powershell
python -m venv .venv-mcp
.venv-mcp\Scripts\python.exe -m pip install "mcp>=2,<3"
```

> The version bound matters: this adapter targets the MCP SDK 2.x API
> (`from mcp.server import MCPServer`). SDK 1.x used `FastMCP` instead, and a
> future 3.x may move it again.

**Claude Code:**

```powershell
claude mcp add --scope user houdini-bridge -- <project>\.venv-mcp\Scripts\python.exe <project>\tools\mcp_server.py
```

**Gemini CLI** — in `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "houdini-bridge": {
      "command": "<project>/.venv-mcp/Scripts/python.exe",
      "args": ["<project>/tools/mcp_server.py"]
    }
  }
}
```

Any other MCP client: same binary, stdio transport.

## API endpoints

Documented in full in `python/claude_houdini/system_prompt.py`.

**Reads (no confirmation):**

- `GET /api/ping` — healthcheck (no auth)
- `GET /api/identity` — pid, port, hip, build, label
- `GET /api/scene` — hip path, fps, frame range, current frame
- `GET /api/nodes?path=...` — list children (`recursive=true` optional)
- `GET /api/node?path=...` — type, parms, inputs/outputs, flags, position
- `GET /api/parm?path=...&parm=...` — value + expression
- `GET /api/selected` — current selection
- `GET /api/cook_errors?path=...` — errors and warnings for a branch
- `GET /api/screenshot?what=...` — capture to disk, returns the path

**Writes (confirmation unless autonomous mode is on):**

- `POST /api/create_node` — `{parent, type, name?, set_parms?, layout?}`
- `POST /api/set_parm` — `{path, parm, value, as_expression?}`
- `POST /api/connect` — `{from_path, from_output, to_path, to_input}`
- `POST /api/delete_node` — `{path}`
- `POST /api/run_python` — `{code}`
- `POST /api/run_python_raw` — the body **is** the code (`text/plain`); the
  recommended route for multi-line, via `curl --data-binary @- <<'PY'`
- `POST /api/layout` — cosmetic, no confirmation

## Layout

```
nodus/
├── install.ps1                    # writes the package descriptor per version
├── package.json                   # template only — install.ps1 writes the real one
├── scripts/
│   ├── 123.py                     # startup (any version): sys.path + panel + bridge
│   └── 456.py                     # same, on scene load (safety net)
├── python_panels/
│   └── claude_chat.pypanel        # Python Panel descriptor
├── toolbar/
│   └── claude_houdini.shelf       # "Claude Chat" shelf tool
├── tools/
│   ├── hbridge.py                 # external CLI (auto-discovers url + token)
│   └── mcp_server.py              # MCP adapter, 15 tools over stdio
└── python/                        # ← no version suffix, deliberately
    └── claude_houdini/
        ├── startup.py             # panel registration + bridge autostart
        ├── panel.py               # UI (PySide6)
        ├── server.py              # HTTP server
        ├── tools.py               # hou.* operations
        ├── capture.py             # screenshots to disk
        ├── confirmation.py        # Qt modal for destructive ops
        ├── cli_runner.py          # persistent `claude` subprocess
        ├── local_runner.py        # Ollama backend
        ├── system_prompt.py       # the API docs the model reads
        ├── state.py               # persisted preferences
        └── config.py
```

**Why `python/` and not `python3.13libs/`**: Houdini only scans the
`python<X.Y>libs` folder matching its own interpreter, so every new release
would force a rename — exactly what broke this panel when H22 moved to Python
3.13. With the code in `python/` and `scripts/123.py` setting `sys.path`, one
checkout works across versions.

## Limitations

- Chat history does not persist across Houdini sessions
- The local backend has no tool use: it cannot read or touch the scene
- Markdown in answers is shown raw (no bold/code-block rendering yet)
- Only tested on Windows

## Troubleshooting

**"`claude` binary not found"** → make sure `claude` is on the PATH of the
Houdini process. Check it in Houdini's Python Source Editor with
`import shutil; print(shutil.which("claude"))`. If it is missing, set
`CLAUDE_HOUDINI_CLI` to the full path.

**Port already in use** → another session has it. `python tools/hbridge.py
sessions` lists them; or set `CLAUDE_HOUDINI_PORT`.

**Typing does nothing in the chat box** → fixed in v0.4. Houdini gives keyboard
focus to a Python Panel's *root* widget, so the panel forwards keystrokes to
the text box via a focus proxy.

**The confirmation modal opens behind Houdini** → it already sets
`WindowStaysOnTopHint`; check your window manager's focus-stealing settings.

## Roadmap

- [x] Vision — captures to disk + `Read`
- [x] Whole-answer undo — `hou.undos.group()` per turn
- [x] Persistent process + token streaming
- [x] Local model backend
- [x] Multi-session discovery + MCP adapter
- [ ] Docs-corpus generator (build `.docs/` from any Houdini install's help)
- [ ] Markdown rendering in the chat log
- [ ] Persist chat history across Houdini sessions
- [ ] Local backend with tool use (Ollama supports tool calling)
- [ ] Show a diff in the confirmation modal
- [ ] Quick slash commands (`/select`, `/explain`)

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, ship it in your pipeline,
commercial or not. Attribution is the only requirement.
