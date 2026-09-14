"""What archive.org knows about a show, in this project's own vocabulary.

The etree collection is where this naming convention came from, so an item
identifier reads like one of our folder names already:

    gd1977-05-08.sbd.cantor.sacks.266.shnf

and its metadata carries exactly the fields phase 3 exists to fill:

    venue       Barton Hall - Cornell University
    coverage    Ithaca, NY
    taper       Betty Cantor
    transferer  Darrin Sacks

Two facts shape every query here, both measured rather than assumed:

* **The date has to anchor the search.**  `identifier:tab*` returns 12,433 items
  including unrelated hashes, and `identifier:abb*` returns ABBA.
  `identifier:tab2001-07-20*` returns the show.
* **The separator varies.**  `gd1977-05-08` has none, `STS9-1999-10-15` has a
  dash.  Both joins are tried.

No API key is needed and none is used.  Nothing here writes to the library.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

from .httpcache import FetchError, HttpCache, OfflineMiss, age_for
from .naming import STATE_NAMES, state_code

SEARCH = "https://archive.org/advancedsearch.php"
METADATA = "https://archive.org/metadata/"

# Items that are not a recording of a show.  These really are in the results:
# "skb2003-02-15.sample_rate_test" sits beside the real recordings of that night.
_NOT_A_SHOW = re.compile(
    r"(?:sample[_-]?rate|test|artwork|cover|poster|photos?|torrent|readme)", re.I)

@dataclass
class Track:
    number: int | None
    title: str
    segue: bool = False          # the title ended with "->" or ">"
    seconds: float | None = None


def parse_length(value) -> float | None:
    """archive.org writes a length as "06:13" or as "184.88", and they disagree.

    For one Cornell track the MP3 derivative says 06:13 and the Shorten original
    says 184.88 - the same track, times that are not the same. The derivative is
    the one archive.org computes itself, so mm:ss is preferred and a bare number
    is only used when nothing better is offered.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        seconds = 0.0
        for n in nums:
            seconds = seconds * 60.0 + n
        return seconds
    try:
        return float(text)
    except ValueError:
        return None


@dataclass
class Recording:
    """One archive.org item, reduced to what we can use."""

    identifier: str
    date: str | None = None
    venue: str | None = None
    city: str | None = None
    state: str | None = None
    creator: str | None = None
    taper: str | None = None
    transferer: str | None = None
    lineage: str | None = None
    source: str | None = None
    collections: tuple[str, ...] = ()
    tracks: list[Track] = field(default_factory=list)

    @property
    def shnid(self) -> str | None:
        """The etree catalogue number, if the identifier carries one."""
        m = re.search(r"\.(\d{3,6})(?:\.|$)", self.identifier)
        return m.group(1) if m else None

    @property
    def is_show(self) -> bool:
        return not _NOT_A_SHOW.search(self.identifier)


# "Ithaca, NY" - but also "Ithaca, New York", "London, England" and bare cities.
_COVERAGE = re.compile(r"^\s*(?P<city>[^,]+?)\s*,\s*(?P<rest>[A-Za-z .]+?)\s*$")


def split_coverage(text: str | None) -> tuple[str | None, str | None]:
    """archive.org's `coverage` is "City, ST" far more often than not."""
    if not text or not text.strip():
        return None, None
    m = _COVERAGE.match(text.strip())
    if not m:
        # No comma at all: "Columbus Ohio" is still a city and a state, and
        # leaving it whole put "Columbus Ohio" in the city with no state beside
        # it.  Only a trailing word this list knows is taken, so a city whose
        # own name ends in a word like "Washington" is not carved up.
        bare = text.strip()
        for words in (2, 1):
            parts = bare.split()
            if len(parts) > words:
                tail = " ".join(parts[-words:])
                code = STATE_NAMES.get(tail.lower())
                if code:
                    return " ".join(parts[:-words]).strip(" ,") or None, code
        return bare or None, None
    city = m.group("city").strip()
    rest = state_code(m.group("rest"))
    return (city or None), (rest or None)


def _first(value):
    """archive.org returns a bare string or a list, depending on the field."""
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


_SEGUE = re.compile(r"\s*(?:->|>|&gt;)\s*$")


def parse_metadata(payload: dict, identifier: str) -> Recording:
    """Turn one /metadata/<id> response into a Recording."""
    meta = payload.get("metadata") or {}
    city, state = split_coverage(_first(meta.get("coverage")))
    collections = meta.get("collection") or ()
    if isinstance(collections, str):
        collections = (collections,)
    rec = Recording(
        identifier=identifier,
        date=(_first(meta.get("date")) or "")[:10] or None,
        venue=_first(meta.get("venue")),
        city=city,
        state=state,
        creator=_first(meta.get("creator")),
        taper=_first(meta.get("taper")),
        transferer=_first(meta.get("transferer")),
        lineage=_first(meta.get("lineage")),
        source=_first(meta.get("source")),
        collections=tuple(collections),
    )
    # One song can appear several times over - as FLAC, as VBR MP3, as a
    # derivative - so titles are taken once per track number, in file order.
    seen: dict = {}
    for f in payload.get("files") or ():
        title = (f.get("title") or "").strip()
        if not title:
            continue
        num = f.get("track")
        try:
            num = int(str(num).split("/")[0])
        except (TypeError, ValueError):
            num = None
        key = (num, title.lower())
        seconds = parse_length(f.get("length"))
        colon_form = ":" in str(f.get("length") or "")
        if key in seen:
            # The same song again in another format.  Take its length only if
            # this one is the derivative's mm:ss and what we have is not.
            track, had_colon = seen[key]
            if seconds is not None and colon_form and not had_colon:
                track.seconds = seconds
                seen[key] = (track, True)
            continue
        track = Track(number=num,
                      title=_SEGUE.sub("", title).strip(),
                      segue=bool(_SEGUE.search(title)),
                      seconds=seconds)
        seen[key] = (track, colon_form)
        rec.tracks.append(track)
    rec.tracks.sort(key=lambda t: (t.number is None, t.number or 0))
    return rec


def search_identifiers(cache: HttpCache, prefixes, date: str, rows: int = 40) -> list[str]:
    """Identifiers whose name starts with a band prefix and this date.

    The date is what makes this precise; see the module docstring for what a
    bare prefix returns.  Both joins are tried because the separator varies.
    """
    seen: list[str] = []
    for pfx in prefixes:
        for joiner in ("", "-"):
            q = "identifier:%s%s%s*" % (pfx, joiner, date)
            url = SEARCH + "?" + urllib.parse.urlencode(
                {"q": q, "fl[]": "identifier", "rows": str(rows), "output": "json"})
            try:
                payload = cache.get(url, max_age_days=age_for(date)).json()
            except (FetchError, OfflineMiss, ValueError):
                continue
            for doc in (payload.get("response") or {}).get("docs") or ():
                ident = doc.get("identifier")
                if ident and ident not in seen:
                    seen.append(ident)
    return seen


def fetch_recording(cache: HttpCache, identifier: str,
                    max_age: float | None = None) -> Recording | None:
    """Full metadata for one item, or None if it is gone or unreadable."""
    try:
        resp = cache.get(METADATA + urllib.parse.quote(identifier),
                         max_age_days=max_age)
    except (FetchError, OfflineMiss):
        return None
    if resp.status == 404 or not resp.body:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    if not payload.get("metadata"):
        return None
    return parse_metadata(payload, identifier)


def recordings_for(cache: HttpCache, prefixes, date: str) -> list[Recording]:
    """Every usable recording archive.org has of one band on one date.

    Filtered two ways: an item that is not a show at all (a sample-rate test
    sits beside the real recordings of one Kimock night), and an item whose own
    metadata disagrees with the date we searched for, which happens when a
    prefix match is a coincidence.
    """
    out: list[Recording] = []
    for ident in search_identifiers(cache, prefixes, date):
        rec = fetch_recording(cache, ident, max_age=age_for(date))
        if rec is None or not rec.is_show:
            continue
        if rec.date and rec.date != date:
            continue
        out.append(rec)
    return out
