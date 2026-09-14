"""One pass over a show folder, producing everything both phases need.

Order matters here.  The classifier runs before the date resolver, because the
classification decides whether the DATE tag is admissible evidence at all: on a
disc rip it holds the release year, and letting it in would date Spring 1990 to
2014.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import classify as _classify
from . import dates as _dates
from .audio import PROVENANCE_TAGS, format_token, quality_token
from .bands import BandResolution, resolve_band
from .classify import Classification
from .config import Config
from .infofile import InfoSelection, InfoFile, select_info_file, taper_slug
from .naming import parse_canonical, strip_dates_from_place, strip_trailing_source
from .scan import ShowFolder, is_year_dir
from .state import NEWER, read_state
from .sources import (
    SourceInference,
    broadcast_provenance,
    detect_provenance,
    infer_source,
    mic_as_provenance,
    provenance_from_name,
    residual_words,
)
from .textio import safe_read_text
from .tokens import find_tokens, has_token, mask_spans, strip_format_suffixes

# Categories a folder can end up in.
SHOW = "SHOW"
UNDATED = "UNDATED"
BOXSET = "BOXSET"
ALBUM = "ALBUM"

SEVERITY_BLOCK = "block"     # never rename this folder
SEVERITY_WARN = "warn"       # rename, but say something
SEVERITY_INFO = "info"


@dataclass
class Issue:
    code: str
    severity: str
    detail: str


@dataclass
class ShowAnalysis:
    show: ShowFolder
    band: BandResolution
    date: _dates.DateResolution
    classification: Classification
    info: InfoSelection
    source: SourceInference
    fmt: str | None = None
    quality: str | None = None
    venue: str | None = None
    city: str | None = None
    state: str | None = None
    # The place was stated by hand in overrides.yaml.  Recorded because the
    # evidence an override is meant to beat is consulted after it is applied.
    place_by_hand: bool = False
    # The gazetteer placed or respelled this room.  Carried for the same reason
    # place_by_hand is: an OFFICIAL release takes its ALBUM tag verbatim and
    # never reads venue/city/state, so without this the tags gain the city and
    # the folder name does not - and the folder replans on every run without
    # ever settling: a rename that changes nothing, every run.
    place_from_gazetteer: bool = False
    # An early/late marker stated by hand.  The marker itself is worked out in
    # phase 1 from the folder name, so the override is carried rather than
    # applied here.
    marker_by_hand: str | None = None
    provenance: str | None = None
    provenance_raw: str | None = None
    provenance_reason: str = ""
    category: str = SHOW
    # The date was settled by agreement across the track filenames.
    date_from_filenames: bool = False
    # The ARTIST (or ALBUMARTIST) the files carry, as read.
    tag_artist: str | None = None
    issues: list[Issue] = field(default_factory=list)
    date_evidence: list[_dates.DateEvidence] = field(default_factory=list)

    @property
    def info_file(self) -> InfoFile | None:
        return self.info.chosen

    @property
    def blocked(self) -> bool:
        return any(i.severity == SEVERITY_BLOCK for i in self.issues)

    @property
    def issue_codes(self) -> list[str]:
        return [i.code for i in self.issues]

    def add(self, code: str, severity: str, detail: str) -> None:
        self.issues.append(Issue(code, severity, detail))


# "VWMULE: Microtech Gefell M21 > nBob actives > ..." - the etree convention of
# stamping the taper handle at the head of the lineage.
_TAPER_HANDLE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_.\-]{2,19})\s*:\s*\S")
# A track filename's leading number: "2-06 ", "01 ", "d1t05 ".  Stripped
# before looking for dates, so a song called "2001" cannot pair with its
# own track number to make one.
_TRACK_NUMBER_PREFIX = re.compile(
    r"^(?:[dst]?\d{1,2}[-_. ]?[ts]?\d{1,3}|\d{1,3})[-_. ]+", re.I)


def unglue_band_prefix(text: str, cfg: Config) -> str:
    """Put a space between a band prefix ending in a digit and the date after it.

    Every date pattern refuses a year that follows a digit - that is what keeps
    an offset or a catalogue number from reading as a date - so in
    "sts92000-02-12" the 9 of STS9 hid the year and the folder had no date at
    all.  Only prefixes the config names, and only ones ending in a digit;
    "ph2000-02-12" never had the problem.
    """
    glued = sorted({p for b in cfg.bands for p in (b.abbrev.lower(), *b.prefixes)
                    if p and p[-1].isdigit()}, key=len, reverse=True)
    if not glued:
        return text
    pattern = re.compile(r"(?i)(?<![a-z0-9])(%s)(?=\d)" % "|".join(map(re.escape, glued)))
    return pattern.sub(r"\1 ", text)


def _drop_track_number(name: str) -> str:
    """Strip a leading track number - unless the name opens with a date.

    "12-31-95 Set 1.flac" is a file named after the show, and dates agreed
    across the filenames are the strongest evidence there is; stripping that
    would throw away the very thing worth reading.
    """
    m = _TRACK_NUMBER_PREFIX.match(name)
    if not m:
        return name
    # Strip only when the title behind the number is a bare year, because that
    # is the whole trap: "2-06 2001" and "2_06_2001" are track 2-06 of the song
    # 2001, and read as 2/06/2001.  Anything else is left alone - a file named
    # after its show, "12-31-95 Set 1.flac", must keep the date it carries.
    if re.match(r"^(?:19|20)\d{2}(?!\d)", name[m.end():]):
        return name[m.end():]
    return name

# The city may not run across a spaced dash.  The old class allowed spaces and
# hyphens alike, so "Red Rocks Amphitheatre - Morrison, CO" matched the city as
# "Red Rocks Amphitheatre - Morrison", failed the real-city check, and took the
# venue, the city and the state down with it - the line names all three.
# "Winston-Salem, NC" still matches: its hyphen has no spaces around it.
_CITY_STATE = re.compile(r"((?:[A-Za-z.']+)(?:[ \-][A-Za-z.']+){0,6}),\s*([A-Z]{2})\b")
# A bare city with no state after it: letters and the punctuation a place
# name actually carries, nothing numeric, and short enough to be a name
# rather than a sentence.  Deliberately strict - it runs only when no US
# state was found, so a loose pattern would invent cities out of blurbs.
# The tail of a "venue, ..." album is not always a city: a Disco Biscuits
# album read "Irvine Auditorium, University of Pennsylvania", which is the
# room and its campus, and University of Pennsylvania went in as the city.
# A tail naming a building, a campus or an institution continues the venue.
_NOT_A_CITY = re.compile(
    r"(?<![A-Za-z])(?:univ(?:ersity)?|college|school|institute|academy"
    r"|center|centre|theat(?:re|er)|arena|stadium|auditorium|hall|pavilion"
    r"|ballroom|club|lounge|cafe|bar|room|field|park|garden|campus"
    r"|amphitheat(?:re|er)|coliseum|colise|opera|house)(?![A-Za-z])", re.I)
_PLACE_TAIL = re.compile(r"[A-Za-z][A-Za-z .'-]{1,30}")
_LEADING_DATE = re.compile(
    r"^\s*(?:(?:19|20)\d{2}[-._/ ]\d{1,2}[-._/ ]\d{1,2}"
    r"|\d{1,2}[-._/ ]\d{1,2}[-._/ ]\d{2,4})\s*[:\-]?\s*"
)


def taper_from_tags(show: ShowFolder) -> tuple[str | None, str]:
    """A taper named in the tags, or in the track filenames.

    Tags are often the only place a well-tagged audience tape says who taped it.
    """
    for f in show.files[:4]:
        for key in ("TAPER", "TAPED BY", "RECORDED BY"):
            value = (f.tags.get(key) or [None])[0]
            if value:
                return value, "%s tag" % key
    for f in show.files[:4]:
        for key in ("COMMENT", "DESCRIPTION"):
            for value in f.tags.get(key) or []:
                m = _TAPER_HANDLE.match(value)
                if m and not m.group(1).lower().startswith(("http", "source", "lineage")):
                    return m.group(1), "handle at the head of the %s tag" % key.lower()
    return None, ""


def _normalise_field_seps(text: str | None) -> str | None:
    """"2012.01.21 :: Congress Theater :: Chicago, IL" - "::" is a comma.

    Stores and trackers separate the fields of an album tag with a double colon
    or a pipe.  Neither survives in a Windows folder name, so they were simply
    dropped, which ran the room and the city together into one venue: "Congress
    Theater Chicago, IL".  Two or more colons only - "9:30 Club" keeps its one.
    """
    if not text:
        return text
    out = re.sub(r"\s*(?:::+|\|)\s*", ", ", text)
    return re.sub(r"(,\s*)+", ", ", out).strip(" ,")


def place_from_tags(show: ShowFolder) -> tuple[str | None, str | None, str | None]:
    """(venue, city, state) mined from VENUE/LOCATION or the ALBUM tag."""
    venue = city = state = None
    for f in show.files[:4]:
        venue = venue or (f.tags.get("VENUE") or [None])[0]
        location = _normalise_field_seps((f.tags.get("LOCATION") or [None])[0])
        album = _normalise_field_seps((f.tags.get("ALBUM") or [None])[0])
        for candidate in (location, album):
            if not candidate:
                continue
            m = _CITY_STATE.search(candidate)
            if m and not city:
                city, state = m.group(1).strip(" -"), m.group(2)
        if venue is None and album:
            # The comma matters: "2004-12-19, Warfield Theater, San Francisco,
            # CA" strips to ", Warfield Theater, ..." and splitting on the comma
            # then yields an empty head, which fails the length check below and
            # loses the venue entirely.  A city with no venue is dropped from
            # the name, so the show came out with no place at all while its
            # ALBUM tag named one.
            stripped = _LEADING_DATE.sub("", album).strip(" -:,")
            if stripped and stripped != album:
                head = stripped.split(",")[0].strip(" -")
                head = strip_trailing_source(head)
                if 3 < len(head) < 60:
                    venue = head
                # "2002.05.23 Aoyama Cay, Tokyo" has no two-letter state, so
                # _CITY_STATE finds nothing and the city was thrown away with
                # the rest of the tail - every show outside the US lost its city
                # this way.  The tail of a "venue, city" album is the city.
                if not city and venue:
                    tail = stripped.split(",")[1:]
                    if len(tail) == 1:
                        cand = strip_trailing_source(tail[0].strip(" -.")) or ""
                        if _PLACE_TAIL.fullmatch(cand) and not _NOT_A_CITY.search(cand):
                            city = cand
    return venue, city, state


# "Selections" is how a store says it sold part of the show, not a place:
# Trey's "9:30 Club - May 11, 1999 - Selections" was filing Selections as the
# city.  It describes the release, so it belongs with the rest of these.
_JUNK_WORDS = re.compile(
    r"(?<![A-Za-z0-9])(?:box\s*set|cd\s*rel|remaster(?:ed)?|reissue|deluxe"
    r"|complete\s+recordings?|official|release|selections?)(?![A-Za-z0-9])", re.I)


def _comma_for_double_space(text: str) -> str:
    """A double space standing in for the comma: "Congress Theater  Chicago IL".

    Collapsing the whitespace first glued the room to the city, and the place
    came out "Congress Theater Chicago, IL".  Two guards keep it honest: the
    right side must start with a capital, and the left must already be more than
    one word - without that second one "The  Fillmore" became "The, Fillmore".
    """
    chunks = [c for c in re.split(r"\s{2,}", text or "") if c.strip()]
    if len(chunks) < 2:
        return text or ""
    out = chunks[0]
    for chunk in chunks[1:]:
        joinable = (len(out.split()) > 1
                    and chunk[:1].isupper()
                    and not out.rstrip().endswith(","))
        out += (", " if joinable else " ") + chunk
    return out


def strip_product_words(text: str) -> str:
    """Drop words that describe the release rather than the place.

    Applied wherever the place text comes from: some releases simply copy the
    folder name into ALBUM, junk and all.
    """
    out = _JUNK_WORDS.sub(" ", text or "")
    out = _comma_for_double_space(out)
    # "Winterland Arena San Francisco CA" - the state is written without the
    # comma the rest of the library uses.
    out = re.sub(r"(?<=[a-z])\s+([A-Z]{2})\s*$", r", \1", out)
    return re.sub(r"\s{2,}", " ", out).strip(" -:,")


def _norm_word(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _band_prefixes(cfg: Config) -> set[str]:
    out: set[str] = set()
    for band in cfg.bands:
        out.add(_norm_word(band.abbrev))
        out.update(_norm_word(p) for p in band.prefixes)
    out.discard("")
    return out


def place_is_whole_string(text: str | None) -> bool:
    """True if this place text already carries its own "City, ST".

    venue_from_folder_name returns the WHOLE place as one string, deliberately -
    "Walnut Creek Amphitheatre, Raliegh, NC".  Adding a city from another source
    beside it says the same city twice, and dedupe_place_parts cannot see the
    duplicate when the two spellings differ: the name came out
    "Walnut Creek Amphitheatre, Raliegh, NC, Raleigh".
    """
    return bool(text and _CITY_STATE.search(text))


def venue_from_folder_name(show: ShowFolder, cfg: Config) -> tuple[str | None, str | None]:
    """The place written in the folder name itself.

    "ph2018-11-03 MGM Grand Garden Arena, Las Vegas, NV [V0]" already says where
    the show was, and nothing else in the folder does.  A "City, ST" is required,
    which keeps loose words like "opera house" out.
    """
    text = show.name
    spans = [c.span for c in _dates.find_dates(text)]
    text = mask_spans(text, spans)
    text = re.sub(r"[\[(][^\])]*[\])]", " ", text)          # [V0], (2002)
    text = strip_format_suffixes(text, cfg.format_suffixes)
    # ...and anywhere else it appears: "mmj2004-05-28.flac16 The Opera House"
    # puts the format in the middle, where a trailing strip cannot reach it.
    for token in sorted(set(cfg.format_suffixes)
                        | {q for toks in cfg.quality_tokens.values() for q in toks},
                        key=len, reverse=True):
        text = re.sub(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(token),
                      " ", text, flags=re.I)
    # Strip a leading band prefix - but only one we actually know.  Blindly
    # dropping any short leading word turns "New York, NY" into "York, NY" and
    # "Los Angeles" into "Angeles".
    lead = re.match(r"^([A-Za-z]{1,6})(?=[\s._-]|$)", text)
    if lead and _norm_word(lead.group(1)) in _band_prefixes(cfg):
        text = text[lead.end():]
    # Words that describe the product, not the place: "gd 1973-11-09 Box Set
    # Winterland Arena San Francisco CA CD REL".
    text = _JUNK_WORDS.sub(" ", text)
    text = re.sub(r"[\s._-]+", " ", text).strip(" .-_")

    m = _CITY_STATE.search(text)
    if not m:
        return None, None
    place = text[: m.end()].strip(" ,.-")
    if len(place) < 4:
        return None, None
    return place, "the folder name"


def _cue_text(show: ShowFolder) -> str:
    out: list[str] = []
    for path in show.sidecars.get("cue", []):
        text, _ = safe_read_text(path, limit=64_000)
        out.append(text)
    return "\n".join(out)


def apply_override(a: ShowAnalysis, entry, cfg: Config | None = None) -> None:
    """Let an answer you gave beat everything the files say."""
    values = entry.values
    # band and marker were declared as override fields and validated on load,
    # but never applied - so stating either was accepted in good faith and
    # silently ignored.  band needs the config to turn an abbreviation into the
    # act it names; an abbreviation that matches nothing is an error in the
    # override rather than something to guess around.
    if values.get("band"):
        want = str(values["band"]).lower()
        target = next((b for b in cfg.bands if b.abbrev.lower() == want), None)             if cfg is not None else None
        if target is None:
            raise ValueError(
                "overrides.yaml: band %r for %r matches no act in the config"
                % (values["band"], entry.match))
        a.band = BandResolution(
            band=target, matched_by="overrides.yaml", confidence=99,
            evidence="set by hand in overrides.yaml", authoritative=True)
    if values.get("marker"):
        a.marker_by_hand = str(values["marker"]).lower()
    if values.get("date"):
        given = values["date"]
        a.date = _dates.DateResolution(
            date=given if isinstance(given, _dt.date) else _dt.date.fromisoformat(str(given)),
            confidence=99,
            reasons=["set by hand in overrides.yaml"],
        )
    # A key written with no value clears the field.  An override could only ever
    # replace one before, and some of what it needs to beat is not a wrong answer
    # but an answer that should not be there at all: "ams" offered as a venue
    # when it is a city, or a source inferred from a classification this same
    # override has just overturned.  `in values` rather than `.get`, so an
    # explicit null is told apart from a key nobody wrote.
    for attr in ("venue", "city", "state"):
        if attr in values:
            setattr(a, attr, str(values[attr]) if values[attr] else None)
            a.place_by_hand = True
    if "provenance" in values:
        a.provenance = str(values["provenance"]) if values["provenance"] else None
    if "format" in values:
        a.fmt = str(values["format"]) if values["format"] else None
    if values.get("classification"):
        a.classification.kind = str(values["classification"]).upper()
        a.classification.notes.append("origin set by hand in overrides.yaml")
    if "source" in values:
        a.source = SourceInference(
            value=str(values["source"]) if values["source"] else None,
            inferred=False, confidence="stated",
            reason="set by hand in overrides.yaml")
    a.add("OVERRIDDEN", SEVERITY_INFO,
          "overrides.yaml sets %s%s"
          % (", ".join("%s=%r" % (k, v) for k, v in sorted(values.items())
                       if k not in ("note", "skip")),
             "; " + str(values["note"]).strip() if values.get("note") else ""))


def analyze_show(show: ShowFolder, cfg: Config, today: _dt.date | None = None,
                 overrides=None) -> ShowAnalysis:
    today = today or _dt.date.today()
    earliest = cfg.settings.earliest_show_date

    # --- what does the folder say about itself --------------------------
    info = select_info_file(show.texts, folder_name=show.name)
    info_file = info.chosen
    info_text = info_file.text if info_file else ""
    info_has_lineage = bool(
        info_file and any(k in info_file.fields for k in ("lineage", "taper", "source", "transferred_by"))
    )

    # ALBUMARTIST counts too, and on its own.  A release tagged only that way is
    # ordinary - King Gizzard's 2023 shows carry "King Gizzard & the Lizard
    # Wizard" there and no ARTIST at all, so thirteen of them resolved to no
    # band whatever, while their dates and venues read perfectly.  ARTIST is
    # still preferred: on a compilation it names the performer while
    # ALBUMARTIST may say "Various Artists".
    tag_artist = None
    fallback = None
    for f in show.files:
        if f.tags.get("ARTIST"):
            tag_artist = f.tags["ARTIST"][0]
            break
        if fallback is None and f.tags.get("ALBUMARTIST"):
            fallback = f.tags["ALBUMARTIST"][0]
    tag_artist = tag_artist or fallback

    # The opening lines of an info file, before any "Key: value" pairs - that is
    # where a folder with empty tags names the act.
    info_lines = [
        line.strip() for line in (info_text.splitlines()[:6] if info_text else [])
        if line.strip() and ":" not in line
    ]
    band = resolve_band(show.name, cfg,
                        parent_artist_dir=show.artist_dirs or show.artist_dir,
                        tag_artist=tag_artist, info_lines=info_lines,
                        track_names=[f.path.name for f in show.files])

    # --- where did it come from -----------------------------------------
    prov_haystacks = [
        ("folder name", show.name),
        ("info file", info_text[:4000]),
    ]
    # A band's own store stamps itself in the tags far more reliably than in
    # the folder name - UMLive writes "UMLive" into COMMENT on every track.
    for tag_name in PROVENANCE_TAGS:
        values = [
            value
            for f in show.files[:4]
            for value in (f.tags.get(tag_name) or [])
        ]
        if values:
            prov_haystacks.append(("%s tag" % tag_name.lower(), " ".join(values)))
    prov_haystacks.append(("file names", " ".join(p.name for p in show.texts[:10])))

    prov_key, prov_reason = detect_provenance(cfg, prov_haystacks)
    store_marker = None
    prov_cfg = cfg.provenance_for(prov_key)
    if prov_cfg and prov_cfg.official:
        store_marker = prov_reason

    # Text from the tags that could carry a lineage: a well-tagged audience
    # tape hides its whole story in COMMENT/DESCRIPTION.
    tag_text = " ".join(
        value
        for f in show.files[:4]
        for key in ("COMMENT", "DESCRIPTION", "SOURCE", "LINEAGE", "TAPER", "ENCODERSETTINGS")
        for value in (f.tags.get(key) or [])
    )

    # Source is worked out BEFORE classification, because "an audience mic was
    # used" is one of the strongest arguments that a folder is not official.
    stated = infer_source(
        cfg,
        folder_name=show.name,
        info_text=info_text,
        tag_text=tag_text,
        stated_source=(info_file.fields.get("source") if info_file else None),
    )

    mic_hits = find_tokens(show.name, cfg.microphones)
    cls = _classify.classify(
        cfg,
        folder_name=show.name,
        files=show.files,
        sidecars=show.sidecars,
        info_text=info_text,
        info_has_lineage=info_has_lineage,
        torrent_marker=bool(info.torrent_markers),
        store_marker=store_marker,
        band_matched_by=band.matched_by,
        has_cover_art=bool(show.images),
        mic_token=bool(mic_hits),
        audience_evidence=(stated.confidence == "stated" and stated.value in ("aud", "mtx")),
        extra_texts=[
            (f.tags.get("ALBUM") or [""])[0] for f in show.files[:3]
        ] + ([show.container.name] if show.container else [])
        + ([show.release_dir.name] if show.release_dir else []),
    )

    # A folder we have already renamed no longer holds the evidence it was
    # classified on: our scheme states the source in the name, numbers tracks
    # dNtNN and writes a VENUE tag, so what is left says more about this
    # pipeline than about the recording.  The answer we reached the first time,
    # from the original evidence, is recorded in .etree_state.json - so prefer
    # it rather than re-deriving a weaker one.
    _state = read_state(show.path) or {}
    _was = _state.get("classification")
    if _was and _was != cls.kind and parse_canonical(show.name) is not None:
        cls.notes.append(
            "kept the recorded classification %r; re-reading a folder we have "
            "already renamed would say %r, but the evidence for that was our "
            "own naming" % (_was, cls.kind))
        cls.kind = _was


    analysis = ShowAnalysis(
        show=show, band=band, date=_dates.DateResolution(None, 0),
        classification=cls, info=info,
        source=SourceInference(),
        tag_artist=tag_artist,
    )

    # --- date -------------------------------------------------------------
    year_range = band.band.active_years if band.band else None
    evidence: list[_dates.DateEvidence] = [
        _dates.evidence_from_text("folder", unglue_band_prefix(show.name, cfg),
                                  earliest=earliest, today=today, year_range=year_range)
    ]
    if show.container is not None:
        evidence.append(_dates.evidence_from_text(
            "parent_folder", unglue_band_prefix(show.container.name, cfg),
            earliest=earliest, today=today, year_range=year_range))
    if info_file:
        ev = _dates.DateEvidence(source="info", text=info_file.path.name,
                                 candidates=list(info_file.date_candidates))
        evidence.append(ev)
    cue = _cue_text(show)
    if cue:
        evidence.append(_dates.evidence_from_text("cue", cue[:8000], earliest=earliest,
                                                  today=today, year_range=year_range,
                                                  allow_six_digit=False))
    # Joined with newlines, never spaces: "1-01 Bertha.mp3 1-02 Playing.mp3"
    # contains "3 1-02", which reads as a date that does not exist.  A newline
    # is not one of the date separators, so it cannot be crossed.
    # The leading disc/track number is not part of a date, but it will happily
    # form one with whatever follows.  "2-06 2001.flac" is disc 2, track 6, of
    # the song "2001" - and reads as 2/06/2001, which cost Phish 2013-10-20
    # twenty-five points of confidence and blocked the folder.  Song titles
    # that are bare years are common enough to matter: 2001, 1999, 1970.
    names = [_drop_track_number(unglue_band_prefix(f.path.name, cfg))
             for f in show.files[:60]]
    track_text = "\n".join(names)
    if track_text:
        track_ev = _dates.evidence_from_text("tracks", track_text, earliest=earliest,
                                             today=today, year_range=year_range)
        # A date carried by most of the files outranks the folder name.  Whoever
        # named twenty files one by one had the date in front of them; a folder
        # is typed once and is where the typo usually is.
        agreeing: dict[_dt.date, int] = {}
        for name in names:
            for cand in _dates.find_dates(name, earliest=earliest, today=today,
                                          year_range=year_range):
                agreeing[cand.date] = agreeing.get(cand.date, 0) + 1
        if agreeing:
            best, count = max(agreeing.items(), key=lambda kv: (kv[1], kv[0]))
            if count >= 3 and count >= 0.6 * len(names):
                track_ev.weight_override = 1.05
                analysis.date_from_filenames = True
                analysis.add(
                    "DATE_FROM_FILENAMES", SEVERITY_INFO,
                    "%s appears in %d of %d track filenames, so the filenames are "
                    "trusted over the folder name" % (best.isoformat(), count, len(names)))
        evidence.append(track_ev)
    if cls.tag_date_admissible:
        tag_text = " ".join(cls.tag_profile.date_values)
        evidence.append(_dates.evidence_from_text("tags", tag_text, earliest=earliest,
                                                  today=today, year_range=year_range))
    # A disc rip whose DATE is the release year often still carries the real
    # show date in ALBUM ("2011/11/05 Eagles Ballroom - Milwaukee, WI").  That
    # is a full date, not a release year, so it is not the trap - but it is
    # weighted low, and a multi-date release is never dated from it.
    # Not only on official folders.  A community copy can carry a clean full
    # date in ALBUM - "2003-07-10 Mountain View, CA - Shoreline Amphitheatre"
    # - while its folder says only "Phish 7-10-03", which reads two ways and
    # is docked for it.  The trap this guards against is a release YEAR in the
    # DATE tag, handled separately; a full date in ALBUM is not that, as the
    # note above says.  Still weighted low, and a multi-date release is still
    # never dated from it.
    if not cls.multi_date and cls.tag_profile.album_values:
        album_ev = _dates.evidence_from_text(
            "tags_album", cls.tag_profile.album_values[0],
            earliest=earliest, today=today, year_range=year_range,
            allow_six_digit=False,
        )
        if album_ev.candidates:
            evidence.append(album_ev)

    if not cls.tag_date_admissible and cls.tag_profile.date_values and cls.kind == _classify.OFFICIAL:
        analysis.add("TAG_DATE_REFUSED", SEVERITY_INFO,
                     "DATE tag %s not used: %s"
                     % (list(cls.tag_profile.date_values)[:3],
                        "disc rip" if cls.shape == _classify.DISC_RIP else "not a full ISO date"))

    analysis.date_evidence = evidence
    analysis.date = _dates.resolve_date(evidence)

    # A family filing prefix plus a date can name the act on its own: by 1987
    # "Jerry Garcia" meant the Jerry Garcia Band.  This runs here, after the
    # date, because the year is the whole of the evidence - and only when the
    # name found nothing more specific, so an explicit "jgb"/"jg_dg" still wins.
    fam = analysis.band.band if analysis.band else None
    if (fam is not None and fam.defaults_to and fam.defaults_from_year
            and analysis.date.date
            and analysis.date.date.year >= fam.defaults_from_year):
        # The exception word ("friends") lived in the ORIGINAL folder name.  On
        # a folder we already renamed, our own canonical name never repeats it
        # - the machine part just says "jg" or "jgb" - so re-checking show.name
        # here silently loses the exception and flips a confirmed "and Friends"
        # show over to the Band.  .etree_state.json remembers what the folder
        # was called before we touched it; check that too.  Both names it keeps:
        # previous_folder_name is only one rename back, so after a second
        # rename it holds our own machine name and the word is gone from it -
        # the exact loss this check exists to prevent.
        _state = read_state(show.path) or {}
        _prior_names = [(_state.get(k) or "").lower()
                        for k in ("original_folder_name", "previous_folder_name")]
        named = [w for w in fam.defaults_unless_named
                 if has_token(show.name.lower(), w)
                 or any(has_token(n, w) for n in _prior_names)]
        target = next((b for b in cfg.bands if b.abbrev == fam.defaults_to), None)
        if named:
            analysis.add("FAMILY_DEFAULT_NOT_APPLIED", SEVERITY_INFO,
                         "%s from %d would default to %s, but the name says %r - "
                         "left as %s" % (fam.name, fam.defaults_from_year,
                                         fam.defaults_to, named[0], fam.abbrev))
        elif target is not None:
            analysis.band = replace(
                analysis.band, band=target,
                evidence="%s; from %d a bare %r is the %s"
                       % (analysis.band.evidence, fam.defaults_from_year,
                          fam.abbrev, target.name))
            analysis.add("FAMILY_DEFAULT_APPLIED", SEVERITY_INFO,
                         "the name says only %r; from %d that is the %s, so %s is used"
                         % (fam.abbrev, fam.defaults_from_year, target.name,
                            target.abbrev))

    # --- source and provenance -------------------------------------------
    # Who taped it, from the info file first, then from the tags - on a
    # well-tagged audience recording the tags are the only place it is written.
    # A folder we have already renamed says what it is.  Re-deriving these
    # fields from the tags we ourselves wrote can reach a worse answer - the
    # taper reappearing as a venue, a country code being dropped - so the
    # canonical name is read back rather than second-guessed.
    # The format token is what says we wrote this name: every canonical name
    # ends with one.  Without that check "mmj2012-09-12.Wiltern" parses too,
    # and a venue someone typed by hand would be read back as a taper.
    canon = parse_canonical(show.name)
    if canon is not None and not canon.fmt:
        canon = None
    if canon is not None and canon.provenance:
        # Same reasoning: the provenance is written into the name we produced,
        # and the evidence it was derived from (a microphone model, say) is no
        # longer in that name to be found again.
        analysis.provenance = canon.provenance
        analysis.provenance_raw = canon.provenance
        analysis.provenance_reason = "read back from the canonical folder name"

    # Everything from here to the end of this block re-derives who taped it
    # from scratch.  When the canonical name already answered that (just
    # above), none of it should run at all: on our own folder, an info file's
    # broadcast-station mention or a tag can easily outrank the taper the
    # ORIGINAL messy name correctly identified, because that name is gone now
    # and cannot corroborate itself a second time.  A real jgb1980-03-01 show
    # drifted from provenance "glassberg" to "wnew" this way.
    canon_has_provenance = canon is not None and canon.provenance

    taper_raw = (info_file.taper if info_file else None) if not canon_has_provenance else None
    taper_why = "the info file" if taper_raw else ""
    if not taper_raw and not canon_has_provenance:
        taper_raw, taper_why = taper_from_tags(show)

    analysis.venue, analysis.city, analysis.state = place_from_tags(show)
    analysis.venue = strip_dates_from_place(analysis.venue) or None
    if canon is not None and canon.location:
        analysis.venue = canon.location
        analysis.city = analysis.state = None
        analysis.add("PLACE_FROM_CANONICAL_NAME", SEVERITY_INFO,
                     "%r read back from the folder name, which this pipeline "
                     "wrote" % canon.location)
    if not analysis.venue:
        from_name, why = venue_from_folder_name(show, cfg)
        if from_name:
            analysis.venue = from_name
            analysis.city = analysis.state = None   # the match already carries them
            analysis.add("VENUE_FROM_NAME", SEVERITY_INFO,
                         "venue %r taken from %s" % (from_name, why))
    if not analysis.venue and info_file and info_file.venue:
        analysis.venue = strip_dates_from_place(info_file.venue) or None

    # A store or label identifies the copy; a tracker does not.  When the
    # provenance we found is not an official channel and we also know who taped
    # it, the taper is the better answer.
    # A matrix or audience recording that mentions a store is quoting its
    # lineage, not its origin: a matrix is routinely built from the LivePhish
    # board feed plus the taper's own mics.  The store did not release this.
    store_is_lineage = (
        prov_cfg is not None
        and prov_cfg.official
        and stated.value in ("aud", "mtx")
        and stated.confidence == "stated"
    )
    if store_is_lineage:
        analysis.add("STORE_IS_LINEAGE", SEVERITY_INFO,
                     "%r appears here, but the source is %s - treating the store as part "
                     "of the lineage, not as the provenance of this copy"
                     % (prov_key, stated.value))

    # A "Taper:" line that is really a signal chain - "MAC > Nakamichi DR-2 >
    # Edirol FA-66" - names equipment, not a person.  Taking it as the taper
    # yields no handle at all, and then the real taper sitting in the folder
    # name gets mistaken for a venue.
    if taper_raw and ">" in taper_raw:
        analysis.add("TAPER_LINE_IS_LINEAGE", SEVERITY_INFO,
                     "the taper line %r reads as a signal chain, not a name - "
                     "ignored" % taper_raw[:60])
        taper_raw = ""

    prefer_taper = bool(taper_raw) and (
        prov_cfg is None or not prov_cfg.official or store_is_lineage
    )
    if store_is_lineage and not taper_raw:
        prov_key, prov_cfg = None, None
    analysis.provenance_raw = taper_raw if prefer_taper else (prov_key or taper_raw)
    if prov_key and not prefer_taper:
        analysis.provenance = prov_key
        analysis.provenance_reason = prov_reason
    elif taper_raw:
        slug, from_config = taper_slug(taper_raw, cfg)
        analysis.provenance = slug
        analysis.provenance_reason = "taper %r from %s" % (taper_raw[:40], taper_why)
        if slug and not from_config:
            analysis.add("TAPER_NOT_IN_CONFIG", SEVERITY_INFO,
                         "%r was shortened to %r by the surname/handle rule; add it to "
                         "the config `tapers:` table to pin the spelling"
                         % (taper_raw[:40], slug))
        if prov_key:
            analysis.add("PROVENANCE_CHOICE", SEVERITY_INFO,
                         "used the taper %r rather than %r (%s)"
                         % (taper_raw[:40], prov_key, prov_reason))
        from_name, why = provenance_from_name(cfg, show.name)
        if from_name and not analysis.provenance:
            # The taper line gave nothing usable.  The folder name is then the
            # only thing naming the taper, and leaving it unclaimed lets the
            # venue rule pick the handle up as a place.
            analysis.provenance = from_name
            analysis.provenance_raw = from_name
            analysis.provenance_reason = why
            analysis.add("PROVENANCE_FROM_NAME", SEVERITY_INFO,
                         "the taper line yielded no usable handle; took %r from the "
                         "folder name (%s)" % (from_name, why))
        elif from_name and from_name != analysis.provenance:
            analysis.add("PROVENANCE_DISAGREES", SEVERITY_WARN,
                         "the tags say %r but the folder name says %r; used the tags"
                         % (analysis.provenance, from_name))
    elif not canon_has_provenance:
        # Last resort: a lone unexplained token in a compact etree name is
        # almost always the taper (mmj2021-11-04.leary).
        from_name, why = provenance_from_name(cfg, show.name)
        if not from_name:
            # A broadcast's station identifies the copy the way a taper does.
            from_name, why = broadcast_provenance(
                cfg, [("the folder name", show.name), ("the info file", info_text[:4000])])
        if not from_name and stated.value in ("aud", "mtx"):
            from_name, why = mic_as_provenance(cfg, show.name)
        if from_name:
            analysis.provenance = from_name
            analysis.provenance_raw = from_name
            analysis.provenance_reason = why
            analysis.add("PROVENANCE_FROM_NAME", SEVERITY_INFO,
                         "provenance %r taken from the folder name (%s) - check it "
                         "is a taper and not something else" % (from_name, why))

    # Stated evidence already decided it; only fall back to inference when the
    # folder said nothing at all.
    if stated.value or stated.conflict:
        analysis.source = stated
    else:
        analysis.source = infer_source(
            cfg,
            folder_name=show.name,
            info_text=info_text,
            tag_text=tag_text,
            is_official=(cls.kind == _classify.OFFICIAL),
            provenance_key=prov_key,
            stated_source=(info_file.fields.get("source") if info_file else None),
        )

    # An unexplained word left in a compact name, once the taper and the
    # station have taken what is theirs, is the venue: "MMJ-Wiltern" is a
    # theatre, "Electric_Factory" is a club.
    if not analysis.venue:
        spare = [
            t for t in residual_words(cfg, show.name)
            # not the taper we already took, and not anyone in the tapers table
            if t.lower() != (analysis.provenance or "") and not cfg.canonical_taper(t)
        ]
        if len(spare) == 1:
            analysis.venue = spare[0].strip()
            analysis.add("VENUE_FROM_NAME", SEVERITY_INFO,
                         "venue %r is the one word in the folder name that nothing "
                         "else explains - check it is a place" % analysis.venue)

    analysis.fmt, fmt_warnings = format_token(show.files)
    analysis.quality, quality_warnings = quality_token(show.files)
    for warning in fmt_warnings + quality_warnings:
        analysis.add("FORMAT", SEVERITY_WARN, warning)

    # The gazetteer, after everything that reads this folder and before the
    # overrides that overrule it.  It does two things and will do either alone:
    # give a room its city so the `City, ST` guard stops throwing the venue
    # away, and settle one room written two ways so a box set stops disagreeing
    # with itself between nights.  It never contradicts the folder - where the
    # two differ it withdraws - and a name shared by two rooms is refused
    # outright rather than guessed, because filling in the wrong city moves a
    # show across the country.
    gaz = getattr(cfg, "venues", None)
    if gaz and analysis.venue:
        year = analysis.date.date.year if analysis.date.date else None
        got = gaz.resolve(analysis.venue, analysis.city, analysis.state, year)
        if got:
            was = analysis.venue
            analysis.venue, analysis.city, analysis.state, why = got
            analysis.place_from_gazetteer = True
            analysis.add("VENUE_FROM_GAZETTEER", SEVERITY_INFO, why)
        else:
            shared = gaz.ambiguous(analysis.venue, year)
            if shared and not analysis.city:
                analysis.add(
                    "VENUE_AMBIGUOUS", SEVERITY_INFO,
                    "%d rooms are called %r (%s) and the folder names no city, "
                    "so the gazetteer says nothing"
                    % (len(shared), analysis.venue,
                       "; ".join("%s, %s" % (v.city, v.state) for v in shared)))

    # Last, so it beats everything worked out above, and so the issues raised
    # below describe the folder as it will actually be treated.
    entry = overrides.for_folder(show.name, show.rel) if overrides else None
    if entry is None and overrides is not None:
        # A folder we have already renamed no longer answers to the name the
        # override was written against, so ask under the name it arrived with -
        # and under the path it arrived at, or a key written as a path went
        # dormant after the first rename: "Here Comes Sunshine (1973)/1973-06-10"
        # stopped reaching its folder once that became "1973-06-10 Robert F.
        # Kennedy Stadium, Washington, DC".
        _state = read_state(show.path) or {}
        _original = _state.get("original_folder_name")
        if _original and _original != show.name:
            _here = Path(show.rel).parent
            for _rel in (_state.get("original_relative_path"),
                         str(_here / _original) if str(_here) != "." else _original):
                entry = overrides.for_folder(_original, _rel)
                if entry is not None:
                    break
    if entry is not None:
        apply_override(analysis, entry, cfg)

    _add_issues(analysis, cfg)
    if entry is not None and entry.is_skip:
        analysis.add("SKIPPED_BY_HAND", SEVERITY_BLOCK,
                     "overrides.yaml says to leave this folder alone%s"
                     % ("; " + str(entry.values["note"]).strip()
                        if entry.values.get("note") else ""))
    return analysis


def _note_missing_tracks(a: ShowAnalysis) -> None:
    """Say so when the track numbers have holes in them.

    A folder holding Set1T03, T04, T06, T09 is an incomplete show, and the
    numbering must be kept rather than closed up - renumbering it 1..4 would
    invent a running order the recording does not have.
    """
    groups: dict[tuple, list[int]] = {}
    for f in a.show.files:
        info = f.name_info
        if info.track is None:
            return                      # not every file is numbered; say nothing
        key = ("set", info.set_no) if info.set_no else ("disc", info.disc or 1)
        groups.setdefault(key, []).append(info.track)
    if not groups or len(a.show.files) < 3:
        return

    missing: list[str] = []
    for (kind, number), tracks in sorted(groups.items(), key=lambda kv: str(kv[0])):
        present = sorted(set(tracks))
        gaps = [n for n in range(1, max(present) + 1) if n not in present]
        if gaps:
            missing.append("%s %s is missing %s"
                           % (kind, number, ", ".join(str(g) for g in gaps)))
    if missing:
        a.add("INCOMPLETE_SHOW", SEVERITY_WARN,
              "%s. The numbering is kept as it is, so the gaps stay visible"
              % "; ".join(missing))


def _is_filing_dir(show: ShowFolder) -> str | None:
    """Why this folder is a container rather than a show, or None.

    A folder holding show folders of its own is filing, not a concert.  Loose
    audio sitting beside them is a stray file that was never put away - naming
    the container after it buries every show underneath a single show's name.
    Disc subdirectories are folded into their parent before this runs, so a
    multi-disc show never looks like a container.
    """
    kids = show.child_show_dirs
    if not kids:
        return None
    if is_year_dir(show.name):
        return ("a year folder holding %d show folder%s - it files shows, it is not one"
                % (len(kids), "" if len(kids) == 1 else "s"))
    # Beyond year folders, judge by proportion: a real show is mostly its own
    # tracks.  One or two loose files beside several shows is a stray, but the
    # box-set case (26 tracks of its own plus one misfiled show inside) is a
    # genuine show and must stay one.
    if len(kids) >= 3 and len(show.files) <= 2:
        return ("%d loose file%s beside %d show folders - the loose audio is unfiled, "
                "not this folder's own recording"
                % (len(show.files), "" if len(show.files) == 1 else "s", len(kids)))
    return None


# ARTIST values that name nobody in particular, so they cannot contradict the
# folder the show is filed under.
_NO_ONE = re.compile(
    r"^\s*(unknown( artist)?|various( artists)?|va|artist|none|n/?a|-+|\?+)\s*$",
    re.I)


def _names_another_act(tag_artist: str | None) -> bool:
    return bool(tag_artist and tag_artist.strip() and not _NO_ONE.match(tag_artist))


def _add_issues(a: ShowAnalysis, cfg: Config) -> None:
    show, cls = a.show, a.classification

    newer = (read_state(show.path) or {}).get(NEWER)
    if newer is not None:
        a.add("WRITTEN_BY_NEWER_VERSION", SEVERITY_BLOCK,
              "its .etree_state.json is format %s, from a newer version of jamp "
              "than this one; update jamp before working on this folder" % newer)

    filing = _is_filing_dir(show)
    if filing:
        a.add("CONTAINER_NOT_A_SHOW", SEVERITY_BLOCK,
              "%s. File the loose audio into a show folder of its own, then re-run: %s"
              % (filing, ", ".join(sorted(f.path.name for f in show.files)[:4])))

    if a.band.band is None:
        a.add("NO_BAND", SEVERITY_BLOCK,
              "no band could be resolved" +
              (" (fuzzy suggestions: %s)" % ", ".join(a.band.suggestions) if a.band.suggestions else ""))
    elif not a.band.authoritative and _names_another_act(a.tag_artist):
        # The folder sits under one act and its files name a performer the
        # config does not know.  Filing it by the parent would rewrite ARTIST
        # to the parent act and lose who actually played - a Dickey Betts &
        # Great Southern show under The Allman Brothers Band became an abb
        # show.  A new library is full of these, so it stops here.
        a.add("ACT_NOT_CONFIGURED", SEVERITY_BLOCK,
              "the ARTIST tag says %r, which is no act in the config; it would "
              "be filed as %s only because of the %s. Add the act to your "
              "jamp.yaml (jamp acts), or set band: in overrides.yaml if the "
              "parent is right"
              % (a.tag_artist, a.band.band.name, a.band.evidence))
    elif not a.band.authoritative:
        a.add("BAND_FROM_PARENT", SEVERITY_WARN, a.band.evidence)
    for note in a.band.notes:
        if "side project" in note:
            a.add("SIDE_PROJECT", SEVERITY_INFO, note)

    if a.date.date is None:
        detail = "no date found"
        if a.date.years_only:
            detail += "; year-only evidence %s - needs manual entry" % list(a.date.years_only)

        # An undated folder with a complete, consistent set of official tags and
        # a single release year is a studio album, not a show missing its date.
        # A known live series (Dave's Picks) or a multi-date release is a
        # concert recording that happens to lack a parseable date, not a studio
        # album, however album-like its tags look.
        album_like = (
            cls.kind == _classify.OFFICIAL
            and cls.series is None
            and not cls.multi_date
            and cls.tag_profile.complete_and_consistent
            and len(cls.tag_profile.album_keys) == 1
            and (cls.tag_profile.date_is_year_only or not cls.tag_profile.date_values)
        )
        if album_like or cls.non_show_hits:
            a.category = ALBUM
            title = (cls.tag_profile.album_values[0]
                     if cls.tag_profile.album_values else a.show.name)
            a.add("ALBUM_NOT_A_SHOW", SEVERITY_BLOCK,
                  "looks like a studio or compilation release (%r%s), not a concert - "
                  "no date is forced onto it%s"
                  % (title,
                     ", released %s" % cls.tag_profile.date_values[0]
                     if cls.tag_profile.date_values else "",
                     "; name contains %s" % ", ".join(cls.non_show_hits)
                     if cls.non_show_hits else ""))
        else:
            a.category = UNDATED
            a.add("NO_DATE", SEVERITY_BLOCK, detail)
    else:
        if a.date.confidence < cfg.settings.min_date_confidence_commit:
            a.add("LOW_DATE_CONFIDENCE", SEVERITY_BLOCK,
                  "confidence %d is under the commit threshold %d: %s"
                  % (a.date.confidence, cfg.settings.min_date_confidence_commit,
                     "; ".join(a.date.reasons)))
        if a.date.ambiguous:
            a.add("AMBIGUOUS_DATE", SEVERITY_WARN,
                  "could also be %s" % ", ".join(d.isoformat() for d in a.date.alternatives))
        if a.date.conflicts:
            # A conflict the filenames have already outvoted is worth reporting,
            # not worth stopping for.
            settled = a.date_from_filenames
            a.add("DATE_CONFLICT", SEVERITY_WARN if settled else SEVERITY_BLOCK,
                  "; ".join(a.date.conflicts)
                  + ("; the track filenames agree on %s and are taken as right"
                     % a.date.iso if settled else ""))

    # Prose in an info file mentions all sorts of dates ("released on December
    # 6, 2005"), so only structural evidence - names, cue sheets, tags - can
    # argue that a folder holds several shows.
    structural = {"folder", "child_folder", "parent_folder", "cue", "tracks", "tags_album"}
    structural_dates = {
        cand.date
        for ev in a.date_evidence if ev.source in structural
        for cand in ev.candidates
    }
    # An info file listing several dates is how a box set announces itself, but
    # prose also mentions dates in passing, so it only counts for a series we
    # already know spans nights.
    info_dates = {
        cand.date
        for ev in a.date_evidence if ev.source == "info"
        for cand in ev.candidates
    }
    # A name that writes the dates as a span - "2002.07.18-19 Mishawaka
    # Compilation" - says outright that it holds more than one night, even
    # though only the first date parses and so the count above never reaches
    # three.  Structural text only, for the same reason as structural_dates:
    # prose in an info file mentions ranges it does not contain.
    spanned = None
    for text in [show.name] + ([show.container.name] if show.container else [])             + list(cls.tag_profile.album_values or []):
        spanned = _dates.find_date_range(text)
        if spanned:
            break

    # A show sitting inside a release folder is one night OF a multi-date
    # release, not a multi-date release itself.  The container is the box set.
    _note_missing_tracks(a)

    if show.release_dir is not None:
        a.add("IN_RELEASE_FOLDER", SEVERITY_INFO,
              "one show of the release %r; the release folder is kept"
              % show.release_dir.name)
    # A series that often spans several nights is a hint, not a verdict: some
    # volumes of Dave's Picks are a single show.  Block only when more than one
    # date is actually there.
    elif (len(structural_dates) >= 3
          or spanned is not None
          or (cls.multi_date and len(structural_dates | info_dates) >= 2)):
        a.category = BOXSET
        a.add("MULTI_DATE_RELEASE", SEVERITY_BLOCK,
              "looks like a multi-date release (%s) - one folder cannot be one show"
              % (cls.series.name if cls.multi_date and cls.series else
                 "the name spans %s to %s"
                 % (spanned[0].isoformat(), spanned[1].isoformat()) if spanned else
                 "%d distinct dates in the names, cue sheets and tags"
                 % len(structural_dates)))
    elif cls.multi_date and cls.series:
        a.add("SINGLE_DATE_OF_A_SERIES", SEVERITY_INFO,
              "%r often spans several nights, but only one date was found here, so "
              "it is treated as one show" % cls.series.name)

    if cls.kind == _classify.UNKNOWN:
        a.add("UNKNOWN_ORIGIN", SEVERITY_BLOCK,
              "; ".join(cls.notes) or "not confidently OFFICIAL or UNOFFICIAL")

    if a.source.conflict:
        a.add("SOURCE_CONFLICT", SEVERITY_WARN, a.source.reason)
    if a.source.inferred:
        a.add("SOURCE_INFERRED", SEVERITY_INFO, a.source.reason)

    shn = [f for f in show.files if f.ext == ".shn"]
    if shn:
        a.add("SHN_NO_TAGS", SEVERITY_WARN,
              "%d SHN files: mutagen cannot read or write their tags" % len(shn))
    unreadable = [f for f in show.files if f.tag_support == "unreadable"]
    if unreadable:
        # A file we cannot read is one we cannot tag, and it would keep the
        # folder looking unfinished on every future run.  Say so and stop.
        a.add("UNREADABLE_AUDIO", SEVERITY_BLOCK,
              "%d file(s) cannot be read and so cannot be tagged - %s (%s). Repair or "
              "remove them and re-run"
              % (len(unreadable), unreadable[0].path.name, unreadable[0].error))

    if not a.info.chosen:
        a.add("NO_INFO_FILE", SEVERITY_INFO,
              "no usable info file among %d text files" % len(show.texts))
    elif len(a.info.considered) > 1:
        others = sorted((s, p.name) for p, s in a.info.considered
                        if p != a.info.chosen.path)
        a.add("INFO_FILE_CHOICE", SEVERITY_INFO,
              "used %s (%.0f); ignored %s"
              % (a.info.chosen.path.name, a.info.chosen.score,
                 ", ".join("%s (%.0f)" % (n, s) for s, n in others[:4])))
    if a.info.torrent_markers:
        a.add("TORRENT_MARKER", SEVERITY_INFO,
              "torrent marker file: %s" % a.info.torrent_markers[0].name)

    for path in show.long_paths:
        a.add("LONG_PATH", SEVERITY_WARN,
              "%d characters, over the %d limit: %s"
              % (len(path), cfg.settings.max_path_length, path))

    if show.container is not None:
        a.add("NESTED_CONTAINER", SEVERITY_WARN,
              "the show sits inside %r, which holds no audio of its own and keeps "
              "its name; you may want to flatten it by hand" % show.container.name)

    if not show.files:
        a.add("NO_AUDIO", SEVERITY_BLOCK, "no audio files")
