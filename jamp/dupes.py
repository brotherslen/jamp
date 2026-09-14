"""Spotting an MP3 copy of a recording you already have losslessly.

Two shapes turn up:

* a whole folder of MP3 that duplicates a FLAC folder of the same recording;
* MP3 and FLAC of the same recording sitting in one folder together.

Both are matched on what the tracks *are*, not on what the files are called: a
store's FLAC set may split at disc 2 where its MP3 set runs straight through, so
the filenames disagree while the music is identical.

Nothing here deletes anything.  It only identifies what phase 2 may move into a
review folder, where you can look at it and delete it by hand.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .audio import AudioFile

LOSSLESS_EXTS = {".flac", ".shn", ".wav", ".aiff", ".aif", ".ape", ".wv"}
LOSSY_EXTS = {".mp3", ".m4a", ".ogg"}


@dataclass
class LossyFinding:
    """MP3 files that duplicate a lossless copy of the same recording."""

    files: list[AudioFile] = field(default_factory=list)
    whole_folder: bool = False
    counterpart: str = ""
    reason: str = ""

    def __bool__(self) -> bool:
        return bool(self.files)


def _track_key(f: AudioFile) -> str | None:
    """What this track is, independent of what the file is called."""
    title = (f.tag("TITLE") or "").strip()
    if not title and f.name_info.title:
        title = f.name_info.title
    if title:
        return re.sub(r"[^a-z0-9]", "", title.lower()) or None
    if f.name_info.track is not None:
        return "t%02d" % f.name_info.track
    return None


def split_by_quality(files: list[AudioFile]) -> tuple[list[AudioFile], list[AudioFile]]:
    lossless = [f for f in files if f.ext in LOSSLESS_EXTS]
    lossy = [f for f in files if f.ext in LOSSY_EXTS]
    return lossless, lossy


DURATION_TOLERANCE = 2.0        # seconds


def _order(f: AudioFile):
    return (f.name_info.disc or 0, f.name_info.track or 0, f.path.name.lower())


def _titles_compatible(a: str | None, b: str | None) -> bool:
    """One title contains the other, once punctuation is ignored.

    A store's FLAC set often carries a tidied title where its MP3 set spells out
    the whole segue: "Plunger" against "Plunger>Jimmy Stewart>Plunger", or
    "Nemo" against "E - Nemo>".  Those are the same performance.
    """
    if not a or not b:
        return False
    return a in b or b in a


def find_in_folder(files: list[AudioFile]) -> LossyFinding:
    """MP3s that duplicate lossless files sitting beside them in one folder.

    Matched track by track in playing order, on duration first and title second.
    Duration is the better evidence: a transcode of a performance runs for
    exactly as long as the original, whatever anyone called it.
    """
    lossless, lossy = split_by_quality(files)
    if not lossless or not lossy:
        return LossyFinding()
    if len(lossless) != len(lossy):
        return LossyFinding(
            reason="%d lossless and %d MP3 files - not a track-for-track copy, so "
                   "none are moved" % (len(lossless), len(lossy)))

    pairs = list(zip(sorted(lossless, key=_order), sorted(lossy, key=_order)))
    by_duration = 0
    for left, right in pairs:
        if left.length and right.length and abs(left.length - right.length) <= DURATION_TOLERANCE:
            by_duration += 1
            continue
        if _titles_compatible(_track_key(left), _track_key(right)):
            continue
        return LossyFinding(
            reason="%r and %r do not line up, so this folder is left alone"
                   % (left.path.name, right.path.name))

    how = ("duration" if by_duration == len(pairs)
           else "duration and title" if by_duration else "title")
    return LossyFinding(
        files=[right for _, right in pairs],
        counterpart="the lossless files in the same folder",
        reason="all %d MP3s line up track for track with a lossless file here, "
               "matched on %s" % (len(pairs), how),
    )
