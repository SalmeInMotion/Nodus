"""Offline Houdini documentation lookup over consolidated markdown volumes.

The corpus is a folder of markdown files (one per domain: SOP, LOP, VEX, hou
API…) where every node or topic is a `## Title (context/internalname)` header.
That shape makes two cheap operations cover most documentation needs:

- exact lookup: find the header for a node the caller already knows the name of
- keyword search: score headers against query words and return whole sections

No embeddings, no index, no external dependencies — a scan of ~30 MB of text is
tens of milliseconds on any SSD, and *whole sections* beat top-k fragment
retrieval when the consumer is a model that can read.

Deliberately importable without `hou`: the MCP adapter runs outside Houdini and
uses this module directly.

The corpus itself is not distributed with Nodus (the text belongs to SideFX);
it is generated locally from the help that ships inside every Houdini install.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import config

_HEADER = re.compile(r"^## (.+?)(?:\s*\(([^)]+)\))?\s*$", re.MULTILINE)

# Words too common to distinguish anything in a docs query.
_STOP = frozenset(
    ("a an and are as at be by for from how in is it of on or the to what "
     "which with node nodes houdini use using").split()
)


def corpus_dir() -> Path:
    return config.DOCS_DIR


def available() -> bool:
    try:
        return any(corpus_dir().glob("*.md"))
    except OSError:
        return False


def volumes() -> list[str]:
    try:
        return sorted(p.name for p in corpus_dir().glob("*.md"))
    except OSError:
        return []


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_.]+", text.lower())
            if w not in _STOP and len(w) > 1}


def _iter_sections(path: Path):
    """Yield (title, internal, start, end, text_getter) per `## ` section."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    matches = list(_HEADER.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        yield m.group(1).strip(), (m.group(2) or "").strip(), text[m.start():end]


def find_node(name: str, max_chars: int = 12000) -> dict | None:
    """Exact-ish lookup: 'Time Shift', 'timeshift' or 'sop/timeshift'."""
    want = name.strip().lower()
    want_flat = re.sub(r"[^a-z0-9]", "", want)
    best = None
    for path in corpus_dir().glob("*.md"):
        for title, internal, body in _iter_sections(path):
            t = title.lower()
            i = internal.lower()
            exact = (t == want or i == want or i.endswith("/" + want)
                     or re.sub(r"[^a-z0-9]", "", t) == want_flat
                     or i.split("/")[-1] == want_flat)
            if exact:
                return {"title": title, "internal": internal,
                        "file": path.name, "text": body[:max_chars]}
            if best is None and want in t:
                best = {"title": title, "internal": internal,
                        "file": path.name, "text": body[:max_chars]}
    return best


def search(query: str, max_sections: int = 3,
           max_chars: int = 7000) -> list[dict]:
    """Keyword search over section headers, whole sections back.

    Headers only, on purpose: scoring 30 MB of body text per query costs more
    than it returns, and in this corpus the header names the thing — a query
    that matches no header is better served by find_node or a broader term.
    """
    words = _tokens(query)
    if not words:
        return []

    scored: list[tuple[float, str, str, str, str]] = []
    for path in corpus_dir().glob("*.md"):
        fname_bonus = 0.5 if words & _tokens(path.stem.replace("-", " ")) else 0.0
        for title, internal, body in _iter_sections(path):
            header_words = _tokens(title) | _tokens(internal.replace("/", " "))
            overlap = words & header_words
            if not overlap:
                continue
            score = len(overlap) / len(words) + fname_bonus
            scored.append((score, title, internal, path.name, body))

    scored.sort(key=lambda item: -item[0])
    out, used = [], 0
    for score, title, internal, fname, body in scored[: max_sections * 3]:
        take = body[: max(1000, max_chars - used)]
        out.append({"score": round(score, 2), "title": title,
                    "internal": internal, "file": fname, "text": take})
        used += len(take)
        if len(out) >= max_sections or used >= max_chars:
            break
    return out
