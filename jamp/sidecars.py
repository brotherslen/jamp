"""Keeping .ffp, .md5, .st5 and .cue honest across a rename.

These files contain the old audio filenames, so renaming the audio silently
breaks every checksum in the folder.  We parse them, map every referenced name
through the rename map, and produce the rewritten text.  If even one reference
cannot be mapped, that file is flagged and left alone - a half-rewritten
checksum file is worse than an out-of-date one.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .textio import safe_read_text
from .winpath import opener

# What a rewrite can write back exactly as it found it.
WRITABLE_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")
# safe_read_text stops reading here, silently; a longer file would be written
# back short.
READ_LIMIT = 512_000

KINDS = ("ffp", "md5", "st5", "cue", "sfv")

REWRITE = "rewrite"
NO_CHANGE = "no_change"
UNRESOLVED = "unresolved"
UNPARSED = "unparsed"

_FFP_LINE = re.compile(r"^(?P<file>.+?):(?P<hash>[0-9a-fA-F]{32})\s*$")
_MD5_LINE = re.compile(r"^(?P<hash>[0-9a-fA-F]{32})\s+\*?(?P<file>.+?)\s*$")
_MD5_BSD = re.compile(r"^MD5\s*\((?P<file>.+?)\)\s*=\s*(?P<hash>[0-9a-fA-F]{32})\s*$", re.I)
_ST5_LINE = re.compile(r"^(?P<hash>[0-9a-fA-F]{32})\s+\[shntool\]\s+\*?(?P<file>.+?)\s*$", re.I)
_SFV_LINE = re.compile(r"^(?P<file>.+?)\s+(?P<hash>[0-9a-fA-F]{8})\s*$")
_CUE_FILE = re.compile(r'^(?P<lead>\s*FILE\s+)(?P<q>"?)(?P<file>.+?)(?P=q)(?P<tail>\s+\w+\s*)$', re.I)

_DISC_SUFFIX = re.compile(r"(d\d{1,2})$", re.I)


@dataclass
class SidecarPlan:
    path: Path
    kind: str
    status: str = UNPARSED
    referenced: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    new_text: str | None = None
    new_name: str | None = None
    encoding: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in (REWRITE, NO_CHANGE)


def kind_for(path: Path) -> str | None:
    """Sidecar kind, tolerating the double extension seen in the wild.

    fingerprint.ffp.txt is an ffp file wearing a .txt coat.
    """
    for suffix in reversed([s.lower().lstrip(".") for s in path.suffixes[-2:]]):
        if suffix in KINDS:
            return suffix
    return None


# Characters Windows will not take in a filename.  A downloader or unzipper
# replaces the lot with "_", so a checksum written against the original name
# no longer matches the file on disk: "It's Ice_.flac" became "It_s Ice_.flac".
_SANITISED = re.compile(r"['\"?:*/\\|<>]")


def _sanitised_key(name: str) -> str:
    return _SANITISED.sub("_", name).lower()


def _sanitised_index(rename_map: dict[str, str]) -> dict[str, str | None]:
    """Rename map keyed by the folded name.

    A fold that two different files share is ambiguous and is dropped rather
    than guessed at - renaming the wrong line is worse than leaving the file.
    """
    out: dict[str, str | None] = {}
    for old, new in rename_map.items():
        key = _sanitised_key(old)
        out[key] = None if key in out else new
    return out


def _rewrite_line(line: str, kind: str, rename_map: dict[str, str],
                  folded: dict[str, str | None] | None = None):
    """Return (new_line, referenced_name or None, resolved: bool)."""
    patterns = {
        "ffp": [(_FFP_LINE, "file")],
        "md5": [(_MD5_LINE, "file"), (_MD5_BSD, "file")],
        "st5": [(_ST5_LINE, "file"), (_MD5_LINE, "file")],
        "sfv": [(_SFV_LINE, "file")],
        "cue": [(_CUE_FILE, "file")],
    }[kind]

    for pattern, group in patterns:
        m = pattern.match(line)
        if not m:
            continue
        old = m.group(group).strip()
        # Checksum files sometimes carry a relative path; only the leaf renames.
        relative = re.sub(r"^\./", "", old.replace("\\", "/"))
        leaf = relative.split("/")[-1]
        # A path names one file even where disc folders repeat the leaf.
        new_leaf = ((rename_map.get(relative) if "/" in relative else None)
                    or rename_map.get(leaf) or rename_map.get(leaf.lower()))
        if new_leaf is None and folded is not None:
            # The name was sanitised on the way to disk; match through the fold.
            new_leaf = folded.get(_sanitised_key(leaf))
        if new_leaf is None:
            return line, leaf, False
        new_old = old[: len(old) - len(leaf)] + new_leaf
        start, end = m.span(group)
        return line[:start] + new_old + line[end:], leaf, True
    return line, None, True


def plan_sidecar(path: Path, rename_map: dict[str, str]) -> SidecarPlan:
    kind = kind_for(path)
    if kind is None:
        return SidecarPlan(path=path, kind="?", status=UNPARSED,
                           notes=["not a sidecar type this tool understands"])

    text, encoding = safe_read_text(path, limit=READ_LIMIT)
    plan = SidecarPlan(path=path, kind=kind, encoding=encoding)
    if encoding.startswith("unreadable"):
        # Not empty: unread.  Calling it empty marked it NO_CHANGE, and the
        # renames around it then left it naming files that no longer exist.
        plan.status = UNPARSED
        plan.notes.append("could not be read (%s)" % encoding.split(":", 1)[-1])
        return plan
    try:
        size = os.stat(opener(path)).st_size
    except OSError:
        size = 0
    if size > READ_LIMIT:
        plan.status = UNPARSED
        plan.notes.append("%d bytes is more than is read, so a rewrite would cut it "
                          "short - left alone" % size)
        return plan
    if encoding == "utf-8-sig":
        # The reader tries utf-8-sig first and it decodes plain UTF-8 too, so
        # it does not say whether there is a BOM.  Writing one back to a file
        # that never had one would change its first three bytes.
        with open(opener(path), "rb") as fh:
            if fh.read(3) != b"\xef\xbb\xbf":
                encoding = plan.encoding = "utf-8"
    if encoding not in WRITABLE_ENCODINGS:
        plan.status = UNPARSED
        plan.notes.append("its encoding (%s) cannot be written back exactly" % encoding)
        return plan
    if not text.strip():
        plan.status = NO_CHANGE
        plan.notes.append("empty file")
        return plan

    newline = "\r\n" if "\r\n" in text else "\n"
    out_lines: list[str] = []
    changed = False
    matched_any = False
    folded = _sanitised_index(rename_map)

    for raw in text.splitlines():
        new_line, referenced, resolved = _rewrite_line(raw, kind, rename_map, folded)
        if referenced is not None:
            matched_any = True
            plan.referenced.append(referenced)
            if not resolved:
                plan.unresolved.append(referenced)
        if new_line != raw:
            changed = True
        out_lines.append(new_line)

    if not matched_any:
        plan.status = UNPARSED
        plan.notes.append("no %s entries recognised in this file" % kind)
        return plan

    if plan.unresolved:
        plan.status = UNRESOLVED
        plan.notes.append(
            "%d of %d entries do not match any audio file being renamed: %s"
            % (len(plan.unresolved), len(plan.referenced),
               ", ".join(sorted(set(plan.unresolved))[:5]))
        )
        return plan

    plan.new_text = newline.join(out_lines) + (newline if text.endswith(("\n", "\r")) else "")
    try:
        plan.new_text.encode(encoding)
    except UnicodeEncodeError:
        plan.status = UNRESOLVED
        plan.new_text = None
        plan.notes.append("the new names cannot be written in its encoding (%s)" % encoding)
        return plan
    plan.status = REWRITE if changed else NO_CHANGE
    return plan


def propose_sidecar_name(path: Path, old_folder_name: str, new_folder_name: str) -> str | None:
    """Rename a sidecar that is named after the folder (um2001-06-02d1.md5)."""
    stem = path.stem
    if stem.lower() == old_folder_name.lower():
        return new_folder_name + path.suffix.lower()
    if stem.lower().startswith(old_folder_name.lower()):
        tail = stem[len(old_folder_name):]
        m = _DISC_SUFFIX.search(tail)
        if m and tail == m.group(1):
            return new_folder_name + m.group(1).lower() + path.suffix.lower()
    return None
