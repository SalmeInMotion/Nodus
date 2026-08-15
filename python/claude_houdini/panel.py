"""PySide6 chat panel: the UI users interact with from Houdini's Python Panel.

Notable behaviours:
- Backend dropdown: Anthropic models (agentic, scene access) or a local Ollama
  model (chat only). The answer is labelled with whichever one produced it.
- "Dev" toggle: off by default, which filters noisy CLI events and truncates
  tool calls/results to one line with click-to-expand.
- The bearer token is masked everywhere it would otherwise be displayed.
- Keyboard focus is forced down into the text box; Houdini gives it to the
  panel's root widget, where keystrokes would be silently dropped.
"""

from __future__ import annotations

import html
import os
import re
import traceback
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from . import config, server, state, system_prompt
from .cli_runner import ClaudeWorker, StreamEvent


_INSTANCE: Optional["ChatPanel"] = None


# Truncation limits (in chars) when NOT in dev mode.
_TRUNC_TOOL_USE = 100
_TRUNC_TOOL_RESULT = 140
_TRUNC_TEXT_EXPANDED = 5000  # max we will ever show inside the expand modal

# Regex patterns of `info` events we hide in standard mode.
_NOISY_INFO_PATTERNS = [
    re.compile(r"^\$\s+claude\s"),       # the full spawn command line
    re.compile(r"^session:\s"),        # session uuid
    re.compile(r"^\[evt\s"),              # unknown / passthrough events
    re.compile(r"^\[raw\]\s"),            # un-parseable JSON lines
    re.compile(r"^\[rate limit\]"),       # fires ~once per turn, pure noise
]


# ============================================================
# Entry points called by the .pypanel descriptor
# ============================================================

def create_widget() -> QtWidgets.QWidget:
    """Return a fresh widget for this panel instance."""
    global _INSTANCE
    panel = ChatPanel()
    _INSTANCE = panel
    return panel


def on_destroy() -> None:
    global _INSTANCE
    if _INSTANCE is not None:
        _INSTANCE.shutdown()
        _INSTANCE = None


# ============================================================
# Panel
# ============================================================

# Anthropic entries are fixed; local entries are discovered from whatever
# Ollama has pulled (chat-capable, >=4B — see local_runner). The menu is built
# once per panel: (label, backend id, model id).
_CLAUDE_BACKENDS = [
    ("Opus 5",   "claude", "claude-opus-5"),
    ("Sonnet 5", "claude", "claude-sonnet-5"),
]


def _discover_backends() -> list[tuple[str, str, str]]:
    from . import local_runner

    entries = list(_CLAUDE_BACKENDS)
    models = local_runner.installed_local_models()
    for m in models:
        label = f"{m['name']} · local"
        if m.get("gb"):
            label += f" ({m['gb']:g} GB)"
        entries.append((label, "local", m["name"]))
    if not models:
        # Ollama down or empty: keep one local entry so the mode stays
        # reachable; the worker autostarts Ollama on first use.
        entries.append((f"{local_runner.local_model()} · local", "local",
                        local_runner.local_model()))
    return entries

# Colour per speaker, so who said what is readable at a glance.
_SPEAKER_COLORS = {
    "claude": "#b6f0a3",
    "local": "#e0b0ff",
}

# Keys a text box has no use for: forwarding them would bounce back up here.
_BARE_MODIFIERS = frozenset({
    QtCore.Qt.Key_Shift, QtCore.Qt.Key_Control, QtCore.Qt.Key_Alt,
    QtCore.Qt.Key_AltGr, QtCore.Qt.Key_Meta, QtCore.Qt.Key_CapsLock,
    QtCore.Qt.Key_NumLock, QtCore.Qt.Key_ScrollLock,
})


class ChatPanel(QtWidgets.QWidget):
    submit_claude = QtCore.Signal(str)
    submit_local = QtCore.Signal(str)

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("ClaudeChatPanel")

        self._busy = False
        self._streaming = False
        self._forwarding = False
        self._worker: Optional[ClaudeWorker] = None
        self._thread: Optional[QtCore.QThread] = None
        self._local_worker = None
        self._local_thread: Optional[QtCore.QThread] = None
        self._backend = state.backend()

        # Restore the persisted local model before anything reads the env.
        if state.local_model():
            from .local_runner import LOCAL_MODEL_ENV
            os.environ.setdefault(LOCAL_MODEL_ENV, state.local_model())

        # Token currently in use by the server (masked in displays).
        self._bearer: Optional[str] = None
        # Storage of full contents for click-to-expand. Key: short id, value: full text.
        self._expandable: dict[str, dict] = {}
        self._expand_counter = 0

        self._build_ui()
        self._start_server_and_worker()

    # ---------- UI ----------

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # Top status bar
        bar = QtWidgets.QHBoxLayout()
        self._status = QtWidgets.QLabel("Starting…")
        self._status.setStyleSheet("color: #888;")
        bar.addWidget(self._status)
        bar.addStretch(1)

        self._backends = _discover_backends()
        self._backend_combo = QtWidgets.QComboBox()
        for label, _backend, _model in self._backends:
            self._backend_combo.addItem(label)
        self._backend_combo.setToolTip(
            "Opus / Sonnet: agentic, they see and modify your scene (uses tokens).\n"
            "Local models (via Ollama): run on your GPU, free and offline, but\n"
            "with NO scene access - VEX, node and syntax questions only."
        )
        self._backend_combo.setCurrentIndex(self._initial_backend_index())
        self._backend_combo.currentIndexChanged.connect(self._on_backend_changed)
        bar.addWidget(self._backend_combo)

        self._auto_chk = QtWidgets.QCheckBox("Autonomous")
        self._auto_chk.setToolTip(
            "When on, destructive changes (create/modify/delete nodes, run_python) are "
            "applied without asking. They still show up in the log."
        )
        self._auto_chk.setChecked(state.auto_mode())
        self._auto_chk.toggled.connect(self._on_auto_toggled)
        bar.addWidget(self._auto_chk)

        self._dev_chk = QtWidgets.QCheckBox("Dev")
        self._dev_chk.setToolTip(
            "When on, shows raw CLI events (spawn command, rate limits, full tracebacks) "
            "and does NOT truncate tool calls or results. Useful to debug the runner."
        )
        self._dev_chk.setChecked(state.dev_mode())
        self._dev_chk.toggled.connect(self._on_dev_toggled)
        bar.addWidget(self._dev_chk)

        self._restart_btn = QtWidgets.QToolButton()
        self._restart_btn.setText("Restart server")
        self._restart_btn.setToolTip("If the internal HTTP server died, press this to bring it back up.")
        self._restart_btn.clicked.connect(self._on_restart_server)
        bar.addWidget(self._restart_btn)

        self._reset_btn = QtWidgets.QToolButton()
        self._reset_btn.setText("New session")
        self._reset_btn.setToolTip("Forget the current history and start a fresh conversation.")
        self._reset_btn.clicked.connect(self._on_reset_session)
        bar.addWidget(self._reset_btn)

        self._cancel_btn = QtWidgets.QToolButton()
        self._cancel_btn.setText("Cancel")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._on_cancel)
        bar.addWidget(self._cancel_btn)

        layout.addLayout(bar)

        # Chat history
        self._chat = QtWidgets.QTextBrowser()
        self._chat.setOpenLinks(False)   # we intercept clicks ourselves
        self._chat.setOpenExternalLinks(False)
        self._chat.anchorClicked.connect(self._on_anchor_clicked)
        self._chat.setStyleSheet(
            "QTextBrowser { background:#1e1e1e; color:#ddd; "
            "border:1px solid #333; padding:6px; font-size:11pt; }"
        )
        layout.addWidget(self._chat, stretch=1)

        # Input area
        self._input = _SubmittablePlainTextEdit()
        self._input.setPlaceholderText("Type your prompt here (Ctrl+Enter to send)…")
        self._input.setFixedHeight(96)
        self._input.submit_requested.connect(self._on_submit)
        layout.addWidget(self._input)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        self._send_btn = QtWidgets.QPushButton("Send  (Ctrl+Enter)")
        self._send_btn.clicked.connect(self._on_submit)
        btn_row.addWidget(self._send_btn)
        layout.addLayout(btn_row)

        # Houdini hands keyboard focus to the *root* widget of a Python Panel,
        # and it takes it back from any child that grabs it (measured: the text
        # box gets FocusIn on click, then FocusOut straight to this panel).
        # A focus proxy sends that focus down to the box where it belongs.
        self._input.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setFocusProxy(self._input)

        self._append_system("Panel initialised. Waiting for the server…")

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802
        """Safety net for the focus quirk above.

        Keystrokes do reach Qt — they arrive here, on the container — so if the
        proxy did not move focus into the box, forward them by hand instead of
        dropping them.

        Two things this must not do:
        - Recurse. A QPlainTextEdit *ignores* keys it has no use for (a bare
          Shift/Ctrl/Alt), and Qt then propagates them back up to this parent,
          which would forward them down again, forever. `_forwarding` breaks
          the cycle, and bare modifiers are not forwarded at all.
        - Swallow keys while the box is read-only (mid-answer).
        """
        if (self._forwarding
                or event.key() in _BARE_MODIFIERS
                or not self._input.isEnabled()
                or self._input.isReadOnly()):
            super().keyPressEvent(event)
            return

        self._forwarding = True
        try:
            self._input.setFocus(QtCore.Qt.OtherFocusReason)
            QtWidgets.QApplication.sendEvent(self._input, event)
        finally:
            self._forwarding = False

    # ---------- Backend selection ----------

    def _initial_backend_index(self) -> int:
        want_backend = state.backend()
        want_model = (state.local_model() if want_backend == "local"
                      else state.anthropic_model())
        fallback = None
        for i, (_label, backend, model) in enumerate(self._backends):
            if backend != want_backend:
                continue
            if model == want_model:
                return i
            fallback = fallback if fallback is not None else i
        return fallback if fallback is not None else 0

    @QtCore.Slot(int)
    def _on_backend_changed(self, index: int) -> None:
        if self._busy:
            QtWidgets.QMessageBox.information(
                self, "Busy", "An answer is in progress. Cancel it first."
            )
            self._backend_combo.blockSignals(True)
            self._backend_combo.setCurrentIndex(self._initial_backend_index())
            self._backend_combo.blockSignals(False)
            return

        label, backend, model = self._backends[index]
        self._backend = backend
        state.set_backend(backend)

        if backend == "local":
            from .local_runner import LOCAL_MODEL_ENV
            state.set_local_model(model)
            os.environ[LOCAL_MODEL_ENV] = model
            self._ensure_local_worker()
            self._append_system(
                f"Backend: <b>{label}</b> - on your GPU, free, with no scene access. "
                "Switch back to Opus/Sonnet for it to see or touch nodes."
            )
            return

        state.set_anthropic_model(model)
        os.environ[config.MODEL_ENV] = model
        # config.model() is read when the process spawns, so drop the current
        # one; the next prompt starts a fresh process on the new model.
        if self._worker is not None:
            self._worker.reset_session()
        self._append_system(
            f"Backend: <b>{label}</b>. The conversation restarts (new process)."
        )

    def _ensure_local_worker(self):
        if self._local_worker is not None:
            return self._local_worker
        from .local_runner import LocalWorker

        self._local_thread = QtCore.QThread(self)
        self._local_worker = LocalWorker()
        self._local_worker.moveToThread(self._local_thread)
        self._local_worker.event.connect(self._on_worker_event)
        self._local_worker.finished.connect(self._on_worker_finished)
        self.submit_local.connect(self._local_worker.send)
        self._local_thread.start()
        return self._local_worker

    def _active_worker(self):
        if self._backend == "local":
            return self._ensure_local_worker()
        return self._worker

    # ---------- Worker / server ----------

    def _start_server_and_worker(self) -> None:
        try:
            url, token = server.start()
        except OSError as e:
            self._append_error(f"Could not start the HTTP server: {e}\n"
                               "The port is probably taken. Set CLAUDE_HOUDINI_PORT "
                               "to another value and restart Houdini.")
            return

        self._bearer = token
        prompt = system_prompt.build(url, token)

        # Honour the model persisted from a previous session before the
        # process spawns (config.model() reads the env at spawn time).
        os.environ.setdefault(config.MODEL_ENV, state.anthropic_model())

        self._thread = QtCore.QThread(self)
        self._worker = ClaudeWorker(prompt)
        self._worker.moveToThread(self._thread)
        self._worker.event.connect(self._on_worker_event)
        self._worker.finished.connect(self._on_worker_finished)
        self.submit_claude.connect(self._worker.send)
        self._thread.start()

        self._status.setText(f"✓ Server: {url} · new session")
        self._append_system(
            f"Server listening on <code>{url}</code>. "
            "Claude uses this endpoint to read and modify your scene."
        )

    def shutdown(self) -> None:
        # The HTTP server is a process-wide singleton tied to the Houdini
        # session; we deliberately leave it running across panel close/open.
        # The `claude` process, however, belongs to this panel — kill it or it
        # outlives the tab.
        for worker in (self._worker, self._local_worker):
            if worker is not None:
                try:
                    worker.shutdown()
                except Exception:
                    pass
        for thread in (self._thread, self._local_thread):
            if thread is not None:
                thread.quit()
                thread.wait(2000)

    # ---------- Slots ----------

    @QtCore.Slot()
    def _on_submit(self) -> None:
        if self._busy:
            return
        text = self._input.toPlainText().strip()
        if not text:
            return

        local = self._backend == "local"
        if not local and self._worker is None:
            return
        if local:
            self._ensure_local_worker()

        self._input.clear()
        self._append_user(text)
        self._set_busy(True)
        # The local backend cannot touch the scene, so there is nothing to undo.
        if not local:
            self._begin_undo_group(text)
        (self.submit_local if local else self.submit_claude).emit(text)

    @QtCore.Slot()
    def _on_restart_server(self) -> None:
        try:
            server.stop()
        except Exception:
            pass
        try:
            url, token = server.start()
            self._bearer = token
            self._append_system(f"Server restarted on <code>{url}</code>.")
            # New token => the running `claude` process holds a stale one, so
            # hand over the rebuilt prompt; the worker restarts itself.
            if self._worker is not None:
                self._worker.set_system_prompt(system_prompt.build(url, token))
        except Exception as e:
            self._append_error(f"Could not restart the server: {e}")

    @QtCore.Slot(bool)
    def _on_auto_toggled(self, checked: bool) -> None:
        state.set_auto_mode(checked)
        if checked:
            self._append_system(
                "⚠ Autonomous mode ON. Changes will be applied without asking."
            )
        else:
            self._append_system(
                "Autonomous mode off. Destructive changes will ask for confirmation again."
            )

    @QtCore.Slot(bool)
    def _on_dev_toggled(self, checked: bool) -> None:
        state.set_dev_mode(checked)
        self._append_system(
            "Dev mode ON - raw CLI events are shown." if checked
            else "Dev mode off - compact view."
        )

    @QtCore.Slot()
    def _on_reset_session(self) -> None:
        if self._busy:
            QtWidgets.QMessageBox.information(
                self, "Busy", "An answer is in progress. Cancel it first."
            )
            return
        for worker in (self._worker, self._local_worker):
            if worker is not None:
                worker.reset_session()
        self._chat.clear()
        self._expandable.clear()
        self._streaming = False
        self._append_system("Session reset. No previous context.")
        self._status.setText(self._status.text().split("·")[0].strip() + " · new session")

    @QtCore.Slot()
    def _on_cancel(self) -> None:
        worker = self._active_worker()
        if worker is not None:
            worker.cancel()

    @QtCore.Slot(object)
    def _on_worker_event(self, evt: StreamEvent) -> None:
        try:
            if evt.kind == "text_delta":
                self._stream_text(evt.payload.get("text", ""))
            elif evt.kind == "text_done":
                self._end_stream()
            elif evt.kind == "text":
                self._end_stream()
                self._append_assistant(evt.payload.get("text", ""))
            elif evt.kind == "tool_use":
                self._end_stream()
                name = evt.payload.get("name", "?")
                inp = evt.payload.get("input") or {}
                self._append_tool_use(name, inp)
            elif evt.kind == "tool_result":
                self._end_stream()
                content = evt.payload.get("content") or ""
                self._append_tool_result(content, bool(evt.payload.get("is_error")))
            elif evt.kind == "info":
                self._append_info(evt.payload.get("message", ""))
            elif evt.kind == "error":
                self._end_stream()
                self._append_error(evt.payload.get("message", "(error)"))
        except Exception:
            self._append_error(traceback.format_exc())

    @QtCore.Slot(bool, str)
    def _on_worker_finished(self, ok: bool, final: str) -> None:
        self._end_stream()
        self._end_undo_group()
        self._set_busy(False)
        if not ok and final:
            self._append_error(final)

    # ---------- undo grouping ----------

    def _begin_undo_group(self, prompt: str) -> None:
        """Collapse everything this turn does into one Ctrl+Z."""
        try:
            from . import tools
            label = "Claude: " + (prompt[:40] + "…" if len(prompt) > 40 else prompt)
            tools.undo_begin(label)
        except Exception as e:
            self._append_info(f"could not open the undo group: {e}")

    def _end_undo_group(self) -> None:
        try:
            from . import tools
            tools.undo_end()
        except Exception as e:
            self._append_info(f"could not close the undo group: {e}")

    @QtCore.Slot(QtCore.QUrl)
    def _on_anchor_clicked(self, url: QtCore.QUrl) -> None:
        s = url.toString()
        if s.startswith("claude://expand/"):
            block_id = s[len("claude://expand/"):]
            entry = self._expandable.get(block_id)
            if entry:
                self._show_full(entry["title"], entry["body"])

    # ---------- Expand modal ----------

    def _show_full(self, title: str, body: str) -> None:
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(900, 600)
        v = QtWidgets.QVBoxLayout(dlg)
        viewer = QtWidgets.QPlainTextEdit()
        viewer.setReadOnly(True)
        viewer.setPlainText(body[:_TRUNC_TEXT_EXPANDED])
        mono = QtGui.QFont("Consolas")
        mono.setStyleHint(QtGui.QFont.Monospace)
        viewer.setFont(mono)
        v.addWidget(viewer)
        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        v.addWidget(btns)
        dlg.exec()

    def _register_expandable(self, title: str, body: str) -> str:
        self._expand_counter += 1
        eid = f"x{self._expand_counter}"
        self._expandable[eid] = {"title": title, "body": body}
        return eid

    # ---------- Rendering ----------

    def _append_user(self, text: str) -> None:
        self._append_block("You", text, color="#7ab8ff")

    def _append_assistant(self, text: str) -> None:
        if not text.strip():
            return
        self._append_block(self._speaker_label(), text,
                           color=_SPEAKER_COLORS.get(self._backend, "#b6f0a3"))

    def _speaker_label(self) -> str:
        """Name the model that is actually answering, not a generic 'Claude'."""
        try:
            return self._backend_combo.currentText()
        except Exception:
            return "Claude"

    # --- incremental streaming ---

    def _stream_text(self, chunk: str) -> None:
        """Append a token chunk to the live answer block, opening it first.

        Inserted as plain text, not HTML: HTML collapses runs of spaces and
        drops them at chunk boundaries, which shredded every answer into
        `ElproblemaesqueRBD…`. insertText keeps spaces, newlines and the
        indentation of code exactly as the model wrote them.
        """
        if not chunk:
            return
        if not self._streaming:
            color = _SPEAKER_COLORS.get(self._backend, "#b6f0a3")
            # Explicit <br>, same as _append_block's label/body split — a lone
            # </div> boundary wasn't enough to keep the raw insertText below
            # from landing on the same visual line as the label.
            self._append_html(
                f"<div style='margin-top:8px;'>"
                f"<span style='color:{color};font-weight:bold;'>"
                f"{html.escape(self._speaker_label())}</span><br></div>",
                newline=False,
            )
            self._streaming = True

        cursor = self._chat.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        fmt = QtGui.QTextCharFormat()
        fmt.setForeground(QtGui.QColor("#dddddd"))
        cursor.insertText(chunk, fmt)
        self._chat.setTextCursor(cursor)
        self._chat.verticalScrollBar().setValue(
            self._chat.verticalScrollBar().maximum())

    def _end_stream(self) -> None:
        if self._streaming:
            self._streaming = False
            self._append_html("", newline=True)

    def _break_stream_line(self) -> None:
        """Move to a fresh line without closing out the streaming block.

        For an `info` line landing mid-answer (server restarted, cancelled,
        rate limit) — a full `_end_stream()` would work too, but it flips
        `_streaming` off, so the *next* delta would reprint the model's name
        as if a new answer had started. This just breaks the line; the
        following delta keeps writing right where the reader left off.
        """
        if self._streaming:
            self._append_html("", newline=True)

    def _append_tool_use(self, name: str, inp: dict) -> None:
        full = inp.get("command") or inp.get("code") or ""
        masked = self._mask_secrets(full)

        if state.dev_mode():
            summary = f"<b style='color:#cdb682;'>→ {html.escape(name)}</b>"
            if masked:
                summary += (
                    f"<pre style='color:#cdb682;margin:2px 0 6px 0;"
                    f"white-space:pre-wrap;'>{html.escape(_truncate(masked, 1400))}</pre>"
                )
            self._append_html(summary)
            return

        # Standard mode: 1 line + expand link
        first_line = _summary_command(masked)
        eid = self._register_expandable(f"→ {name} (full)", masked)
        self._append_html(
            f"<span style='color:#cdb682;'>→ <b>{html.escape(name)}</b> "
            f"<span style='color:#a08a55;'>{html.escape(first_line)}</span> "
            f"<a href='claude://expand/{eid}' style='color:#6a8caf;"
            f"text-decoration:none;font-size:9pt;'>[show full]</a></span>"
        )

    def _append_tool_result(self, content: str, is_error: bool) -> None:
        color = "#e98a8a" if is_error else "#888"
        header = "← error" if is_error else "← result"
        body = self._mask_secrets(str(content))

        if state.dev_mode():
            esc = html.escape(_truncate(body, 1400))
            self._append_html(
                f"<span style='color:{color};'>{header}</span>"
                f"<pre style='color:{color};margin:2px 0 6px 0;"
                f"white-space:pre-wrap;'>{esc}</pre>"
            )
            return

        # Standard mode: 1-line summary + expand link
        summary = _result_summary(body, is_error)
        eid = self._register_expandable(f"{header} (full)", body)
        self._append_html(
            f"<span style='color:{color};'>{html.escape(header)} "
            f"<span style='color:#9a9a9a;'>{html.escape(summary)}</span> "
            f"<a href='claude://expand/{eid}' style='color:#6a8caf;"
            f"text-decoration:none;font-size:9pt;'>[show full]</a></span>"
        )

    def _append_info(self, text: str) -> None:
        # Filter noisy info messages in standard mode.
        if not state.dev_mode():
            for pat in _NOISY_INFO_PATTERNS:
                if pat.match(text):
                    return
        self._break_stream_line()
        self._append_html(
            f"<span style='color:#666;font-size:9pt;'>· {html.escape(text)}</span>"
        )

    def _append_error(self, text: str) -> None:
        masked = self._mask_secrets(text)
        if state.dev_mode():
            self._append_html(
                f"<pre style='color:#ff6464;white-space:pre-wrap;'>{html.escape(masked)}</pre>"
            )
            return
        # Standard mode: error message in red, with expand for full traceback
        first = masked.splitlines()[0] if masked else "(error)"
        eid = self._register_expandable("Error (full)", masked)
        self._append_html(
            f"<span style='color:#ff6464;'>⚠ {html.escape(first[:180])} "
            f"<a href='claude://expand/{eid}' style='color:#6a8caf;"
            f"text-decoration:none;font-size:9pt;'>[show full]</a></span>"
        )

    def _append_system(self, text_html: str) -> None:
        self._append_html(
            f"<span style='color:#888;font-style:italic;'>{text_html}</span>"
        )

    def _append_block(self, who: str, text: str, *, color: str) -> None:
        # pre-wrap keeps indentation: people paste VEX in here, and plain HTML
        # would collapse every run of spaces into one.
        body = html.escape(text).replace("\n", "<br>")
        self._append_html(
            f"<div style='margin-top:8px;'>"
            f"<span style='color:{color};font-weight:bold;'>{html.escape(who)}</span><br>"
            f"<span style='color:#ddd;white-space:pre-wrap;'>{body}</span></div>"
        )

    def _append_html(self, html_str: str, newline: bool = True) -> None:
        cursor = self._chat.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        self._chat.setTextCursor(cursor)
        self._chat.insertHtml(html_str + ("<br>" if newline else ""))
        self._chat.verticalScrollBar().setValue(self._chat.verticalScrollBar().maximum())

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._send_btn.setEnabled(not busy)
        self._input.setReadOnly(busy)
        self._cancel_btn.setEnabled(busy)
        self._send_btn.setText("Thinking…" if busy else "Send  (Ctrl+Enter)")

    # ---------- Helpers ----------

    def _mask_secrets(self, text: str) -> str:
        if self._bearer and self._bearer in text:
            return text.replace(self._bearer, "***")
        return text


class _SubmittablePlainTextEdit(QtWidgets.QPlainTextEdit):
    submit_requested = QtCore.Signal()

    def keyPressEvent(self, e: QtGui.QKeyEvent) -> None:  # noqa: N802
        if e.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            if e.modifiers() & QtCore.Qt.ControlModifier:
                self.submit_requested.emit()
                return
        super().keyPressEvent(e)


# ============================================================
# Module helpers
# ============================================================

def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _summary_command(cmd: str) -> str:
    """Extract a one-line summary of a (possibly multi-line) shell command."""
    if not cmd:
        return ""
    # Split by ; or newline, keep the LAST non-trivial statement (usually the
    # real curl / python call after env var setup).
    parts = re.split(r";\s*|\n", cmd)
    parts = [p.strip() for p in parts if p.strip()]
    chosen = parts[-1] if parts else cmd.strip()
    # Collapse internal whitespace
    chosen = re.sub(r"\s+", " ", chosen)
    return _truncate(chosen, _TRUNC_TOOL_USE)


def _result_summary(body: str, is_error: bool) -> str:
    """Extract a one-line summary of a tool result body."""
    if not body:
        return "(empty)"
    # If it parses as our API JSON, give a meaningful summary
    body_strip = body.strip()
    if body_strip.startswith("{"):
        # Cheap regex sniffing — no full JSON parse to stay fast
        if '"ok": true' in body_strip or '"ok":true' in body_strip:
            # Try to capture count / path / created node hint
            m = re.search(r'"count":\s*(\d+)', body_strip)
            if m:
                return f"✓ {m.group(1)} items"
            m = re.search(r'"path":\s*"([^"]+)"', body_strip)
            if m:
                return f"✓ {m.group(1)}"
            return "✓ ok"
        if '"error"' in body_strip or '"is_error": true' in body_strip:
            m = re.search(r'"error":\s*"([^"]+)"', body_strip)
            if m:
                return "✗ " + _truncate(m.group(1), 100)
            return "✗ error"
    # Fallback: first non-empty line
    first = next((ln for ln in body.splitlines() if ln.strip()), body)
    first = re.sub(r"\s+", " ", first.strip())
    return _truncate(first, _TRUNC_TOOL_RESULT)
