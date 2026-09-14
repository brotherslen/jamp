"""Reaching files whose path is longer than Windows' default limit.

Prefixing an absolute path with \\\\?\\ turns off the 260-character MAX_PATH
rule.  Without it, a folder nested a few levels deep with long names simply
cannot be opened - Python reports "No such file or directory" for a file that
is plainly there, which is a confusing way to lose data.

Everything that opens, reads, writes or renames a file goes through here.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Leave headroom: the limit applies to the whole path, and callers append
# filenames to directories they got from us.
THRESHOLD = 240


def extended(path: Path | str) -> str:
    """A form of `path` that Windows will open however long it is."""
    text = os.fspath(path)
    if sys.platform != "win32":
        return text
    if text.startswith("\\\\?\\"):
        return text
    absolute = os.path.abspath(text)
    if len(absolute) < THRESHOLD:
        return text
    if absolute.startswith("\\\\"):                  # UNC: \\server\share
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def opener(path: Path | str):
    """Path to hand to open()/mutagen, extended only when it needs to be."""
    return extended(path)


def is_too_long(path: Path | str, limit: int = 260) -> bool:
    return len(os.fspath(path)) > limit
