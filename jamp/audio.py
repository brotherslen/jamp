"""Audio file discovery, format detection, tag reading and track-name parsing.

mutagen cannot read or write SHN at all, so SHN files are detected by extension,
marked ``tag_support = "none"`` and reported.  We never pretend to have tags for
them and phase 2 must refuse to tag them.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import integrity
from .dates import find_dates
from .winpath import opener

AUDIO_EXTS = {".flac", ".shn", ".mp3", ".m4a", ".ogg", ".wav", ".aiff", ".aif", ".ape", ".wv", ".wma"}
LOSSLESS_EXTS = {".flac", ".shn", ".wav", ".aiff", ".aif", ".ape", ".wv"}
SIDECAR_EXTS = {".ffp", ".md5", ".st5", ".cue", ".sfv", ".txt", ".nfo", ".log"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff"}

# Canonical -> the keys each container uses.
_ID3_MAP = {
    "TPE1": "ARTIST",
    "TPE2": "ALBUMARTIST",
    "TALB": "ALBUM",
    "TIT2": "TITLE",
    "TRCK": "TRACKNUMBER",
    "TPOS": "DISCNUMBER",
    "TCON": "GENRE",
    "TDRC": "DATE",
    "TYER": "DATE",
    "TDAT": "DATE_DDMM",
    "COMM": "COMMENT",
    # Store and label fingerprints live in these as often as in COMMENT.
    "TPUB": "PUBLISHER",
    "TCOP": "COPYRIGHT",
    "TENC": "ENCODEDBY",
    "TSSE": "ENCODERSETTINGS",
    "TOAL": "ORIGINALALBUM",
    "WXXX": "URL",
    "WOAF": "URL",
    "WOAR": "URL",
}

_MP4_MAP = {
    "\xa9ART": "ARTIST",
    "aART": "ALBUMARTIST",
    "\xa9alb": "ALBUM",
    "\xa9nam": "TITLE",
    "trkn": "TRACKNUMBER",
    "disk": "DISCNUMBER",
    "\xa9gen": "GENRE",
    "\xa9day": "DATE",
}

INTERESTING_TAGS = (
    "ARTIST", "ALBUMARTIST", "ALBUM", "TITLE", "TRACKNUMBER",
    "DISCNUMBER", "DATE", "GENRE", "RELEASE", "SOURCE", "SOURCE_CONFIDENCE",
    "COMMENT", "VENUE", "LOCATION", "TAPER", "LINEAGE",
    "PUBLISHER", "COPYRIGHT", "ENCODEDBY", "ORIGINALALBUM", "URL",
)

# Tags worth searching for a store or label name.  A band's own store stamps
# itself here far more reliably than it does in the folder name: UMLive writes
# "UMLive" into COMMENT on every track.
PROVENANCE_TAGS = (
    "COMMENT", "PUBLISHER", "COPYRIGHT", "ENCODEDBY", "URL",
    "ORIGINALALBUM", "ALBUM", "ARTWORK",
)


@dataclass
class TrackName:
    """What a track filename claims about disc / set / track / title."""

    disc: int | None = None
    # The disc came from the folder the file sits in, not from its own name.
    disc_from_folder: bool = False
    set_no: int | None = None
    track: int | None = None
    # A letter glued to the track number: "d1t0a".  Tapers write track zero
    # for material before the show - a soundcheck, a tuning jam - and letter
    # it when there is more than one.  It is part of the track's identity,
    # not decoration, so it survives the rename.
    suffix: str = ""
    title: str | None = None
    pattern: str = "unparsed"


@dataclass
class AudioFile:
    path: Path
    ext: str
    size: int
    fmt: str | None = None            # flac16 / flac24 / mp3 / shn / ...
    bits: int | None = None
    rate: int | None = None
    channels: int | None = None
    length: float | None = None
    tags: dict[str, list[str]] = field(default_factory=dict)
    tag_support: str = "unknown"      # full / none / unreadable
    tags_read: bool = False
    quality: str | None = None        # v0 / v2 / 320 / 256k ... for lossy files
    error: str | None = None
    name_info: TrackName = field(default_factory=TrackName)
    # The encoder's MD5 of the DECODED audio, from the file's own STREAMINFO.
    # It is what the recording *is*, independent of tags, filename and even
    # compression level - two FLACs of one master share it while sharing no
    # bytes - so it is the one honest answer to "are these the same recording?".
    # None for anything but FLAC, and for a FLAC whose encoder left it zeroed.
    audio_md5: str | None = None
    # Exact sample count, so a truncated copy is arithmetic rather than a guess.
    samples: int | None = None

    @property
    def name(self) -> str:
        return self.path.name

    def tag(self, key: str) -> str | None:
        vals = self.tags.get(key)
        return vals[0] if vals else None


# --------------------------------------------------------------------------
# track filename parsing
# --------------------------------------------------------------------------

_TRACK_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    # "Set1T03" / "Disc2 T04" spelled out.  Tried first: the short forms below
    # would otherwise miss it entirely and the track would be renumbered from
    # its position, which on an incomplete show invents a running order.
    ("SetNTNN", re.compile(r"(?<![a-z0-9])set\s*(\d{1,2})\s*[-_. ]*t\s*(\d{1,3})(?![0-9])", re.I)),
    ("DiscNTNN", re.compile(r"(?<![a-z0-9])(?:disc|disk|cd)\s*(\d{1,2})\s*[-_. ]*t\s*(\d{1,3})(?![0-9])", re.I)),
    # The optional trailing letter is "d1t0a" - track zero, lettered.  Its own
    # lookahead keeps it from eating the first letter of a title, so
    # "d1t01Bertha" still parses exactly as it did before.
    ("dNtNN", re.compile(r"(?<![a-z0-9])d(\d{1,2})t(\d{1,3})(?:([a-z])(?![a-z0-9]))?(?![0-9])", re.I)),
    ("sNtNN", re.compile(r"(?<![a-z0-9])s(\d{1,2})t(\d{1,3})(?:([a-z])(?![a-z0-9]))?(?![0-9])", re.I)),
    ("dN_NN_title", re.compile(r"(?<![a-z0-9])d(\d{1,2})[-_. ]?(\d{2})[-_. ]+(.+)$", re.I)),
    ("N-NN title", re.compile(r"^(\d)[-_.](\d{1,2})[-_. ]+(.+)$")),
    # Two-digit disc: "01.01 - Promised Land", "02-08 Johnny B. Goode".  Without
    # this the leading number reads as the track and everything after the
    # separator is swallowed into the title, so every file on disc 1 becomes
    # track 1 and collides.
    ("NN-NN title", re.compile(r"^(\d{2})[-_.](\d{2})[-_. ]+(.+)$")),
    ("NNN title", re.compile(r"^(\d)(\d{2})[-_. ]+(.+)$")),
    # Roman set numbers, the way a taper writes them: "I 01 Jam",
    # "II 01 The Curtain With".  Without this all three sets read as
    # track 1 and collide on one name.
    ("RomanN title", re.compile(r"^(i{1,3}|iv|vi{0,3})[\s._-]+(\d{1,3})[\s._-]+(.+)$", re.I)),
    ("NN title", re.compile(r"^(\d{1,3})[-_. ]+(.+)$")),
    # Flat etree numbering with no disc: "dtb2008-09-11t01.flac".
    ("tNN", re.compile(r"(?<![a-z0-9])t(\d{1,3})(?![0-9])", re.I)),
    ("trailing NN", re.compile(r"[-_. ](\d{1,3})\s*$")),
]


def parse_track_name(filename: str) -> TrackName:
    """Pull disc / set / track / title out of a track filename.

    The date portion is masked first so that ``ph2018-12-29_mtx_01`` yields
    track 01 and not "track 2018".
    """
    stem = Path(filename).stem
    spans = [c.span for c in find_dates(stem)]
    masked = list(stem)
    for start, end in spans:
        for i in range(start, end):
            masked[i] = " "
    text = "".join(masked)
    # Drop a leading band prefix run so "mmj   d1t01" -> "d1t01" - but not a
    # roman set number with its track behind it, which this was swallowing:
    # "II 01 The Curtain With" became "01 ...", so every set read as track 1.
    text = re.sub(r"^(?!(?:i{1,3}|iv|vi{0,3})[\s._-]+\d)[A-Za-z]{1,6}(?=[\s_.-])",
                  " ", text, flags=re.I).strip(" ._-")

    for name, pattern in _TRACK_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        if name in ("dNtNN", "DiscNTNN"):
            suffix = m.group(3) if name == "dNtNN" and m.lastindex and m.lastindex >= 3 else None
            return TrackName(disc=int(m.group(1)), track=int(m.group(2)),
                             suffix=(suffix or "").lower(), pattern=name)
        if name in ("sNtNN", "SetNTNN"):
            suffix = m.group(3) if name == "sNtNN" and m.lastindex and m.lastindex >= 3 else None
            return TrackName(set_no=int(m.group(1)), track=int(m.group(2)),
                             suffix=(suffix or "").lower(), pattern=name)
        if name == "dN_NN_title":
            return TrackName(disc=int(m.group(1)), track=int(m.group(2)),
                             title=_clean_title(m.group(3)), pattern=name)
        if name == "N-NN title":
            return TrackName(disc=int(m.group(1)), track=int(m.group(2)),
                             title=_clean_title(m.group(3)), pattern=name)
        if name == "NN-NN title":
            disc, track = int(m.group(1)), int(m.group(2))
            # "12.01 - Song" on a single-disc rip is track 12, not disc 12.
            # Discs run low and tracks stay under 30; outside that, read the
            # leading number as a flat track and keep the rest as the title.
            if 1 <= disc <= 20 and 1 <= track <= 30:
                return TrackName(disc=disc, track=track,
                                 title=_clean_title(m.group(3)), pattern=name)
            return TrackName(track=disc,
                             title=_clean_title(m.group(2) + " " + m.group(3)),
                             pattern="NN flat")
        if name == "NNN title":
            disc, track = int(m.group(1)), int(m.group(2))
            # "101 Cumberland Blues" is disc 1 track 01; a flat track 101 is
            # vanishingly rare, but a track number over 30 is a giveaway.
            if 1 <= disc <= 9 and 1 <= track <= 30:
                return TrackName(disc=disc, track=track,
                                 title=_clean_title(m.group(3)), pattern=name)
            return TrackName(track=int(m.group(1) + m.group(2)),
                             title=_clean_title(m.group(3)), pattern="NNN flat")
        if name == "RomanN title":
            roman = {"i": 1, "ii": 2, "iii": 3, "iv": 4,
                     "v": 5, "vi": 6, "vii": 7, "viii": 8}[m.group(1).lower()]
            return TrackName(set_no=roman, track=int(m.group(2)),
                             title=_clean_title(m.group(3)), pattern=name)
        if name == "NN title":
            return TrackName(track=int(m.group(1)), title=_clean_title(m.group(2)), pattern=name)
        if name == "tNN":
            return TrackName(track=int(m.group(1)), pattern=name)
        if name == "trailing NN":
            return TrackName(track=int(m.group(1)), pattern=name)
    return TrackName(pattern="unparsed")


def _clean_title(raw: str) -> str:
    out = raw.replace("_", " ").strip(" ._-")
    return re.sub(r"\s{2,}", " ", out)


# --------------------------------------------------------------------------
# tags
# --------------------------------------------------------------------------

# Vorbis comments that hold a whole picture, base64-encoded.  Nothing reads the
# image, and kept as text it was 48 MB of the library's 57 MB of saved reads.
_VORBIS_PICTURES = frozenset({"METADATA_BLOCK_PICTURE", "COVERART"})


def _normalize_vorbis(tags) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, values in tags.items():
        key = key.upper()
        if key in _VORBIS_PICTURES:
            # The key stays, so a picture is still seen to be there.
            out.setdefault(key, []).extend(
                "<picture, %d characters of base64>" % len(str(v)) for v in values)
            continue
        out.setdefault(key, []).extend(str(v) for v in values)
    return out


def _normalize_id3(tags) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for frame_id, frame in tags.items():
        base = frame_id.split(":")[0]
        canon = _ID3_MAP.get(base)
        if canon is None:
            if base == "TXXX":
                canon = str(getattr(frame, "desc", "TXXX")).upper()
            elif base == "APIC":
                # The artwork description is often the store's own file path.
                desc = str(getattr(frame, "desc", "")).strip()
                if desc:
                    out.setdefault("ARTWORK", []).append(desc)
                continue
            else:
                continue
        try:
            values = [str(t) for t in frame.text]
        except AttributeError:
            values = [str(frame)]
        out.setdefault(canon, []).extend(values)
    return out


def _normalize_mp4(tags) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, values in tags.items():
        canon = _MP4_MAP.get(key)
        if canon is None:
            # MP4 has no atom for VENUE or SOURCE, so we write them as iTunes
            # freeform atoms.  Read them back the same way: without this the
            # field looks empty on the next pass and is written again on every
            # run, and the folder never settles.
            if key.startswith("----:"):
                name = key.rsplit(":", 1)[-1].upper()
                vals = []
                for v in values:
                    try:
                        vals.append(bytes(v).decode("utf-8", "replace"))
                    except Exception:
                        vals.append(str(v))
                out.setdefault(name, []).extend(vals)
            continue
        if canon in ("TRACKNUMBER", "DISCNUMBER"):
            out.setdefault(canon, []).extend(str(v[0]) for v in values if v)
        else:
            out.setdefault(canon, []).extend(str(v) for v in values)
    return out


# The first bytes each container must start with.  MP3 and M4A are left out:
# an MP3 may open with any of several things and mutagen sorts them out, and an
# M4A's "ftyp" sits at offset 4 behind a length word.
_MAGIC = {
    ".flac": (b"fLaC",),
    ".shn": (b"ajkg",),
    ".ogg": (b"OggS",),
    ".wav": (b"RIFF",),
    ".aiff": (b"FORM",),
    ".aif": (b"FORM",),
    ".ape": (b"MAC ",),
    ".wv": (b"wvpk",),
    ".wma": (bytes.fromhex("3026b275"),),
}


def structural_problem(path: Path, ext: str, size: int) -> str | None:
    """Is this file dead before anything tries to parse it?

    Cheap - it reads sixteen bytes - and it runs for every container, including
    the ones nothing else opens.  ph1999-07-25 was 18 SHN files totalling 855 MB
    with nothing in them but zeros, and it settled quietly because SHN cannot be
    tagged: the pipeline renamed the folder without ever reading a byte of it.
    A file that is empty, all zeros, or simply not what its extension claims is
    reported as unreadable, so the folder is held back instead.
    """
    if size == 0:
        return "the file is 0 bytes"
    try:
        with open(opener(path), "rb") as fh:
            head = fh.read(16)
    except OSError as exc:
        return "cannot be opened: %s" % exc
    if not head:
        return "the file is empty"
    if not any(head):
        return "begins with %d zero bytes - there is no header here" % len(head)
    # An ID3v2 tag before the audio is legal and common - plenty of taggers put
    # one on a FLAC - and the real magic then sits past it.  mutagen reads these
    # perfectly well, so the container check has nothing useful to say: 45 good
    # files across two Phish shows were called damaged for this.
    if head.startswith(b"ID3"):
        return None
    want = _MAGIC.get(ext)
    if want and not any(head.startswith(m) for m in want):
        return ("not %s data: expected %r at the start, found %r"
                % (ext.lstrip("."), want[0], head[:4]))
    return None


def read_audio_file(path: Path, read_tags: bool = True) -> AudioFile:
    ext = path.suffix.lower()
    try:
        size = os.stat(opener(path)).st_size
    except OSError:
        size = 0
    af = AudioFile(path=path, ext=ext, size=size, name_info=parse_track_name(path.name))

    # Before anything else, and whatever the container: a file that is dead on
    # disk must not be renamed and tagged as though it held a show.
    problem = structural_problem(path, ext, size)
    if problem:
        af.tag_support = "unreadable"
        af.error = problem
        return af

    # Identity before tags, and deliberately before anything that can fail.
    # This reads 42 bytes at a fixed offset, so it answers for a file whose
    # metadata chain is rubble further in - exactly the files mutagen refuses
    # outright.  Without it such a folder reports no format, no duration and no
    # identity at all, and gets described as empty rather than as damaged.
    if ext == ".flac":
        si = integrity.flac_streaminfo(path)
        if si is not None:
            af.audio_md5 = si["md5"]
            af.samples = si["samples"]
            af.rate, af.bits = si["rate"], si["depth"]
            af.channels, af.length = si["channels"], si["seconds"]
            if si["depth"]:
                af.fmt = "flac%d" % si["depth"]

    if ext == ".shn":
        af.fmt = "shn"
        af.tag_support = "none"
        af.error = "SHN is not supported by mutagen; tags cannot be read or written"
        return af

    if not read_tags:
        af.fmt = {".mp3": "mp3"}.get(ext)
        return af

    try:
        import mutagen
        from mutagen.flac import FLAC
        from mutagen.mp3 import MP3
    except ImportError as exc:  # pragma: no cover - dependency missing
        af.error = "mutagen not installed: %s" % exc
        af.tag_support = "unreadable"
        return af

    try:
        # Extended form, or a deeply nested show simply cannot be opened.
        obj = mutagen.File(opener(path))
    except Exception as exc:  # mutagen raises a wide range of parse errors
        af.error = "%s: %s" % (exc.__class__.__name__, exc)
        af.tag_support = "unreadable"
        return af

    if obj is None:
        af.tag_support = "none"
        af.error = "unrecognised audio container"
        return af

    info = getattr(obj, "info", None)
    if info is not None:
        af.rate = getattr(info, "sample_rate", None)
        af.bits = getattr(info, "bits_per_sample", None)
        af.channels = getattr(info, "channels", None)
        af.length = getattr(info, "length", None)

    tags = getattr(obj, "tags", None)
    af.tag_support = "full"
    af.tags_read = True
    if tags is None:
        af.tags = {}
    else:
        from mutagen.id3 import ID3
        from mutagen.mp4 import MP4Tags

        # By type, not by class name: AIFF carries its ID3 as "_IFFID3", which
        # the name check missed, so an AIFF's frames went to the Vorbis reader
        # and its TITLE read as absent.
        if isinstance(tags, ID3):
            af.tags = _normalize_id3(tags)
        elif isinstance(tags, MP4Tags):
            af.tags = _normalize_mp4(tags)
        else:
            af.tags = _normalize_vorbis(tags)

    for picture in getattr(obj, "pictures", []) or []:
        desc = str(getattr(picture, "desc", "")).strip()
        if desc:
            af.tags.setdefault("ARTWORK", []).append(desc)

    if ext == ".flac":
        if af.bits in (16, 24):
            af.fmt = "flac%d" % af.bits
        elif af.bits:
            af.fmt = "flac%d" % af.bits
        else:
            af.fmt = "flac"
    elif ext == ".mp3":
        af.fmt = "mp3"
        af.quality = _mp3_quality(info)
    elif ext:
        af.fmt = ext.lstrip(".")
    return af


def _mp3_quality(info) -> str | None:
    """The LAME preset an MP3 was made with, read from the stream.

    "V0" in a folder name is this fact written down; taking it from the file
    means we do not have to trust the name.
    """
    bitrate = getattr(info, "bitrate", None)
    if not bitrate:
        return None
    kbps = bitrate / 1000.0
    mode = str(getattr(info, "bitrate_mode", "")).rsplit(".", 1)[-1].upper()
    if mode in ("VBR", "ABR"):
        if 200 <= kbps <= 280:
            return "v0"
        if 165 <= kbps < 200:
            return "v2"
        if 120 <= kbps < 165:
            return "v5"
        return "vbr%dk" % round(kbps)
    return "%dk" % round(kbps)


def quality_token(files: list[AudioFile]) -> tuple[str | None, list[str]]:
    """One lossy-quality label for the folder, plus any disagreement."""
    seen: dict[str, int] = {}
    for f in files:
        if f.quality:
            seen[f.quality] = seen.get(f.quality, 0) + 1
    if not seen:
        return None, []
    warnings = []
    if len(seen) > 1:
        warnings.append("mixed MP3 encodings: " + ", ".join(
            "%s x%d" % (k, v) for k, v in sorted(seen.items())))
    return max(seen.items(), key=lambda kv: kv[1])[0], warnings


def format_token(files: list[AudioFile]) -> tuple[str | None, list[str]]:
    """One format token for the whole folder, plus any mixed-format warnings."""
    seen: dict[str, int] = {}
    for f in files:
        if f.fmt:
            seen[f.fmt] = seen.get(f.fmt, 0) + 1
    if not seen:
        return None, []
    warnings: list[str] = []
    if len(seen) > 1:
        warnings.append("mixed formats in one folder: " + ", ".join(
            "%s x%d" % (k, v) for k, v in sorted(seen.items())))
    # Sorted, so the answer never depends on the order files came back in: a
    # folder of 12 FLAC and 12 MP3 must not call itself mp3 one run and flac
    # the next.
    ordered = sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(ordered) > 1 and ordered[1][1] == ordered[0][1]:
        warnings.append(
            "an even split between %s and %s - no format token can be honest here"
            % (ordered[0][0], ordered[1][0]))
        return None, warnings
    best = ordered[0][0]
    # naming.VALID_FORMATS is the one list; this used to carry a second copy of
    # it, so adding m4a there changed nothing and the token was still dropped.
    from .naming import VALID_FORMATS

    if best not in VALID_FORMATS:
        warnings.append("non-standard format token %r - left out of the folder name" % best)
        return None, warnings
    return best, warnings
