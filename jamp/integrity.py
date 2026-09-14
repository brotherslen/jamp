"""What a recording *is*, and whether it still is that.

Two questions that share one number and cost wildly different amounts, which is
why they are separate functions here rather than one "check the audio" call.

**Identity is nearly free.**  `flac_streaminfo` reads 34 bytes at a fixed offset
and comes back with the sample rate, the bit depth, the exact sample count and
an MD5 of the decoded audio, written by the encoder.  Cheap enough to do for
every file on every phase 0 run.

**Integrity is expensive.**  `verify` decodes the whole file and compares what
comes out against that MD5.  Seconds a file, hours a library, and therefore
opt-in.

Leaving the first until late was a design mistake, since corrected: audio
identity should have been a phase 0 primitive rather than a phase 3 discovery,
because duplicate detection, source matching and truncation detection all fall
out of it.  The MD5 is the part that matters there - it hashes the *audio*, so
two different FLAC compression levels of one recording carry the same MD5 while
sharing not a single byte on disk.  `ph1996-12-04` was settled exactly that way,
465 MB against 434 MB of identical music.  Names, track counts and file sizes
had all failed on that question before.

**Why STREAMINFO is read by hand rather than through mutagen.**  Mutagen walks
the metadata block chain and refuses the whole file when any block in it is
malformed - which is the state of every file this was written to examine.
STREAMINFO sits immediately after the four-byte marker and is readable even when
everything after it is rubble, so a file that no tagger will open still answers
what it is and how long it should be.
"""
from __future__ import annotations

import os
import sys
import re
import struct
import subprocess
from pathlib import Path

from .winpath import opener

PASS = "PASS"
MISMATCH = "MISMATCH"
UNREADABLE = "UNREADABLE"
NO_MD5 = "NO_MD5"

# STREAMINFO is always the first metadata block and is always 34 bytes.
_STREAMINFO_AT = 8
_STREAMINFO_LEN = 34
ZERO_MD5 = "0" * 32

# FLAC hashes its samples PACKED - ceil(bits/8) bytes each, little-endian - so
# the decoder's output format has to be pinned to match or the comparison is
# meaningless.  ffmpeg decodes anything above 16 bits to s32, four bytes a
# sample, and its default `-f md5` hashes that: a healthy 24-bit folder came
# back 23/23 MISMATCH against a hash of a different byte layout.  Only 16-bit
# folders had been used as controls, which is how a bug like that survives.
_PCM_FOR_DEPTH = {8: "pcm_s8", 12: "pcm_s16le", 16: "pcm_s16le",
                  20: "pcm_s24le", 24: "pcm_s24le", 32: "pcm_s32le"}


# --- identity: cheap, and safe to do for everything -------------------------

def flac_streaminfo(path: Path | str) -> dict | None:
    """The encoder's own account of the audio, or None if this is not FLAC.

    Reads 42 bytes.  Never raises: an unreadable file is not an exception here,
    it is an answer of None, because the caller is inventorying a library and a
    dead file is one of the things it is inventorying.
    """
    try:
        with open(opener(path), "rb") as fh:
            head = fh.read(_STREAMINFO_AT + _STREAMINFO_LEN)
            # An ID3v2 tag in front of the FLAC is legal and common - plenty of
            # taggers write one - and the marker then sits past it.  audio.py's
            # structural check has always known that; this did not, and called
            # two whole intact LivePhish shows unreadable, 45 files, on a full
            # verification pass.  The tag's length is four syncsafe bytes: seven
            # bits each, the high bit always clear.
            if head[:3] == b"ID3" and len(head) >= 10:
                size = ((head[6] & 0x7F) << 21 | (head[7] & 0x7F) << 14
                        | (head[8] & 0x7F) << 7 | (head[9] & 0x7F))
                fh.seek(10 + size)
                head = fh.read(_STREAMINFO_AT + _STREAMINFO_LEN)
    except OSError:
        return None
    if len(head) < _STREAMINFO_AT + _STREAMINFO_LEN or head[:4] != b"fLaC":
        return None
    p = head[_STREAMINFO_AT:]
    v = struct.unpack(">I", p[10:14])[0]
    rate = v >> 12
    total = ((v & 0x0F) << 32) | struct.unpack(">I", p[14:18])[0]
    md5 = p[18:34].hex()
    return {
        "min_blocksize": struct.unpack(">H", p[0:2])[0],
        "max_blocksize": struct.unpack(">H", p[2:4])[0],
        "rate": rate,
        "channels": ((v >> 9) & 0x07) + 1,
        "depth": ((v >> 4) & 0x1F) + 1,
        "samples": total,
        "seconds": (total / rate) if rate else None,
        # An all-zero field means the encoder never wrote one.  Reported as
        # absent rather than as a hash of nothing, so no two such files can
        # ever look like duplicates of each other.
        "md5": None if md5 == ZERO_MD5 else md5,
    }


# --- integrity: expensive, and therefore opt-in -----------------------------

def locate_ffmpeg(explicit: str | None = None) -> tuple[str | None, str]:
    """Where ffmpeg is, and how it was found - or (None, why not).

    In order: the argument; ``JAMP_FFMPEG``; the path remembered in the user
    folder; an ffmpeg beside the standalone executable; the cached path the SHN and ALAC conversion sweep left behind
    (honoured so this agrees with the build that did those conversions);
    PATH; and on Windows the folder winget installs Gyan's build into, which is
    not on PATH until a new terminal is opened.
    """
    if explicit:
        return explicit, "given on the command line"
    env = os.environ.get("JAMP_FFMPEG") or os.environ.get("ETREE_FFMPEG")
    if env:
        return env, "JAMP_FFMPEG"
    try:
        from . import userdir
        import json

        p = userdir.user_paths_path()
        if p.exists():
            got = json.loads(p.read_text(encoding="utf-8")).get("ffmpeg")
            if got and Path(got).exists():
                return got, "remembered in %s" % p
    except (OSError, ValueError):
        pass
    # The standalone build: an ffmpeg placed beside the jamp executable.
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).parent
        for candidate in (here / "ffmpeg.exe", here / "ffmpeg"):
            if candidate.is_file():
                return str(candidate), "beside jamp"
    cached = Path(os.environ.get("TEMP", "")) / "ffmpeg_path.txt"
    if os.environ.get("TEMP") and cached.exists():
        try:
            got = cached.read_text(encoding="utf-8").strip()
            if got and Path(got).exists():
                return got, "cached in %s" % cached
        except OSError:
            pass
    import shutil

    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path, "PATH"
    local = os.environ.get("LOCALAPPDATA")
    if sys.platform == "win32" and local:
        winget = Path(local) / "Microsoft" / "WinGet"
        for candidate in [winget / "Links" / "ffmpeg.exe",
                          *sorted((winget / "Packages").glob(
                              "*FFmpeg*/*/bin/ffmpeg.exe"), reverse=True)]:
            if candidate.exists():
                return str(candidate), "winget"
    return None, ("not found - install it from https://ffmpeg.org/download.html "
                  "(winget install Gyan.FFmpeg, brew install ffmpeg, or your "
                  "package manager)")


def find_ffmpeg(explicit: str | None = None) -> str:
    """ffmpeg's path, or the bare name for the caller's error to report."""
    return locate_ffmpeg(explicit)[0] or "ffmpeg"


_MD5_LINE = re.compile(r"MD5=([0-9a-f]{32})", re.I)


def decode_md5(ffmpeg: str, path: Path, depth: int,
               timeout: int = 1800) -> tuple[str | None, str]:
    """MD5 of the decoded audio, straight from ffmpeg.  Returns (md5, detail).

    `-map 0:a` matters: plenty of these files carry embedded cover art, and
    without it ffmpeg decodes the picture too and can fail the whole command
    over a damaged PNG that has nothing to do with the recording.
    """
    codec = _PCM_FOR_DEPTH.get(depth)
    if codec is None:
        return None, "unsupported bit depth %r - cannot pin the byte layout" % depth
    # The plain path, never the \\?\ form.  Python's own open() needs the
    # extended prefix past MAX_PATH; ffmpeg refuses it outright ("Error opening
    # input: Invalid argument"), so a long path would have reported a healthy
    # file UNREADABLE.
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-i", os.fspath(path),
           "-map", "0:a", "-c:a", codec, "-f", "md5", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "decode timed out after %ds" % timeout
    except OSError as exc:
        return None, "could not run ffmpeg: %s" % exc
    m = _MD5_LINE.search(r.stdout or "")
    detail = " | ".join(dict.fromkeys(
        l.strip() for l in (r.stderr or "").splitlines() if l.strip()))[:300]
    return (m.group(1).lower() if m else None), detail


def verify(ffmpeg: str, path: Path) -> dict:
    """Decode the file and compare it against the fingerprint it carries.

    Four outcomes, and the difference between two of them is the whole point:

        PASS        decodes to exactly the audio its own header describes
        MISMATCH    decodes cleanly, to something else - silent damage
        UNREADABLE  no decoder can get audio out of it
        NO_MD5      the encoder never wrote one; nothing to compare against

    MISMATCH is the case nothing else here can see.  A file can hold a valid
    header, decode without a single error, show its tags and play, while most of
    its audio has been replaced by silence.  Calling that UNREADABLE would send
    somebody looking for the wrong problem, and calling it PASS is how it went
    unnoticed in the first place.  NO_MD5 is not a failure either: "cannot be
    checked" and "failed" are different statements, and collapsing them puts
    clean files on a damage report.
    """
    si = flac_streaminfo(path)
    out = {"status": None, "stored_md5": "", "decoded_md5": "",
           "seconds": None, "detail": ""}
    if si is None:
        out["status"] = UNREADABLE
        out["detail"] = "no readable fLaC marker or STREAMINFO"
        return out
    out["seconds"] = si["seconds"]
    if si["md5"] is None:
        out["status"] = NO_MD5
        out["detail"] = "the encoder left the MD5 field zeroed"
        return out
    out["stored_md5"] = si["md5"]
    got, detail = decode_md5(ffmpeg, path, si["depth"])
    out["decoded_md5"] = got or ""
    out["detail"] = detail
    out["status"] = UNREADABLE if got is None else (
        PASS if got == si["md5"] else MISMATCH)
    return out
