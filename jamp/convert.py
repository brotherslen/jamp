"""`jamp convert`: SHN to FLAC, proven lossless file by file.

Shorten is a dead format: most players cannot open it and nothing can tag it.
Every `.shn` is converted with ffmpeg to a `.flac` of the same name beside it,
and a conversion counts only when **both files decode to identical audio** -
checked by hashing what ffmpeg decodes from each, not by trusting the encoder.

Rules, each a restriction:

* **Nothing is overwritten.** Where a `.flac` of that name already exists it is
  checked against the SHN instead, and reported as already converted - or as a
  mismatch, which leaves both alone.
* **Nothing unproven is left behind.** The FLAC is written under a temporary
  name and takes its real name only once its audio has been proven identical.
  A FLAC that fails the check is removed - it is this command's own output,
  never the user's file.
* **The SHN stays where it is.** `--set-aside` moves an SHN into
  `_etree_review/originals/` only once its FLAC is proven: in the same run, or
  by an earlier run's log with both files unchanged since; otherwise the pair
  is decoded again first.
* A `<folder>.ffp` is written for the new FLAC files when the folder has none,
  from the fingerprint each FLAC carries.  The folder's existing `.md5` or
  `.st5` files, which name the `.shn` files, are left as they are and noted.

This needs ffmpeg with its Shorten decoder (`jamp doctor` checks).
"""
from __future__ import annotations

import csv
import datetime as _dt
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import batch
from .integrity import flac_streaminfo, locate_ffmpeg
from .report import run_lock, write_csv, write_json
from .winpath import opener

COMMAND = "convert"
PLAN_NAME = "convert_plan.json"
LOG_NAME = "convert_committed.csv"

CONVERT, EXISTS = "convert", "flac exists"
CONVERTED, VERIFIED, MISMATCH, FAILED = "converted", "already converted", "MISMATCH", "FAILED"

_MD5_LINE = re.compile(r"MD5=([0-9a-f]{32})", re.I)
_PART = ".jamp-part.flac"


@dataclass
class ShnPlan:
    shn: Path
    flac: Path
    status: str = CONVERT
    result: str = ""
    detail: str = ""
    shn_md5: str = ""
    flac_md5: str = ""
    set_aside_to: Path | None = None


def audio_md5(ffmpeg: str, path: Path, timeout: int = 3600) -> tuple[str | None, str]:
    """MD5 of the decoded audio, as 32-bit samples so any bit depth compares.

    Both sides of a comparison go through this same call, so the widening is
    applied to both and cannot hide or invent a difference.  The plain path is
    handed to ffmpeg, which refuses the extended \\\\?\\ form.
    """
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-i", os.fspath(path),
           "-map", "0:a", "-c:a", "pcm_s32le", "-f", "md5", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "decode timed out"
    except OSError as exc:
        return None, "could not run ffmpeg: %s" % exc
    m = _MD5_LINE.search(r.stdout or "")
    if r.returncode != 0 or not m:
        err = (r.stderr or "").strip()
        return None, err.splitlines()[-1][:200] if err else "no audio decoded"
    return m.group(1).lower(), ""


def encode(ffmpeg: str, src: Path, dst: Path, timeout: int = 3600) -> str:
    """'' on success, else what went wrong."""
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-y", "-i", os.fspath(src),
           "-map", "0:a", "-c:a", "flac", "-compression_level", "8",
           "-f", "flac", os.fspath(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "encode timed out"
    except OSError as exc:
        return "could not run ffmpeg: %s" % exc
    if r.returncode != 0:
        return (r.stderr or "ffmpeg failed").strip().splitlines()[-1][:200]
    return ""


def plan_files(shns: list[Path]) -> list[ShnPlan]:
    plans = []
    for shn in shns:
        flac = shn.with_suffix(".flac")
        plans.append(ShnPlan(shn=shn, flac=flac,
                             status=EXISTS if os.path.exists(opener(flac)) else CONVERT))
    return plans


def convert_one(ffmpeg: str, sp: ShnPlan) -> None:
    """Convert (or check an existing FLAC), setting result and both hashes."""
    shn_md5, why = audio_md5(ffmpeg, sp.shn)
    if not shn_md5:
        sp.result, sp.detail = FAILED, "the SHN does not decode: %s" % why
        return
    sp.shn_md5 = shn_md5
    if sp.status == EXISTS:
        flac_md5, why = audio_md5(ffmpeg, sp.flac)
        sp.flac_md5 = flac_md5 or ""
        if flac_md5 == shn_md5:
            sp.result, sp.detail = VERIFIED, "the FLAC beside it has identical audio"
        else:
            sp.result = MISMATCH
            sp.detail = ("the FLAC beside it does not decode: %s" % why if not flac_md5
                         else "the FLAC beside it holds different audio; both left alone")
        return

    part = sp.shn.with_name(sp.shn.stem + _PART)
    if os.path.exists(opener(part)):
        os.remove(opener(part))            # a leftover of this command's own
    why = encode(ffmpeg, sp.shn, part)
    if why:
        _remove(part)
        sp.result, sp.detail = FAILED, "ffmpeg could not convert it: %s" % why
        return
    flac_md5, why = audio_md5(ffmpeg, part)
    sp.flac_md5 = flac_md5 or ""
    if flac_md5 != shn_md5:
        _remove(part)
        sp.result = MISMATCH
        sp.detail = ("the new FLAC does not decode: %s" % why if not flac_md5
                     else "the new FLAC's audio differs from the SHN; not kept")
        return
    if os.path.exists(opener(sp.flac)):
        _remove(part)
        sp.result, sp.detail = FAILED, "a FLAC of that name appeared meanwhile"
        return
    os.rename(opener(part), opener(sp.flac))
    sp.result, sp.detail = CONVERTED, "decoded audio identical"


def _remove(path: Path) -> None:
    try:
        os.remove(opener(path))
    except OSError:
        pass


def read_log(path: Path) -> dict[str, dict]:
    """Proven pairs from an earlier run, keyed by the SHN's path, lower-cased."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            if row.get("result") in (CONVERTED, VERIFIED) and row.get("shn_md5") \
                    and row.get("shn_md5") == row.get("flac_md5"):
                out[row["shn_path"].lower()] = row
    return out


def proven_by_log(sp: ShnPlan, log: dict[str, dict]) -> bool:
    row = log.get(str(sp.shn).lower())
    if not row:
        return False
    return (batch.stat_key(sp.shn) == _key(row, "shn")
            and batch.stat_key(sp.flac) == _key(row, "flac"))


def _key(row, side):
    try:
        return int(row["%s_size" % side]), int(row["%s_mtime" % side])
    except (KeyError, ValueError):
        return None


def write_ffp(folder: Path, flacs: list[Path]) -> Path | None:
    """`<folder>.ffp` from each FLAC's own fingerprint, if the folder has no .ffp."""
    if any(p.suffix.lower() == ".ffp" for p in folder.iterdir()):
        return None
    lines = []
    for flac in sorted(flacs, key=lambda p: p.name.lower()):
        info = flac_streaminfo(flac)
        if not info or not info.get("md5"):
            return None
        lines.append("%s:%s" % (flac.name, info["md5"]))
    target = folder / (folder.name + ".ffp")
    with open(opener(target), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return target


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

    ffmpeg, how = locate_ffmpeg(getattr(args, "ffmpeg", None))
    if not ffmpeg:
        print("convert: ffmpeg is needed and was %s" % how, file=sys.stderr)
        return 2

    with run_lock(out_dir, COMMAND):
        errors: list = []
        shns = batch.files_in_scope(root, cfg, artists, {".shn"}, errors)
        plans = plan_files(shns)
        earlier = read_log(out_dir / LOG_NAME)
        if args.commit:
            # Proven by an earlier run and untouched since: no need to decode
            # again just to set it aside.
            todo = [sp for sp in plans
                    if not (args.set_aside and sp.status == EXISTS and proven_by_log(sp, earlier))]
            for sp in plans:
                if sp not in todo:
                    row = earlier[str(sp.shn).lower()]
                    sp.result, sp.detail = VERIFIED, "proven by an earlier run, unchanged since"
                    sp.shn_md5, sp.flac_md5 = row["shn_md5"], row["flac_md5"]
            done = 0
            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                for _ in pool.map(lambda sp: convert_one(ffmpeg, sp), todo):
                    done += 1
                    if done % 25 == 0 or done == len(todo):
                        print("  %d of %d checked" % (done, len(todo)), flush=True)
            by_folder: dict[Path, list[ShnPlan]] = {}
            for sp in plans:
                by_folder.setdefault(sp.shn.parent, []).append(sp)
            for folder, group in by_folder.items():
                if all(sp.result == CONVERTED for sp in group):
                    try:
                        written = write_ffp(folder, [sp.flac for sp in group])
                    except OSError:
                        written = None
                    if written:
                        group[0].detail += "; wrote %s" % written.name
        for sp in plans:
            if not args.set_aside:
                continue
            if not args.commit:
                sp.set_aside_to = batch.set_aside_target(sp.shn, root, cfg)
            elif sp.result in (CONVERTED, VERIFIED):
                try:
                    sp.set_aside_to = batch.set_aside(sp.shn, root, cfg)
                except Exception as exc:
                    sp.detail += "; SHN kept, not set aside: %s" % exc
        _report(out_dir, root, cfg, plans, errors, args, wanted)
    return 1 if any(sp.result in (FAILED, MISMATCH) for sp in plans) else 0


def _report(out_dir, root, cfg, plans, errors, args, wanted) -> None:
    def rel(p):
        return str(p.relative_to(root)) if p else ""

    committed = args.commit
    lines = ["Convert SHN to FLAC %s" % ("COMMITTED" if committed
                                         else "(dry run - nothing was written)"),
             "=" * 60,
             "generated %s" % _dt.datetime.now().isoformat(timespec="minutes"), ""]
    folders = sorted({sp.shn.parent for sp in plans}, key=lambda p: str(p).lower())
    lines.append("%d SHN file(s) in %d folder(s)" % (len(plans), len(folders)))
    if committed:
        counts: dict[str, int] = {}
        for sp in plans:
            counts[sp.result or "not run"] = counts.get(sp.result or "not run", 0) + 1
        lines.append("  " + ", ".join("%d %s" % (v, k) for k, v in sorted(counts.items())))
        set_aside = sum(1 for sp in plans if sp.set_aside_to)
        if args.set_aside:
            lines.append("  %d SHN file(s) set aside to %s/%s"
                         % (set_aside, cfg.settings.review_folder, batch.ORIGINALS))
    else:
        lines.append("  %d to convert, %d with a FLAC already beside them (to be checked)"
                     % (sum(1 for sp in plans if sp.status == CONVERT),
                        sum(1 for sp in plans if sp.status == EXISTS)))
        lines.append("  Each conversion is checked by decoding both files; expect "
                     "a few seconds per file.")
    lines.append("")
    for folder in folders:
        group = [sp for sp in plans if sp.shn.parent == folder]
        lines.append(rel(folder))
        for sp in group:
            if committed:
                ok = sp.result in (CONVERTED, VERIFIED)
                lines.append("  %-18s %s%s%s" % (
                    sp.result or "not run", sp.shn.name,
                    "  (SHN set aside)" if sp.set_aside_to else "",
                    "" if ok and "not set aside" not in sp.detail else "  - " + sp.detail))
            else:
                lines.append("  %-18s %s" % ("check existing" if sp.status == EXISTS
                                             else "convert", sp.shn.name))
        sidecars = [p.name for p in folder.iterdir()
                    if p.suffix.lower() in (".md5", ".st5")] if folder.is_dir() else []
        if sidecars:
            lines.append("  note: %s still name the .shn files; they are left as they are"
                         % ", ".join(sorted(sidecars)))
        lines.append("")
    if errors:
        lines.append("Folders that could not be read (%d)" % len(errors))
        lines.extend("  %s: %s" % e for e in errors)
        lines.append("")
    if not args.set_aside and plans:
        lines.append("The SHN files are left where they are. Once you are happy, run "
                     "again with --set-aside to move every SHN whose FLAC is proven "
                     "into %s/%s. Nothing is deleted; empty that folder yourself."
                     % (cfg.settings.review_folder, batch.ORIGINALS))
    text = "\n".join(lines) + "\n"
    (out_dir / "convert_summary.txt").write_text(text, encoding="utf-8")
    header = ["shn_path", "flac_path", "result", "detail", "shn_md5", "flac_md5",
              "shn_size", "shn_mtime", "flac_size", "flac_mtime", "set_aside_to"]

    def row(sp):
        shn_k = batch.stat_key(sp.set_aside_to or sp.shn) or ("", "")
        flac_k = batch.stat_key(sp.flac) or ("", "")
        # Keyed by where the SHN was when it was proven, so a later run finds it.
        return [str(sp.shn), str(sp.flac), sp.result or sp.status, sp.detail,
                sp.shn_md5, sp.flac_md5, shn_k[0], shn_k[1], flac_k[0], flac_k[1],
                str(sp.set_aside_to or "")]

    if committed:
        # Appended, not replaced: the proof that lets a later --set-aside skip
        # decoding everything again must survive the next run.
        log = out_dir / LOG_NAME
        new = not log.exists()
        with open(log, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(header)
            for sp in plans:
                w.writerow(row(sp))
    else:
        write_csv(out_dir / "convert_plan.csv", header, (row(sp) for sp in plans))
        write_json(out_dir / PLAN_NAME, {
            "generated": _dt.datetime.now().isoformat(timespec="seconds"), **wanted})
    print(text + "reports: %s" % out_dir)
