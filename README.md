# Claude Houdini — chat panel + tool API dentro de Houdini

> **v0.3** (2026-08-05): soporte Houdini 22 (Python 3.13). Layout
> version-agnostic: el código vive en `python/` y lo arranca `scripts/123.py`,
> así que una versión nueva de Houdini ya no obliga a renombrar carpetas.
> Modelo por defecto Opus 5. Nuevo endpoint `run_python_raw`. El subproceso
> `claude` ya no hereda el Python de Houdini (rompía `python` en Bash).
> Backup de la versión anterior en `../houdini-claude_backup_v2/`.
>
> v2 (2026-05-16): modo dev/standard, truncado de tool blocks, click-to-expand,
> token mascarado, fix de traceback espurio en `run_python`.

Un panel de chat embebido en Houdini que se conecta con el CLI `claude` de tu
suscripción de Claude Code y le da acceso a tu escena vía una API HTTP local.

Claude puede:
- Leer tu escena: nodos, parámetros, selección, screenshot del network editor
- Crear / modificar / borrar nodos (con confirmación en un modal, o sin ella
  en modo autónomo)
- Setear parámetros, conectar nodos, ejecutar Python arbitrario dentro de Houdini

## Requisitos

- **Houdini 21 o 22** (Python 3.11 / 3.13 + PySide6 — vienen de serie)
- **Claude Code CLI** instalado y logueado (`claude` en el PATH)
- Windows (probado en Win11; en mac/linux haría falta ajustar rutas)

## Instalación

```powershell
cd C:/IA/Tools/Houdini/houdini-claude
.\install.ps1
```

Detecta todas las carpetas `houdini<ver>` de tu Documents (siguiendo la
redirección de OneDrive) y escribe `packages/claude_houdini.json` en cada una.
Para una sola versión: `.\install.ps1 -Versions 22.0`. Reinicia Houdini.

Para abrir el panel:
- **Shelf**: pestaña `Claude` → botón **Claude Chat**
- O el menú del **"+"** de cualquier pane tab → **Claude Chat**

## Cómo funciona

```
┌─────────────────── Houdini process ─────────────────────┐
│                                                          │
│  Panel PySide6 (tu input + chat history)                │
│       ↓ submit                                           │
│  ClaudeWorker (QThread)                                  │
│       ↓ spawn                                            │
│  subprocess: claude --print --output-format stream-json  │
│       ↓ stdin (prompt)                                   │
│       ← stdout (stream de eventos JSON)                  │
│                                                          │
│  HTTP server en 127.0.0.1:8742  ──┐                     │
│  (auth con bearer token)          │                     │
│       ↑ curl                      │                     │
│  Bash tool del CLI claude ────────┘                     │
│       ↓                                                  │
│  endpoints /api/* → hou.* (en main thread)              │
│       ↓                                                  │
│  destructivos → modal Qt → user accept/deny             │
└──────────────────────────────────────────────────────────┘
```

El CLI `claude` corre como subproceso usando **tu suscripción de Claude Code**
(no necesita API key). El system prompt le explica la API HTTP y le pide usarla
vía `curl`. Las acciones destructivas pasan por un modal en Houdini.

## Configuración

Variables de entorno opcionales:

| Variable | Default | Para qué |
|---|---|---|
| `CLAUDE_HOUDINI_PORT` | `8742` | Puerto del HTTP server |
| `CLAUDE_HOUDINI_CLI` | `claude` | Ruta al binario `claude` si no está en PATH |
| `CLAUDE_HOUDINI_AUTOSTART` | `1` | Poner a `0` para no arrancar el server al abrir Houdini |
| `CLAUDE_HOUDINI_TOKEN` | — | Fija el bearer token en vez de generar uno aleatorio por sesión |

## Multi-sesión (v0.4)

Cada Houdini publica su propio `.sessions/session_<puerto>.json` (el
`session.json` legacy se mantiene, gana el último). Si el puerto 8742 está
ocupado por otra sesión, el server salta automáticamente a 8752-8759
(8743-8751 quedan reservados para instancias pineadas, p.ej. los sandbox de
SuperCache). Con `CLAUDE_HOUDINI_PORT` fijado NO hay fallback: puerto pineado
es puerto exacto.

`GET /api/identity` responde quién es la sesión: `{pid, port, hip, build,
label, started}`. La etiqueta sale de `CLAUDE_HOUDINI_LABEL` (o se autodetecta
el marker de SuperCache). **Toda herramienta automática debería llamar a
identity antes de mutar nada** cuando hay varios Houdinis abiertos.

El autostart ahora es real: el server arranca al abrir Houdini (no solo al
abrir el panel). Se desactiva con `CLAUDE_HOUDINI_AUTOSTART=0`.

## Uso desde una terminal externa (`hbridge.py`)

Además del panel, puedes conducir Houdini desde un `claude` (o tú mismo) que
corra **fuera** de Houdini. El server publica url + token al arrancar, así que
no hay que copiar el token a mano:

```powershell
cd C:/IA/Tools/Houdini/houdini-claude
python tools/hbridge.py sessions            # qué Houdinis están vivos
python tools/hbridge.py identity            # quién es la sesión resuelta
python tools/hbridge.py scene
python tools/hbridge.py nodes /obj -r
python tools/hbridge.py run mi_setup.py     # "-" para leer de stdin
python tools/hbridge.py get /api/parm path=/obj/geo1 parm=tx
python tools/hbridge.py post /api/create_node '{\"parent\":\"/obj\",\"type\":\"geo\"}'
```

Con una sola sesión viva, `hbridge` la resuelve solo. Con varias, exige
`--port N` (y `sessions` te dice cuáles hay) en vez de conectar a ciegas.

`run` con un archivo `.py` es la forma cómoda de mandar código multilínea
(construir el JSON a mano para `curl` es un incordio). Solo stdlib, cualquier
Python 3.9+.

> Desde **Git Bash** hay que anteponer `MSYS_NO_PATHCONV=1`, o convertirá `/obj`
> en `C:/Program Files/Git/obj` y el bridge responderá 400. En PowerShell no pasa.

## Adaptador MCP (v0.4) — cualquier IA, no solo Claude

`tools/mcp_server.py` expone el bridge como un server **MCP estándar** (stdio),
así que cualquier cliente MCP puede ver y manejar la escena: Claude Code,
Gemini CLI, frameworks de agentes locales... El adaptador es un proxy fino
sobre `hbridge.py`: mismo descubrimiento multi-sesión, mismos modales de
confirmación dentro de Houdini (no añade privilegios).

15 tools: `houdini_sessions`, `houdini_identity`, `houdini_scene`,
`houdini_nodes`, `houdini_node`, `houdini_parm`, `houdini_selected`,
`houdini_cook_errors`, `houdini_screenshot`, `houdini_create_node`,
`houdini_set_parm`, `houdini_connect`, `houdini_delete_node`,
`houdini_layout`, `houdini_run_python`.

Necesita su venv (una vez):

```powershell
python -m venv .venv-mcp
.venv-mcp\Scripts\python.exe -m pip install mcp
```

**Claude Code** (ya registrado en este equipo, scope user):

```powershell
claude mcp add --scope user houdini-bridge -- C:\IA\Tools\Houdini\houdini-claude\.venv-mcp\Scripts\python.exe C:\IA\Tools\Houdini\houdini-claude\tools\mcp_server.py
```

**Gemini CLI** — en `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "houdini-bridge": {
      "command": "C:/IA/Tools/Houdini/houdini-claude/.venv-mcp/Scripts/python.exe",
      "args": ["C:/IA/Tools/Houdini/houdini-claude/tools/mcp_server.py"]
    }
  }
}
```

Cualquier otro cliente MCP: mismo binario, transporte stdio.

## Endpoints expuestos a Claude

Documentados en `python/claude_houdini/system_prompt.py`. Resumen:

**Lectura (sin confirmación):**
- `GET /api/scene` — HIP, fps, frame range
- `GET /api/nodes?path=...` — listar hijos
- `GET /api/node?path=...` — detalle de nodo (tipo, parms, inputs/outputs, flags)
- `GET /api/parm?path=...&parm=...` — valor + expresión
- `GET /api/selected` — selección actual
- `GET /api/network_screenshot?path=...` — PNG base64 del network editor

**Escritura (con confirmación):**
- `POST /api/create_node` — `{parent, type, name?, set_parms?, layout?}`
- `POST /api/set_parm` — `{path, parm, value, as_expression?}`
- `POST /api/connect` — `{from_path, from_output, to_path, to_input}`
- `POST /api/delete_node` — `{path}`
- `POST /api/run_python` — `{code}` — escape hatch para lo que no tenga endpoint
- `POST /api/run_python_raw` — el body ES el código (`text/plain`), sin JSON;
  la vía recomendada para multilínea (`curl --data-binary @- <<'PY'`)
- `POST /api/layout` — cosmético, sin confirm

## Estructura

```
houdini-claude/
├── install.ps1                    # Escribe el package descriptor por versión
├── package.json                   # Plantilla del package
├── scripts/
│   ├── 123.py                     # Startup (todas las versiones): sys.path + panel
│   └── 456.py                     # Igual, al cargar escena (red de seguridad)
├── python_panels/
│   └── claude_chat.pypanel        # Descriptor del Python Panel
├── toolbar/
│   └── claude_houdini.shelf       # Shelf tool "Claude Chat"
├── tools/
│   └── hbridge.py                 # CLI externo (autodescubre url+token)
├── .sessions/
│   ├── session.json               # url+token publicados al arrancar el server
│   └── state.json                 # auto_mode / dev_mode
├── .workspace/                    # cwd del subproceso claude (scratch)
└── python/                        # ← sin sufijo de versión, a propósito
    └── claude_houdini/
        ├── startup.py             # Registro del panel (lo llama scripts/123.py)
        ├── panel.py               # UI (PySide6)
        ├── server.py              # HTTP server
        ├── tools.py               # hou.* operations
        ├── confirmation.py        # Modal Qt para destructivos
        ├── cli_runner.py          # Subprocess del CLI claude
        ├── system_prompt.py       # Doc de la API que ve Claude
        ├── state.py               # auto_mode / dev_mode persistidos
        └── config.py
```

**Por qué `python/` y no `python3.13libs/`**: Houdini solo escanea
`python<X.Y>libs` coincidente con su intérprete, así que cada versión nueva
obligaba a renombrar la carpeta (fue justo lo que rompió el panel al pasar a
H22). Con el código en `python/` + `scripts/123.py` haciendo el `sys.path`,
el paquete funciona en H21, H22 y las que vengan sin tocar nada.

## Backends (desplegable del panel)

| Opción | Qué es |
|---|---|
| **Opus 5** | Agéntico. Ve y modifica la escena vía la API HTTP. Por defecto. |
| **Sonnet 4.6** | Igual pero más rápido y barato, para tareas mecánicas. |
| **Qwen3.6 local** | Ollama en tu GPU (`127.0.0.1:11434`). Gratis, offline, **sin acceso a la escena**: solo preguntas de VEX, nodos y sintaxis. |

Cambiar de backend reinicia la conversación (el proceso se relanza). Se
persiste entre sesiones en `.sessions/state.json`.

El modelo local se cambia con `CLAUDE_HOUDINI_LOCAL_MODEL` (default
`qwen3.6:latest`), y el de Anthropic con `CLAUDE_HOUDINI_MODEL`.

## Visión

`/api/screenshot?what=viewport|houdini|network` guarda un PNG en
`.workspace/captures/` y devuelve la ruta; Claude la abre con su tool `Read`,
que renderiza imágenes. Devolver base64 dentro del JSON no servía: el modelo no
puede *mirar* un blob de texto, y además costaba una fortuna en tokens.

- `viewport` — solo la vista 3D, limpia (vía flipbook)
- `houdini` — la ventana entera: literalmente lo que ves tú
- `network` — el editor de nodos (acepta `&path=/obj/geo1`)

## Undo

Cada turno se envuelve en un `hou.undos.group()`, así que **un solo Ctrl+Z
deshace la respuesta entera**, cree Claude uno o veinte nodos. Se desactiva con
`CLAUDE_HOUDINI_UNDO_GROUP=0`.

## Limitaciones

- No persiste el historial del chat entre sesiones de Houdini (el proceso
  `claude` muere al cerrar el panel).
- El modo local no tiene tool use: no puede leer ni tocar la escena.
- Solo Windows probado.

## Troubleshooting

**"No se encontró el binario claude"** → asegúrate de que `claude` está en el
PATH del proceso Houdini. Verifica abriendo el Python Source Editor en Houdini
y ejecutando: `import subprocess; print(subprocess.check_output(["where","claude"]).decode())`.
Si no aparece, setea la env var `CLAUDE_HOUDINI_CLI` con la ruta completa.

**"Puerto ocupado"** → setea `CLAUDE_HOUDINI_PORT=9000` antes de abrir Houdini.

**El modal de confirmación se queda en background** → Houdini > Edit > Preferences >
asegúrate de que las modal dialogs roban foco. O recompila el dialog para usar
`WindowStaysOnTopHint` (ya está activado en `confirmation.py`).

## Roadmap

- [x] ~~Visión real~~ → capturas a disco + `Read` (v0.3)
- [x] ~~Undo de una respuesta entera~~ → `hou.undos.group()` por turno (v0.3)
- [x] ~~Proceso persistente~~ → `--input-format stream-json` (v0.3)
- [x] ~~Modelo local~~ → Qwen3.6 vía Ollama, modo chat (v0.3)
- [ ] Endpoint `/api/help_search` conectado a `vfx-docs-rag` (docs locales de
      Houdini): con el modelo local daría respuestas de sintaxis gratis y sin
      inventarse nombres de nodos
- [ ] Persistir el historial del chat entre sesiones de Houdini
- [ ] Modo local con tool use (Ollama soporta tool calling; calidad por ver)
- [ ] Mostrar un diff en el modal de confirmación (qué cambia exactamente)
- [ ] Slash commands rápidos (`/select`, `/explain`)
