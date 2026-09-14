"""What phish.in knows about a Phish show - including how long each track is.

This is the source that puts Phish back on a proper footing.

archive.org does not carry Phish, and phish.net - the reference work for the
setlists - has a `tracktime` column that is empty on every row.  So until now a
Phish title rested on the track count agreeing exactly, which is the fragile
rule: one tuning track or one segue shifts every song after it by one.

phish.in has per-track durations, in milliseconds, and needs no key.  That means
a Phish title can be written on an *alignment* - the same gate that has caught
every wrong match elsewhere - rather than on a count.  So this source is asked
before phish.net, and phish.net remains the fallback for anything phish.in has
no audio for.

A show with no audio still has a venue worth having, and `audio_status` says so.
Its tracks are listed with zero or missing durations, which would align against
nothing, so they are dropped rather than fed to the matcher as if real.

Nothing here writes to the library.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .httpcache import FetchError, HttpCache, OfflineMiss, age_for
from .naming import state_code

BASE = "https://phish.in/api/v2/"


@dataclass
class Track:
    position: int | None
    title: str
    seconds: float | None = None
    set_name: str | None = None


@dataclass
class Show:
    date: str | None = None
    venue: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    audio_status: str | None = None
    tracks: list[Track] = field(default_factory=list)

    @property
    def has_durations(self) -> bool:
        return bool(self.tracks) and all(t.seconds for t in self.tracks)

    @property
    def titles(self) -> list[str]:
        return [t.title for t in self.tracks]

    @property
    def set_sizes(self) -> list[int]:
        sizes: dict = {}
        for t in self.tracks:
            sizes.setdefault(t.set_name or "?", 0)
            sizes[t.set_name or "?"] += 1
        return list(sizes.values())


def _text(value) -> str | None:
    if value is None:
        return None
    out = " ".join(str(value).split())
    return out or None


def parse_show(payload: dict) -> Show | None:
    if not payload or not payload.get("date"):
        return None
    venue = payload.get("venue") if isinstance(payload.get("venue"), dict) else {}
    show = Show(
        date=_text(payload.get("date")),
        venue=_text(venue.get("name") or payload.get("venue_name")),
        city=_text(venue.get("city")),
        state=state_code(_text(venue.get("state"))),
        country=_text(venue.get("country")),
        audio_status=_text(payload.get("audio_status")),
    )
    for row in payload.get("tracks") or ():
        title = _text(row.get("title"))
        if not title:
            continue
        try:
            pos = int(row.get("position"))
        except (TypeError, ValueError):
            pos = None
        # Milliseconds, and zero means "listed but no audio" rather than a
        # track that lasts no time.  A zero fed to the matcher would align
        # against nothing and drag the whole show's score down with it.
        ms = row.get("duration")
        try:
            seconds = float(ms) / 1000.0 if ms else None
        except (TypeError, ValueError):
            seconds = None
        show.tracks.append(Track(position=pos, title=title, seconds=seconds,
                                 set_name=_text(row.get("set_name"))))
    show.tracks.sort(key=lambda t: (t.position is None, t.position or 0))
    return show


def show_for(cache: HttpCache, date: str) -> Show | None:
    try:
        resp = cache.get(BASE + "shows/%s" % date, max_age_days=age_for(date))
    except (FetchError, OfflineMiss):
        return None
    if resp.status == 404 or not resp.body:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    show = parse_show(payload)
    if show is not None and show.date and show.date != date:
        return None
    return show
