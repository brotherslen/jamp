"""Writing tags, and being able to put them back.

Every folder gets a `.etree_backup.json` holding the complete original tags of
every file in it, written before a single tag is changed.  That file is the one
thing this pipeline deliberately adds inside the library, because a tag rewrite
is the only genuinely unrecoverable operation here - a rename can be reversed
from the log, an overwritten tag cannot.

MP3 is written as ID3v2.3, as the brief requires.  SHN cannot be tagged at all
and is refused rather than pretended about.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .winpath import opener

BACKUP_NAME = ".etree_backup.json"
# See state.FORMAT: the backup's layout, which an older version must not guess at.
BACKUP_FORMAT = 1
FORMAT_KEY = "etree_format"


def check_backup_format(payload: dict, where) -> None:
    """Refuse a backup written by a newer jamp rather than restore it wrongly."""
    from .state import newer_format

    if newer_format(payload):
        raise TagWriteError("%s was written by a newer version of jamp (format %s; "
                            "this version reads up to %d) - update jamp to restore "
                            "from it" % (where, payload.get(FORMAT_KEY), BACKUP_FORMAT))

# Canonical name -> ID3v2.3 frame.  Anything not here becomes a TXXX frame
# keyed by the canonical name, which is how RELEASE and SOURCE_CONFIDENCE
# survive a round trip.
_ID3_FRAMES = {
    "ARTIST": "TPE1",
    "ALBUMARTIST": "TPE2",
    "ALBUM": "TALB",
    "TITLE": "TIT2",
    "TRACKNUMBER": "TRCK",
    "DISCNUMBER": "TPOS",
    "GENRE": "TCON",
    "DATE": "TDRC",
    "PUBLISHER": "TPUB",
    "COPYRIGHT": "TCOP",
    "ENCODEDBY": "TENC",
}


class TagWriteError(RuntimeError):
    pass


@dataclass
class BackupEntry:
    filename: str
    tags: dict


def _is_id3(tags) -> bool:
    """ID3 however it is carried: MP3, or a chunk inside WAV or AIFF."""
    from mutagen import id3

    return isinstance(tags, id3.ID3)


def _is_mp4(tags) -> bool:
    from mutagen.mp4 import MP4Tags

    return isinstance(tags, MP4Tags)


# Artwork and other binary blobs are left exactly where they are, on both sides
# of a restore.  Nothing in this pipeline writes them, so the copy in the file
# is still the original - and carrying a cover image for every track would make
# the backup many times the size of everything else in it.
_ID3_LEFT_IN_PLACE = frozenset({"APIC", "PIC", "GEOB", "PRIV"})
_MP4_LEFT_IN_PLACE = frozenset({"covr"})


def _backup_id3(tags) -> dict:
    """The whole ID3 tag, as the bytes of a tag holding the same frames.

    Recording each frame as text lost everything that is not text: a TXXX
    frame's description, which is what tells TXXX:SOURCE from TXXX:TAPER, and a
    comment's language.  Restoring from that put every TXXX under one empty
    name, each overwriting the last.  The rendered tag carries every field of
    every frame, and mutagen reads it back exactly.
    """
    import base64
    import io
    from mutagen import id3

    kept = id3.ID3()
    for frame in tags.values():
        if frame.FrameID not in _ID3_LEFT_IN_PLACE:
            kept.add(frame)
    raw = b""
    if len(kept):
        buf = io.BytesIO()
        kept.save(buf, v2_version=4, padding=lambda info: 0)
        raw = buf.getvalue()
    return {
        "__kind__": "id3-bytes",
        "version": tags.version[1] if tags.version else 4,
        "b64": base64.b64encode(raw).decode("ascii"),
        # Not used by the restore.  Kept so a person can read the backup.
        "frames": {str(k): [str(t) for t in getattr(v, "text", [])]
                   for k, v in kept.items()},
    }


def _mp4_value(value) -> dict:
    """One MP4 atom's value, with enough type to write it back."""
    import base64
    from mutagen.mp4 import MP4FreeForm

    if isinstance(value, bool):
        return {"bool": value}
    if isinstance(value, int):
        return {"int": value}
    items = list(value) if isinstance(value, (list, tuple)) else [value]
    if items and all(isinstance(x, tuple) for x in items):
        return {"pairs": [list(x) for x in items]}
    if items and all(isinstance(x, MP4FreeForm) for x in items):
        return {"freeform": [{"b64": base64.b64encode(bytes(x)).decode("ascii"),
                              "dataformat": int(x.dataformat)} for x in items]}
    if items and all(isinstance(x, bool) for x in items):
        return {"bool": items[0]}
    if items and all(isinstance(x, int) for x in items):
        return {"ints": items}
    return {"text": [str(x) for x in items]}


def _backup_mp4(tags) -> dict:
    return {
        "__kind__": "mp4",
        "atoms": {str(k): _mp4_value(v) for k, v in tags.items()
                  if k not in _MP4_LEFT_IN_PLACE},
    }


def read_all_tags(paths) -> dict:
    """Every tag of every file, in a form that can be written back."""
    import mutagen

    out: dict = {}
    for path in paths:
        path = Path(path)
        if path.suffix.lower() == ".shn":
            continue
        try:
            audio = mutagen.File(opener(path))
        except Exception as exc:                     # unreadable is still worth noting
            out[path.name] = {"__error__": "%s: %s" % (exc.__class__.__name__, exc)}
            continue
        if audio is None or audio.tags is None:
            out[path.name] = {}
            continue
        if _is_id3(audio.tags):
            out[path.name] = _backup_id3(audio.tags)
        elif _is_mp4(audio.tags):
            out[path.name] = _backup_mp4(audio.tags)
        else:
            out[path.name] = {
                "__kind__": "vorbis",
                "fields": {str(k): _tag_values(v) for k, v in audio.tags.items()},
            }
    return out


def write_backup(folder: Path, paths, rename_map: dict | None = None) -> Path:
    """Dump the folder's original tags next to the audio, before anything changes.

    Never overwritten: if a backup already exists it is the older, more original
    one and it stays.

    `rename_map` is old name -> new name for the renames about to happen.  The
    backup is keyed by the names the files have NOW, but by the time anyone
    wants to restore from it those files have been renamed, so the new name is
    recorded alongside each entry.  Without it a restore silently matches
    nothing.
    """
    target = Path(folder) / BACKUP_NAME
    # Checked through the extended path, as it is written.  Past MAX_PATH a
    # plain exists() says False for a backup that is there, and the "never
    # overwritten" backup would be replaced by tags this pipeline already wrote.
    if Path(opener(target)).exists():
        return target
    files = read_all_tags(paths)
    for old, new in (rename_map or {}).items():
        if old in files:
            files[old]["__renamed_to__"] = new
    payload = {
        FORMAT_KEY: BACKUP_FORMAT,
        "folder": str(folder),
        "written_by": "jamp phase 2",
        "files": files,
    }
    Path(opener(target)).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def _tag_values(value) -> list[str]:
    """A tag's values as a list of strings.

    Nearly every tag value is already a list, but MP4 stores cpil and pgap as
    bare booleans and tmpo as a bare int.  Iterating one of those raises, and
    the backup runs before anything is written - so a single gapless flag took
    the whole folder down before a tag had been touched.
    """
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    return [str(value)]


# Canonical name -> the MP4 atom that carries it.  MP4 has a fixed vocabulary;
# anything outside it travels as an iTunes-style freeform atom.
_MP4_WRITE = {
    "ARTIST": "\xa9ART", "ALBUMARTIST": "aART", "ALBUM": "\xa9alb",
    "TITLE": "\xa9nam", "GENRE": "\xa9gen", "DATE": "\xa9day",
}


def _apply_id3(frames, proposed: dict) -> None:
    """Write canonical names into an ID3 frame set."""
    from mutagen import id3

    for key, value in proposed.items():
        frame_id = _ID3_FRAMES.get(key.upper())
        if frame_id is None:
            frames.delall("TXXX:%s" % key.upper())
            frames.add(id3.TXXX(encoding=3, desc=key.upper(), text=[str(value)]))
            continue
        frames.delall(frame_id)
        frames.add(getattr(id3, frame_id)(encoding=3, text=[str(value)]))


def _apply_mp4(tags, proposed: dict) -> None:
    """Write canonical names into MP4/M4A atoms."""
    from mutagen.mp4 import MP4FreeForm

    for key, value in proposed.items():
        name = key.upper()
        atom = _MP4_WRITE.get(name)
        if atom is not None:
            tags[atom] = [str(value)]
            continue
        if name in ("TRACKNUMBER", "DISCNUMBER"):
            atom = "trkn" if name == "TRACKNUMBER" else "disk"
            # trkn/disk are (number, total) pairs.  Keep whatever total the
            # file already carries; writing a bare number would erase it.
            total = 0
            existing = tags.get(atom) or []
            if existing and isinstance(existing[0], tuple) and len(existing[0]) > 1:
                total = existing[0][1]
            try:
                number = int(str(value).split("/")[0])
            except ValueError:
                continue
            tags[atom] = [(number, total)]
            continue
        tags["----:com.apple.iTunes:%s" % name] = [
            MP4FreeForm(str(value).encode("utf-8"))
        ]


def write_tags(path: Path, proposed: dict) -> None:
    """Apply `proposed` (canonical name -> value) to one file."""
    import mutagen
    from mutagen import id3

    path = Path(path)
    if path.suffix.lower() == ".shn":
        raise TagWriteError("SHN files cannot be tagged")
    if not proposed:
        return
    target = opener(path)

    if path.suffix.lower() == ".mp3":
        try:
            frames = id3.ID3(target)
        except id3.ID3NoHeaderError:
            frames = id3.ID3()
        _apply_id3(frames, proposed)
        frames.save(target, v2_version=3)          # ID3v2.3, per the brief
        return

    audio = mutagen.File(target)
    if audio is None:
        raise TagWriteError("unrecognised audio container")
    if audio.tags is None:
        audio.add_tags()
    if _is_mp4(audio.tags):
        _apply_mp4(audio.tags, proposed)
        audio.save()
        return
    if _is_id3(audio.tags):
        # A WAV or AIFF carrying ID3 frames.  The generic branch below would
        # assign a plain list into a frame dictionary, which ID3 refuses.
        _apply_id3(audio.tags, proposed)
        audio.save()
        return
    for key, value in proposed.items():
        audio.tags[key.upper()] = [str(value)]
    audio.save()


# Frames a backup written before "id3-bytes" can describe: plain text frames,
# TXXX and COMM.  Paired-text frames (TIPL, TMCL, IPLS) were flattened beyond
# recovery, so like artwork they are left as the file has them.
_LEGACY_ID3_UNRESTORABLE = frozenset({"TIPL", "TMCL", "IPLS"})


def _legacy_id3_restorable(frame_id: str) -> bool:
    return ((frame_id.startswith("T") and frame_id not in _LEGACY_ID3_UNRESTORABLE)
            or frame_id == "COMM")


def _legacy_id3_frames(frames: dict) -> list:
    """Frames rebuilt from an older text-only backup, as far as it allows.

    The description and language live in the key mutagen gave each frame -
    "TXXX:SOURCE", "COMM:desc:eng" - so they can still be recovered from it.
    """
    from mutagen import id3

    out = []
    for key, values in frames.items():
        base, _, rest = str(key).partition(":")
        values = [str(v) for v in values]
        if base == "TXXX":
            out.append(id3.TXXX(encoding=3, desc=rest, text=values))
        elif base == "COMM":
            desc, _, lang = rest.partition(":")
            out.append(id3.COMM(encoding=3, desc=desc, lang=(lang or "eng")[:3],
                                text=values))
        elif _legacy_id3_restorable(base) and hasattr(id3, base):
            out.append(getattr(id3, base)(encoding=3, text=values))
    return out


def _legacy_mp4_atoms(fields: dict) -> dict:
    """MP4 atoms from an older backup, which wrote every value through str().

    Track pairs came out as "(3, 18)" and freeform atoms as a bytes literal;
    both are read back into what the atom actually holds.  Artwork is left in
    the file, as it is for a current backup.
    """
    import ast
    from mutagen.mp4 import MP4FreeForm

    atoms = {}
    for key, values in fields.items():
        if key in _MP4_LEFT_IN_PLACE:
            continue
        if key in ("trkn", "disk"):
            pairs = []
            for v in values:
                nums = re.findall(r"\d+", v)
                if nums:
                    pairs.append((int(nums[0]), int(nums[1]) if len(nums) > 1 else 0))
            atoms[key] = pairs
        elif key.startswith("----"):
            out = []
            for v in values:
                data = ast.literal_eval(v) if v[:2] in ("b'", 'b"') else v.encode("utf-8")
                out.append(MP4FreeForm(data))
            atoms[key] = out
        elif values and all(v in ("True", "False") for v in values):
            atoms[key] = values[0] == "True"
        elif key == "tmpo":
            atoms[key] = [int(v) for v in values]
        else:
            atoms[key] = list(values)
    return atoms


def _decode_mp4(atoms: dict) -> dict:
    import base64
    from mutagen.mp4 import AtomDataType, MP4FreeForm

    out = {}
    for key, value in atoms.items():
        if "bool" in value:
            out[key] = bool(value["bool"])
        elif "int" in value:
            out[key] = int(value["int"])
        elif "pairs" in value:
            out[key] = [tuple(p) for p in value["pairs"]]
        elif "freeform" in value:
            out[key] = [MP4FreeForm(base64.b64decode(x["b64"]),
                                    dataformat=AtomDataType(x["dataformat"]))
                        for x in value["freeform"]]
        elif "ints" in value:
            out[key] = [int(x) for x in value["ints"]]
        else:
            out[key] = list(value.get("text") or [])
    return out


def _prepare_restore(path: Path, entry: dict):
    """Everything needed to restore one file, decoded before anything is written.

    Returns a function that does the writing.  Decoding all of a folder's
    entries first means a backup that cannot be understood stops the restore
    before the first file is touched, not halfway through the folder.
    """
    import base64
    import io

    import mutagen
    from mutagen import id3

    target = opener(path)
    probe = mutagen.File(target)
    if probe is None:
        raise TagWriteError("%s: unrecognised audio container" % path.name)
    kind = entry.get("__kind__")
    from mutagen.mp4 import MP4

    file_is_id3 = _is_id3(probe.tags) or (
        probe.tags is None and isinstance(probe, id3.ID3FileType))
    file_is_mp4 = isinstance(probe, MP4)

    if file_is_id3:
        if kind == "id3-bytes":
            raw = base64.b64decode(entry.get("b64") or "")
            frames = list(id3.ID3(io.BytesIO(raw)).values()) if raw else []
            version = entry.get("version") if entry.get("version") in (3, 4) else 4
            removable = lambda fid: fid not in _ID3_LEFT_IN_PLACE     # noqa: E731
        elif kind == "id3":
            frames = _legacy_id3_frames(entry.get("frames") or {})
            version = 3                     # what phase 2 always wrote
            removable = _legacy_id3_restorable
        elif not kind:
            frames, version = [], 3         # the file had no tags at all
            removable = lambda fid: fid not in _ID3_LEFT_IN_PLACE     # noqa: E731
        else:
            raise TagWriteError("%s holds ID3 but its backup is %r" % (path.name, kind))

        def apply():
            # Through mutagen.File, never ID3.save on the path: for a WAV or
            # AIFF that writes a bare ID3 header over the front of the RIFF or
            # FORM container, and the file stops being a WAV at all.
            audio = mutagen.File(target)
            if audio.tags is None:
                audio.add_tags()
            for key in [k for k, f in audio.tags.items() if removable(f.FrameID)]:
                del audio.tags[key]
            for frame in frames:
                audio.tags.add(frame)
            audio.save(v2_version=version)
        return apply

    if file_is_mp4:
        if kind == "mp4":
            atoms = _decode_mp4(entry.get("atoms") or {})
        elif kind == "vorbis" or not kind:
            atoms = _legacy_mp4_atoms(entry.get("fields") or {})
        else:
            raise TagWriteError("%s is MP4 but its backup is %r" % (path.name, kind))

        def apply():
            audio = mutagen.File(target)
            if audio.tags is None:
                audio.add_tags()
            for key in [k for k in audio.tags.keys() if k not in _MP4_LEFT_IN_PLACE]:
                del audio.tags[key]
            for key, value in atoms.items():
                audio.tags[key] = value
            audio.save()
        return apply

    if kind not in ("vorbis", None):
        raise TagWriteError("%s: backup is %r but the file is %s"
                            % (path.name, kind, probe.__class__.__name__))
    fields = entry.get("fields") or {}

    def apply():
        audio = mutagen.File(target)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.clear()
        for key, values in fields.items():
            audio.tags[key] = list(values)
        audio.save()
    return apply


def restore_from_backup(folder: Path) -> int:
    """Put a folder's tags back exactly as they were.  Returns files restored."""
    backup = Path(folder) / BACKUP_NAME
    if not Path(opener(backup)).exists():
        raise TagWriteError("no %s in %s" % (BACKUP_NAME, folder))
    payload = json.loads(Path(opener(backup)).read_text(encoding="utf-8"))
    check_backup_format(payload, backup)
    entries = payload.get("files") or {}
    found = []
    for filename, entry in entries.items():
        # Prefer the name the file was renamed to; fall back to the original,
        # which is what it still has if the rename never happened or was undone.
        path = Path(folder) / (entry.get("__renamed_to__") or filename)
        if not Path(opener(path)).exists():
            path = Path(folder) / filename
        if not Path(opener(path)).exists() or "__error__" in entry:
            continue
        found.append((path, entry))
    prepared = [_prepare_restore(path, entry) for path, entry in found]
    restored = 0
    for apply in prepared:
        apply()
        restored += 1
    if entries and not restored:
        # Silently restoring nothing is the worst outcome for an undo tool: the
        # caller believes the folder is back the way it was.
        raise TagWriteError(
            "%s lists %d file(s) but none of them are in %s under either their "
            "original or their new name - nothing was restored"
            % (BACKUP_NAME, len(entries), folder))
    return restored
