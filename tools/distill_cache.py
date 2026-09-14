"""Boil the phase 3 cache down to the facts it actually uses.

The cache stores raw responses, which is right for a working cache: if a parser
improves, every stored answer can be re-read without touching the network.  But
it is a poor thing to hand somebody else.  One archive.org metadata payload
carries file listings, checksums, torrent metadata and reviews; phase 3 reads
about a dozen fields from it and a list of track titles and lengths.

This produces the distilled form - shows and tracks, nothing else - by running
the same parsers the pipeline uses, so it cannot drift from what phase 3 would
have understood.

The trade is deliberate: the distilled database is small enough to share and
cannot be re-parsed later, while the raw cache is large and can.  Keep the raw
one for yourself; hand this one out.

    py tools/distill_cache.py <cache.sqlite> <distilled.sqlite>
"""
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jamp import archiveorg, jerrybase, mmjarchive, phishin  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS shows (
    id         INTEGER PRIMARY KEY,
    source     TEXT NOT NULL,
    key        TEXT NOT NULL,          -- identifier, slug or date
    date       TEXT,
    venue      TEXT,
    city       TEXT,
    state      TEXT,
    taper      TEXT,
    transferer TEXT,
    act        TEXT,
    UNIQUE (source, key)
);
CREATE TABLE IF NOT EXISTS tracks (
    show_id  INTEGER NOT NULL REFERENCES shows(id),
    position INTEGER,
    title    TEXT,
    seconds  REAL,
    segue    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tracks_by_show ON tracks(show_id);
CREATE INDEX IF NOT EXISTS shows_by_date ON shows(date);
"""


def add(dst, source, key, *, date=None, venue=None, city=None, state=None,
        taper=None, transferer=None, act=None, tracks=()):
    cur = dst.execute(
        "INSERT OR IGNORE INTO shows (source, key, date, venue, city, state, "
        "taper, transferer, act) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, key, date, venue, city, state, taper, transferer, act))
    if not cur.rowcount:
        return 0
    show_id = cur.lastrowid
    dst.executemany(
        "INSERT INTO tracks (show_id, position, title, seconds, segue) "
        "VALUES (?, ?, ?, ?, ?)",
        [(show_id, p, t, s, int(bool(g))) for p, t, s, g in tracks])
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", type=Path)
    ap.add_argument("destination", type=Path)
    args = ap.parse_args()

    src = sqlite3.connect("file:%s?mode=ro" % args.source.as_posix(), uri=True)
    if args.destination.exists():
        args.destination.unlink()
    dst = sqlite3.connect(str(args.destination))
    dst.executescript(SCHEMA)

    counts = {}
    for url, status, body in src.execute("SELECT url, status, body FROM http"):
        if status == 404 or not body:
            continue
        try:
            if "archive.org/metadata/" in url:
                rec = archiveorg.parse_metadata(json.loads(body), url.rsplit("/", 1)[-1])
                n = add(dst, "archive.org", rec.identifier, date=rec.date,
                        venue=rec.venue, city=rec.city, state=rec.state,
                        taper=rec.taper, transferer=rec.transferer,
                        act=rec.creator,
                        tracks=[(t.number, t.title, t.seconds, t.segue)
                                for t in rec.tracks])
                counts["archive.org"] = counts.get("archive.org", 0) + n
            elif "phish.in/api/v2/shows/" in url:
                show = phishin.parse_show(json.loads(body))
                if show:
                    n = add(dst, "phish.in", show.date or url.rsplit("/", 1)[-1],
                            date=show.date, venue=show.venue, city=show.city,
                            state=show.state,
                            tracks=[(t.position, t.title, t.seconds, False)
                                    for t in show.tracks])
                    counts["phish.in"] = counts.get("phish.in", 0) + n
            elif "jerrybase.com/events/" in url:
                text = body.decode("utf-8", "replace")
                slug = url.rsplit("/", 1)[-1]
                ev = jerrybase.parse_event(text, slug)
                if ev:
                    n = add(dst, "jerrybase", slug, date=ev.date, venue=ev.venue,
                            city=ev.city, state=ev.state, act=ev.act,
                            tracks=[(i + 1, s, None, False)
                                    for i, s in enumerate(ev.songs)])
                    counts["jerrybase"] = counts.get("jerrybase", 0) + n
            # A show page is /shows/YYYY/YYYY-MM-DD-slug; the year index is
            # /shows/YYYY and has no setlist of its own.
            elif re.search(r"mymorningjacket\.net/.*/shows/\d{4}/\d{4}-\d{2}-\d{2}", url):
                text = body.decode("utf-8", "replace")
                show = mmjarchive.parse_show(text, url)
                if show:
                    n = add(dst, "mmjarchive", url.rsplit("/", 1)[-1], date=show.date,
                            venue=show.venue, city=show.city, state=show.state,
                            tracks=[(i + 1, e.title, None, False)
                                    for i, e in enumerate(show.entries)])
                    counts["mmjarchive"] = counts.get("mmjarchive", 0) + n
        except Exception:                      # noqa: BLE001 - counted, not silent
            counts.setdefault("unparsed", 0)
            counts["unparsed"] += 1
    dst.commit()
    dst.execute("VACUUM")
    shows = dst.execute("SELECT COUNT(*) FROM shows").fetchone()[0]
    tracks = dst.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    dst.close()
    src.close()

    before = args.source.stat().st_size / 1048576.0
    after = args.destination.stat().st_size / 1048576.0
    print("distilled %d shows and %d tracks" % (shows, tracks))
    for k, v in sorted(counts.items()):
        print("   %-14s %d" % (k, v))
    print()
    print("  %.1f MB -> %.1f MB  (%.0fx smaller)" % (before, after, before / max(after, 0.01)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
