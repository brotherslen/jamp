"""Building the target folder and track names, and checking they are legal.

    <band><YYYY-MM-DD>[.<source>][.<provenance>][.<format>]
    <band><YYYY-MM-DD>[s|d]<n>t<nn>.<ext>

A field that is not known is left out.  "unknown" is never written, and any
component that reduces to a placeholder is dropped with a warning.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path

VALID_SOURCES = ("sbd", "aud", "mtx")
# m4a and wma each earn a token.  Without one a lossy show was named as though
# it had no format at all - thirteen King Gizzard shows at 319 kbps would have
# read exactly like a lossless copy of the same night.  wma had a second cost:
# with no token, canon.fmt stayed None, so a WMA folder could never be
# recognised as one this pipeline wrote and was offered for work on every
# single run, for ever.
VALID_FORMATS = ("flac16", "flac24", "mp3", "m4a", "wma", "shn")

PLACEHOLDERS = {"unknown", "unk", "none", "na", "n/a", "null", "misc", "various", "tbd"}

_WINDOWS_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *("com%d" % i for i in range(1, 10)),
    *("lpt%d" % i for i in range(1, 10)),
}

# The band part accepts exactly what the builders emit: slug(band, 8), which
# keeps digits.  It used to be [a-z]{1,6}, so "sts92012-01-21.sbd.flac16" never
# parsed - every guard that asks "did we write this name?" answered no for
# STS9, and a settled folder re-derived its classification from tags our own
# commit had rewritten.  Lazy, so "sts9" + "2012-01-21" is found rather than a
# band swallowing the year's first digit; the date's dashes leave one reading.
_BAND = r"(?P<band>[a-z][a-z0-9]{0,7}?)"

_CANONICAL = re.compile(
    r"^" + _BAND + r"(?P<date>\d{4}-\d{2}-\d{2})(?P<rest>(?:\.[A-Za-z0-9]+)*)$"
)

_CANONICAL_TRACK = re.compile(
    r"^" + _BAND + r"(?P<date>\d{4}-\d{2}-\d{2})(?P<marker>early|late)?"
    r"(?P<kind>[sd])(?P<n>\d{1,2})t(?P<track>\d{2,3})$"
)


@dataclass
class NameProposal:
    name: str
    warnings: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


@dataclass
class ParsedName:
    band: str
    date: _dt.date
    source: str | None = None
    provenance: str | None = None
    fmt: str | None = None
    marker: str | None = None
    location: str | None = None


def slug(value: str | None, limit: int = 24) -> str | None:
    """Lower-case alphanumerics only.  Placeholders become None."""
    if value is None:
        return None
    out = re.sub(r"[^a-z0-9]", "", str(value).lower())
    if not out or out in PLACEHOLDERS:
        return None
    return out[:limit]


SHOW_MARKERS = ("early", "late")

# The folder name is machine part + " - " + human part:
#
#     gd1977-05-08.sbd.miller.flac16 - Barton Hall, Ithaca, NY
#
# Everything before the separator is the strict scheme and is what the pipeline
# parses, compares and de-duplicates on.  Everything after it is for reading in
# Explorer.  Track filenames never carry it.
LOCATION_SEP = " - "

# "May 1, 2010" - a date spelled out, which find_dates does not look for.
_WRITTEN_DATE = re.compile(
    r",?\s*\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"\d{1,2}(?:st|nd|rd|th)?\s*,?\s*(?:19|20)\d{2}\b",
    re.I,
)


# Brackets left holding nothing once the date inside them is gone.
_EMPTY_BRACKETS = re.compile(r"[\(\[\{]\s*[\)\]\}]")


def strip_dates_from_place(text: str) -> str:
    """Remove any date from a place string, numeric or spelled out.

    A venue read out of an info file or a tag routinely carries the show date -
    "Merriweather Post Pavilion - May 1, 2010 - Columbia, MD".  make_location
    strips it on the way into a folder name, but the ALBUM tag is built from
    the venue directly, so the date has to come off the value itself or it ends
    up written twice: once as the album's date and once inside its place.
    """
    from .dates import find_dates

    if not text:
        return text
    for candidate in reversed(find_dates(text)):
        start, end = candidate.span
        text = text[:start] + " " + text[end:]
    text = _WRITTEN_DATE.sub(" ", text)
    text = strip_bracketed_source(text) or text
    text = strip_audio_spec(text) or text
    # Taking the date out of "Garcia Live Vol. 8 (11-23-91)" leaves the brackets
    # standing empty, and that "( )" was going into the folder name and the
    # ALBUM tag both - then being read back next run, which is how a name grows
    # punctuation it was never given.
    text = _EMPTY_BRACKETS.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+,", ",", text)
    return re.sub(r"(,\s*)+", ", ", text).strip(" ,.-")


# A tag that ends by naming the lineage - "Mr. Small's Theater sbd" - is telling
# us the source, not the name of the room.  Ten Disco Biscuits shows bought from
# nugs.net were heading for folder names ending "sbd" this way.  Only a whole
# trailing word goes, and only when something survives it, so "The Board Room"
# keeps its name and a venue actually called "Soundboard" is not erased.
_TRAILING_SOURCE = re.compile(
    r"\s+(?:sbd|aud|mtx|dsbd|fm|soundboard|audience|matrix|dat|cdr)\s*$", re.I)


def strip_trailing_source(text: str | None) -> str | None:
    if not text:
        return text
    out = _TRAILING_SOURCE.sub("", text).strip(" -")
    return out or text


# A source in brackets is unambiguous - "(SBD) Zydeco" and "Zydeco (SBD)" both
# say the lineage, never the name of the room - so unlike a bare trailing word
# it can be taken out wherever it sits.  Only bracketed, which is why the
# Matrix in San Francisco and a hall called The Board Room keep their names.
_BRACKETED_SOURCE = re.compile(
    r"[\(\[\{]\s*(?:sbd|aud|mtx|dsbd|fm|soundboard|audience|matrix|dat|cdr"
    r"|pre[\s-]?fm|alt[\s-]?sbd)\s*[\)\]\}]", re.I)


# A bracket holding a bit depth and a sample rate describes the file, not the
# room: a nugs album read "KSU MAC Center (16/44.1)", and because "/" cannot
# survive in a Windows folder name the venue came out "KSU MAC Center (1644.1)".
# The mangled form is matched too, so a name already carrying one is cleaned up.
# A separator, a decimal point or a unit must be present, so a bracketed year
# like "(1985)" is not mistaken for one.  Dates are stripped from a place
# elsewhere; this is only about audio specifications.
_AUDIO_SPEC = re.compile(
    r"[\(\[\{]\s*\d{1,2}\s*(?:bit)?\s*(?:"
    r"[-/]\s*\d{2,3}(?:\.\d)?"          # 16/44.1, 24-96
    r"|\d{2}\.\d"                        # 1644.1, the "/" already lost
    r"|\s*(?:khz|hz|kbps|bit)"           # 96kHz, 24bit
    r")\s*(?:k|khz|hz|kbps|bit)?\s*[\)\]\}]", re.I)


def strip_audio_spec(text: str | None) -> str | None:
    """Drop a bracketed "(16/44.1)" or "(24bit/96kHz)" from a place string."""
    if not text:
        return text
    out = _AUDIO_SPEC.sub(" ", text)
    out = re.sub(r"\s{2,}", " ", out).strip(" -,")
    return out or text


# Sources write a state however the person entering it felt like: archive.org
# gives "Ky" and "Ga.", phish.net gives "British Columbia".  This library
# spells a state or province as its code, and a name that disagrees with every
# other folder is worse than one that is plainly foreign, so these are
# translated.  Originally written for coverage strings; it applies to any
# source, which is why it lives here rather than beside one of them.
# (was: so a state arrives spelled
# out as often as coded: "Chicago Heights, Illinois", "San Luis Obispo,
# California".  This library spells a US state or Canadian province as its code,
# and a name that disagrees with every other folder is worse than one that is
# plainly foreign, so these are translated.  Anywhere not on this list - England,
# Japan - is left exactly as written, because it is not a code and pretending
# otherwise would invent one.
STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "washington dc": "DC", "washington d.c.": "DC",
    "ontario": "ON", "quebec": "QC", "british columbia": "BC", "alberta": "AB",
    "manitoba": "MB", "saskatchewan": "SK", "nova scotia": "NS",
    "new brunswick": "NB", "newfoundland": "NL",
}


def state_code(text: str | None) -> str | None:
    """A state as this library spells it, or the text unchanged."""
    if not text:
        return None
    cleaned = text.strip().rstrip(".").strip()
    if len(cleaned) == 2 and cleaned.isalpha():
        return cleaned.upper()
    return STATE_NAMES.get(cleaned.lower(), cleaned) or None


def strip_bracketed_source(text: str | None) -> str | None:
    if not text:
        return text
    out = _BRACKETED_SOURCE.sub(" ", text)
    out = re.sub(r"\s{2,}", " ", out).strip(" -,")
    return out or text


def strip_set_marker(text: str) -> str:
    """Drop a leading set marker from a place string.

    Stores abbreviate sets as roman numerals in the album tag - "I Atlanta, GA"
    is set one in Atlanta, not a venue called "I".
    """
    out = re.sub(r"^\s*(?:set|disc|disk|cd)\s*[ivx\d]+\b[\s.,:-]*", "", text, flags=re.I)
    out = re.sub(r"^\s*(?:[IVX]{1,4})\b[\s.,:-]*", "", out)
    return out.strip()


def dedupe_place_parts(venue=None, city=None, state=None) -> list[str]:
    """Drop a part already contained in an earlier one.

    A venue is often written "Waterfront Park, Louisville" and the city then
    repeats it.  Used by both the folder name and the ALBUM tag, so a venue read
    back out of a previous ALBUM is never joined onto its own city again.
    """
    parts: list[str] = []
    seen = ""
    for part in (venue, city, state):
        if not part or not part.strip():
            continue
        cleaned = part.strip(" ,-")
        flat = re.sub(r"[^a-z0-9]", "", cleaned.lower())
        if not flat:
            continue
        if len(flat) > 4:
            duplicate = flat in seen
        else:
            # A state code is a substring of half the words in English
            # ("IN" inside "Indianapolis"), so short parts match on words only.
            duplicate = any(
                re.search(r"\b%s\b" % re.escape(cleaned.lower()), kept.lower())
                for kept in parts
            )
        if duplicate:
            continue
        seen += flat
        parts.append(cleaned)
    return parts


def _strip_dates_from(text: str) -> str:
    """Take every date out of a place.

    The date is already the front of the folder name, so "Oakland 10-31-91"
    only says it twice.
    """
    from .dates import find_dates

    for candidate in reversed(find_dates(text)):
        start, end = candidate.span
        text = text[:start] + " " + text[end:]
    return text


def make_location(
    venue: str | None = None,
    city: str | None = None,
    state: str | None = None,
    limit: int = 60,
) -> str | None:
    """"Barton Hall, Ithaca, NY", Windows-safe, or None."""
    parts = dedupe_place_parts(venue, city, state)
    if not parts:
        return None

    text = ", ".join(parts)

    # Dates come out BEFORE the Windows-illegal characters do, and the order is
    # the whole point.  "/" is illegal in a filename and was being removed
    # first, which turned the 8/16/1996 inside a store's ALBUM tag into the
    # digit run 8161996 - no longer a date to anything downstream, and so
    # carried into the folder name and the VENUE tag as though it were part of
    # the venue.  Two folders library-wide were affected: the Clifford Ball,
    # and "Red Rocks Amphitheatre, Morrison, CO 7878", where 7/8/78 collapsed
    # to four digits and was even harder to spot.
    text = _strip_dates_from(text)
    text = _WINDOWS_ILLEGAL.sub("", text)
    # A second pass, for a date that only became one once the separators went:
    # some tags write 8.16.1996, and "." survives as a legal character.
    text = _strip_dates_from(text)

    text = strip_set_marker(text)
    text = strip_bracketed_source(text) or text
    text = strip_audio_spec(text) or text

    # Some stores write the city first: "Commerce City, CO - Dicks Sporting
    # Goods Park".  Put the venue back in front so every folder reads the same.
    swapped = re.match(r"^(?P<city>.+?,\s*[A-Z]{2})\s*[-–]\s*(?P<venue>.+)$", text)
    if swapped:
        text = "%s, %s" % (swapped.group("venue").strip(), swapped.group("city").strip())
    # A third pass, in the position the only pass used to occupy.  The strippers
    # above can uncover a date that was not visible before them - taking the
    # brackets off "(10-31-91 SBD)" leaves one standing in the open - so this
    # stays where it was.  Three passes make the change purely additive: a date
    # that used to be removed still is, and two that used to survive no longer
    # do.
    text = _strip_dates_from(text)
    # ...including one written out: "Merriweather Post Pavilion, May 1, 2010".
    text = _WRITTEN_DATE.sub(" ", text)
    # make_location strips dates itself rather than calling
    # strip_dates_from_place, so it needs the same tidy-up: taking the date out
    # of "Garcia Live Vol. 8 (11-23-91)" leaves the brackets standing empty.
    text = _EMPTY_BRACKETS.sub(" ", text)
    # An official ALBUM often reads "Eagles Ballroom - Milwaukee, WI"; keep the
    # separator meaning one thing only.
    text = text.replace(LOCATION_SEP, ", ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r"(,\s*)+", ", ", text).strip(" ,.-")
    if len(text) > limit:
        # A place that will not fit loses whole pieces, never half a word.
        # A venue's parenthetical alias is the part worth giving up: it is the
        # same hall said twice, while the city and state that follow it are
        # short and are what actually identify the show.
        #
        # Cutting characters instead produced "Cynthia Mitchell Woods Pavilion
        # (Woodlands Center for the" - an unbalanced bracket, no city, and two
        # different recordings of one night sharing a folder name.
        without_aside = re.sub(r"\s*\([^)]*\)", "", text)
        without_aside = re.sub(r"\s{2,}", " ", without_aside)
        without_aside = re.sub(r"(,\s*)+", ", ", without_aside).strip(" ,.-")
        if without_aside and len(without_aside) <= limit:
            text = without_aside
        else:
            text = (without_aside or text)[:limit].rsplit(" ", 1)[0].strip(" ,.-")
            # Never end on a bracket that was never closed.
            if text.count("(") > text.count(")"):
                text = text[:text.rindex("(")].strip(" ,.-")
    return text or None


def split_location(folder_name: str) -> tuple[str, str | None]:
    """Split a folder name into (machine part, location) at the first ' - '."""
    machine, sep, human = folder_name.partition(LOCATION_SEP)
    return (machine, human or None) if sep else (folder_name, None)


def build_folder_name(
    band: str,
    date: _dt.date,
    source: str | None = None,
    provenance: str | None = None,
    fmt: str | None = None,
    marker: str | None = None,
    location: str | None = None,
) -> NameProposal:
    warnings: list[str] = []
    dropped: list[str] = []
    parts = ["%s%s" % (slug(band, 8) or "", date.isoformat())]

    # Two performances on one date: the marker goes straight after the date.
    if marker:
        if marker in SHOW_MARKERS:
            parts.append(marker)
        else:
            dropped.append("marker=%r is not one of %s" % (marker, SHOW_MARKERS))

    if source:
        if source in VALID_SOURCES:
            parts.append(source)
        else:
            dropped.append("source=%r is not one of %s" % (source, VALID_SOURCES))

    prov = slug(provenance)
    if provenance and not prov:
        dropped.append("provenance=%r reduced to a placeholder" % provenance)
    elif prov:
        parts.append(prov)

    if fmt:
        if fmt in VALID_FORMATS:
            parts.append(fmt)
        else:
            dropped.append("format=%r is not one of %s" % (fmt, VALID_FORMATS))

    name = ".".join(parts)
    if location:
        cleaned = make_location(location)
        if cleaned:
            name = name + LOCATION_SEP + cleaned
    warnings.extend(check_windows_name(name))
    return NameProposal(name=name, warnings=warnings, dropped=dropped)


def build_release_show_name(
    date: _dt.date,
    venue: str | None = None,
    city: str | None = None,
    state: str | None = None,
    marker: str | None = None,
    limit: int = 80,
) -> NameProposal:
    """A show folder inside an official release: "1990-03-21 Copps Coliseum, Hamilton, ON".

    Inside a named release the band, the source and the release itself are all
    settled by the parent folder, so the child reads as a date and a place and
    nothing else.  The date is followed by a space, not a comma - that is the
    form "30 Trips Around The Sun" and "Spring 1990 (The Other One)" already
    use on disk, and matching it means those folders need no rename at all.
    Track filenames keep the normal scheme.
    """
    head = date.isoformat()
    if marker in SHOW_MARKERS and marker:
        head = "%s %s" % (head, marker)
    place = make_location(venue, city, state, limit=limit)
    name = "%s %s" % (head, place) if place else head
    return NameProposal(name=name, warnings=check_windows_name(name))


# The comma after the date is optional so names written by earlier runs still
# parse and are recognised as already-correct rather than renamed again.
_RELEASE_SHOW = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})(?:[,\s]\s*(?P<marker>early|late))?"
    r"(?:[,\s]\s*(?P<place>.+))?$"
)


def parse_release_show_name(name: str) -> ParsedName | None:
    m = _RELEASE_SHOW.match(name.strip())
    if not m:
        return None
    try:
        date = _dt.date.fromisoformat(m.group("date"))
    except ValueError:
        return None
    return ParsedName(band="", date=date, marker=m.group("marker"),
                      location=(m.group("place") or None))


def build_track_name(
    band: str,
    date: _dt.date,
    number: int,
    track: int,
    ext: str,
    kind: str = "d",
    marker: str | None = None,
    suffix: str = "",
) -> NameProposal:
    if kind not in ("s", "d"):
        raise ValueError("kind must be 's' (set) or 'd' (disc), got %r" % kind)
    width = 2 if track < 100 else 3
    # Track zero is material before the show.  Two of them in one folder are
    # told apart only by their letter, so it has to survive: without it both
    # become d1t00 and one would overwrite the other.
    name = "%s%s%s%s%d%s%s" % (
        slug(band, 8) or "", date.isoformat(),
        marker if marker in SHOW_MARKERS else "",
        kind, number, "t" + str(track).zfill(width),
        (suffix or "").lower(),
    )
    ext = ext if ext.startswith(".") else "." + ext
    full = name + ext.lower()
    return NameProposal(name=full, warnings=check_windows_name(full))


def check_windows_name(name: str) -> list[str]:
    problems: list[str] = []
    bad = _WINDOWS_ILLEGAL.findall(name)
    if bad:
        problems.append("illegal Windows characters: %s" % ", ".join(sorted(set(bad))))
    if name != name.rstrip(" ."):
        problems.append("Windows strips trailing spaces and dots")
    if Path(name).stem.lower() in _WINDOWS_RESERVED:
        problems.append("%r is a reserved Windows device name" % name)
    return problems


def check_path_length(path: Path | str, limit: int = 260) -> str | None:
    text = str(path)
    if len(text) > limit:
        return "path is %d characters, over the %d limit: %s" % (len(text), limit, text)
    return None


def parse_canonical(name: str) -> ParsedName | None:
    """Read a name that is already in the scheme, so a second run is a no-op."""
    name, location = split_location(name)
    m = _CANONICAL.match(name)
    if not m:
        return None
    try:
        date = _dt.date.fromisoformat(m.group("date"))
    except ValueError:
        return None
    rest = [p for p in m.group("rest").split(".") if p]
    source = provenance = fmt = marker = None
    if rest and rest[0] in SHOW_MARKERS:
        marker = rest.pop(0)
    if rest and rest[0] in VALID_SOURCES:
        source = rest.pop(0)
    if rest and rest[-1] in VALID_FORMATS:
        fmt = rest.pop()
    if rest:
        provenance = rest.pop(0)
    # Our builder emits exactly the fields above and nothing else, so a token
    # still left over means this name was NOT written by us, however much it
    # looks like one: an original etree name whose taper and catalogue number
    # happen to sit where our provenance and format go.  Reading such a name
    # back as ours took the first spare token as the provenance and dropped the
    # remainder on the floor - "um2012-04-13.ccm4v.zman.flac16" kept the
    # microphone and lost the taper, which in a folder with no info file was
    # the only surviving record of who taped it.  Every caller asks this
    # function one question, "did we write this?", so the answer is no.
    if rest:
        return None
    return ParsedName(
        band=m.group("band"), date=date, source=source,
        provenance=provenance, fmt=fmt, marker=marker, location=location,
    )


def parse_canonical_track(stem: str) -> ParsedName | None:
    m = _CANONICAL_TRACK.match(stem)
    if not m:
        return None
    try:
        date = _dt.date.fromisoformat(m.group("date"))
    except ValueError:
        return None
    return ParsedName(band=m.group("band"), date=date)


def build_album_tag(
    date: _dt.date,
    venue: str | None = None,
    city: str | None = None,
    state: str | None = None,
    release: str | None = None,
) -> str:
    """"YYYY-MM-DD: Venue, City, ST", or just the date.

    A place is only written when the venue is actually known.  A half-filled
    "2011-11-05: Milwaukee, WI" looks like a fact and is not one, so the whole
    place falls away and the date stands alone.
    """
    # Deduplicated, but not reformatted: an official venue text is kept exactly
    # as the release shipped it.
    place = ", ".join(dedupe_place_parts(venue, city, state)) if venue and venue.strip() else ""
    album = "%s: %s" % (date.isoformat(), place) if place else date.isoformat()
    if release:
        album = "%s [%s]" % (album, release.strip())
    return album
