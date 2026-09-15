"""Boil the phase 3 cache down to the facts it actually uses.

The cache stores raw responses, which is right for a working cache: if a parser
improves, every stored answer can be re-read without touching the network.  But
it is a poor thing to hand somebody else, and slow to search.  One archive.org
metadata payload carries file listings, checksums, torrent metadata and reviews;
phase 3 reads about a dozen fields from it and a list of track titles and
lengths.

This produces the distilled form - shows and tracks, nothing else - by running
the same parsers the pipeline uses, so it cannot drift from what phase 3 would
have understood.  `jamp complete` reads it, and `phase3 --shows` reads it before
the network.

It used to be only `tools/distill_cache.py`, which a standalone or pip install
does not have - so `complete` could not run for anyone but the maintainer.  Now
`phase3 --seed` writes it beside the cache, and `complete` builds it when it is
missing or older than the cache.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from . import archiveorg, jerrybase, mmjarchive, phishin

SHOWS_NAME = "shows.sqlite"

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


def shows_path_for(cache: Path) -> Path:
    """Where the distilled database for a cache lives: right beside it."""
    return Path(cache).with_name(SHOWS_NAME)


def is_stale(cache: Path, shows: Path) -> bool:
    """Missing, or older than the cache it was made from."""
    shows, cache = Path(shows), Path(cache)
    if not shows.exists():
        return True
    return cache.exists() and cache.stat().st_mtime > shows.stat().st_mtime


def _add(dst, source, key, *, date=None, venue=None, city=None, state=None,
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


def distill(source: Path, destination: Path) -> dict:
    """Write `destination` from the cache at `source`; return what went in.

    Written to a temporary file and moved into place, so a reader never sees a
    half-written database and a failure leaves the previous one intact.
    """
    source, destination = Path(source), Path(destination)
    temp = destination.with_name(destination.name + ".part")
    if temp.exists():
        temp.unlink()
    src = sqlite3.connect("file:%s?mode=ro" % source.as_posix(), uri=True)
    dst = sqlite3.connect(str(temp))
    counts: dict[str, int] = {}
    try:
        dst.executescript(SCHEMA)
        for url, status, body in src.execute("SELECT url, status, body FROM http"):
            if status == 404 or not body:
                continue
            try:
                source_name, n = _distill_one(dst, url, body)
                if source_name:
                    counts[source_name] = counts.get(source_name, 0) + n
            except Exception:                    # noqa: BLE001 - counted, not silent
                counts["unparsed"] = counts.get("unparsed", 0) + 1
        dst.commit()
        dst.execute("VACUUM")
        shows = dst.execute("SELECT COUNT(*) FROM shows").fetchone()[0]
        tracks = dst.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    finally:
        dst.close()
        src.close()
    temp.replace(destination)
    return {"shows": shows, "tracks": tracks, "by_source": counts,
            "cache_mb": source.stat().st_size / 1048576.0,
            "shows_mb": destination.stat().st_size / 1048576.0}


def _distill_one(dst, url: str, body: bytes) -> tuple[str | None, int]:
    if "archive.org/metadata/" in url:
        rec = archiveorg.parse_metadata(json.loads(body), url.rsplit("/", 1)[-1])
        return "archive.org", _add(
            dst, "archive.org", rec.identifier, date=rec.date, venue=rec.venue,
            city=rec.city, state=rec.state, taper=rec.taper,
            transferer=rec.transferer, act=rec.creator,
            tracks=[(t.number, t.title, t.seconds, t.segue) for t in rec.tracks])
    if "phish.in/api/v2/shows/" in url:
        show = phishin.parse_show(json.loads(body))
        if not show:
            return None, 0
        return "phish.in", _add(
            dst, "phish.in", show.date or url.rsplit("/", 1)[-1], date=show.date,
            venue=show.venue, city=show.city, state=show.state,
            tracks=[(t.position, t.title, t.seconds, False) for t in show.tracks])
    if "jerrybase.com/events/" in url:
        slug = url.rsplit("/", 1)[-1]
        ev = jerrybase.parse_event(body.decode("utf-8", "replace"), slug)
        if not ev:
            return None, 0
        return "jerrybase", _add(
            dst, "jerrybase", slug, date=ev.date, venue=ev.venue, city=ev.city,
            state=ev.state, act=ev.act,
            tracks=[(i + 1, s, None, False) for i, s in enumerate(ev.songs)])
    # A show page is /shows/YYYY/YYYY-MM-DD-slug; the year index is /shows/YYYY
    # and has no setlist of its own.
    if re.search(r"mymorningjacket\.net/.*/shows/\d{4}/\d{4}-\d{2}-\d{2}", url):
        show = mmjarchive.parse_show(body.decode("utf-8", "replace"), url)
        if not show:
            return None, 0
        return "mmjarchive", _add(
            dst, "mmjarchive", url.rsplit("/", 1)[-1], date=show.date,
            venue=show.venue, city=show.city, state=show.state,
            tracks=[(i + 1, e.title, None, False) for i, e in enumerate(show.entries)])
    return None, 0
