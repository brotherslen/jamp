"""What archive.mymorningjacket.net knows about a My Morning Jacket show.

The fourth source, and the first that says where the *gaps* are.

archive.org has durations, so a title there is written on an alignment.
jerrybase and phish.net have neither durations nor any notion of a non-song
gap, so a title from those rests on the track count agreeing exactly - and a
count is fragile, because one tuning track or one encore break shifts every
song after it by one.

This site prints the break.  2005-11-23 lists twenty songs with a `----------`
between the fifteenth and the sixteenth, and our folder holds twenty-one tracks
of which the sixteenth is fifty-six seconds long.  That is the encore break, at
exactly the notated position.  So the count reconciles - 20 songs + 1 break = 21
tracks - and it reconciles for a stated reason rather than by assumption.

Shows are addressed as `/index.php/shows/YYYY/YYYY-MM-DD-venue-slug`, and the
slug cannot be derived from the date alone, so the year index is fetched once
(and cached) to find it.

Nothing here writes to the library.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .httpcache import FetchError, HttpCache, OfflineMiss, age_for
from .naming import state_code

BASE = "https://archive.mymorningjacket.net"
YEAR_INDEX = BASE + "/index.php/shows/%s"

# "15. Anytime" - the whole setlist is numbered, which is what makes the
# breaks between them legible.
_ENTRY = re.compile(r"^\s*(\d{1,3})\s*[.)]\s+(.+?)\s*$")
# A run of dashes on its own line: the site's notation for a gap in the show.
_BREAK = re.compile(r"^\s*[-–—]{3,}\s*$")
# "2005-11-23 The Palace Theatre - Louisville, KY"
_HEADING = re.compile(
    r"^\s*(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<venue>.+?)\s+-\s+"
    r"(?P<city>[^,]+?)\s*,\s*(?P<state>[A-Za-z. ]{2,20})\s*$")

SONG = "song"
BREAK = "break"


@dataclass
class Entry:
    kind: str
    title: str | None = None


@dataclass
class Show:
    url: str
    date: str | None = None
    venue: str | None = None
    city: str | None = None
    state: str | None = None
    entries: list[Entry] = field(default_factory=list)

    @property
    def songs(self) -> list[str]:
        return [e.title for e in self.entries if e.kind == SONG and e.title]

    @property
    def breaks(self) -> int:
        return sum(1 for e in self.entries if e.kind == BREAK)

    def plan_for(self, track_count: int) -> list[Entry] | None:
        """How `track_count` of our tracks line up with this setlist, or None.

        Two shapes are accepted, and nothing else.  Either the taper cut the
        breaks out, in which case our count equals the number of songs; or they
        left them in, in which case it equals songs plus breaks and each break
        is a track that gets no title.  Anything else is a disagreement this
        source cannot resolve, because it has no durations to check against.
        """
        songs = [e for e in self.entries if e.kind == SONG]
        if track_count == len(songs):
            return songs
        if track_count == len(self.entries):
            return list(self.entries)
        return None


def _visible(html: str) -> list[str]:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    import html as _html

    return [l.strip() for l in _html.unescape(text).splitlines() if l.strip()]


def parse_show(html: str, url: str) -> Show | None:
    show = Show(url=url)
    lines = _visible(html)
    for line in lines[:6]:
        m = _HEADING.match(line)
        if m:
            show.date = m.group("date")
            show.venue = m.group("venue").strip()
            show.city = m.group("city").strip()
            show.state = state_code(m.group("state"))
            break

    # The setlist is the run of numbered lines.  Stop at the first numbered line
    # that goes backwards - the page ends with navigation and other shows.
    last = 0
    for line in lines:
        if _BREAK.match(line):
            if show.entries:
                show.entries.append(Entry(BREAK))
            continue
        m = _ENTRY.match(line)
        if not m:
            continue
        n = int(m.group(1))
        if n != last + 1:
            if show.entries:
                break
            continue
        last = n
        show.entries.append(Entry(SONG, m.group(2).strip()))
    # A trailing break belongs to nothing.
    while show.entries and show.entries[-1].kind == BREAK:
        show.entries.pop()
    return show if show.entries else None


def _year_links(cache: HttpCache, year: str) -> dict:
    """{date: url} for every show that year, from the year index."""
    try:
        resp = cache.get(YEAR_INDEX % year)
    except (FetchError, OfflineMiss):
        return {}
    if resp.status == 404 or not resp.body:
        return {}
    html = resp.body.decode("utf-8", "replace")
    out = {}
    for href in re.findall(r'href="([^"]*shows/\d{4}/[^"]+)"', html):
        m = re.search(r"/(\d{4}-\d{2}-\d{2})-", href)
        if not m:
            continue
        url = href if href.startswith("http") else BASE + "/" + href.lstrip("/")
        out.setdefault(m.group(1), url)
    return out


def show_for(cache: HttpCache, date: str) -> Show | None:
    links = _year_links(cache, date[:4])
    url = links.get(date)
    if not url:
        return None
    try:
        resp = cache.get(url, max_age_days=age_for(date))
    except (FetchError, OfflineMiss):
        return None
    if resp.status == 404 or not resp.body:
        return None
    show = parse_show(resp.body.decode("utf-8", "replace"), url)
    if show is not None and show.date and show.date != date:
        return None
    return show
