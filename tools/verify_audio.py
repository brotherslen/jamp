"""Decode every FLAC and check it against the fingerprint it already carries.

The same check as `jamp phase0 --verify-audio`, done by the same code
(`jamp.verify`), without phase 0's inventory around it: for a quick pass over
one folder, or a ledger kept somewhere of your choosing.

Four results, and the difference between them matters:

    PASS          decoded audio matches the MD5 in the file's own header
    MISMATCH      it decodes, but to different audio - silent corruption
    UNREADABLE    no decoder can get audio out of it at all
    NO_MD5        the encoder never wrote one; nothing to check against

Resumable: results are appended as they are produced and a second run skips
anything already recorded at the same size and mtime.

    py tools/verify_audio.py <ROOT> --out <ledger.csv>
    py tools/verify_audio.py <ROOT> --out <ledger.csv> --workers 8 --artist Phish
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jamp.integrity import MISMATCH, NO_MD5, PASS, UNREADABLE, find_ffmpeg  # noqa: E402
from jamp.verify import verify_files  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("root", type=Path)
    ap.add_argument("--out", type=Path, required=True,
                    help="CSV ledger to append results to; re-running skips what it holds")
    ap.add_argument("--artist", action="append", default=None,
                    help="limit to this top-level folder of ROOT; repeatable")
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) // 2),
                    help="parallel decodes (default: half the cores)")
    ap.add_argument("--ffmpeg", default=None)
    args = ap.parse_args(argv)

    # Accented names reach a cp1252 console; printing one must not end the run.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    root = args.root.resolve()
    if not root.is_dir():
        print("ROOT is not a directory: %s" % root, file=sys.stderr)
        return 2
    files = sorted(p for p in root.rglob("*.flac")
                   if not args.artist or p.relative_to(root).parts[0] in set(args.artist))
    t0 = time.time()
    verdicts = verify_files(files, root, args.out, find_ffmpeg(args.ffmpeg),
                            workers=args.workers, progress=lambda m: print(m, flush=True))
    counts: dict[str, int] = {}
    for row in verdicts.values():
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        if row["status"] in (MISMATCH, UNREADABLE):
            print("  %-10s %s" % (row["status"], row["file"]))
    print("\ndone in %.0f min" % ((time.time() - t0) / 60))
    for k in (PASS, MISMATCH, UNREADABLE, NO_MD5):
        print("  %-11s %d" % (k, counts.get(k, 0)))
    print("  full results: %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
