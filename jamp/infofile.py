"""Finding and reading the one text file that actually describes the show.

Folders routinely contain several .txt files - news clippings, band comments,
a "Torrent downloaded from ..." stub, the seeder's notes.  We score candidates
on filename and on how many taping-vocabulary keywords they contain, pick one,
and report the rest so you can see what was ignored.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .dates import DateCandidate, find_dates
from .textio import safe_read_text

TEXT_EXTS = {".txt", ".nfo", ".info", ".md", ".log"}

TORRENT_MARKER_RE = re.compile(r"torrent\s*downloaded\s*from", re.I)

# Vocabulary that only appears in a real lineage / taping note.
CONTENT_KEYWORDS = {
    "lineage": 6, "taper": 6, "transferred": 5, "recorded by": 5, "source:": 5,
    "sector boundaries": 5, "seeded": 4, "soundboard": 3, "matrix": 3,
    "set 1": 3, "set i": 3, "setlist": 3, "encore": 3, "venue": 3,
    "shn": 2, "flac": 2, "dat": 2, "cassette": 2, "master": 2, "disc 1": 2,
    "sbd": 2, "aud": 2, "mics": 2, "microphones": 2, "patch": 1, "edited": 1,
}

NAME_SCORES = (
    (re.compile(r"^info\.(txt|nfo)$", re.I), 12),
    (re.compile(r"^.*\binfo\b.*$", re.I), 8),
    (re.compile(r"^.*(lineage|source|notes)\b.*$", re.I), 7),
    (re.compile(r"^readme", re.I), 5),
    (re.compile(r"^.*setlist.*$", re.I), 4),
    (re.compile(r"^(ffp|fingerprint)", re.I), -10),
    (re.compile(r"(news|review|article|clipping|comment|billboard|interview)", re.I), -8),
)

# "Filler:", "FILLER (1998-10-30):" - spare tracks from another night.
_FILLER_LINE = re.compile(r"\bfillers?\b", re.I)

FIELD_ALIASES = {
    "source": "source",
    "src": "source",
    "lineage": "lineage",
    "taper": "taper",
    "taped by": "taper",
    "recorded by": "taper",
    "recording by": "taper",
    "transferred by": "transferred_by",
    "transfer": "transferred_by",
    "transferred": "transferred_by",
    "seeded by": "seeded_by",
    "venue": "venue",
    "location": "location",
    "city": "city",
    "date": "date",
    "band": "band",
    "artist": "band",
    "conversion": "conversion",
    "notes": "notes",
    "mics": "source",
    "microphones": "source",
}

# A set or disc number as a taper writes it: a digit, a word, or a numeral.
# Disc headers used to take digits only, so "Disc One (5) 50:14" / "Disc Two"
# were not headers at all: the setlist lost its discs, both ran 1..N with no
# disc number, and disc 2 track 2 could only be found by number - which is how
# jgb1976-04-03's second disc was given the first disc's titles.
_NUMBER_WORD = r"\d+|one|two|three|four|five|six|seven|eight|nine|ten|x|ix|iv|v?i{1,3}"

_SET_HEADER = re.compile(
    r"^\s*(?:(set)\s*(" + _NUMBER_WORD + r")|(encore)\s*(\d*)"
    r"|(?:disc|disk|cd)\s*(" + _NUMBER_WORD + r"))\b[:.\-]?\s*(.*)$",
    re.I,
)

_TRACK_LINE = re.compile(
    r"^\s*(?:[dst](?P<disc>\d{1,2})[t-]?)?"
    # The dot after the number is optional: plenty of setlists are simply
    # "01 Shady Grove".  Whitespace after it is not.
    r"(?P<num>\d{1,2})\s*[.):\-]?\s+"
    r"(?P<title>.{2,120}?)\s*$",
    re.I,
)

_ETREE_TRACK_LINE = re.compile(
    r"^\s*(?P<file>[a-z0-9_.\-]*[ds](?P<disc>\d{1,2})t(?P<num>\d{1,3})[a-z0-9_.\-]*)\s+[-:]?\s*(?P<title>.{2,120}?)\s*$",
    re.I,
)

# Every trailing bracketed time, not just the last: "Don't Let Go [18:27] [0:40]"
# is a length and a gap, and only the gap used to come off.
_DURATION = re.compile(r"(?:\s*[\(\[]\s*\d{1,2}:\d{2}(?::\d{2})?\s*[\)\]])+\s*$")
# ...and the time written in front: "[06:49] Chalk Dust Torture".  Left on, it
# went into TITLE, and once a setlist could be matched by disc it replaced a
# clean existing title with the timestamped one.
_LEADING_DURATION = re.compile(
    r"^\s*(?:[\(\[]\s*\d{1,2}:\d{2}(?::\d{2})?\s*[\)\]]\s*[-–:]?\s*)+")


# Damage a setlist line picks up in transit or from a taper's own markup, none
# of it part of a song's name: a bracket holding a time or a '#' anywhere
# ("[12:#53]", "[#10:43]", "(02:34)") or only a footnote number ("[1]", "(2)"),
# a slash not between letters ("Dear //Prudence", "/Catfish John"), a footnote
# asterisk ("Heavy Metal Jam*->") and a trailing comma ("Sittin' In Limbo,").
# Kept: "(v1)" and "(Moby Dick)", which are part of what the track is;
# "AC/DC Bag"; and "w/" as in "Speech w/ Gloria Steinem".
_BRACKETED_NOISE = re.compile(r"\s*(?:[\[(][^\])]*[:#][^\])]*[\])]|[\[(]\s*\d{1,2}\s*[\])])")
_STRAY_SLASH = re.compile(r"(?<![A-Za-z])/+|/+(?![A-Za-z])")
_ABBREVIATION_SLASH = re.compile(r"(?<![A-Za-z])[A-Za-z]/$")
_FOOTNOTE_STAR = re.compile(r"\*+(?=\s*(?:-*>|$))")


def _unslash(text: str) -> str:
    def repl(m):
        # "w/ Gloria Steinem": a single letter and a slash is an abbreviation.
        if _ABBREVIATION_SLASH.search(text[:m.start()] + "/"):
            return m.group(0)
        return " "                   # "Tuning/"Please" stays two words
    return _STRAY_SLASH.sub(repl, text)


def _clean_title(text: str) -> str:
    text = _LEADING_DURATION.sub("", _DURATION.sub("", text))
    text = _BRACKETED_NOISE.sub("", text)
    text = _unslash(text)
    text = _FOOTNOTE_STAR.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text.rstrip(",").strip()


_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8,
          "ix": 9, "x": 10, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
          "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}

# The set named in the tail of a disc header: "Disc 1 - Set One".
_SET_IN_REMAINDER = re.compile(r"\bset\s*(" + _NUMBER_WORD + r")\b", re.I)


def _header_number(token: str) -> int | None:
    token = token.lower()
    return int(token) if token.isdigit() else _ROMAN.get(token)


# The city may not run across a spaced dash.  The old class allowed spaces and
# hyphens alike, so "Red Rocks Amphitheatre - Morrison, CO" matched the city as
# "Red Rocks Amphitheatre - Morrison", failed the real-city check, and took the
# venue, the city and the state down with it - though the line names all three.
# "Winston-Salem, NC" still matches: its hyphen has no spaces around it.
_CITY_STATE = re.compile(r"((?:[A-Za-z.']+)(?:[ \-][A-Za-z.']+){0,6}),\s*([A-Z]{2})\b")


def _is_real_city(text: str) -> bool:
    """Reject a "city" that is actually a run-on sentence.

    _CITY_STATE's own capture group allows up to 40 characters of plain
    letters and spaces, which a press-release paragraph with only one comma
    in it satisfies just as well as a real city: "My Morning Jacket will take
    the stage of the historic Capitol Theatre in Port Chester, NY" matched
    "historic Capitol Theatre in Port Chester" as the city.  No real city or
    state name runs to five words; this is cheap insurance against prose.
    """
    return len(text.split()) <= 4


@dataclass
class InfoTrack:
    set_no: int | None
    disc: int | None
    number: int | None
    title: str


@dataclass
class InfoFile:
    path: Path
    encoding: str
    text: str
    score: float
    fields: dict[str, str] = field(default_factory=dict)
    tracks: list[InfoTrack] = field(default_factory=list)
    date_candidates: list[DateCandidate] = field(default_factory=list)
    venue: str | None = None
    city: str | None = None
    state: str | None = None

    @property
    def taper(self) -> str | None:
        return self.fields.get("taper") or self.fields.get("transferred_by")


@dataclass
class InfoSelection:
    chosen: InfoFile | None
    considered: list[tuple[Path, float]] = field(default_factory=list)
    torrent_markers: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


RIP_LOG_HEADER = re.compile(
    r"^\s*(?:X Lossless Decoder|Exact Audio Copy|EAC extraction|dBpoweramp|"
    r"whipper|morituri)", re.I)


def is_rip_log(text: str) -> bool:
    """A CD ripper's log.

    Worth reading - its header names the release and usually the show - but its
    own version string and extraction timestamp are date-shaped and are not the
    show, which dates.py already refuses.
    """
    return bool(RIP_LOG_HEADER.match(text[:200]))


def score_text_file(path: Path, text: str) -> float:
    score = 0.0
    for pattern, points in NAME_SCORES:
        if pattern.search(path.name):
            score += points
    if is_rip_log(text):
        # It carries none of the taping vocabulary, but the line naming the
        # release is often the only place the show date is written.
        score += 6
    low = text.lower()
    for keyword, points in CONTENT_KEYWORDS.items():
        if keyword in low:
            score += points
    if len(text) < 40:
        score -= 6
    if len(text) > 200_000:
        score -= 4
    return score


def select_info_file(
    candidates: list[Path], folder_name: str | None = None
) -> InfoSelection:
    sel = InfoSelection(chosen=None)
    best: tuple[float, Path, str, str] | None = None
    parsed: list[tuple[Path, str, str, float]] = []
    stem = (folder_name or "").lower()

    for path in candidates:
        if path.suffix.lower() not in TEXT_EXTS:
            continue
        text, encoding = safe_read_text(path)
        if TORRENT_MARKER_RE.search(path.name) or TORRENT_MARKER_RE.search(text[:2000]):
            sel.torrent_markers.append(path)
            if len(text.strip()) < 400:
                sel.considered.append((path, -99.0))
                continue
        score = score_text_file(path, text)
        if stem and path.stem.lower() == stem:
            score += 10
        sel.considered.append((path, score))
        parsed.append((path, text, encoding, score))
        if best is None or score > best[0]:
            best = (score, path, text, encoding)

    if best and best[0] > 0:
        score, path, text, encoding = best
        sel.chosen = parse_info_text(path, text, encoding, score)
        _borrow_from_others(sel, parsed, best[1])
    elif best:
        sel.notes.append(
            "no text file scored above zero; best was %s (%.0f) - not used"
            % (best[1].name, best[0])
        )
    return sel


def _borrow_from_others(sel: InfoSelection, parsed: list, chosen_path: Path) -> None:
    """Fill gaps in the chosen file from the other text files in the folder.

    One file often carries the lineage while a second carries the setlist, and a
    third the venue.  We still trust one file as the primary - it decides where
    a conflict lands - but we do not throw away what the others know.
    """
    chosen = sel.chosen
    for path, text, encoding, score in parsed:
        if path == chosen_path or score <= -50:      # -50: a torrent stub
            continue
        other = parse_info_text(path, text, encoding, score)
        borrowed = []
        if not chosen.tracks and other.tracks:
            chosen.tracks = other.tracks
            borrowed.append("setlist (%d tracks)" % len(other.tracks))
        if not chosen.venue and other.venue:
            chosen.venue = other.venue
            borrowed.append("venue")
        if not chosen.city and other.city:
            chosen.city, chosen.state = other.city, other.state
            borrowed.append("city")
        for key, value in other.fields.items():
            if key not in chosen.fields and value:
                chosen.fields[key] = value
                borrowed.append(key)
        if borrowed:
            sel.notes.append("took %s from %s" % (", ".join(borrowed), path.name))


_LABELLED_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z /]{0,24})\s*:\s*(.+)$")


def _looks_like_a_venue(line: str) -> bool:
    """Reject "Artist: My Morning Jacket" being read as the venue.

    A labelled line is only a venue if its label says so; otherwise the line is
    describing something else entirely.
    """
    m = _LABELLED_LINE.match(line)
    if not m:
        return True
    label = _clean(m.group(1)).lower()
    return FIELD_ALIASES.get(label) in ("venue", "location", "city")


def _clean(value: str) -> str:
    return re.sub(r"\s{2,}", " ", value.strip().strip("-:*|").strip())


def parse_info_text(path: Path, text: str, encoding: str, score: float) -> InfoFile:
    info = InfoFile(path=path, encoding=encoding, text=text, score=score)
    lines = text.splitlines()

    for raw in lines[:200]:
        line = raw.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        canon = FIELD_ALIASES.get(_clean(key).lower())
        if canon and _clean(value):
            info.fields.setdefault(canon, _clean(value))

    # A filler line is about a different show.  Spare tracks from another night
    # used to fill out a disc are ordinary in trading - "Filler: 2000/10/01
    # Desert Sky Pavilion, Phoenix, AZ" - and that date belongs to the other
    # night.  Left in it competes with the real one: Phish 2000-09-30 scored
    # 90 to 76 and blocked rather than guess, and a filler date that happened
    # to score higher would have silently misdated the show.
    head = lines[:60]
    kept = [ln for ln in head if not _FILLER_LINE.search(ln)]
    # ...unless the only date in the file is on such a line, in which case it
    # is better read as the show's own than thrown away.
    # No bare six-digit dates from prose: in a rip log those are CRCs and
    # sector counts, not shows.
    header = "\n".join(kept)
    info.date_candidates = find_dates(header, allow_six_digit=False)
    if not info.date_candidates:
        header = "\n".join(head)
        info.date_candidates = find_dates(header, allow_six_digit=False)
    for key in ("date",):
        if info.fields.get(key):
            info.date_candidates.extend(
                find_dates(info.fields[key], allow_six_digit=False))

    venue_line = info.fields.get("venue")
    location_line = info.fields.get("location") or info.fields.get("city")
    if venue_line:
        info.venue = venue_line
    hunt = "\n".join(filter(None, [venue_line, location_line, header]))
    m = _CITY_STATE.search(hunt)
    if m and _is_real_city(m.group(1)):
        info.city = _clean(m.group(1))
        info.state = m.group(2)
    if not info.venue:
        for i, raw in enumerate(lines[:12]):
            m = _CITY_STATE.search(raw)
            if not m or not _is_real_city(m.group(1)):
                continue
            # The far more common shape: venue and city/state on one line,
            # "Orpheum Theater, Boston, MA".  Prefer whatever precedes the
            # match on the SAME line - the line above is only the venue when
            # the city/state genuinely stands alone.  Without this check, a
            # bare band-name line directly above ("Jerry Garcia Band") was
            # taken for the venue whenever the venue itself carried its own
            # city and state.
            #
            # Only the LAST comma- or semicolon-delimited segment before the
            # match is the venue - "Jerry Garcia Band, 24-July-1980, Bushnell
            # Auditorium, Hartford, CT" packs band, date and venue onto one
            # line, and "Roseland Ballroom; New York, NY" separates venue from
            # city with a semicolon instead of a comma.
            #
            # A spaced dash outranks the commas, because it separates the venue
            # block from the city block and the commas inside the venue block
            # belong to the venue: "Anaconda Theater, UCSB - Santa Barbara, CA"
            # is one room on one campus, and taking the last comma segment threw
            # the theatre away and kept the campus.
            before = raw[:m.start()]
            dash = re.search(r"\s+[-–]\s*$", before)
            if dash:
                same_line = _clean(before[:dash.start()])
            else:
                segments = [s.strip() for s in re.split(r"[,;]", before) if s.strip()]
                same_line = _clean(segments[-1]) if segments else ""
            if (same_line and 3 < len(same_line) < 80 and not find_dates(same_line)
                    and _looks_like_a_venue(same_line)):
                info.venue = same_line
            elif not same_line and i > 0:
                prev = _clean(lines[i - 1])
                if 3 < len(prev) < 80 and not find_dates(prev) and _looks_like_a_venue(prev):
                    info.venue = prev
            break

    # A ripper's log lists its tracks as a table of offsets, and every row of
    # that table reads as a numbered setlist line.  Library-wide, all 1,441
    # "tracks" taken from rip logs were such rows, and 1,010 of them were
    # written into the 30 Trips and Europe '72 box sets as titles.  The log is
    # still read for its header - the release and the show - just not this.
    info.tracks = [] if is_rip_log(text) else parse_setlist(lines)
    return info


# A "title" that is only times, offsets and punctuation.  A bare number is
# kept: "2001", "1999" and "555" are songs.
_ONLY_TIMES = re.compile(r"[\d:.,\-–\s]+(?:\([^)]*\))?")
_BARE_NUMBER = re.compile(r"\d{2,4}")


def is_a_title(text: str) -> bool:
    """Could this be the name of a song, rather than a row of numbers?

    A rip log's TOC row "4 | 22:07:10 | 06:21:27 | 99535 | 128136" matches the
    setlist line pattern exactly - number, then text - and so does a duration
    note in a taper's info file ("7:51", "2:26-2:28 (spotty)").  None of them
    is a song.
    """
    text = text.strip()
    if not re.search(r"[A-Za-z0-9]", text) or "|" in text:
        return False
    if _BARE_NUMBER.fullmatch(text):
        return True
    return not _ONLY_TIMES.fullmatch(text)


def parse_setlist(lines: list[str]) -> list[InfoTrack]:
    tracks: list[InfoTrack] = []
    cur_set: int | None = None
    cur_disc: int | None = None

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue

        m = _SET_HEADER.match(line)
        if m and not _TRACK_LINE.match(line):
            if m.group(1):
                cur_set = _header_number(m.group(2))
            elif m.group(3):
                cur_set = (cur_set or 0) + 100  # encore marker, kept distinct
            elif m.group(5):
                cur_disc = _header_number(m.group(5))
            remainder = m.group(6)
            # "Disc 1 - Set One" says both things at once.
            trailing = _SET_IN_REMAINDER.search(remainder or "")
            if trailing:
                cur_set = _header_number(trailing.group(1)) or cur_set
            if remainder and len(remainder) > 2 and not remainder[0].isdigit():
                continue
            continue

        em = _ETREE_TRACK_LINE.match(line)
        if em:
            title = _clean_title(em.group("title"))
            if title and is_a_title(title):
                tracks.append(
                    InfoTrack(set_no=cur_set, disc=int(em.group("disc")),
                              number=int(em.group("num")), title=_clean(title))
                )
            continue

        tm = _TRACK_LINE.match(line)
        if tm:
            title = _clean_title(tm.group("title"))
            if (not title or title.lower().startswith(("http", "www."))
                    or not is_a_title(title)):
                continue
            disc = int(tm.group("disc")) if tm.group("disc") else cur_disc
            tracks.append(
                InfoTrack(set_no=cur_set, disc=disc,
                          number=int(tm.group("num")), title=_clean(title))
            )
    return tracks


def taper_slug(raw: str | None, cfg=None) -> tuple[str | None, bool]:
    """Turn a taper field into a provenance token: their handle or surname.

    Returns (slug, from_config).  The config's `tapers:` table is authoritative;
    anything not listed there falls back to the surname/handle heuristic and is
    reported, so you can add it to the table.
    """
    if not raw:
        return None, False
    if cfg is not None:
        known = cfg.canonical_taper(raw)
        if known:
            return known, True

    # "Transfer: Tascam DA-20MKII > HHb CDR-830" is a deck chain, not a person.
    # Anything that looks like equipment is refused outright rather than slugged
    # into a folder name.
    if ">" in raw:
        return None, False
    value = re.split(r"[;,/(]|\band\b|\bwith\b", raw, maxsplit=1)[0]
    words = [w for w in re.split(r"[^A-Za-z0-9'&_.\-]+", value) if w]
    if not words or len(words) > 3:
        # More than three words is a sentence or a gear list, not a name.
        return None, False
    if 2 <= len(words) <= 3 and all(w[:1].isupper() for w in words):
        pick = words[-1]          # "Charlie Miller" -> miller
    else:
        pick = "".join(words)     # a one-word online handle
    slug = re.sub(r"[^a-z0-9]", "", pick.lower())
    # A handle or surname is short.  Anything longer is almost certainly a
    # sentence or a piece of gear that slipped through.
    if len(slug) > 16:
        return None, False
    return (slug or None), False
