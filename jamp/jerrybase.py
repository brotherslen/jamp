"""What jerrybase.com knows about a Jerry Garcia show.

archive.org does not carry the Garcia material - the estate had it restricted -
so eight of Jerry's folders came back from phase 3 with nothing at all.
jerrybase is the reference work for that catalogue, needs no key, and addresses
a show as `/events/YYYYMMDD-NN`.

**The suffix matters.**  1987-10-31 at the Lunt-Fontanne is FOUR events - early
and late, each played twice over as an acoustic set and an electric one.
Picking the wrong one is not a near miss, it is a different performance, so both
the act and the marker in our folder name are matched against theirs before
anything is taken.

**There are no durations here**, and that is the important difference from
`archiveorg.py`.  Every "duration" and "length" string on the page is
JavaScript.  So the duration alignment in `setlist.py` - which is what makes a
title safe to write - cannot be used, and titles from this source rest on the
track count agreeing exactly.  That is a far weaker claim, and `confirm.py`
gates it accordingly: a venue is taken freely, a title only on an exact match.

Nothing here writes to the library.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .httpcache import FetchError, HttpCache, OfflineMiss, age_for

BASE = "https://jerrybase.com/events/"

# "Early", "Late", "Acoustic" - the words jerrybase uses to tell two shows on
# one night apart.  Ours are in `confirm._MARKER`; these are matched to those.
_MARKER = re.compile(r"(?<![A-Za-z])(early|late|acoustic|electric)(?![A-Za-z])", re.I)


@dataclass
class Event:
    slug: str
    date: str | None = None
    act: str | None = None
    marker: str | None = None
    venue: str | None = None
    city: str | None = None
    state: str | None = None
    tags: tuple[str, ...] = ()
    sets: list[tuple[str, list[str]]] = field(default_factory=list)

    @property
    def songs(self) -> list[str]:
        """Every song in order, set boundaries flattened away."""
        return [s for _, songs in self.sets for s in songs]

    @property
    def set_sizes(self) -> list[int]:
        return [len(songs) for _, songs in self.sets]


def _strip(fragment: str) -> str:
    """Tags out, entities in.

    Song titles are full of apostrophes - "That's What Love Will Make You Do" -
    and they arrive as &#39;.  Writing that into a TITLE tag would put the
    entity itself in the file, which is worse than no title at all because it
    looks deliberate.
    """
    import html as _html

    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def parse_event(html: str, slug: str) -> Event | None:
    """One /events/ page.  Returns None if it is not an event page at all."""
    heads = re.findall(r"<h4[^>]*>(.*?)</h4>", html, re.S)
    if len(heads) < 2:
        return None
    ev = Event(slug=slug)

    # First heading: <strong>Act</strong> then "1987-10-31 [Sat]  Early"
    act = re.search(r"<strong>(.*?)</strong>", heads[0], re.S)
    ev.act = _strip(act.group(1)) if act else None
    head_text = _strip(heads[0])
    date = re.search(r"(\d{4})\s*-\s*(\d{2})\s*-\s*(\d{2})", head_text)
    if date:
        ev.date = "%s-%s-%s" % date.groups()
    tail = head_text[date.end():] if date else head_text
    marker = _MARKER.search(tail)
    # The act itself can name the marker - "Jerry Garcia Acoustic Band" - and
    # that is just as good a statement of which performance this is.
    if not marker and ev.act:
        marker = _MARKER.search(ev.act)
    ev.marker = marker.group(1).lower() if marker else None

    # Second heading: <a href="/venues/N">Venue</a>, <a href="...">City, ST</a>
    venue = re.search(r'href="/venues/\d+"[^>]*>(.*?)</a>', heads[1], re.S)
    ev.venue = _strip(venue.group(1)) if venue else None
    place = re.search(r'href="[^"]*city=[^"]*"[^>]*>(.*?)</a>', heads[1], re.S)
    if place:
        text = _strip(place.group(1))
        if "," in text:
            city, _, state = text.rpartition(",")
            ev.city, ev.state = city.strip() or None, state.strip() or None
        else:
            ev.city = text or None
    if not ev.venue and not ev.city:
        # No venue block at all - a cancelled or placeholder entry.
        rest = _strip(heads[1])
        if rest:
            ev.venue = rest

    tags = re.search(r'id="event_tags"[^>]*>(.*?)</div>\s*</div>', html, re.S)
    if tags:
        ev.tags = tuple(_strip(t).lower() for t in
                        re.findall(r'<span class="badge[^"]*">(.*?)</span>', tags.group(1), re.S))

    for block in re.findall(r'<div class="stacked-field">(.*?)</div>', html, re.S):
        name = re.search(r"<strong>(.*?)</strong>", block, re.S)
        songs = [_strip(s) for s in
                 re.findall(r'href="/songs/\d+"[^>]*>(.*?)</a>', block, re.S)]
        songs = [s for s in songs if s]
        if songs:
            ev.sets.append((_strip(name.group(1)) if name else "Set", songs))
    return ev


def fetch_event(cache: HttpCache, date_compact: str, n: str) -> Event | None:
    slug = "%s-%s" % (date_compact, n)
    try:
        resp = cache.get(BASE + slug, max_age_days=age_for(date_compact))
    except (FetchError, OfflineMiss):
        return None
    if resp.status == 404 or not resp.body:
        return None
    return parse_event(resp.body.decode("utf-8", "replace"), slug)


def events_for(cache: HttpCache, date: str, limit: int = 6) -> list[Event]:
    """Every event jerrybase lists for one date, in its own order.

    Stops at the first suffix that is not there.  A date with an early and a
    late show has -01 and -02; a gap would mean a numbering we do not
    understand, and guessing past it would invent shows.
    """
    compact = date.replace("-", "")
    out: list[Event] = []
    for i in range(1, limit + 1):
        ev = fetch_event(cache, compact, "%02d" % i)
        if ev is None:
            break
        if ev.date and ev.date != date:
            break
        out.append(ev)
    return out


def act_matches(band_names, act: str | None) -> bool:
    """Is this jerrybase act the band our folder says it is?

    Compared on words rather than exactly: jerrybase writes "Jerry Garcia and
    David Grisman" where the config says "Jerry Garcia & David Grisman", and
    "Jerry Garcia Acoustic Band" is the Garcia Band for our purposes but must
    still not be confused with the Grisman duo.
    """
    if not act:
        return False
    def words(text):
        return {w for w in re.split(r"[^a-z0-9]+", text.lower()) if w and w != "and"}
    theirs = words(act)
    for name in band_names:
        ours = words(name)
        if ours and ours <= theirs:
            return True
    return False
