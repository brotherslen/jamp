"""Deciding where a folder came from: OFFICIAL, UNOFFICIAL or UNKNOWN.

This runs before anything else is trusted, because it decides which evidence is
authoritative:

* OFFICIAL - the tags are right, build the names from them.
* UNOFFICIAL  - the folder name and info file are right, rebuild the tags.
* UNKNOWN  - report, change nothing.

UNOFFICIAL means exactly what it says: not a release put out by the band or a
label.  How the copy travelled has nothing to do with it - by torrent, from
archive.org, or hand to hand - and an official release can be ripped and seeded
like anything else, so distribution is never evidence either way.  What the
pipeline needs to know is "official release or not", because that is what
decides whether the tags or the folder name is the thing to trust.

Signals are scored rather than chained, so one odd folder cannot flip the whole
decision, and every weight lives in the config.  A winner needs both a minimum
score and a minimum margin over the loser; otherwise the answer is UNKNOWN.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .audio import INTERESTING_TAGS, AudioFile
from .config import Config, Series
from .tokens import find_tokens, has_phrase, has_token

OFFICIAL = "OFFICIAL"
UNOFFICIAL = "UNOFFICIAL"
UNKNOWN = "UNKNOWN"

STORE = "store"
DISC_RIP = "disc_rip"

_COMPACT_PREFIX = re.compile(
    r"^[a-z]{1,5}[-._]?(?:19|20)?\d{2}[-._]?\d{1,2}[-._]?\d{1,2}(?![0-9])", re.I)
_ETREE_TRACK = re.compile(r"[ds]\d{1,2}t\d{2}", re.I)
_YEAR_ONLY_TAG = re.compile(r"^\s*(19|20)\d{2}\s*$")
_FULL_ISO_TAG = re.compile(r"^\s*(19|20)\d{2}-\d{2}-\d{2}")


@dataclass
class Signal:
    name: str
    weight: int
    detail: str


def album_key(album: str) -> str:
    """An ALBUM value with the date and any set/disc marker taken out.

    A store tags each set of a show with its own album - "2011/10/29 I Atlanta,
    GA" and "2011/10/29 II Atlanta, GA" - and those are one release, not two.
    Used only for judging consistency, never for display.
    """
    from .dates import find_dates
    from .tokens import mask_spans

    text = mask_spans(album, [c.span for c in find_dates(album)])
    text = re.sub(r"\b(?:set|disc|disk|cd|night)\s*[ivxIVX\d]+\b", " ", text, flags=re.I)
    text = re.sub(r"(?<![A-Za-z])[IVX]{1,4}(?![A-Za-z])", " ", text)
    return re.sub(r"[^a-z0-9]", "", text.lower())


@dataclass
class TagProfile:
    """What the tags in a folder look like, taken as a whole."""

    files_with_tags: int = 0         # files mutagen can tag at all
    files_with_any_tag: int = 0      # of those, how many carry anything
    coverage: float = 0.0            # fraction carrying the core four fields
    album_values: tuple[str, ...] = ()
    album_keys: tuple[str, ...] = ()
    artist_values: tuple[str, ...] = ()
    date_values: tuple[str, ...] = ()
    titles_look_real: bool = False
    tracknumbers_contiguous: bool = False
    max_discnumber: int = 0
    date_is_year_only: bool = False
    date_is_full_iso: bool = False

    @property
    def complete_and_consistent(self) -> bool:
        return (
            self.files_with_tags > 0
            and self.coverage >= 0.9
            and len(self.album_keys) == 1
            and len(self.artist_values) == 1
            and self.titles_look_real
        )


@dataclass
class Classification:
    kind: str = UNKNOWN
    shape: str | None = None
    official_score: int = 0
    unofficial_score: int = 0
    signals: list[Signal] = field(default_factory=list)
    series: Series | None = None
    multi_date: bool = False
    non_show_hits: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tag_profile: TagProfile = field(default_factory=TagProfile)

    @property
    def signal_names(self) -> list[str]:
        return [s.name for s in self.signals]

    @property
    def tags_are_authoritative(self) -> bool:
        return self.kind == OFFICIAL

    @property
    def tag_date_admissible(self) -> bool:
        """Disc rips carry the RELEASE year in DATE.  Never let it near the
        date resolver."""
        return (
            self.kind == OFFICIAL
            and self.shape == STORE
            and self.tag_profile.date_is_full_iso
        )


_PLACEHOLDER_TITLE = re.compile(r"^(track|untitled|audio)[\s_.\-]*\d*$", re.I)
_BARE_NUMBER = re.compile(r"^0*(\d{1,3})$")


def _first(values: list[str] | None) -> str | None:
    return values[0] if values else None


def _is_song_title(title: str, f: AudioFile) -> bool:
    """Is this a real song title, or a machine-made stand-in?

    The question is whether a human named it, not whether it contains letters:
    "555" and "2001" are Phish songs, and demanding letters wrongly convicted a
    whole folder of having generated titles.
    """
    title = (title or "").strip()
    if not title:
        return False
    if _ETREE_TRACK.search(title):                      # "d1t01"
        return False
    if _PLACEHOLDER_TITLE.match(title):                 # "Track 04", "Untitled"
        return False
    if title.lower() == f.path.stem.lower():            # the filename repeated
        return False
    m = _BARE_NUMBER.match(title)
    if m and f.name_info.track is not None and int(m.group(1)) == f.name_info.track:
        return False                                    # "05" for track 5
    return True


def profile_tags(files: list[AudioFile]) -> TagProfile:
    tagged = [f for f in files if f.tags_read and f.tag_support == "full"]
    prof = TagProfile(files_with_tags=len(tagged))
    if not tagged:
        return prof

    core = ("ARTIST", "ALBUM", "TITLE", "TRACKNUMBER")
    have = sum(1 for f in tagged if all(f.tags.get(k) for k in core))
    prof.coverage = have / len(tagged)
    prof.files_with_any_tag = sum(
        1 for f in tagged if any(f.tags.get(k) for k in INTERESTING_TAGS)
    )

    prof.album_values = tuple(sorted({(_first(f.tags.get("ALBUM")) or "").strip()
                                      for f in tagged} - {""}))
    prof.album_keys = tuple(sorted({album_key(v) for v in prof.album_values} - {""}))
    prof.artist_values = tuple(sorted({(_first(f.tags.get("ARTIST")) or "").strip()
                                       for f in tagged} - {""}))
    prof.date_values = tuple(sorted({(_first(f.tags.get("DATE")) or "").strip()
                                     for f in tagged} - {""}))

    titles = [(_first(f.tags.get("TITLE")) or "").strip() for f in tagged]
    real = [t for t, f in zip(titles, tagged) if _is_song_title(t, f)]
    prof.titles_look_real = bool(titles) and len(real) / len(titles) >= 0.9

    nums: list[int] = []
    for f in tagged:
        raw = _first(f.tags.get("TRACKNUMBER")) or ""
        m = re.match(r"\s*(\d{1,3})", raw)
        if m:
            nums.append(int(m.group(1)))
    prof.tracknumbers_contiguous = bool(nums) and sorted(nums) == list(
        range(min(nums), min(nums) + len(nums))
    )

    discs: list[int] = []
    for f in tagged:
        raw = _first(f.tags.get("DISCNUMBER")) or ""
        m = re.match(r"\s*(\d{1,2})", raw)
        if m:
            discs.append(int(m.group(1)))
    prof.max_discnumber = max(discs) if discs else 0

    if prof.date_values:
        prof.date_is_year_only = all(_YEAR_ONLY_TAG.match(d) for d in prof.date_values)
        prof.date_is_full_iso = all(_FULL_ISO_TAG.match(d) for d in prof.date_values)
    return prof


def match_series(cfg: Config, texts) -> Series | None:
    for text in texts:
        if not text:
            continue
        for series in cfg.series:
            for pattern in series.patterns:
                if has_phrase(text, pattern):
                    return series
    return None


def find_non_show_tokens(cfg: Config, folder_name: str) -> list[str]:
    hits: list[str] = []
    for token in cfg.non_show_tokens:
        needle = token.strip()
        if " " in needle:
            if has_phrase(folder_name, needle):
                hits.append(needle)
        elif has_token(folder_name, needle):
            hits.append(needle)
    return hits


# The number a recording is catalogued under on etree, in a field of its own:
# jg80-08-09.029088.jgb.partial.sbd.jupille.  No store or label writes one, so
# its presence settles the question of where the folder came from.
_ETREE_SOURCE_ID = re.compile(r"(?<=\.)\d{5,6}(?=[.\s_-]|$)")


def classify(
    cfg: Config,
    folder_name: str,
    files: list[AudioFile],
    sidecars: dict,
    info_text: str = "",
    info_has_lineage: bool = False,
    torrent_marker: bool = False,
    store_marker: str | None = None,
    band_matched_by: str | None = None,
    has_cover_art: bool = False,
    mic_token: bool = False,
    audience_evidence: bool = False,
    extra_texts=(),
) -> Classification:
    # Did this pipeline write this folder name?  Several signals describe the
    # etree convention - a compact band+date name, dNtNN tracks, the source
    # stated in the name, a VENUE tag - and our own output has all of them by
    # construction.  Counting them on a folder we renamed makes every commit
    # drag it towards UNOFFICIAL, which says something about us and nothing
    # about the recording.  The format token is what marks a name as ours.
    from .naming import parse_canonical

    _canon = parse_canonical(folder_name)
    ours = _canon is not None and bool(_canon.fmt)

    w_off = cfg.weights.get("official", {})
    w_tor = cfg.weights.get("unofficial", {})
    signals: list[Signal] = []
    off = 0
    tor = 0

    prof = profile_tags(files)
    series = match_series(cfg, [folder_name, *extra_texts])

    def add(bucket: str, key: str, detail: str) -> None:
        nonlocal off, tor
        table = w_off if bucket == "official" else w_tor
        weight = table.get(key, 0)
        if not weight:
            return
        signals.append(Signal("%s.%s" % (bucket, key), weight, detail))
        if bucket == "official":
            off += weight
        else:
            tor += weight

    # --- official ---------------------------------------------------------
    if series:
        add("official", "series_match", "matches official series %r" % series.name)
    if store_marker and (audience_evidence or mic_token):
        # LivePhish, nugs and dead.net sell board recordings; there has never
        # been an audience release from any of them.  So a folder that names a
        # store AND carries a mic or an audience source is a fan recording
        # quoting the store in its lineage - a matrix built on the board feed,
        # typically.  The store is not evidence of a release here, and paying
        # it 30 points would make every such matrix look official.
        cls_note_store_is_lineage = (
            "%r appears here alongside audience evidence; no store sells an "
            "audience recording, so it is being read as part of the lineage "
            "rather than as the publisher" % store_marker)
        signals.append(Signal("official.store_marker_ignored", 0,
                              cls_note_store_is_lineage))
    elif store_marker:
        add("official", "store_marker", store_marker)
    if prof.complete_and_consistent:
        add("official", "tags_complete_consistent",
            "%d files, %.0f%% carry artist/album/title/track, one album value %r"
            % (prof.files_with_tags, prof.coverage * 100, prof.album_values[0][:60]))
    # The band resolver matches on the date-MASKED name, so it reports
    # "alias_leading" for "4-17-82 Grateful Dead sbd flac" too.  That is right
    # for deciding who played, but this signal is about shape: a store download
    # is called "My Morning Jacket 2023-11-03 Fox Theatre", band first.  A name
    # that opens with the date is a taper's, so it earns nothing here.
    if band_matched_by == "alias_leading" and not re.match(r"\s*\d", folder_name):
        add("official", "full_band_name", "folder opens with the spelled-out band name")
    for fmt, tokens in cfg.quality_tokens.items():
        if any(has_token(folder_name, t) or has_phrase(folder_name, t) for t in tokens):
            add("official", "bracket_quality_token", "quality token for %s in the name" % fmt)
            break
    if not sidecars.get("ffp") and not sidecars.get("st5") and not sidecars.get("md5"):
        add("official", "no_etree_sidecars", "no ffp/md5/st5 checksums present")
    if has_cover_art:
        add("official", "cover_art", "artwork present")
    copyrights = {
        value
        for f in files[:4]
        for value in (f.tags.get("COPYRIGHT") or [])
        if value.strip()
    }
    if copyrights:
        add("official", "copyright_tag",
            "a copyright tag is set (%s)" % sorted(copyrights)[0][:60])
    untagged = prof.files_with_tags > 0 and prof.files_with_any_tag == 0
    if untagged:
        add("official", "tags_absent", "taggable files carry no tags at all")
    # Official releases are board-sourced. A named microphone or a matrix
    # keyword is therefore evidence against an official release, however clean
    # the tags happen to be.
    if audience_evidence:
        add("official", "audience_evidence",
            "an audience or matrix source is named in the name, info file or tags")
        add("unofficial", "audience_evidence",
            "an audience or matrix source is named, which is how tapes circulate")

    # --- unofficial -------------------------------------------------------
    if torrent_marker:
        add("unofficial", "torrent_marker_file", "a 'Torrent downloaded from' file is present")
    if sidecars.get("ffp") or sidecars.get("st5"):
        add("unofficial", "ffp_or_st5", "etree fingerprint files present")
    if sidecars.get("md5"):
        add("unofficial", "md5", "md5 checksums present")
    if info_has_lineage:
        add("unofficial", "info_lineage", "info file carries a lineage/taper/source line")
    # Our own scheme IS the etree convention: we rename folders to
    # "mmj2010-05-01..." and tracks to "d1t01".  Counting either as evidence
    # means every folder drifts towards unofficial the moment we commit it,
    # which is a statement about this pipeline and not about the recording.
    if _COMPACT_PREFIX.match(folder_name) and not ours:
        add("unofficial", "compact_prefix_name", "compact band+date folder name")
    if _ETREE_SOURCE_ID.search(folder_name):
        add("unofficial", "etree_source_id",
            "the folder name carries an etree catalogue number")
    # Naming the source in the folder itself is taper vocabulary.  No label
    # ships a product called "4-17-82 Grateful Dead sbd flac"; a taper labels
    # their copy that way so it can be told from the other copies of the night.
    # (The folder NAME only - an official release's info file may well say SBD.)
    name_sources = () if ours else sorted({
        tok
        for group in ("soundboard", "audience", "matrix")
        for tok in find_tokens(folder_name.lower(), cfg.source_tokens.get(group, ()))
    })
    if name_sources:
        add("unofficial", "source_token_in_name",
            "the folder name states the source (%s)" % ", ".join(name_sources))
    if any(f.ext == ".shn" for f in files):
        add("unofficial", "shn_present", "SHN audio present")
    if any(_ETREE_TRACK.search(f.path.name) for f in files) and not ours:
        add("unofficial", "etree_track_names", "track filenames use dNtNN")
    if untagged:
        add("unofficial", "no_tags_at_all",
            "%d taggable files, none carrying a single tag - not a store download"
            % prof.files_with_tags)
    if prof.files_with_tags and prof.coverage < 0.6:
        add("unofficial", "tags_missing",
            "only %.0f%% of files carry the core tags" % (prof.coverage * 100))
    if mic_token:
        add("unofficial", "mic_token", "a microphone model appears in the name")
    named_after_file = [
        f for f in files
        if (f.tag("TITLE") or "").strip().lower() == f.path.stem.lower()
        and (f.tag("TITLE") or "").strip()
    ]
    if named_after_file and len(named_after_file) >= 0.8 * max(len(files), 1):
        add("unofficial", "titles_are_filenames",
            "every TITLE is just the filename repeated; a store writes real titles")
    # VENUE, TAPER, LINEAGE and SOURCE are taper vocabulary.  A store writes
    # ARTIST/ALBUM/TITLE and stops; nobody at nugs fills in a VENUE field.
    # Phase 2 writes a VENUE tag itself, so on a folder this pipeline has
    # already renamed the VENUE proves nothing about who made the recording -
    # counting it flipped eleven committed folders out of OFFICIAL.  It is the
    # only one of these six we write, so the rest still carry their full weight.
    taper_keys = ("TAPER", "LINEAGE", "SOURCE", "TRANSFERRER", "LOCATION")
    if not ours:
        taper_keys = ("VENUE",) + taper_keys
    taper_fields = sorted({
        key
        for f in files[:6]
        for key in taper_keys
        if f.tags.get(key)
    })
    if taper_fields:
        add("unofficial", "taper_tags",
            "tags a taper writes and a store does not: %s" % ", ".join(taper_fields))

    # --- decide -----------------------------------------------------------
    cls = Classification(
        official_score=off, unofficial_score=tor, signals=signals,
        series=series, tag_profile=prof,
        non_show_hits=find_non_show_tokens(cfg, folder_name),
    )
    lead, other = (OFFICIAL, UNOFFICIAL) if off >= tor else (UNOFFICIAL, OFFICIAL)
    top, bottom = max(off, tor), min(off, tor)
    if top >= cfg.settings.classify_min_score and (top - bottom) >= cfg.settings.classify_min_margin:
        cls.kind = lead
    else:
        cls.kind = UNKNOWN
        cls.notes.append(
            "official %d vs unofficial %d: need >=%d with a margin of >=%d"
            % (off, tor, cfg.settings.classify_min_score, cfg.settings.classify_min_margin)
        )

    # An official release announces itself: it belongs to a named series, or
    # carries a store's marker, or both.  A folder with neither, that no amount
    # of scoring can settle, is not a release nobody noticed - it is a fan copy
    # whose evidence is thin.  Calling it unofficial is the safe reading, and
    # only the naming scheme turns on it.
    if (cls.kind == UNKNOWN and cfg.settings.unknown_defaults_to_unofficial
            and not series and not store_marker):
        cls.kind = UNOFFICIAL
        cls.notes.append(
            "neither side settled it (official %d vs unofficial %d), and nothing "
            "here names a series or a store - read as unofficial" % (off, tor))

    # An etree catalogue number is categorical, not additive: etree hosts only
    # trade-friendly recordings and carries no official releases at all, so a
    # shnid is positive proof this is a fan copy however well it is tagged.
    #
    # Nothing else here is: a "Torrent downloaded from" file and an info file
    # with a lineage or source line both describe how a copy travelled and how
    # it was made, and an official release can be ripped, documented and seeded
    # like anything else.  Those stay ordinary weighted signals.
    if _ETREE_SOURCE_ID.search(folder_name):
        cls.kind = UNOFFICIAL
        cls.notes.append(
            "settled as a fan copy by the etree catalogue number in the name - "
            "etree hosts no official releases (scores were official %d vs "
            "unofficial %d)" % (off, tor))
        if series or store_marker:
            # Contradictory evidence.  The catalogue number still wins, but a
            # folder that names a release AND carries a shnid is worth a look:
            # either the number is a false match or the folder mixes two things.
            cls.notes.append(
                "NB: it also names %s, which an etree-catalogued folder should "
                "not - worth checking by hand"
                % ("the series %r" % series.name if series
                   else "the store %r" % store_marker))

    if cls.kind == OFFICIAL:
        disc_rip_reasons = []
        if series:
            disc_rip_reasons.append("official series")
        if prof.date_is_year_only:
            disc_rip_reasons.append("DATE tag is a bare year")
        # A full ISO date in DATE is not the release-year trap, so structural
        # hints alone must not demote a store download to a disc rip.
        if not prof.date_is_full_iso:
            if prof.max_discnumber > 1 and not store_marker:
                disc_rip_reasons.append("multi-disc structure, no store marker")
            if any(f.name_info.pattern in ("NNN title", "N-NN title") for f in files):
                disc_rip_reasons.append("disc+track filename numbering")
        if store_marker and not series:
            # A store download stays a store download even when its DATE tag is
            # only a year - nugs and UMLive both do that.  The year is still
            # refused as evidence below; only the label changes.
            cls.shape = STORE
        elif disc_rip_reasons:
            cls.shape = DISC_RIP
            cls.notes.append("disc rip: " + "; ".join(disc_rip_reasons))
        else:
            cls.shape = STORE
        if not prof.date_is_full_iso and prof.date_values:
            cls.notes.append(
                "DATE tag %s refused as date evidence (release-year trap)"
                % list(prof.date_values)[:3])
        if series and series.multi_date:
            cls.multi_date = True
            cls.notes.append(
                "%r spans several shows - reported for manual handling" % series.name
            )
    return cls
