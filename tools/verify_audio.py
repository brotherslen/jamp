"""Decode every FLAC and check it against the fingerprint it already carries.

This closes a known gap: the structural sweep
that found 309 unplayable files could only look at headers, "so it cannot detect
flipped bits inside an otherwise-valid file.  `flac -t` would, and would verify
against the fingerprint every FLAC already carries."  ffmpeg is now installed
with a FLAC decoder, so it can be done.

**Why the stored MD5 is the right fingerprint.**  Every FLAC's STREAMINFO block
holds an MD5 of the *decoded audio*, written by the encoder.  It therefore
survives retagging and renaming exactly the way a `.ffp` sidecar does, and
unlike a plain `.md5` it says nothing about the tags.  Comparing it against what
the file decodes to today asks the only question that matters: is this still the
audio the encoder saw?

That catches a class nothing else here can.  A file may hold a valid header,
decode without a single error, show its tags in Explorer and play - while most
of its audio has been replaced by silence.  One such folder was found by hand:
21 files averaging 60% zero bytes, every one of them decoding "successfully" to
something that is not what its own header says it should be.  A header check
calls those files healthy.  A decode does not.

Four results, and the difference between them matters:

    PASS          decoded audio matches the MD5 in the file's own header
    MISMATCH      it decodes, but to different audio - silent corruption
    UNREADABLE    no decoder can get audio out of it at all
    NO_MD5        the encoder never wrote one; nothing to check against

NO_MD5 is not a failure.  Some encoders leave the field zeroed, and a file that
cannot be checked is a different statement from a file that failed.

Resumable on purpose.  A full pass over a library this size is measured in
hours, so results are appended as they are produced and a second run skips
anything already recorded at the same size and mtime.  Interrupting it costs
only the file in flight.

    py tools/verify_audio.py <ROOT> --out <report.csv>
    py tools/verify_audio.py <ROOT> --out <report.csv> --workers 8
    py tools/verify_audio.py <ROOT> --out <report.csv> --artist Phish
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The reading and the decoding both live in the package, so this command and
# `phase0 --verify-audio` cannot drift apart. Two copies of a check like this is
# how one of them quietly stops agreeing with the other.
from jamp.integrity import (  # noqa: E402
    MISMATCH, NO_MD5, PASS, UNREADABLE, find_ffmpeg, flac_streaminfo, verify)

FIELDS = ["status", "file", "bytes", "mtime", "stored_md5", "decoded_md5",
          "seconds", "detail"]


def check(ffmpeg: str, path: Path, root: Path) -> list:
    st = path.stat()
    v = verify(ffmpeg, path)
    return [v["status"], str(path.relative_to(root)), st.st_size, int(st.st_mtime),
            v["stored_md5"], v["decoded_md5"],
            "%.1f" % v["seconds"] if v.get("seconds") else "", v["detail"]]


def needs_header(out: Path) -> bool:
    """Missing or empty.  Existence alone was the test, and Ctrl-C during the
    opening scan leaves a 0-byte file: no header was written, every resume then
    read its first data row as the header, and the resume state was lost."""
    return not out.exists() or out.stat().st_size == 0


def already_done(out: Path) -> set:
    """(relpath, size, mtime) of everything a previous run recorded.

    Keyed on size and mtime as well as the name so that a file which has since
    been replaced is checked again rather than inheriting an old verdict.
    """
    if not out.exists():
        return set()
    done = set()
    try:
        with out.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                try:
                    done.add((r["file"], int(r["bytes"]), int(r["mtime"])))
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        return set()
    return done


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("root", type=Path)
    ap.add_argument("--out", type=Path, required=True,
                    help="CSV to append results to; re-running skips what it holds")
    ap.add_argument("--artist", action="append", default=None,
                    help="limit to this top-level folder of ROOT; repeatable")
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) // 2),
                    help="parallel decodes (default: half the cores)")
    ap.add_argument("--ffmpeg", default=None)
    args = ap.parse_args(argv)

    # The library holds accented names - "Barceló Maya Beach" carries a
    # combining acute - and a Windows console defaults to cp1252, which cannot
    # encode one.  Printing the path was enough to kill the run before a single
    # file was checked.  Reports are written as UTF-8 regardless; this is only
    # about what reaches the terminal.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):        # not a real console; fine
            pass

    root = args.root.resolve()
    if not root.is_dir():
        print("ROOT is not a directory: %s" % root, file=sys.stderr)
        return 2
    ffmpeg = find_ffmpeg(args.ffmpeg)
    if args.out.parent and not args.out.parent.exists():
        args.out.parent.mkdir(parents=True, exist_ok=True)

    files = []
    for p in root.rglob("*.flac"):
        rel = p.relative_to(root)
        if args.artist and (not rel.parts or rel.parts[0] not in set(args.artist)):
            continue
        files.append(p)
    files.sort()

    done = already_done(args.out)
    todo = []
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        if (str(p.relative_to(root)), st.st_size, int(st.st_mtime)) not in done:
            todo.append(p)

    print("%d flac files under %s" % (len(files), root))
    print("  %d already recorded in %s, %d to check"
          % (len(files) - len(todo), args.out, len(todo)))
    if not todo:
        return 0
    print("  ffmpeg: %s" % ffmpeg)
    print("  %d workers" % args.workers)

    fresh = needs_header(args.out)
    counts = {PASS: 0, MISMATCH: 0, UNREADABLE: 0, NO_MD5: 0}
    t0 = time.time()
    with args.out.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if fresh:
            w.writerow(FIELDS)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(check, ffmpeg, p, root): p for p in todo}
            for i, fut in enumerate(as_completed(futures), 1):
                try:
                    row = fut.result()
                except Exception as exc:               # noqa: BLE001
                    p = futures[fut]
                    row = [UNREADABLE, str(p.relative_to(root)), 0, 0, "", "", "",
                           "checker raised: %s" % str(exc)[:200]]
                counts[row[0]] = counts.get(row[0], 0) + 1
                w.writerow(row)
                if row[0] in (MISMATCH, UNREADABLE):
                    fh.flush()
                    print("  %-10s %s" % (row[0], row[1]), flush=True)
                if i % 100 == 0:
                    fh.flush()
                    rate = i / max(1e-6, time.time() - t0)
                    print("  %d/%d  %.1f/s  pass %d  mismatch %d  unreadable %d"
                          % (i, len(todo), rate, counts[PASS], counts[MISMATCH],
                             counts[UNREADABLE]), flush=True)

    print("\ndone in %.0f min" % ((time.time() - t0) / 60))
    for k in (PASS, MISMATCH, UNREADABLE, NO_MD5):
        print("  %-11s %d" % (k, counts.get(k, 0)))
    print("  full results: %s" % args.out)
    if counts.get(MISMATCH):
        print("\n  MISMATCH means the file decodes but not to the audio its own")
        print("  header describes. That is damage, and no retag or rename fixes it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
