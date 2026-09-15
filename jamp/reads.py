"""What each audio file said when it was last read, and when that still holds.

Reading tags is nearly all of what a plan costs.  On Phish, 9,787 files took
229 s from a cold disk and 14 s warm, against 6 s for analysing the lot and 1 s
for planning.  A commit used to read everything again on every pass, and once
more afterwards to count what was left, although almost nothing had changed
since the dry run a few minutes earlier.

So phase 1 keeps what it read, and phase 2 reuses a read for any file whose
size and modification time are what they were.  The analysis still runs, the
same code on the same input: this saves the disk, not the thinking, and there
is no second reader of the plan to disagree with the first.

A tag editor told to keep file dates can change a file without changing either,
and a read reused across that is stale.  The dry run read the same stale
answer, so what the commit does is still what was read.
"""
from __future__ import annotations

import copy
import gzip
import json
import os
from dataclasses import fields
from pathlib import Path

from . import __version__
from .audio import AudioFile, parse_track_name, read_audio_file
from .winpath import opener

READS_NAME = "phase1_reads.json.gz"
READS_FORMAT = 1

# Saved per file beside its path and stamp; name_info is not, being worked out
# from the name alone.
_SAVED = tuple(f.name for f in fields(AudioFile)
               if f.name not in ("path", "size", "name_info"))


def file_stamp(path: Path | str) -> tuple[int, int] | None:
    """(size, modification time in ns), or None if the file cannot be seen."""
    try:
        st = os.stat(opener(path))
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


# A file's modification time moves in steps, coarse ones on some file systems,
# so a file changed twice within one step keeps its size and time.  A read taken
# between the two would be reused although it no longer holds - the test that
# wrote a tag straight after creating a file found exactly that.  A file
# modified this recently is read, but its read is not kept (git's "racy" rule).
RACY_NS = 3 * 1_000_000_000


def _too_recent(stamp: tuple[int, int]) -> bool:
    import time

    return time.time_ns() - stamp[1] < RACY_NS


def folder_fingerprint(show) -> str:
    """A digest of every file the analysis of `show` reads, as it is on disk.

    Audio, text and sidecar files by name, size and modification time, and the
    state file, which decides whether a folder is settled.  Artwork is left
    out: nothing about a plan depends on it.
    """
    import hashlib

    paths = [f.path for f in show.files] + list(show.texts)
    for group in show.sidecars.values():
        paths.extend(group)
    paths.append(Path(show.path) / ".etree_state.json")
    h = hashlib.sha1(str(show.path).encode("utf-8"))
    for p in sorted({str(p) for p in paths}):
        stamp = file_stamp(p)
        h.update(("\n%s\t%s" % (p, "absent" if stamp is None
                                else "%d\t%d" % stamp)).encode("utf-8"))
    return h.hexdigest()


class ReadCache:
    """Audio reads keyed by path, each good only while its stamp matches."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[tuple[int, int], AudioFile]] = {}
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._entries)

    def read(self, path: Path, read_tags: bool = True) -> AudioFile:
        # A read without tags is a different answer; never mix the two.
        if not read_tags:
            return read_audio_file(path, read_tags=False)
        key = str(path)
        stamp = file_stamp(path)
        held = self._entries.get(key)
        if stamp is not None and held is not None and held[0] == stamp:
            self.hits += 1
            # A copy: the scan writes a folder's disc number into name_info,
            # and a later pass must start from what the file said.
            return copy.deepcopy(held[1])
        self.misses += 1
        af = read_audio_file(path, read_tags=True)
        if stamp is not None and not _too_recent(stamp):
            self._entries[key] = (stamp, copy.deepcopy(af))
        else:
            self._entries.pop(key, None)
        return af

    # -- on disk ------------------------------------------------------------

    def save(self, out_dir: Path) -> Path:
        path = Path(out_dir) / READS_NAME
        data = {
            "reads_format": READS_FORMAT,
            # read_audio_file changes between versions; a read by another
            # version is not the answer this one would give.
            "jamp_version": __version__,
            "files": [
                {"path": key, "size": stamp[0], "mtime_ns": stamp[1],
                 **{name: getattr(af, name) for name in _SAVED}}
                for key, (stamp, af) in sorted(self._entries.items())
            ],
        }
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path

    @classmethod
    def load(cls, out_dir: Path) -> tuple["ReadCache", str | None]:
        """The saved reads, and why none were used if they were not."""
        cache = cls()
        path = Path(out_dir) / READS_NAME
        if not path.exists():
            return cache, "no saved reads in %s" % out_dir
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, EOFError) as exc:
            return cache, "the saved reads cannot be read: %s" % exc
        if data.get("reads_format") != READS_FORMAT:
            return cache, "the saved reads are in another format"
        if data.get("jamp_version") != __version__:
            return cache, ("the saved reads were made by jamp %s"
                           % data.get("jamp_version"))
        for row in data.get("files", []):
            p = Path(row["path"])
            af = AudioFile(path=p, ext=row["ext"], size=row["size"],
                           name_info=parse_track_name(p.name))
            for name in _SAVED:
                if name in row:
                    setattr(af, name, row[name])
            cache._entries[row["path"]] = ((row["size"], row["mtime_ns"]), af)
        return cache, None
