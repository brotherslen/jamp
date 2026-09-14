"""Encoding-tolerant text reading.

Info files in this library come from twenty years of different taping tools and
are variously utf-8, cp1252 and latin-1.  We never want a UnicodeDecodeError to
take out a whole scan, so we try a chain and record which one won.
"""
from __future__ import annotations

from pathlib import Path

from .winpath import opener

ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


def read_text(path: Path, limit: int = 512_000) -> tuple[str, str]:
    """Return (text, encoding_used).  Never raises on decode problems."""
    with open(opener(path), "rb") as fh:
        raw = fh.read(limit)
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1/replace"


def safe_read_text(path: Path, limit: int = 512_000) -> tuple[str, str]:
    """read_text but also tolerant of IO errors (locked files, long paths)."""
    try:
        return read_text(path, limit)
    except OSError as exc:  # pragma: no cover - environment dependent
        return "", f"unreadable:{exc.__class__.__name__}"
