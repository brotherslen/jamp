"""`jamp unpack`: extract the ZIP files in a library, beside themselves.

Shows still in the ZIP they were downloaded in are invisible to every other
command, so this comes first.  Each ZIP is extracted next to itself, laid out
by what is in it (see `layout`): one show folder stays one folder, a show on
several disc folders becomes one folder named after the ZIP, and a ZIP of
several shows puts each show folder beside the ZIP.

Rules, each a restriction:

* **Nothing is overwritten.** A ZIP is not extracted if anything it would
  create already exists.  If everything it holds is already there at the same
  size, the ZIP is reported as already extracted.
* **Nothing half-extracted is left behind.** Files go into a temporary folder
  beside the ZIP, each checked against the CRC the ZIP records, and are moved
  into place only when every file is out.
* **A ZIP cannot write outside its own folder.** A member with an absolute path
  or a `..` in it refuses the whole ZIP.
* **An encrypted, damaged or unsupported ZIP is reported, not guessed at**, and
  so are RAR and 7z archives, which need a tool this does not have.
* **The ZIP stays where it is.** `--set-aside` moves a ZIP into
  `_etree_review/originals/` only once its contents are proven to be on disk:
  every file present, with the CRC the ZIP records.
"""
from __future__ import annotations

import datetime as _dt
import os
import shutil
import sys
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import batch
from .audio import AUDIO_EXTS
from .report import report_file, run_lock, write_csv, write_json
from .scan import _DISC_DIR
from .winpath import opener

COMMAND = "unpack"
PLAN_NAME = "unpack_plan.json"

EXTRACT, ALREADY, REFUSED, UNSUPPORTED = ("extract", "already extracted",
                                          "refused", "unsupported")
UNSUPPORTED_SUFFIXES = {".rar", ".7z"}
_TEMP_PREFIX = ".jamp-unpacking-"


@dataclass
class Member:
    name: str               # as stored in the ZIP
    dest: PurePosixPath     # where it goes, relative to the ZIP's own folder
    size: int
    crc: int


@dataclass
class ZipPlan:
    path: Path
    status: str = EXTRACT
    targets: list[Path] = field(default_factory=list)   # what appears beside the ZIP
    layout: str = ""
    members: list[Member] = field(default_factory=list)
    reason: str = ""
    result: str = ""
    set_aside_to: Path | None = None
    skipped: int = 0        # macOS resource forks left in the ZIP

    @property
    def size(self) -> int:
        return sum(m.size for m in self.members)


APPLEDOUBLE_MAGIC = b"\x00\x05\x16\x07"


def _is_appledouble(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bool:
    if info.file_size < 4 or info.file_size > 1 << 20:
        return False
    try:
        with zf.open(info) as fh:
            return fh.read(4) == APPLEDOUBLE_MAGIC
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return False


def _safe_member(name: str) -> PurePosixPath | None:
    p = PurePosixPath(name.replace(chr(92), "/"))
    if p.is_absolute() or ".." in p.parts or not p.parts or ":" in p.parts[0]:
        return None
    return p


def _size_on_disk(path: Path) -> int | None:
    try:
        return os.stat(opener(path)).st_size
    except OSError:
        return None


def _crc_of(path: Path) -> int:
    crc = 0
    with open(opener(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


def _is_audio(member: PurePosixPath) -> bool:
    return member.suffix.lower() in AUDIO_EXTS


def layout(zip_path: Path, members: list[PurePosixPath]) -> tuple[dict, str]:
    """Where each member goes, relative to the ZIP's folder, and why.

    * **One folder holding everything:** that folder, beside the ZIP.
    * **Disc folders** ("Disc 1", "CD2", "... (Disc 1)"), or audio loose at the
      top of the ZIP: one show, so one folder named after the ZIP.
    * **Two or more folders holding audio that are not discs:** separate shows,
      each extracted beside the ZIP.  Anything loose beside them - a tour's
      artwork, a text file - goes into a folder named after the ZIP rather
      than scattering into the act folder.
    * **Anything else** - one audio folder with an "Artwork" folder, say -
      belongs together, in a folder named after the ZIP.
    """
    stem = PurePosixPath(zip_path.stem)
    tops = {m.parts[0] for m in members if len(m.parts) > 1}
    loose = [m for m in members if len(m.parts) == 1]
    if len(tops) == 1 and not loose:
        return {m: m for m in members}, "one folder"
    discs = {t for t in tops if _DISC_DIR.match(t)}
    if len(discs) >= 2 or any(_is_audio(m) for m in loose):
        return {m: stem / m for m in members}, "one show, in a folder named after the ZIP"
    shows = {m.parts[0] for m in members if len(m.parts) > 1 and _is_audio(m)} - discs
    if len(shows) >= 2:
        where = {m: (m if len(m.parts) > 1 else stem / m) for m in members}
        return where, "%d separate folders%s" % (
            len(tops), ", and the loose files in a folder named after the ZIP"
            if loose else "")
    return {m: stem / m for m in members}, "everything in a folder named after the ZIP"


def plan_zip(path: Path) -> ZipPlan:
    zp = ZipPlan(path=path)
    if path.suffix.lower() in UNSUPPORTED_SUFFIXES:
        zp.status = UNSUPPORTED
        zp.reason = "%s archives need 7-Zip or similar; extract it by hand" % path.suffix
        return zp
    try:
        with zipfile.ZipFile(opener(path)) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
    except (zipfile.BadZipFile, OSError) as exc:
        zp.status, zp.reason = REFUSED, "not a readable ZIP: %s" % exc
        return zp
    if not infos:
        zp.status, zp.reason = REFUSED, "the ZIP is empty"
        return zp

    parsed = []
    for info in infos:
        member = _safe_member(info.filename)
        if member is None:
            zp.status = REFUSED
            zp.reason = "a file in it would land outside its folder: %r" % info.filename
            return zp
        if info.flag_bits & 0x1:
            zp.status, zp.reason = REFUSED, "it is password-protected"
            return zp
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED,
                                      zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            zp.status = REFUSED
            zp.reason = ("it uses a compression method this cannot read (%d)"
                         % info.compress_type)
            return zp
        parsed.append((info, member))

    # macOS writes a resource-fork file beside each real one when it zips a
    # folder: "__MACOSX/._track.flac", or - after some downloaders rename it -
    # "__track.flac" right beside the audio.  4 KB, named like a track, and
    # not audio at all: extracted, they blocked a show as UNREADABLE_AUDIO.
    # Recognised by their magic number, never by name alone.
    try:
        with zipfile.ZipFile(opener(path)) as zf:
            kept = []
            for info, member in parsed:
                if "__MACOSX" in member.parts or _is_appledouble(zf, info):
                    zp.skipped += 1
                else:
                    kept.append((info, member))
            parsed = kept
    except (zipfile.BadZipFile, OSError) as exc:
        zp.status, zp.reason = REFUSED, "not a readable ZIP: %s" % exc
        return zp
    if not parsed:
        zp.status, zp.reason = REFUSED, "the ZIP holds nothing but macOS resource forks"
        return zp

    where, zp.layout = layout(path, [m for _, m in parsed])
    zp.members = [Member(i.filename, where[m], i.file_size, i.CRC) for i, m in parsed]
    tops = sorted({m.dest.parts[0] for m in zp.members}, key=str.lower)
    zp.targets = [path.parent / t for t in tops]

    existing = [t for t in zp.targets if os.path.exists(opener(t))]
    if existing:
        whole = len(existing) == len(zp.targets) and all(
            _size_on_disk(path.parent / Path(*m.dest.parts)) == m.size
            for m in zp.members)
        if whole:
            zp.status = ALREADY
            zp.reason = ("all %d of its files are already beside it at the same size"
                         % len(zp.members))
        else:
            zp.status = REFUSED
            zp.reason = "already there, and not matching the ZIP: %s" % ", ".join(
                t.name for t in existing)
        return zp
    try:
        free = shutil.disk_usage(opener(path.parent)).free
    except OSError:
        free = None
    if free is not None and free < zp.size:
        zp.status = REFUSED
        zp.reason = "needs %.1f GB and the drive has %.1f GB free" % (
            zp.size / 1e9, free / 1e9)
    return zp


def extract(zp: ZipPlan) -> None:
    """Extract into a temporary folder, check every CRC, then move it all out.

    If moving the extracted folders into place fails part-way, the ones already
    moved go back into the temporary folder before it is removed, so a failure
    leaves the ZIP's folder exactly as it was.
    """
    temp = zp.path.parent / (_TEMP_PREFIX + zp.path.stem)
    if os.path.exists(opener(temp)):
        raise FileExistsError("an earlier extraction left %s behind; remove it first"
                              % temp.name)
    for target in zp.targets:
        if os.path.exists(opener(target)):
            raise FileExistsError("%s appeared since the plan was made" % target.name)
    os.makedirs(opener(temp))
    moved: list[Path] = []
    try:
        with zipfile.ZipFile(opener(zp.path)) as zf:
            for m in zp.members:
                dest = temp / Path(*m.dest.parts)
                os.makedirs(opener(dest.parent), exist_ok=True)
                crc = 0
                with zf.open(m.name) as src, open(opener(dest), "wb") as out:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        crc = zlib.crc32(chunk, crc)
                        out.write(chunk)
                # zipfile checks the CRC itself when a member is read to the
                # end; it is checked here as well so safety does not rest on
                # that detail.
                if (crc & 0xFFFFFFFF) != m.crc:
                    raise zipfile.BadZipFile("CRC mismatch in %s" % m.name)
        for target in zp.targets:
            os.rename(opener(temp / target.name), opener(target))
            moved.append(target)
    except BaseException:
        for target in reversed(moved):
            try:
                os.rename(opener(target), opener(temp / target.name))
            except OSError:
                pass
        # Only ever the temporary folder this call made.
        shutil.rmtree(opener(temp), ignore_errors=True)
        raise
    os.rmdir(opener(temp))                      # empty by now


def contents_proven(zp: ZipPlan) -> str | None:
    """None when every file in the ZIP is on disk with its CRC; else why not."""
    for m in zp.members:
        path = zp.path.parent / Path(*m.dest.parts)
        if _size_on_disk(path) != m.size:
            return "%s is missing or a different size" % m.dest
        if _crc_of(path) != m.crc:
            return "%s differs from the copy in the ZIP" % m.dest
    return None


def run(args) -> int:
    try:
        root, out_dir, cfg = batch.resolve(args, COMMAND)
    except batch.Refused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    artists = set(args.artist) if args.artist else None
    wanted = batch.scope_key(root, artists, set_aside=bool(args.set_aside))
    if args.commit and not args.skip_plan_check:
        why = batch.plan_check(out_dir / PLAN_NAME, wanted, COMMAND)
        if why:
            return batch.refuse_commit(why, COMMAND)

    with run_lock(out_dir, COMMAND):
        errors: list = []
        archives = batch.files_in_scope(root, cfg, artists,
                                        {".zip"} | UNSUPPORTED_SUFFIXES, errors)
        plans = [plan_zip(p) for p in archives]
        for zp in plans:
            if args.commit and zp.status == EXTRACT:
                try:
                    extract(zp)
                    zp.result = "extracted"
                except Exception as exc:
                    zp.result = "FAILED: %s: %s" % (exc.__class__.__name__, exc)
            if not args.set_aside or zp.status not in (EXTRACT, ALREADY):
                continue
            if not args.commit:
                zp.set_aside_to = batch.set_aside_target(zp.path, root, cfg)
            elif zp.status == ALREADY or zp.result == "extracted":
                why = contents_proven(zp)
                try:
                    if why:
                        raise ValueError(why)
                    zp.set_aside_to = batch.set_aside(zp.path, root, cfg)
                except Exception as exc:
                    zp.result = "; ".join(filter(None, [
                        zp.result, "ZIP kept, not set aside: %s" % exc]))
        _report(out_dir, root, cfg, plans, errors, args, wanted)
    return 1 if any(zp.result.startswith("FAILED") for zp in plans) else 0


def _report(out_dir, root, cfg, plans, errors, args, wanted) -> None:
    def rel(p):
        return str(p.relative_to(root)) if p else ""

    committed = args.commit
    lines = ["Unpack %s" % ("COMMITTED" if committed
                            else "(dry run - nothing was written)"),
             "=" * 60,
             "generated %s" % _dt.datetime.now().isoformat(timespec="minutes"), ""]
    counts: dict[str, int] = {}
    for zp in plans:
        counts[zp.status] = counts.get(zp.status, 0) + 1
    lines.append("%d archive(s)%s" % (len(plans), ": " + ", ".join(
        "%d %s" % (v, k) for k, v in sorted(counts.items())) if plans else " found"))
    lines.append("")
    for status, heading in ((EXTRACT, "Extracted" if committed else "To extract"),
                            (ALREADY, "Already extracted"),
                            (REFUSED, "Not extracted"),
                            (UNSUPPORTED, "Not a ZIP")):
        group = [zp for zp in plans if zp.status == status]
        if not group:
            continue
        lines.append("%s (%d)" % (heading, len(group)))
        for zp in group:
            lines.append("  %s" % rel(zp.path))
            if status == EXTRACT:
                lines.append("      %d files, %.1f MB - %s:"
                             % (len(zp.members), zp.size / 1e6, zp.layout))
                if zp.skipped:
                    lines.append("      (%d macOS resource-fork file(s) left out)" % zp.skipped)
                for target in zp.targets:
                    lines.append("      -> %s" % rel(target))
            if zp.reason:
                lines.append("      %s" % zp.reason)
            if zp.result and zp.result != "extracted":
                lines.append("      %s" % zp.result)
            if zp.set_aside_to:
                lines.append("      %s %s" % ("set aside to" if committed
                                              else "would be set aside to",
                                              rel(zp.set_aside_to)))
        lines.append("")
    if errors:
        lines.append("Folders that could not be read (%d)" % len(errors))
        lines.extend("  %s: %s" % e for e in errors)
        lines.append("")
    if not args.set_aside and any(zp.status in (EXTRACT, ALREADY) for zp in plans):
        lines.append("The ZIPs are left where they are. Once the extracted folders "
                     "look right, run again with --set-aside to move those ZIPs into "
                     "%s/%s. Nothing is deleted; empty that folder yourself."
                     % (cfg.settings.review_folder, batch.ORIGINALS))
    text = "\n".join(lines) + "\n"
    report_file(out_dir / "unpack_summary.txt").write_text(text, encoding="utf-8")
    write_csv(out_dir / ("unpack_committed.csv" if committed else "unpack_plan.csv"),
              ["archive", "status", "extracted_to", "files", "bytes", "reason",
               "result", "set_aside_to"],
              ([rel(zp.path), zp.status, " | ".join(rel(t) for t in zp.targets),
                len(zp.members), zp.size, zp.reason, zp.result, rel(zp.set_aside_to)]
               for zp in plans))
    if not committed:
        write_json(out_dir / PLAN_NAME, {
            "generated": _dt.datetime.now().isoformat(timespec="seconds"), **wanted})
    print(text + "reports: %s" % out_dir)
