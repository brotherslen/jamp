"""What phish.net knows about a Phish show.

archive.org does not carry Phish - the band had it pulled from the Live Music
Archive - so seventeen of Phish's folders came back from phase 3 with nothing.
phish.net is the reference work for that catalogue.

**It needs a key**, free but requested, from https://phish.net/api/keys/.  The
private API key, not the public one: the docs say the key goes in a parameter
named `apikey`, and the public key is the browser-side variant.  The key is read
from a file outside the repo and never printed, never logged, and never stored -
`cache_key()` strips it, so what the cache and the reports hold is the URL
without it.

**There are no durations here either.**  `tracktime` exists as a column and is
empty on every row checked, so - exactly as with jerrybase - a title rests on
the track count agreeing rather than on the duration alignment that makes one
safe elsewhere.  `confirm.py` gates it accordingly.

What phish.net does give that jerrybase does not is `trans_mark`, the segue
notation, and flags for jams and soundchecks.  Those explain a count mismatch in
the report; they are not used to force one to fit.
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from .httpcache import FetchError, HttpCache, OfflineMiss, age_for
from .naming import state_code

BASE = "https://api.phish.net/v5/"

class MissingKey(Exception):
    """No API key, so phish.net cannot be asked.  Said out loud, never guessed around."""


def read_key(path: Path | str) -> str:
    p = Path(path)
    if not p.exists():
        raise MissingKey("no phish.net key at %s" % p)
    key = p.read_text(encoding="utf-8").strip()
    if not key:
        raise MissingKey("the phish.net key file %s is empty" % p)
    return key


@dataclass
class Song:
    position: int | None
    title: str
    set_name: str | None = None
    segue: bool = False
    is_jam: bool = False


@dataclass
class Show:
    date: str | None = None
    artist: str | None = None
    venue: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    songs: list[Song] = field(default_factory=list)

    @property
    def titles(self) -> list[str]:
        return [s.title for s in self.songs]

    @property
    def set_sizes(self) -> list[int]:
        sizes: dict = {}
        for s in self.songs:
            sizes.setdefault(s.set_name or "?", 0)
            sizes[s.set_name or "?"] += 1
        return list(sizes.values())


def _clean(text) -> str | None:
    """phish.net uses typographic quotes - The “E” Center - which are fine in a
    tag but not in a Windows folder name, so they are normalised here rather
    than left for the namer to strip into nothing."""
    if text is None:
        return None
    out = str(text)
    for bad, good in (("“", '"'), ("”", '"'), ("‘", "'"),
                      ("’", "'"), ("–", "-"), ("—", "-")):
        out = out.replace(bad, good)
    out = " ".join(out.split())
    return out or None


def cache_key(path: str, params: dict) -> str:
    """The URL as it goes into the cache: everything except the key."""
    safe = {k: v for k, v in sorted(params.items()) if k != "apikey"}
    return BASE + path + ("?" + urllib.parse.urlencode(safe) if safe else "")


def _get(cache: HttpCache, key: str | None, path: str, max_age=None, **params):
    url = cache_key(path, params)
    fetch = None
    if key:
        with_key = dict(params, apikey=key)
        fetch = BASE + path + "?" + urllib.parse.urlencode(with_key)
    try:
        resp = cache.get(url, fetch_url=fetch, max_age_days=max_age)
    except (FetchError, OfflineMiss):
        return None
    if resp.status == 404 or not resp.body:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    if payload.get("error"):
        return None
    return payload.get("data") or []


def show_for(cache: HttpCache, key: str | None, date: str) -> Show | None:
    """The show on one date, with its setlist, or None.

    Two calls: `shows` carries the venue and `setlists` carries the songs. The
    setlist rows repeat the venue, so the second alone would nearly do - but a
    show with no setlist entered still has a venue worth having.
    """
    max_age = age_for(date)
    shows = _get(cache, key, "shows/showdate/%s.json" % date, max_age=max_age)
    if shows is None:
        return None
    rows = [r for r in shows if (r.get("artist_name") or "").strip().lower() == "phish"]
    if not rows and shows:
        rows = shows
    if not rows:
        return None
    head = rows[0]
    show = Show(date=_clean(head.get("showdate")), artist=_clean(head.get("artist_name")),
                venue=_clean(head.get("venue")), city=_clean(head.get("city")),
                # phish.net spells a province out - "British Columbia" - where the
                # rest of the library says BC.
                state=state_code(_clean(head.get("state"))),
                country=_clean(head.get("country")))

    setlist = _get(cache, key, "setlists/showdate/%s.json" % date,
                   max_age=max_age) or []
    for r in setlist:
        if (r.get("artist_name") or "").strip().lower() not in ("phish", ""):
            continue
        title = _clean(r.get("song"))
        if not title:
            continue
        try:
            pos = int(r.get("position"))
        except (TypeError, ValueError):
            pos = None
        show.songs.append(Song(
            position=pos,
            title=title,
            set_name=_clean(r.get("set")),
            segue=">" in (r.get("trans_mark") or ""),
            is_jam=str(r.get("isjam") or "") in ("1", "true", "True"),
        ))
    show.songs.sort(key=lambda s: (s.position is None, s.position or 0))
    if not show.venue and setlist:
        show.venue = _clean(setlist[0].get("venue"))
        show.city = show.city or _clean(setlist[0].get("city"))
        show.state = show.state or state_code(_clean(setlist[0].get("state")))
    return show
