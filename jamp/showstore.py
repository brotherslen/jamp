"""Read a distilled show database, so a 2 MB file can stand in for the network.

`tools/distill_cache.py` boils a seeded cache down to shows and tracks - 89 MB
of raw responses became 2 MB of facts.  This reads that back and hands phase 3
the same objects the live sources would have, so a person given the small file
gets the same answers without a request, a key, or the 96 MB.

It is consulted **before** the network rather than after.  Somebody holding this
file already has the answer; going to archive.org first to fetch what they were
just handed would be slower for them and ruder to the archive.  Anything the
store does not hold still falls through to the live sources as usual.

The objects returned are the sources' own types, deliberately.  Everything
downstream - the alignment, the tiering, the consensus on titles - then works on
a stored show exactly as it does on a fetched one, with no second code path to
keep honest.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import archiveorg, jerrybase, mmjarchive, phishin


class ShowStore:
    """Read-only view of a distilled database.  Missing or unreadable is fine."""

    def __init__(self, path: Path | str | None):
        self.path = Path(path) if path else None
        self.db = None
        self.hits = 0
        if not self.path or not self.path.exists():
            return
        try:
            self.db = sqlite3.connect(
                "file:%s?mode=ro" % self.path.as_posix(), uri=True)
            self.db.execute("SELECT 1 FROM shows LIMIT 1")
        except sqlite3.Error:
            self.db = None

    def __bool__(self) -> bool:
        return self.db is not None

    # -- lookups ---------------------------------------------------------
    def _rows(self, source: str, date: str):
        if self.db is None:
            return []
        return self.db.execute(
            "SELECT id, key, date, venue, city, state, taper, transferer, act "
            "FROM shows WHERE source = ? AND date = ?", (source, date)).fetchall()

    def _tracks(self, show_id: int):
        return self.db.execute(
            "SELECT position, title, seconds, segue FROM tracks "
            "WHERE show_id = ? ORDER BY position IS NULL, position",
            (show_id,)).fetchall()

    def recordings_for(self, prefixes, date: str) -> list:
        """archive.org recordings, filtered to the band the way a search would."""
        out = []
        wanted = tuple(p.lower() for p in prefixes)
        for sid, key, d, venue, city, state, taper, transferer, act in self._rows(
                "archive.org", date):
            if wanted and not key.lower().startswith(wanted):
                continue
            rec = archiveorg.Recording(
                identifier=key, date=d, venue=venue, city=city, state=state,
                creator=act, taper=taper, transferer=transferer)
            rec.tracks = [archiveorg.Track(number=p, title=t, seconds=s,
                                           segue=bool(g))
                          for p, t, s, g in self._tracks(sid)]
            out.append(rec)
        if out:
            self.hits += 1
        return out

    def phishin_show(self, date: str):
        rows = self._rows("phish.in", date)
        if not rows:
            return None
        sid, key, d, venue, city, state, _taper, _tr, _act = rows[0]
        show = phishin.Show(date=d, venue=venue, city=city, state=state)
        show.tracks = [phishin.Track(position=p, title=t, seconds=s)
                       for p, t, s, _g in self._tracks(sid)]
        self.hits += 1
        return show

    def jerrybase_events(self, date: str) -> list:
        out = []
        for sid, key, d, venue, city, state, _taper, _tr, act in self._rows(
                "jerrybase", date):
            ev = jerrybase.Event(slug=key, date=d, act=act, venue=venue,
                                 city=city, state=state)
            # The distilled form keeps song order and drops the set boundaries,
            # which nothing downstream reads: `songs` flattens them anyway.
            songs = [t for _p, t, _s, _g in self._tracks(sid) if t]
            if songs:
                ev.sets = [("Set", songs)]
            # The marker (early/late/acoustic) is not stored, so a stored event
            # cannot disambiguate a night with several shows.  Left as None
            # rather than guessed: choose_event will decline instead of picking.
            out.append(ev)
        if out:
            self.hits += 1
        return out

    def mmj_show(self, date: str):
        rows = self._rows("mmjarchive", date)
        if not rows:
            return None
        sid, key, d, venue, city, state, _taper, _tr, _act = rows[0]
        show = mmjarchive.Show(url="stored:%s" % key, date=d, venue=venue,
                               city=city, state=state)
        for _p, title, _s, _g in self._tracks(sid):
            # A break was stored as an entry with no title, which is exactly how
            # it is modelled in the live parser.
            show.entries.append(
                mmjarchive.Entry(mmjarchive.SONG, title) if title
                else mmjarchive.Entry(mmjarchive.BREAK))
        self.hits += 1
        return show if show.entries else None

    def stats(self) -> dict:
        if self.db is None:
            return {"shows": 0, "tracks": 0, "hits": 0}
        shows = self.db.execute("SELECT COUNT(*) FROM shows").fetchone()[0]
        tracks = self.db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        return {"shows": shows, "tracks": tracks, "hits": self.hits}

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None
