"""Confirmation modal for destructive tool calls.

Called from the HTTP server background thread. Uses hdefereval to marshal the
Qt dialog into Houdini's main thread and block until the user responds.
"""

from __future__ import annotations

import textwrap
from typing import Optional

import hdefereval
from PySide6 import QtCore, QtGui, QtWidgets


def _build_message(tool: str, summary: str, details: Optional[str]) -> str:
    body = [f"<b>Claude quiere ejecutar:</b> <code>{tool}</code>", "", summary]
    if details:
        body.append("")
        body.append("<details><summary>Detalles</summary><pre>")
        body.append(_escape(details))
        body.append("</pre></details>")
    return "<br>".join(body)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _show_modal(tool: str, summary: str, details: Optional[str]) -> bool:
    parent = QtWidgets.QApplication.activeWindow()
    dlg = QtWidgets.QDialog(parent)
    dlg.setWindowTitle("Claude · Confirmar acción")
    dlg.setMinimumWidth(560)
    dlg.setWindowFlags(dlg.windowFlags() | QtCore.Qt.WindowStaysOnTopHint)

    layout = QtWidgets.QVBoxLayout(dlg)

    header = QtWidgets.QLabel(f"<h3>{tool}</h3>")
    layout.addWidget(header)

    summary_lbl = QtWidgets.QLabel(summary)
    summary_lbl.setWordWrap(True)
    summary_lbl.setTextFormat(QtCore.Qt.RichText)
    layout.addWidget(summary_lbl)

    if details:
        det = QtWidgets.QPlainTextEdit()
        det.setReadOnly(True)
        det.setPlainText(textwrap.dedent(details).strip())
        det.setMaximumHeight(280)
        mono = QtGui.QFont("Consolas")
        mono.setStyleHint(QtGui.QFont.Monospace)
        det.setFont(mono)
        layout.addWidget(det)

    btns = QtWidgets.QDialogButtonBox()
    allow_btn = btns.addButton("Aplicar", QtWidgets.QDialogButtonBox.AcceptRole)
    deny_btn = btns.addButton("Cancelar", QtWidgets.QDialogButtonBox.RejectRole)
    allow_btn.setDefault(False)
    deny_btn.setDefault(True)
    layout.addWidget(btns)

    state = {"allowed": False}

    def _allow():
        state["allowed"] = True
        dlg.accept()

    allow_btn.clicked.connect(_allow)
    deny_btn.clicked.connect(dlg.reject)

    dlg.exec()
    return state["allowed"]


def confirm(tool: str, summary: str, details: Optional[str] = None) -> bool:
    """Show a modal in the Houdini main thread; return True if user approved."""
    return bool(hdefereval.executeInMainThreadWithResult(_show_modal, tool, summary, details))
