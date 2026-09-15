"""Boil the phase 3 cache down to the facts it actually uses.

The work is done by `jamp.distill`, which `phase3 --seed` and `jamp complete`
also use; this script is the same thing for a cache and a destination of your
choosing - a copy to hand to somebody else, say.

    py tools/distill_cache.py <cache.sqlite> <distilled.sqlite>
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jamp.distill import distill  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path)
    ap.add_argument("destination", type=Path)
    args = ap.parse_args()
    got = distill(args.source, args.destination)
    print("distilled %d shows and %d tracks" % (got["shows"], got["tracks"]))
    for k, v in sorted(got["by_source"].items()):
        print("   %-14s %d" % (k, v))
    print()
    print("  %.1f MB -> %.1f MB  (%.0fx smaller)"
          % (got["cache_mb"], got["shows_mb"], got["cache_mb"] / max(got["shows_mb"], 0.01)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
