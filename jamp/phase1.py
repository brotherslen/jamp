"""Phase 1 - the dry-run engine.

Produces, for every show folder, the complete proposed end state: the folder
name, every track name, every tag value, and the rewritten checksum and cue
files.  It writes CSVs and a JSON plan and touches nothing else.  There is no
code path in this module that renames, moves, deletes or tags anything.
"""
from __future__ import annotations

import datetime as _dt
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import classify as _classify
from .analyze import (
    place_is_whole_string,
    SEVERITY_BLOCK,
    SEVERITY_INFO,
    SEVERITY_WARN,
    ShowAnalysis,
    analyze_show,
    strip_product_words,
    _normalise_field_seps,
)
from .audio import AudioFile
from .config import Config
from .dupes import LOSSLESS_EXTS, LOSSY_EXTS, LossyFinding, find_in_folder
from .infofile import InfoTrack
from .naming import (
    build_album_tag,
    build_folder_name,
    build_release_show_name,
    build_track_name,
    check_path_length,
    NameProposal,
    make_location,
    parse_canonical,
    split_location,
    strip_set_marker,
    strip_audio_spec,
    strip_trailing_source,
)
from .report import TextReport, ensure_out_dir, run_lock, write_csv, write_json
from .scan import scan
from .sidecars import plan_sidecar, propose_sidecar_name
from .state import STATE_NAME, is_settled, read_state

PLAN = "PLAN"
UNCHANGED = "UNCHANGED"
SKIP_BLOCKED = "SKIP_BLOCKED"
COLLISION = "COLLISION"
SPLIT_SHOW = "SPLIT_SHOW"
MERGE = "MERGE"
TWO_SHOWS = "TWO_SHOWS"
DUPLICATE = "DUPLICATE"

# early and late are written into the name straight after the date.
_MARKER_CANON = {
    "early": "early", "1st": "early", "first": "early",
    "1stshow": "early", "firstshow": "early", "matinee": "early",
    "afternoon": "early",
    "late": "late", "2nd": "late", "second": "late",
    "2ndshow": "late", "secondshow": "late", "evening": "late",
}

# Two separate performances on one date - overwhelmingly a Jerry Garcia Band
# iteration playing an early and a late show.  Not a split show, and not a
# duplicate: two different concerts that the naming scheme has no field for.
_SHOW_MARKER = re.compile(
    # early/late and friends can stand alone; a bare ordinal cannot, or
    # "ph2018-12-28 second copy" reads as the second show of the night.
    r"(?:^|[\s._(-])(early|late|afternoon|evening|matinee"
    r"|(?:1st|2nd|first|second)\s*(?:show|set))(?:[\s._)-]|$)", re.I
)


def show_marker(folder_name: str) -> str | None:
    """'early' or 'late' for a folder that says which performance it is."""
    m = _SHOW_MARKER.search(folder_name)
    if not m:
        return None
    raw = re.sub(r"[^a-z0-9]", "", m.group(1).lower())
    return _MARKER_CANON.get(raw)


def group_stem(folder_name: str) -> str:
    """The folder name with any set/disc suffix and early/late marker removed.

    Sibling folders sharing a stem are candidates for being one show - either
    split in two, or two performances on one night.
    """
    stem = _SET_SUFFIX.sub("", _set_suffix_body(folder_name))
    stem = _SHOW_MARKER.sub(" ", stem)
    return re.sub(r"[\s._-]+", " ", stem).strip().lower()

# A trailing set/disc/part marker on a folder name: "gd1973-12-10 s1".
_SET_SUFFIX = re.compile(
    r"(?:[\s._-]+|(?<=\d))(set|part|s|d|disc|disk|cd)\s*(\d{1,2})\s*$", re.I)

# What that marker says the split represents.
_SUFFIX_KIND = {"set": "s", "s": "s", "part": "s", "d": "d", "disc": "d", "disk": "d", "cd": "d"}


# A format or quality note in brackets sits after the set marker often enough
# to matter: "... Soldier Field Chicago IL Set 1 (flac16)".  Without dropping it
# the two sets of one show never match, and the pair is reported as two
# different shows that happen to resolve to the same name.
_TRAILING_BRACKET = re.compile(r"\s*[\[({][^\])}]*[\])}]\s*$")


# A format or quality note tacked on the end, dotted rather than bracketed:
# "ph2012-06-07s1.mp3".  Same problem as the bracketed form - it hides the set
# marker, so the two halves of one show never match.
# Every token here is a format WORD.  Bare numbers are deliberately excluded:
# stripping "16" would reduce "Dave's Picks 16" to "Dave's Picks", giving two
# different volumes the same group stem.
_TRAILING_FORMAT = re.compile(
    r"[\s._-](?:mp3|flac|shn|shnf|ape|wav|aiff|m4a|v0|v2"
    r"|flac16|flac24|flac2496|flac2448|flac1644)\s*$", re.I)


def _set_suffix_body(folder_name: str) -> str:
    """The name with trailing format and bracket noise peeled off."""
    prev = None
    text = folder_name
    while text != prev:
        prev = text
        text = _TRAILING_BRACKET.sub("", text).rstrip()
        text = _TRAILING_FORMAT.sub("", text).rstrip()
    return text


def split_suffix(folder_name: str) -> tuple[str, int] | None:
    """('s', 2) for 'gd1973-12-10 s2', ('d', 1) for 'Show disc 1', else None."""
    m = _SET_SUFFIX.search(_set_suffix_body(folder_name))
    if not m:
        return None
    kind = _SUFFIX_KIND.get(m.group(1).lower())
    return (kind, int(m.group(2))) if kind else None
_LEADING_DATE = re.compile(r"^\s*(?:(?:19|20)\d{2}[-._/ ]\d{1,2}[-._/ ]\d{1,2}|\d{1,2}[-._/ ]\d{1,2}[-._/ ]\d{2,4})\s*[:\-]?\s*")


@dataclass
class TrackPlan:
    file: AudioFile
    old_name: str
    new_name: str
    kind: str                       # s or d
    number: int
    track: int
    title: str | None = None
    title_source: str = ""
    numbering_source: str = ""
    tags: dict[str, tuple[str, str]] = field(default_factory=dict)  # field -> (current, proposed)
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.old_name != self.new_name or any(
            cur != new for cur, new in self.tags.values()
        )


@dataclass
class ShowPlan:
    analysis: ShowAnalysis
    status: str = SKIP_BLOCKED
    new_folder_name: str | None = None
    tracks: list[TrackPlan] = field(default_factory=list)
    sidecars: list = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Set when this folder is one half of a show split across siblings.
    merge_target: Path | None = None
    merge_with: list[str] = field(default_factory=list)
    merge_role: str = ""            # primary / member
    # The official release folder this show sits in, kept as-is.
    release_folder: str | None = None
    # MP3 that duplicates a lossless copy of the same recording.
    lossy: LossyFinding = field(default_factory=LossyFinding)

    @property
    def show(self):
        return self.analysis.show

    @property
    def old_folder_name(self) -> str:
        return self.analysis.show.name

    @property
    def new_path(self) -> Path | None:
        if not self.new_folder_name:
            return None
        return self.show.path.parent / self.new_folder_name

    @property
    def folder_changed(self) -> bool:
        return bool(self.new_folder_name) and self.new_folder_name != self.old_folder_name


# ---------------------------------------------------------------------------
# numbering and titles
# ---------------------------------------------------------------------------

def _natural_sort_key(text: str) -> list:
    """Sort "s2t04" before "s102t01" - plain string order gets this backwards,
    since '0' < 't' puts "s102..." ahead of "s1..." and "s2...".  Splitting into
    alternating text/digit runs and comparing digit runs as integers is what a
    person means by "in order": track9 before track10, set2 before set102.
    """
    return [int(chunk) if chunk.isdigit() else chunk
            for chunk in re.split(r"(\d+)", text.lower())]


def _int_tag(f: AudioFile, key: str) -> int | None:
    raw = f.tag(key) or ""
    m = re.match(r"\s*(\d{1,3})", raw)
    return int(m.group(1)) if m else None


def _setlist_index(tracks: list[InfoTrack]) -> dict[tuple, InfoTrack]:
    """Setlist entries keyed by how a track will be numbered.

    Sets and discs are indexed separately.  Keying only by disc meant a folder
    numbered by SET looked its titles up under disc 1, so every track of set 2
    was given the title of the set 1 track with the same number - s2t01 came
    out as "Frankenttein" when it is "Wilson >".
    """
    out: dict[tuple, InfoTrack] = {}
    for t in tracks:
        if t.number is None:
            continue
        if t.set_no is not None:
            out.setdefault(("s", t.set_no, t.number), t)
        if t.disc is not None:
            out.setdefault(("d", t.disc, t.number), t)
        out.setdefault((None, t.number), t)
    return out


# The one numbering that is a guess rather than evidence.  Named, because a
# warning hangs off it: matching the label as text meant rewording it would
# silently switch that warning off.
ASSUMED_ORDER = "sorted order (assumed)"


def plan_numbering(a: ShowAnalysis) -> list[tuple[AudioFile, str, int, int, str]]:
    """Decide (kind, number, track) for every file.

    's' is only used when the set really is known - from sNtNN filenames or a
    setlist that maps cleanly onto every file.  Otherwise 'd'.
    """
    # A folder we have already renamed carries our own sNtNN filenames, and an
    # encore's "+100" set number ("s102t01") sorts BEFORE "s1t01"/"s2t04" under
    # plain string order - misaligning every file against the setlist by one
    # and producing a closed rename cycle (s102t01 -> s1t01 -> s1t02 -> ... ->
    # s102t01) that collides on its very first step.  This is what corrupted a
    # real Constitution Hall commit on a --reclassify pass.
    files = sorted(a.show.files, key=lambda f: _natural_sort_key(str(f.path)))
    official = a.classification.kind == _classify.OFFICIAL

    if official and all(_int_tag(f, "TRACKNUMBER") for f in files):
        out = []
        for f in files:
            # No DISCNUMBER but the filename says d1 / d2: believe the filename.
            # Otherwise every disc collapses onto disc 1 and disc 2 track 1
            # overwrites disc 1 track 1.
            disc = _int_tag(f, "DISCNUMBER") or f.name_info.disc or 1
            source = "tags" if _int_tag(f, "DISCNUMBER") else "tags, disc from filename"
            out.append((f, "d", disc, _int_tag(f, "TRACKNUMBER"), source))
        # A numbering that puts two files in one slot is not a numbering.  Real
        # rips carry this: "20 Around And Around" and "21 One More Saturday
        # Night" are both tagged track 21.  The filenames beside them are
        # sequential and unique, so fall through and use those instead.
        slots = [(d, n) for _, _, d, n, _ in out]
        if len(set(slots)) == len(slots):
            return out

    if all(f.name_info.set_no and f.name_info.track is not None for f in files):
        return [(f, "s", f.name_info.set_no, f.name_info.track, "filename sNtNN")
                for f in files]

    if all(f.name_info.disc and f.name_info.track is not None for f in files):
        return [(f, "d", f.name_info.disc, f.name_info.track, "filename dNtNN")
                for f in files]

    info = a.info_file
    if info and info.tracks and len(info.tracks) == len(files):
        sets = {t.set_no for t in info.tracks if t.set_no}
        if sets and all(t.set_no for t in info.tracks):
            counters: dict[int, int] = defaultdict(int)
            out = []
            for f, t in zip(files, info.tracks):
                counters[t.set_no] += 1
                out.append((f, "s", t.set_no, counters[t.set_no], "info file setlist"))
            return out

    if all(f.name_info.track is not None for f in files):
        return [(f, "d", f.name_info.disc or 1, f.name_info.track, "filename track number")
                for f in files]

    # Last resort: sorted order.  Flagged wherever it is used.
    return [(f, "d", 1, i, ASSUMED_ORDER) for i, f in enumerate(files, start=1)]


def choose_title(a: ShowAnalysis, f: AudioFile, kind: str, number: int,
                 track: int, groups: int = 1) -> tuple[str | None, str]:
    """The title for one track.

    `groups` is how many discs or sets the folder's numbering uses.  A setlist
    entry found by its track number alone says nothing about which disc it is
    on, so it only answers for a folder with one.
    """
    official = a.classification.kind == _classify.OFFICIAL
    existing = (f.tag("TITLE") or "").strip()

    if official and existing:
        return existing, "existing tag (official, left alone)"

    info = a.info_file
    index = _setlist_index(info.tracks) if info and info.tracks else {}
    # An encore is numbered as its own set in the prose ("Encore:") but
    # still filed under the last set in the filenames, so a set lookup
    # falls back to the disc before giving up.
    hit = index.get((kind, number, track)) or index.get(("d", number, track))
    if hit:
        # The folder's own setlist is the authority on what a track is called,
        # wording included - your rule, 2026-09-13. Its lines are cleaned of
        # times, stray slashes and footnote marks as they are parsed, so what
        # wins is the setlist's name for the song, not its damage.
        return hit.title, "info file setlist"

    if existing:
        return existing, "existing tag"
    # By number alone, and only as a fill.  In a folder numbered d1/d2 against
    # a setlist headed "Set I"/"Set II", disc 2 track 1 found set 1 track 1 this
    # way - and, being ranked above the existing tag, replaced a correct TITLE
    # with the wrong song.  With one disc there is nothing to confuse it with.
    hit = index.get((None, track)) if groups == 1 else None
    if hit:
        return hit.title, "info file setlist, by track number"
    if f.name_info.title:
        return f.name_info.title, "track filename"
    return None, "none found"


# Any date, anywhere in a string - used to split a box set's ALBUM tag.
_ANY_DATE = re.compile(
    r"(?:(?:19|20)\d{2}[-._/ ]\d{1,2}[-._/ ]\d{1,2}"
    r"|\d{1,2}[-._/]\d{1,2}[-._/]\d{2,4})")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _album_place(a: ShowAnalysis,
                 release_name: str | None = None,
                 ) -> tuple[str | None, str | None, str | None, str | None]:
    """(venue, city, state, verbatim) - verbatim wins for official releases."""
    # An override beats every other kind of evidence, and both routes below
    # would otherwise throw it away: an official release takes its ALBUM tag
    # verbatim without ever reading these fields, and the fallback prefers an
    # info file's venue over them.  So a hand-stated place on a store download
    # was applied to the analysis, reported as OVERRIDDEN, and then silently
    # dropped from the name it was written to correct.
    # The gazetteer takes the same route for the same reason. It does not
    # contradict an ALBUM tag - it withdraws whenever the two disagree - so
    # what reaches here only ever adds the city to a room the tag already
    # named, or settles which of two spellings of it this library uses.
    if a.place_by_hand or a.place_from_gazetteer:
        return a.venue, a.city, a.state, None
    cls = a.classification
    if cls.kind == _classify.OFFICIAL and cls.tag_profile.album_values:
        album = cls.tag_profile.album_values[0]
        if release_name:
            # Inside a box set the ALBUM tag is the product string for the whole
            # set, with this show written into it:
            #   "July '78 - 1978-07-01 Arrowhead Stadium, Kansas City, MO".
            # The release title comes before the date and the show after it, so
            # the tail is the place and the head is the name of the box - which
            # the parent folder already carries and must not be repeated here.
            # Drop our own " [Release]" suffix first.  It can carry a date of
            # its own, and slicing from the last date in the string would then
            # cut into the middle of the bracket and leave the closing "]"
            # hanging on the end of the venue - the name growing a stray
            # bracket on every run.
            album = re.sub(r"\s*\[[^\]]*\]\s*$", "", album)
            spans = [m.span() for m in _ANY_DATE.finditer(album)]
            tail = album[spans[-1][1]:] if spans else album
            tail = strip_product_words(
                re.sub(r"\s*[\[(][^\])]*[\])]\s*$", "", tail))
            if not tail or _norm(tail) in _norm(release_name):
                # Either nothing followed the date, or what did is the box set
                # over again ("June 1976" inside "June 1976 (2020)").  Neither
                # is a place; let the child folder's own name speak instead.
                return None, None, None, None
            return None, None, None, tail
        # Keep the official venue text exactly as shipped; only drop a leading
        # date, so the date is not written twice, and a set marker, which is
        # not part of the place.
        # An official release's ALBUM is kept as shipped, but a lineage tacked
        # on the end of it is still not part of the venue's name: the nugs.net
        # downloads tag "2007-10-23 - Legend's sbd".
        # "2012.01.21 :: Congress Theater :: Chicago, IL" - the store's field
        # separator has to become a comma before anything else reads this text.
        # The colons are illegal in a folder name and were simply dropped, which
        # ran the room into the city: "Congress Theater Chicago, IL".
        album = _normalise_field_seps(album) or album
        text = strip_trailing_source(strip_set_marker(_LEADING_DATE.sub("", album)))
        # An official ALBUM often carries the transfer spec - nugs shipped
        # "KSU MAC Center (16/44.1)" - and "/" cannot survive in a folder name,
        # so the venue was becoming "KSU MAC Center (1644.1)".
        text = strip_audio_spec(text) or text
        # We ourselves append " [Release]" to ALBUM.  Reading it back as part
        # of the venue would grow the name a little more on every run.
        text = re.sub(r"\s*\[[^\]]*\]\s*$", "", text)
        return None, None, None, text.strip(" -:")
    # Take each part from whichever source actually has it.  An info file that
    # names a city but no venue must not shut out the venue sitting in the
    # folder name.
    info = a.info_file
    venue = (info.venue if info else None) or a.venue
    city = (info.city if info else None) or a.city
    state = (info.state if info else None) or a.state
    # A venue that already carries its own "City, ST" is a whole place string,
    # not the name of a room, so a city from another source is that same city
    # again - and when the spellings differ, dedupe cannot see it.
    if place_is_whole_string(venue):
        city = state = None
    return venue, city, state, None


def _fit_to_path_limit(plan: ShowPlan, proposal, a: ShowAnalysis, cfg: Config) -> str:
    """Trim the venue out of a folder name that would make paths too long.

    The venue is the optional half of the name and it is kept in the VENUE tag
    regardless, so it is the right thing to give up.  Budgeted against the
    longest track filename, since that is what actually has to fit.
    """
    name = proposal.name
    machine, location = split_location(name)
    if not location:
        return name

    longest_track = max((len(t) for t in (
        "%s%s%s%d%s" % (a.band.abbrev, a.date.iso, "d", 9, "t999.flac"),
    )), default=40)
    budget = len(str(plan.show.path.parent)) + 1 + len(name) + 1 + longest_track
    if budget <= cfg.settings.max_path_length:
        return name

    plan.warnings.append(
        "the venue %r is left out of the folder name: with it the longest track "
        "path would be %d characters, over the %d limit. It is still written to "
        "the VENUE tag" % (location, budget, cfg.settings.max_path_length)
    )
    return machine


def release_tail(release: str | None) -> str | None:
    """The release title, fit to stand at the end of a folder name.

    An official release's own name is worth keeping - it is what the record is
    called - but the date inside it is written by the folder already, so
    "Download Series Vol. 08: 1973-12-10" contributes "Download Series Vol. 08".
    """
    if not release:
        return None
    text = _ANY_DATE.sub(" ", release)
    text = re.sub(r"\s*[\[(][^\])]*[\])]\s*$", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" -:,.")
    return text or None


def folder_location(cfg: Config, venue, city, state, verbatim, release=None) -> str | None:
    """The human-readable tail of the folder name, per the config policy.

    `verbatim` is an official release's own ALBUM text, used as-is rather than
    taken apart - unless it turns out to be the release title rather than a
    place, as "Download Series Vol. 08" is.
    """
    policy = cfg.settings.folder_location
    if policy == "off":
        return None
    limit = cfg.settings.folder_location_max
    if verbatim:
        location = make_location(verbatim, limit=limit)
        if location and release:
            flat = re.sub(r"[^a-z0-9]", "", location.lower())
            if flat and re.sub(r"[^a-z0-9]", "", release.lower()).startswith(flat):
                return None      # it is the release name, not a venue
        return location
    if policy == "venue_only" and not venue:
        return None
    return make_location(venue, city, state, limit=limit)


def _release_name(a: ShowAnalysis) -> str | None:
    cls = a.classification
    if not cls.series:
        return None
    # We write the release into its own RELEASE tag, so on a second run read it
    # back from there.  Re-deriving it from ALBUM would pick up the "[Release]"
    # suffix we appended and nest it a little deeper every time.
    for f in a.show.files[:4]:
        existing = (f.tag("RELEASE") or "").strip()
        if existing:
            return existing
    album = cls.tag_profile.album_values[0] if cls.tag_profile.album_values else None
    if album:
        album = re.sub(r"\s*\[[^\]]*\]\s*$", "", album).strip()
    return album or cls.series.name


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------

def plan_show(a: ShowAnalysis, cfg: Config, reclassify: bool = False) -> ShowPlan:
    plan = ShowPlan(analysis=a)

    # Already committed under this exact name: leave it alone.  Re-deriving the
    # answer from tags we ourselves wrote can reach a different conclusion.
    if not reclassify and is_settled(a.show.path):
        plan.status = UNCHANGED
        plan.new_folder_name = a.show.name
        plan.reasons.append(
            "settled by a previous commit (%s); pass --reclassify to think again"
            % STATE_NAME
        )
        return plan

    if a.blocked:
        plan.status = SKIP_BLOCKED
        plan.reasons = ["%s: %s" % (i.code, i.detail) for i in a.issues
                        if i.severity == SEVERITY_BLOCK]
        return plan

    band = a.band.band
    assert band is not None and a.date.date is not None  # guaranteed by a.blocked

    release = _release_name(a)
    venue, city, state, verbatim = _album_place(
        a, a.show.release_dir.name if a.show.release_dir is not None else None)
    # show_marker() scans the whole folder name and cannot tell a real
    # "early"/"late" marker from the same word inside a venue's own text.  On a
    # folder we already renamed, our own venue suffix can carry it - "Convention
    # Hall Early Show" - and re-deriving would then add a marker that was never
    # there.  parse_canonical only ever reads the marker from the machine part,
    # so on our own name it is trusted outright, including when it found none.
    _canon_for_marker = parse_canonical(a.show.name)
    if a.marker_by_hand:
        marker = a.marker_by_hand
    elif _canon_for_marker is not None and _canon_for_marker.fmt:
        marker = _canon_for_marker.marker
    else:
        marker = show_marker(a.show.name)

    if a.classification.multi_date and a.show.release_dir is None:
        # A multi-date official series match - "Pure Jerry", "Dick's Picks",
        # "Road Trips" - means the release as a whole is not one show under one
        # date; the config's own multi_date flag says exactly that.  A folder
        # that happens to carry one clean, parseable date is not therefore an
        # ordinary single show, and renaming it away from its release identity
        # ("Pure Jerry #4 Garcia Merl Saunders Band - Keystone Berkeley, 9.1.74")
        # into the machine scheme was losing that identity on installments
        # whose date happened to be extractable, while installments with no
        # clean date kept it by accident.  This is the same rule
        # a.show.release_dir already applies to a box set's own container
        # folder, for a release that is just one folder rather than several.
        # Only consult state's previous_folder_name when the CURRENT name is
        # one this pipeline generated (it parses as our canonical form) - that
        # is a folder already renamed once, whose true original lives only in
        # .etree_state.json now.  A current name that does NOT parse that way
        # is already a real release name - "Pure Jerry #4 ..." - and must be
        # trusted as-is.  Without this check, previous_folder_name after a
        # revert records the machine name from JUST BEFORE the revert, and the
        # next --reclassify chases that straight back to it: the folder
        # oscillates between its two names on alternating runs.
        _canon_now = parse_canonical(a.show.name)
        if _canon_now is not None and _canon_now.fmt:
            _state = read_state(a.show.path) or {}
            original_name = _state.get("previous_folder_name") or a.show.name
        else:
            original_name = a.show.name
        proposal = NameProposal(name=original_name)
        plan.reasons.append(
            "%r is a multi-date official series - the folder keeps its own "
            "name; only its tags and track names are set from the one date "
            "found" % (a.classification.series.name
                      if a.classification.series else "series"))
    elif a.show.release_dir is not None:
        # Inside an official release folder the band and source are already
        # settled by the release, so the show reads as a date and a place.
        # The album tag is often the release title rather than a venue inside a
        # box set, so it is filtered the same way and the folder name is the
        # fallback.
        place = folder_location(cfg, venue, city, state, verbatim, release)
        if not place:
            place = make_location(a.venue, a.city, a.state,
                                  limit=cfg.settings.folder_location_max)
        proposal = build_release_show_name(
            a.date.date, venue=place, marker=marker,
            limit=cfg.settings.folder_location_max + 20,
        )
        plan.release_folder = a.show.release_dir.name
    else:
        proposal = build_folder_name(
            band=band.abbrev,
            date=a.date.date,
            source=a.source.value,
            provenance=a.provenance,
            fmt=a.fmt,
            marker=marker,
            location=(folder_location(cfg, venue, city, state, verbatim, release)
                      # The ALBUM text was the release title, not a place.  The
                      # title is still the best thing to call this folder, so
                      # keep it rather than ending up with a bare date.
                      or release_tail(release)),
        )
    # The venue is a nicety in the folder name and a certainty in the tags, so
    # if carrying it would push the longest track path near the Windows limit,
    # drop it from the name and keep it in the tag.
    plan.new_folder_name = _fit_to_path_limit(plan, proposal, a, cfg)
    if marker:
        plan.warnings.append(
            "named as the %r show of %s - check that is right, since 'early' and "
            "'late' can appear in a venue name too" % (marker, a.date.iso)
        )
    plan.warnings.extend(proposal.warnings)
    for dropped in proposal.dropped:
        plan.warnings.append("dropped from the name: %s" % dropped)

    if verbatim is not None:
        album = build_album_tag(a.date.date, venue=verbatim, release=release)
    else:
        album = build_album_tag(a.date.date, venue=venue, city=city, state=state,
                                release=release)

    numbering = plan_numbering(a)
    groups = len({(kind, number) for _, kind, number, _, _ in numbering})
    for f, kind, number, track, numbering_source in numbering:
        name_proposal = build_track_name(
            band=band.abbrev, date=a.date.date, number=number,
            track=track, ext=f.ext, kind=kind, marker=marker,
            suffix=f.name_info.suffix,
        )
        title, title_source = choose_title(a, f, kind, number, track, groups=groups)
        tp = TrackPlan(
            file=f, old_name=f.name, new_name=name_proposal.name,
            kind=kind, number=number, track=track,
            title=title, title_source=title_source, numbering_source=numbering_source,
            warnings=list(name_proposal.warnings),
        )
        if numbering_source == ASSUMED_ORDER:
            tp.warnings.append("track order assumed from sorted filenames")
        if title is None:
            tp.warnings.append("no title available from tags, info file or filename")

        if f.tag_support == "none":
            tp.warnings.append("tags cannot be written to this file (%s)" % f.ext)
        else:
            proposed = {
                "ARTIST": band.name,
                "ALBUMARTIST": band.name,
                "ALBUM": album,
                "DATE": a.date.date.isoformat(),
                "TRACKNUMBER": str(track),
                "DISCNUMBER": str(number),
            }
            existing_genre = (f.tag("GENRE") or "").strip()
            keep_genre = (
                cfg.settings.preserve_official_genre
                and a.classification.kind == _classify.OFFICIAL
                and bool(existing_genre)
            )
            if not keep_genre:
                proposed["GENRE"] = cfg.settings.genre
            if title:
                proposed["TITLE"] = title
            if release:
                proposed["RELEASE"] = release
            # The venue goes in a tag whether or not it fits in the folder name.
            place_tag = make_location(venue or a.venue, city or a.city, state or a.state,
                                      limit=120) if (venue or a.venue) else None
            if place_tag:
                proposed["VENUE"] = place_tag
            if a.source.inferred and a.source.tag_value:
                proposed["SOURCE_CONFIDENCE"] = a.source.tag_value
            for key, value in proposed.items():
                current = (f.tag(key) or "").strip()
                if current != value:
                    tp.tags[key] = (current, value)

        long_path = check_path_length(
            (plan.new_path or a.show.path) / tp.new_name, cfg.settings.max_path_length
        )
        if long_path:
            tp.warnings.append(long_path)
        plan.tracks.append(tp)

    # Two tracks resolving to one filename would silently destroy one of them.
    # The source data really does contain this: a folder with two files both
    # tagged disc 3 track 1.  Report it; rename nothing.
    claimed: dict[str, list[str]] = defaultdict(list)
    for t in plan.tracks:
        claimed[t.new_name.lower()].append(t.old_name)
    clashes = {name: olds for name, olds in claimed.items() if len(olds) > 1}
    if clashes:
        plan.status = SKIP_BLOCKED
        for name, olds in sorted(clashes.items()):
            plan.reasons.append(
                "%d files would become %r (%s) - they carry the same disc and track "
                "number, so one would overwrite the other. Fix the numbering and "
                "re-run" % (len(olds), name, ", ".join(sorted(olds)))
            )
        return plan

    # Every track goes in the map, including the ones whose name does not
    # change - otherwise an already-correct filename looks like a missing
    # reference and the whole checksum file gets flagged.
    for kind, paths in sorted(a.show.sidecars.items()):
        for path in paths:
            sp = plan_sidecar(path, rename_map_for(path, plan.tracks))
            sp.new_name = propose_sidecar_name(path, a.show.name, plan.new_folder_name)
            plan.sidecars.append(sp)

    changed = plan.folder_changed or any(t.changed for t in plan.tracks) or any(
        s.status == "rewrite" for s in plan.sidecars
    )
    plan.status = PLAN if changed else UNCHANGED
    if not changed:
        plan.reasons.append("already matches the scheme; a second run changes nothing")
    return plan


def rename_map_for(sidecar: Path, tracks: list[TrackPlan]) -> dict[str, str]:
    """Old name -> new name, as a checksum or cue file at `sidecar` refers to them.

    Every track goes in, including the ones whose name does not change -
    otherwise an already-correct filename looks like a missing reference and the
    whole checksum file gets flagged.

    Keyed by bare filename it was one map for the whole show, and disc folders
    repeat names: "Disc 1/01.flac" and "Disc 2/01.flac" collapsed to one entry,
    and a disc 1 checksum line could be pointed at disc 2's new name.  A track
    beside the sidecar answers to its bare name; one elsewhere answers to its
    path from the sidecar, and to its bare name only when no other track shares
    it.
    """
    here = sidecar.parent
    counts = defaultdict(int)
    for t in tracks:
        counts[t.old_name.lower()] += 1
    out: dict[str, str] = {}
    for t in tracks:
        if t.file.path.parent == here:
            out[t.old_name] = t.new_name
            continue
        try:
            out[t.file.path.relative_to(here).as_posix()] = t.new_name
        except ValueError:
            pass
        if counts[t.old_name.lower()] == 1:
            out.setdefault(t.old_name, t.new_name)
    return out


def _agree(values) -> tuple[object | None, bool]:
    """One distinct non-empty value means agreement; two or more means we drop it."""
    distinct = {v for v in values if v}
    if len(distinct) == 1:
        return distinct.pop(), True
    return None, not distinct


def merge_split_show(group: list[ShowPlan], cfg: Config) -> None:
    """Plan two sibling folders holding one show as a single merged folder.

    'gd1973-12-10 s1' and 's2' become one folder whose tracks are s1t01.. and
    s2t01...  Box sets are not affected: merging requires the same band AND the
    same date, and a box set spans several dates.
    """
    group.sort(key=lambda p: p.old_folder_name.lower())
    suffixes = {id(p): split_suffix(p.old_folder_name) for p in group}

    def bail(reason: str) -> None:
        for p in group:
            p.status = SPLIT_SHOW
            p.merge_target = None
            p.reasons.append(
                "one show split across sibling folders (%s), but %s - reported "
                "rather than merged"
                % (", ".join(q.old_folder_name for q in group if q is not p), reason)
            )

    # Siblings whose stems match only because the format was stripped are the
    # same recording twice, not two halves of one show.  "...set2.13831.shnf"
    # and "...set2.13831.shnf.FLAC" are one set in SHN and in FLAC, and both
    # say set2 - a split needs different markers, not the same one twice.
    # Merging them would interleave two formats into one folder.
    formats = [p.analysis.fmt for p in group]
    if len(set(formats)) == len(formats) and all(formats):
        for p in group:
            # Status stays what plan_show decided.  Setting PLAN here turned a
            # settled folder, or one already correctly named, into work that
            # does nothing: phase 2 rewrote its state file on every run,
            # "remaining" never reached 0, and --until-settled spent its passes.
            p.warnings.append(
                "sits beside %s, which is the same show in a different format, "
                "not the other half of it; each is named on its own"
                % ", ".join(q.old_folder_name for q in group if q is not p)
            )
        return

    if any(suffixes[id(p)] is None for p in group):
        bail("at least one folder has no set or disc marker in its name")
        return

    kinds = {suffixes[id(p)][0] for p in group}
    if len(kinds) > 1:
        bail("the folders disagree about whether they are sets or discs")
        return
    kind = kinds.pop()
    if len({suffixes[id(p)][1] for p in group}) != len(group):
        bail("two folders claim the same set or disc number")
        return

    primary = group[0]
    a = primary.analysis
    source, source_ok = _agree([p.analysis.source.value for p in group])
    provenance, prov_ok = _agree([p.analysis.provenance for p in group])
    fmt, fmt_ok = _agree([p.analysis.fmt for p in group])

    location, _ = _agree([split_location(p.new_folder_name or "")[1] for p in group])
    proposal = build_folder_name(
        band=a.band.band.abbrev, date=a.date.date,
        source=source, provenance=provenance, fmt=fmt,
        marker=show_marker(primary.old_folder_name), location=location,
    )
    target = primary.show.path.parent / proposal.name

    # Renumber every track under its own set/disc number before committing to
    # the merge, so a clash is caught while backing out is still free.
    proposed: list[tuple[ShowPlan, list[tuple[TrackPlan, str, int]]]] = []
    seen: dict[str, str] = {}
    for p in group:
        _, number = suffixes[id(p)]
        renamed: list[tuple[TrackPlan, str, int]] = []
        ordered = sorted(p.tracks, key=lambda t: (t.number, t.track, t.old_name.lower()))
        for i, t in enumerate(ordered, start=1):
            name = build_track_name(
                band=a.band.band.abbrev, date=a.date.date,
                number=number, track=i, ext=t.file.ext, kind=kind,
            ).name
            if name in seen:
                bail("merging would give two files the same name (%s)" % name)
                return
            seen[name] = p.old_folder_name
            renamed.append((t, name, i))
        proposed.append((p, renamed))

    for p, renamed in proposed:
        _, number = suffixes[id(p)]
        for t, name, index in renamed:
            t.new_name = name
            t.kind = kind
            t.number = number
            t.track = index
            if "DISCNUMBER" in t.tags:
                t.tags["DISCNUMBER"] = (t.tags["DISCNUMBER"][0], str(number))
            elif (t.file.tag("DISCNUMBER") or "").strip() != str(number):
                t.tags["DISCNUMBER"] = ((t.file.tag("DISCNUMBER") or ""), str(number))
            if "TRACKNUMBER" in t.tags:
                t.tags["TRACKNUMBER"] = (t.tags["TRACKNUMBER"][0], str(t.track))
            elif (t.file.tag("TRACKNUMBER") or "").strip() != str(t.track):
                t.tags["TRACKNUMBER"] = ((t.file.tag("TRACKNUMBER") or ""), str(t.track))

        for sidecar in p.sidecars:
            refreshed = plan_sidecar(sidecar.path, rename_map_for(sidecar.path, p.tracks))
            sidecar.status = refreshed.status
            sidecar.new_text = refreshed.new_text
            sidecar.referenced = refreshed.referenced
            sidecar.unresolved = refreshed.unresolved
            sidecar.notes = refreshed.notes

        p.status = MERGE
        p.new_folder_name = proposal.name
        p.merge_target = target
        p.merge_role = "primary" if p is primary else "member"
        p.merge_with = [q.old_folder_name for q in group if q is not p]
        p.reasons.append(
            "one show split across sibling folders; merging %s into %r as %s%d"
            % (", ".join([p.old_folder_name] + p.merge_with), proposal.name, kind, number)
        )
        p.warnings.append(
            "merging moves files between folders, which no other plan row does"
        )
        if not source_ok:
            p.warnings.append("the folders disagree about the source; it was left out")
        if not prov_ok:
            p.warnings.append("the folders disagree about the provenance; it was left out")
        if not fmt_ok:
            p.warnings.append("the folders disagree about the format; it was left out")


def detect_collisions(plans: list[ShowPlan], cfg: Config) -> None:
    """Two shows resolving to one name is reported, never resolved.

    Split shows are found first, and by band and date rather than by the
    proposed name: two halves of one release can pick up different provenance
    (only disc 2 mentioning the label, say) and would otherwise slip through as
    two unrelated shows.
    """
    live = [p for p in plans if p.status in (PLAN, UNCHANGED) and p.new_path]

    by_show: dict[tuple, list[ShowPlan]] = defaultdict(list)
    for p in live:
        by_show[(
            str(p.show.path.parent).lower(),
            p.analysis.band.abbrev,
            p.analysis.date.iso,
            group_stem(p.old_folder_name),
        )].append(p)

    split_plans: set[int] = set()
    for group in by_show.values():
        if len(group) < 2:
            continue
        markers = [show_marker(p.old_folder_name) for p in group]
        if all(markers) and len(set(markers)) == len(markers):
            # Two performances on one date, not two halves of one show.  They
            # already carry the marker in their names, so they stay apart.
            for p, marker in zip(group, markers):
                p.reasons.append(
                    "the %r of two shows on %s (the other: %s); kept as separate "
                    "folders, distinguished by the marker after the date"
                    % (marker, p.analysis.date.iso,
                       ", ".join(q.old_folder_name for q in group if q is not p))
                )
            split_plans.update(id(p) for p in group)
            continue
        merge_split_show(group, cfg)
        split_plans.update(id(p) for p in group)

    # Compared on the machine part only: two copies of one show are still two
    # copies even if one of them knows the venue and the other does not.
    by_target: dict[str, list[ShowPlan]] = defaultdict(list)
    for p in live:
        if id(p) not in split_plans:
            machine, _ = split_location(p.new_path.name)
            by_target[str(p.new_path.parent / machine).lower()].append(p)

    for target, group in by_target.items():
        if len(group) < 2:
            continue
        same_show = len({(q.analysis.band.abbrev, q.analysis.date.iso) for q in group}) == 1
        for p in group:
            others = [q.old_folder_name for q in group if q is not p]
            if same_show:
                # Same band, same date, same everything the name is built from:
                # these are two copies of one show, not two shows.
                p.status = DUPLICATE
                p.reasons.append(
                    "duplicate of %s - same band, date, source and format, so both "
                    "resolve to %r. Nothing is renamed or deleted; delete the copy "
                    "you do not want and re-run"
                    % (", ".join(others), Path(target).name)
                )
                qualities = {q.analysis.quality for q in group if q.analysis.quality}
                if len(qualities) > 1:
                    p.reasons.append(
                        "but they are not identical: the MP3 encodings differ (%s), so "
                        "one is a better copy than the other"
                        % ", ".join(sorted(qualities))
                    )
            else:
                p.status = COLLISION
                p.reasons.append(
                    "target name %r is also claimed by %s; neither is renamed"
                    % (Path(target).name, ", ".join(others))
                )

    # The grouping above keys on the whole target path, so two copies of one
    # show sitting in different folders never meet: an STS9 show existed twice,
    # once loose and once inside a wrapper folder, and both planned the same
    # name without a word being said.  They do not collide - the paths differ -
    # so this warns rather than blocks, but --unnest would lift one out and turn
    # it into a collision, and phase 1 cannot preview that because --unnest is
    # phase 2's flag.  Better to say so now than to be refused later.
    by_name: dict[str, list[ShowPlan]] = defaultdict(list)
    for p in live:
        if id(p) in split_plans or p.status in (DUPLICATE, COLLISION):
            continue
        machine, _ = split_location(p.new_path.name)
        by_name[machine.lower()].append(p)

    for machine, group in by_name.items():
        if len(group) < 2 or len({p.new_path.parent for p in group}) < 2:
            continue
        if len({(q.analysis.band.abbrev, q.analysis.date.iso) for q in group}) != 1:
            continue
        for p in group:
            others = [q.old_folder_name for q in group if q is not p]
            p.analysis.add(
                "SAME_SHOW_IN_ANOTHER_FOLDER", SEVERITY_WARN,
                "%s in a different folder resolves to the same name %r; these are "
                "two copies of one show. Nothing is deleted - decide which to keep"
                % (", ".join(others), machine))


def learn_venues_from_the_library(analyses: list[ShowAnalysis],
                                  gazetteer=None) -> None:
    """Teach a short venue its fuller form from elsewhere in the library.

    Consecutive nights at one theatre often disagree: one folder says "Wiltern"
    and the next "Wiltern, Los Angeles, CA".  The longer form is already yours,
    it is already right, and using it makes the naming consistent without
    asking anybody anything.

    But a venue name is not unique, and this runs after the gazetteer has
    already declined to guess.  Keyed on the name alone it took the city of
    whichever room it saw with the most detail: "Fox Theatre, Atlanta, GA"
    placed a bare "Fox Theatre" in Atlanta, and gave "Fox Theatre, Oakland" the
    state GA.  So a place is learned only when every folder naming the room
    agrees on one city, never for a name the gazetteer knows to be shared or
    too common to stand alone, and never onto a place set by hand or by the
    gazetteer.  A state is only ever added to the city it belongs to.
    """
    from .venues import same_city

    def flat(text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", text.lower())

    def refused(a: ShowAnalysis) -> bool:
        if gazetteer is None:
            return False
        year = a.date.date.year if a.date and a.date.date else None
        return bool(gazetteer.ambiguous(a.venue, year)) or gazetteer.is_generic(a.venue)

    # Every place the library gives each venue name, city first.
    places: dict[str, list[tuple[str | None, str | None]]] = defaultdict(list)
    for a in analyses:
        if not a.venue or not (a.city or a.state):
            continue
        # A folder the analysis could not make sense of is not evidence about
        # a room.  One with no band placed Phish's "Cologne" in a city called
        # "Germany", and only a whole-library run ever saw it: the source was
        # an act nobody has configured.  It can still learn; it cannot teach.
        if getattr(a, "blocked", False):
            continue
        key = flat(a.venue)
        if len(key) >= 5:
            places[key].append((a.city, a.state))

    def one_place(key: str) -> tuple[str | None, str | None] | None:
        """The single (city, state) the library agrees on, or None."""
        known = places.get(key) or []
        cities = [c for c, _ in known if c]
        states = {s.strip().upper() for _, s in known if s}
        if not cities or len(states) > 1:
            return None
        if any(not same_city(cities[0], c) for c in cities[1:]):
            return None
        # The fullest spelling of the one city: the first that came with a state.
        city = next((c for c, s in known if c and s), cities[0])
        return city, (states.pop() if states else None)

    for a in analyses:
        if not a.venue or (a.city and a.state):
            continue
        if a.place_by_hand or a.place_from_gazetteer:
            continue
        place = one_place(flat(a.venue))
        if place is None or refused(a):
            continue
        city, state = place
        if a.city and not same_city(a.city, city):
            continue                 # another room of the same name
        if a.state and state and a.state.strip().upper() != state:
            continue
        if (city and not a.city) or (state and not a.state):
            a.city = a.city or city
            a.state = a.state or state
            a.add("VENUE_FROM_LIBRARY", SEVERITY_INFO,
                  "%r is at %s elsewhere in the library, so that is used here too"
                  % (a.venue, ", ".join(p for p in (a.city, a.state) if p)))


def note_same_copy_elsewhere(plans: list[ShowPlan]) -> None:
    """Point out the same copy sitting in two places.

    Matched on band, date AND provenance, so two different tapers' recordings of
    one night - which you want to keep both of - are not flagged, but the same
    store download filed twice is.  Only ever a note: nothing is renamed or
    deleted on the strength of it.
    """
    seen: dict[tuple, list[ShowPlan]] = defaultdict(list)
    for p in plans:
        a = p.analysis
        if a.date.date and a.band.abbrev and a.provenance:
            seen[(a.band.abbrev, a.date.iso, a.provenance)].append(p)

    for (band, date, provenance), group in seen.items():
        if len(group) < 2:
            continue
        if any(p.status in (MERGE, DUPLICATE) for p in group):
            continue
        for p in group:
            others = [q.show.rel for q in group if q is not p]
            p.warnings.append(
                "the same copy appears elsewhere: %s %s from %s is also at %s - "
                "probably redundant, but nothing is renamed or deleted on that basis"
                % (band, date, provenance, "; ".join(others))
            )

        # An all-MP3 folder standing next to a folder that has the same
        # recording in FLAC is a lossy copy of it.
        lossless_holders = [
            q for q in group if any(f.ext in LOSSLESS_EXTS for f in q.show.files)
        ]
        if not lossless_holders:
            continue
        for p in group:
            if p in lossless_holders:
                continue
            if not all(f.ext in LOSSY_EXTS for f in p.show.files):
                continue
            p.lossy = LossyFinding(
                files=list(p.show.files),
                whole_folder=True,
                counterpart=lossless_holders[0].show.rel,
                reason="every file here is MP3, and %s holds the same recording "
                       "(%s %s from %s) losslessly"
                       % (lossless_holders[0].show.rel, band, date, provenance),
            )


def build_plans(root: Path, cfg: Config, today: _dt.date | None = None,
                reclassify: bool = False,
                include_top: set[str] | None = None,
                overrides=None,
                scan_errors: list | None = None) -> list[ShowPlan]:
    result = scan(root, cfg, read_tags=True, tag_sample=0, include_top=include_top)
    if scan_errors is not None:
        scan_errors.extend(result.errors)
    analyses = [analyze_show(show, cfg, today=today, overrides=overrides)
                for show in result.shows]
    # Venues are settled across the whole library before any name is built, so
    # two nights at one theatre cannot end up spelled two ways.
    learn_venues_from_the_library(analyses, getattr(cfg, "venues", None))
    plans = [plan_show(a, cfg, reclassify=reclassify) for a in analyses]
    detect_collisions(plans, cfg)
    note_same_copy_elsewhere(plans)

    for p in plans:
        if not p.lossy:
            found = find_in_folder(p.show.files)
            if found:
                p.lossy = found
            elif found.reason:
                p.warnings.append(found.reason)
        if p.lossy:
            p.warnings.append(
                "%d MP3 file(s) duplicate a lossless copy: %s. Move them aside with "
                "phase2 --quarantine-lossy"
                % (len(p.lossy.files), p.lossy.reason)
            )
    return plans


def run(root: Path, out_dir: Path, cfg: Config, today: _dt.date | None = None,
        reclassify: bool = False, include_top: set[str] | None = None,
        overrides=None) -> dict:
    # Guard here as well as in the CLI, so no caller can drop reports into the
    # library by going around the front door.
    out_dir = ensure_out_dir(out_dir, root)
    with run_lock(out_dir, "phase1"):
        errors: list[tuple[str, str]] = []
        plans = build_plans(root, cfg, today=today, reclassify=reclassify,
                            include_top=include_top, overrides=overrides,
                            scan_errors=errors)
        return _write_reports(out_dir, plans, cfg, scan_errors=errors,
                              root=root, scope=include_top)


def _write_reports(out_dir: Path, plans: list[ShowPlan], cfg: Config,
                   scan_errors: list | None = None, root: Path | None = None,
                   scope: set[str] | None = None) -> dict:
    counts: dict[str, int] = defaultdict(int)
    for p in plans:
        counts[p.status] += 1

    write_csv(
        out_dir / "phase1_folders.csv",
        ["status", "relative_path", "current_folder", "proposed_folder", "merge_into",
         "merge_role", "band", "date",
         "date_confidence", "classification", "shape", "source", "source_inferred",
         "provenance", "format", "mp3_quality", "tracks", "look_it_up_at",
         "warnings", "reasons"],
        (
            [
                p.status, p.show.rel, p.old_folder_name, p.new_folder_name or "",
                str(p.merge_target) if p.merge_target else "", p.merge_role,
                p.analysis.band.abbrev or "", p.analysis.date.iso or "",
                p.analysis.date.confidence, p.analysis.classification.kind,
                p.analysis.classification.shape or "", p.analysis.source.value or "",
                "yes" if p.analysis.source.inferred else "no",
                p.analysis.provenance or "", p.analysis.fmt or "",
                p.analysis.quality or "", len(p.tracks),
                cfg.reference_for(p.analysis.band.abbrev,
                                  album=(p.analysis.category == "ALBUM"))
                if p.status in (SKIP_BLOCKED, DUPLICATE, COLLISION, SPLIT_SHOW) else "",
                " | ".join(p.warnings), " | ".join(p.reasons),
            ]
            for p in plans
        ),
    )

    write_csv(
        out_dir / "phase1_tracks.csv",
        ["status", "relative_folder", "current_file", "proposed_file", "kind", "number",
         "track", "numbering_source", "title", "title_source", "warnings"],
        (
            [p.status, p.show.rel, t.old_name, t.new_name, t.kind, t.number, t.track,
             t.numbering_source, t.title or "", t.title_source, " | ".join(t.warnings)]
            for p in plans for t in p.tracks
        ),
    )

    write_csv(
        out_dir / "phase1_tags.csv",
        ["status", "relative_folder", "file", "field", "current_value", "proposed_value"],
        (
            [p.status, p.show.rel, t.old_name, field_name, current, proposed]
            for p in plans for t in p.tracks
            for field_name, (current, proposed) in sorted(t.tags.items())
        ),
    )

    write_csv(
        out_dir / "phase1_sidecars.csv",
        ["relative_folder", "sidecar", "kind", "status", "proposed_name", "entries",
         "unresolved", "notes"],
        (
            [p.show.rel, s.path.name, s.kind, s.status, s.new_name or "",
             len(s.referenced), len(s.unresolved), " | ".join(s.notes)]
            for p in plans for s in p.sidecars
        ),
    )

    # Files that cannot be read at all: a worklist for replacing them.  Nothing
    # is deleted; the folders they sit in are blocked until they are dealt with.
    write_csv(
        out_dir / "phase1_damaged.csv",
        ["relative_folder", "file", "bytes", "problem", "look_it_up_at"],
        (
            [p.show.rel, f.path.name, f.size, f.error or "unreadable",
             cfg.reference_for(p.analysis.band.abbrev)]
            for p in plans for f in p.show.files
            if f.tag_support == "unreadable" or f.size == 0
        ),
    )

    write_csv(
        out_dir / "phase1_issues.csv",
        ["relative_path", "severity", "code", "detail"],
        (
            [p.show.rel, i.severity, i.code, i.detail]
            for p in plans for i in p.analysis.issues
        ),
    )

    write_json(
        out_dir / "phase1_plan.json",
        {
            "generated": _dt.datetime.now().isoformat(timespec="seconds"),
            "config": str(cfg.path),
            "user_config": str(cfg.user_path) if cfg.user_path else None,
            # What this plan covered, so phase 2 can tell whether a dry run of
            # the same scope was made before it commits.
            "root": str(Path(root).resolve()) if root else None,
            "scope": sorted(scope) if scope else None,
            "commit_threshold": cfg.settings.min_date_confidence_commit,
            "counts": dict(counts),
            "shows": [
                {
                    "path": str(p.show.path),
                    "status": p.status,
                    "current_folder": p.old_folder_name,
                    "proposed_folder": p.new_folder_name,
                    "merge_into": str(p.merge_target) if p.merge_target else None,
                    "merge_role": p.merge_role or None,
                    "merge_with": p.merge_with,
                    "band": p.analysis.band.abbrev,
                    "date": p.analysis.date.iso,
                    "date_confidence": p.analysis.date.confidence,
                    "date_reasons": p.analysis.date.reasons,
                    "classification": p.analysis.classification.kind,
                    "shape": p.analysis.classification.shape,
                    "source": p.analysis.source.value,
                    "source_inferred": p.analysis.source.inferred,
                    "source_reason": p.analysis.source.reason,
                    "provenance": p.analysis.provenance,
                    "format": p.analysis.fmt,
                    "reasons": p.reasons,
                    "warnings": p.warnings,
                    "issues": [{"code": i.code, "severity": i.severity, "detail": i.detail}
                               for i in p.analysis.issues],
                    "tracks": [
                        {
                            "current": t.old_name, "proposed": t.new_name,
                            "kind": t.kind, "number": t.number, "track": t.track,
                            "numbering_source": t.numbering_source,
                            "title": t.title, "title_source": t.title_source,
                            "tags": {k: {"current": c, "proposed": n}
                                     for k, (c, n) in t.tags.items()},
                            "warnings": t.warnings,
                        }
                        for t in p.tracks
                    ],
                    "sidecars": [
                        {"file": s.path.name, "kind": s.kind, "status": s.status,
                         "proposed_name": s.new_name, "entries": len(s.referenced),
                         "unresolved": s.unresolved, "notes": s.notes}
                        for s in p.sidecars
                    ],
                }
                for p in plans
            ],
        },
    )

    rep = TextReport("Phase 1 dry run - nothing was written to the library")
    rep.heading("Outcome per folder")
    rep.histogram(dict(counts), total=len(plans) or None)

    renaming = [p for p in plans if p.status == PLAN]
    rep.heading("Proposed renames (%d)" % len(renaming))
    for p in renaming[:60]:
        rep.line("  %s" % p.show.rel)
        rep.line("      -> %s   [%s %s, date confidence %d]"
                 % (p.new_folder_name, p.analysis.classification.kind,
                    p.analysis.source.value or "no source",
                    p.analysis.date.confidence))

    merges: dict[str, list[ShowPlan]] = defaultdict(list)
    for p in plans:
        if p.status == MERGE and p.merge_target:
            merges[str(p.merge_target)].append(p)
    if merges:
        rep.heading("Split shows to be merged (%d shows, %d folders)"
                    % (len(merges), sum(len(g) for g in merges.values())))
        rep.line("  These are the only rows that move files between folders.")
        for target, group in sorted(merges.items()):
            rep.line("  -> %s" % Path(target).name)
            for p in sorted(group, key=lambda q: q.old_folder_name.lower()):
                first = p.tracks[0] if p.tracks else None
                rep.line("      %-44s %d tracks as %s%d%s"
                         % (p.old_folder_name[:44], len(p.tracks), first.kind if first else "?",
                            first.number if first else 0,
                            "  (%s)" % p.merge_role if p.merge_role else ""))

    for status, heading in ((DUPLICATE, "Duplicates - same show twice, delete one"),
                            (SPLIT_SHOW, "Split shows reported, not merged"),
                            (COLLISION, "Name collisions"),
                            (SKIP_BLOCKED, "Skipped - reported, not touched")):
        group = [p for p in plans if p.status == status]
        if not group:
            continue
        rep.heading("%s (%d)" % (heading, len(group)))
        for p in group[:60]:
            rep.line("  %s" % p.show.rel)
            for reason in p.reasons[:3]:
                rep.line("      %s" % reason[:120])
            if status == SKIP_BLOCKED:
                reference = cfg.reference_for(
                    p.analysis.band.abbrev,
                    album=(p.analysis.category == "ALBUM"),
                )
                if reference:
                    rep.line("      look it up at %s" % reference)

    damaged = [(p, f) for p in plans for f in p.show.files
               if f.tag_support == "unreadable" or f.size == 0]
    if damaged:
        rep.heading("Files that cannot be read (%d)" % len(damaged))
        rep.line("  Nothing is deleted. These folders stay blocked until the files")
        rep.line("  are replaced or removed; the full list is phase1_damaged.csv.")
        for p, f in damaged[:40]:
            rep.line("  %s" % p.show.rel)
            rep.line("      %-44s %d bytes" % (f.path.name[:44], f.size))

    inferred = [p for p in plans if p.analysis.source.inferred and p.status == PLAN]
    rep.heading("Source inferred rather than stated (%d)" % len(inferred))
    for p in inferred[:40]:
        rep.line("  %-52s %s  (%s)" % (p.new_folder_name, p.analysis.source.value,
                                       p.analysis.source.reason[:60]))

    broken = [(p, s) for p in plans for s in p.sidecars if not s.ok]
    rep.heading("Sidecars that cannot be rewritten automatically (%d)" % len(broken))
    for p, s in broken[:40]:
        rep.line("  %s / %s" % (p.show.rel, s.path.name))
        for note in s.notes[:2]:
            rep.line("      %s" % note[:120])

    if scan_errors:
        rep.heading("Folders and files that could not be read (%d)" % len(scan_errors))
        rep.line("  Nothing inside these was planned, and nothing else says so.")
        for path, error in scan_errors[:40]:
            rep.line("  %s" % path)
            rep.line("      %s" % error[:120])

    rep.save(out_dir / "phase1_summary.txt")
    return dict(counts)
