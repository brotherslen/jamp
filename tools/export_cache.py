"""Make a shareable copy of the phase 3 cache.

The cache is keyed by URL, and those URLs are deterministic: the band prefixes
come from the shipped config and the rest is the show's date.  So two people
with the same show generate byte-identical URLs, and a seeded cache handed to
somebody else answers their run without a single request.  Nothing in it is tied
to a machine, a folder layout, or an API key - the key is used to *fetch* and
never stored.

**phish.net is excluded by default, deliberately.**  Their terms permit local
storage - "we encourage you to cache frequently accessed data locally" - which
is not the same as redistributing it, and sharing those rows would hand the
benefit of one person's key to people who never agreed to their terms.  Anyone
who wants phish.net can request a free key of their own in a minute, and anyone
who does not gets phish.in, which has track durations and is the better source
regardless.  It is 11% of the cache.

Everything else is a copy of what a public page or an open API returned.
Sharing it means thousands of requests those services never have to serve.

    py tools/export_cache.py <source.sqlite> <destination.sqlite>
    py tools/export_cache.py <src> <dst> --include phish.net     # if you must
    py tools/export_cache.py <src> <dst> --only archive.org phish.in
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# URL fragment -> the name people use for it.
SOURCES = {
    "archive.org": "archive.org",
    "phish.in": "phish.in",
    "api.phish.net": "phish.net",
    "jerrybase": "jerrybase",
    "mymorningjacket": "mmjarchive",
}

EXCLUDED_BY_DEFAULT = {"phish.net"}


def source_of(url: str) -> str:
    for fragment, name in SOURCES.items():
        if fragment in url:
            return name
    return "other"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path)
    ap.add_argument("destination", type=Path)
    ap.add_argument("--include", nargs="*", default=[],
                    help="add a source that is excluded by default")
    ap.add_argument("--only", nargs="*", default=[],
                    help="export just these sources")
    args = ap.parse_args()

    if not args.source.exists():
        print("no cache at %s" % args.source, file=sys.stderr)
        return 2

    wanted = set(args.only) if args.only else (
        set(SOURCES.values()) - EXCLUDED_BY_DEFAULT | set(args.include))

    src = sqlite3.connect("file:%s?mode=ro" % args.source.as_posix(), uri=True)
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    if args.destination.exists():
        args.destination.unlink()
    dst = sqlite3.connect(str(args.destination))
    dst.executescript("""
    CREATE TABLE IF NOT EXISTS http (
        url        TEXT PRIMARY KEY,
        status     INTEGER NOT NULL,
        body       BLOB    NOT NULL,
        fetched_at TEXT    NOT NULL
    );
    CREATE TABLE IF NOT EXISTS failures (
        url TEXT PRIMARY KEY, reason TEXT NOT NULL, failed_at TEXT NOT NULL
    );
    """)

    kept = dropped = 0
    kept_bytes = 0
    for url, status, body, fetched_at in src.execute(
            "SELECT url, status, body, fetched_at FROM http"):
        if source_of(url) in wanted:
            dst.execute("INSERT OR REPLACE INTO http VALUES (?, ?, ?, ?)",
                        (url, status, body, fetched_at))
            kept += 1
            kept_bytes += len(body or b"")
        else:
            dropped += 1
    dst.commit()
    dst.execute("VACUUM")
    dst.close()
    src.close()

    print("exported %d URLs (%.1f MB), left out %d" %
          (kept, kept_bytes / 1048576.0, dropped))
    print("  included: %s" % ", ".join(sorted(wanted)))
    left_out = sorted(set(SOURCES.values()) - wanted)
    if left_out:
        print("  excluded: %s" % ", ".join(left_out))
    print("  written to %s (%.1f MB on disk)"
          % (args.destination, args.destination.stat().st_size / 1048576.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
