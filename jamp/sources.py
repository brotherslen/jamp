"""Working out whether a recording is sbd, aud or mtx.

Evaluation order is fixed and matters:

1. matrix / mtx anywhere in the name or info file wins outright, because a
   matrix is a blend and will legitimately mention both mics and the board.
2. a microphone model means an audience mic was used -> aud.
3. FM / SBD / soundboard -> sbd.
4. an explicit "aud"/"audience" token -> aud.
5. an official store download or disc rip -> sbd, INFERRED.
6. a known store or label in the provenance -> its configured source, INFERRED.
7. nothing -> no source at all.  The field is left out of the name; it is never
   filled with a placeholder.

A microphone model AND a board token with no matrix keyword is a conflict, not a
decision: nothing is written and the folder is reported.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import re

from .config import Config
from .dates import find_dates
from .tokens import find_tokens, has_phrase, has_token, mask_spans

VALID_SOURCES = ("sbd", "aud", "mtx")

# Words that say which performance, or how a folder is laid out - never a place.
SHOW_WORDS = frozenset({
    "early", "late", "matinee", "afternoon", "evening", "first", "second",
    "1st", "2nd", "show", "set", "sets", "disc", "disk", "cd", "part",
    "complete", "partial", "incomplete", "bonus", "encore",
})


@dataclass
class SourceInference:
    value: str | None = None
    inferred: bool = False
    confidence: str = "none"        # stated / inferred / conflict / none
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    conflict: bool = False

    @property
    def tag_value(self) -> str | None:
        """What goes in SOURCE_CONFIDENCE, so assumptions stay findable."""
        if self.value is None:
            return None
        if not self.inferred:
            return "stated: %s" % self.reason
        return "inferred: %s" % self.reason


def _scan_text(text: str) -> str:
    """Lower-case the text with date digits blanked out.

    Without this, the 4011 in a DPA 4011 mic and the digits of a date can trade
    false positives in either direction.
    """
    spans = [c.span for c in find_dates(text)]
    return _mask_mic_placement(mask_spans(text, spans).lower())


# "behind the board", "in front of the sbd", "at the desk" - a taper saying
# where the microphones stood, not where the signal came from.  Read as
# lineage it turns an audience tape into a soundboard, which is the one thing
# the source field must never get wrong.
_MIC_PLACEMENT = re.compile(
    r"(?:behind|in\s+front\s+of|front\s+of|beside|next\s+to|left\s+of|right\s+of|at|near|by)"
    r"\s+(?:the\s+)?(?:sbd|board|desk|soundboard|mixer)\b",
    re.I)


def _mask_mic_placement(text: str) -> str:
    return _MIC_PLACEMENT.sub(lambda m: " " * len(m.group(0)), text)


def infer_source(
    cfg: Config,
    folder_name: str,
    info_text: str = "",
    is_official: bool = False,
    provenance_key: str | None = None,
    stated_source: str | None = None,
    tag_text: str = "",
) -> SourceInference:
    name = _scan_text(folder_name)
    info = _scan_text((info_text or "")[:8000])
    haystacks = [("folder name", name)]
    if stated_source:
        haystacks.append(("info source line", stated_source.lower()))
    haystacks.append(("info file", info))
    if tag_text:
        # A well-tagged audience tape puts its whole lineage in COMMENT or
        # DESCRIPTION and nowhere else.  Without this, such a folder looks
        # exactly like an official release.
        haystacks.append(("tags", _scan_text(tag_text[:8000])))

    # NB: the three-letter values are matched as whole tokens, never as
    # substrings - otherwise "audience" reads as "aud" and a matrix of an
    # audience mic and the board comes out as aud.
    matrix_hits = [
        "%s: %s" % (where, tok)
        for where, text in haystacks
        for tok in find_tokens(text, cfg.source_tokens.get("matrix", ()))
    ]
    if matrix_hits:
        return SourceInference(
            value="mtx", inferred=False, confidence="stated",
            reason="matrix keyword (%s)" % matrix_hits[0], evidence=matrix_hits,
        )

    mic_hits = [
        "%s: %s" % (where, tok)
        for where, text in haystacks
        for tok in find_tokens(text, cfg.microphones)
    ]
    board_hits = [
        "%s: %s" % (where, tok)
        for where, text in haystacks
        for tok in find_tokens(text, cfg.source_tokens.get("soundboard", ()))
    ]

    if mic_hits and board_hits:
        return SourceInference(
            value=None, inferred=False, confidence="conflict", conflict=True,
            reason="both a microphone (%s) and a board token (%s) with no matrix keyword"
                   % (mic_hits[0], board_hits[0]),
            evidence=mic_hits + board_hits,
        )

    if mic_hits:
        return SourceInference(
            value="aud", inferred=False, confidence="stated",
            reason="microphone model (%s)" % mic_hits[0], evidence=mic_hits,
        )

    if board_hits:
        return SourceInference(
            value="sbd", inferred=False, confidence="stated",
            reason="board keyword (%s)" % board_hits[0], evidence=board_hits,
        )

    aud_hits = [
        "%s: %s" % (where, tok)
        for where, text in haystacks
        for tok in find_tokens(text, cfg.source_tokens.get("audience", ()))
    ]
    if aud_hits:
        return SourceInference(
            value="aud", inferred=False, confidence="stated",
            reason="audience keyword (%s)" % aud_hits[0], evidence=aud_hits,
        )

    prov = cfg.provenance_for(provenance_key)
    if prov and prov.source:
        return SourceInference(
            value=prov.source, inferred=True, confidence="inferred",
            reason="provenance %s is board-sourced per config" % prov.key,
            evidence=["provenance=%s" % prov.key],
        )

    if is_official:
        return SourceInference(
            value="sbd", inferred=True, confidence="inferred",
            reason="official release; official releases are board-sourced",
            evidence=["classification=OFFICIAL"],
        )

    return SourceInference(
        value=None, inferred=False, confidence="none",
        reason="no source evidence; field left out of the name",
    )


_COMPACT_NAME = re.compile(r"^[a-z]{1,5}[-._]?\d{2,4}[-._]\d{1,2}[-._]\d{1,2}", re.I)


# A taper field in an etree name: one word, or co-tapers joined with - or +
# ("schillo-shriver", "cohen+vita").  Every part is at least three letters, so
# the "t" of "t-flac16" and bare initials never qualify.
_TAPER_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9']{2,}(?:[-+][A-Za-z][A-Za-z0-9']{2,})+$")
_TAPER_WORD = re.compile(r"^[A-Za-z][A-Za-z0-9']{2,}$")

# The number a recording is catalogued under on etree, in a field of its own.
_ETREE_SOURCE_ID = re.compile(r"\.\d{5,6}(?=\.)")


def _taper_head(token: str) -> str:
    """The first of a pair of co-tapers - one handle goes in the name."""
    return re.split(r"[-+]", token.strip())[0].lower()


def provenance_from_name(cfg: Config, folder_name: str) -> tuple[str | None, str]:
    """A taper handle sitting in a compact etree folder name (mmj2021-11-04.leary).

    Deliberately timid: only compact band+date names, only dot-separated
    plain tokens, and only where the layout says which field is the taper.  An
    unexplained loose word is far more often the venue, so it is left alone.
    """
    if not _COMPACT_NAME.match(folder_name):
        return None, ""
    spans = [c.span for c in find_dates(folder_name)]
    masked = mask_spans(folder_name, spans)

    known: set[str] = set(cfg.format_suffixes)
    known.update(t for toks in cfg.quality_tokens.values() for t in toks)
    known.update(cfg.microphones)
    known.update(cfg.broadcast_stations)
    # "late" says which performance and "jgb" says which act; neither is a
    # person, and both sit right next to the taper field in etree names.
    known.update(SHOW_WORDS)
    known.update({"unknown", "unk", "various", "misc", "master", "transfer"})
    # Where the taper stood and how the tape was handled - never who taped it.
    # "fob.cohen+vita" is front-of-board, then the tapers.
    known.update({"fob", "ffob", "dfc", "oty", "motb", "sbe", "sbeok",
                  "sbefail", "sbefixed", "shnf", "seekable", "reseed",
                  "remaster", "remastered", "partial", "incomplete"})
    for group in cfg.source_tokens.values():
        known.update(group)
    for band in cfg.bands:
        known.add(band.abbrev)
        for alias in (band.name, *band.aliases, *band.prefixes):
            flat = re.sub(r"[^a-z0-9]", "", alias.lower())
            if flat:
                known.add(flat)

    parts = [p.strip() for p in masked.split(".")]

    def plausible(token: str) -> bool:
        if not (_TAPER_TOKEN.match(token) or _TAPER_WORD.match(token)):
            return False
        head = _taper_head(token)
        if not (3 <= len(head) <= 20) or head in known:
            return False
        if token.upper() in cfg.us_states or head.upper() in cfg.us_states:
            return False
        return not any(has_token(head, mic) for mic in cfg.microphones)

    # etree names run band+date . location . taper . mic . format, but plenty
    # put the taper straight after the mic instead.  Either neighbour of the
    # microphone is the taper; nothing else in the name is.  This is what
    # rescues "padelimike" out of ph2018-12-28.New.York.NY.padelimike.akg414,
    # and "dyche" out of jg80-07-27.120085.jgb.nak700.dyche.t-flac16.
    for i, part in enumerate(parts):
        if i >= 2 and any(has_token(part.lower(), mic) for mic in cfg.microphones):
            for neighbour in (parts[i - 1], parts[i + 1] if i + 1 < len(parts) else ""):
                if plausible(neighbour):
                    return (_taper_head(neighbour),
                            "the token beside the microphone in the folder name")
            break

    # Co-tapers joined with - or + are a taper field wherever they sit: no city
    # or venue is written that way.
    for i, part in enumerate(parts):
        if i < 1 or not (_TAPER_TOKEN.match(part) and plausible(part)):
            continue
        # etree names run taper . transferer, so a lone plausible word directly
        # before the pair is the taper and the pair are who moved the tape:
        # "fm.glassberg.cohen-jupille" is Glassberg's tape.
        before = parts[i - 1]
        if _TAPER_WORD.match(before) and plausible(before):
            return (_taper_head(before),
                    "%r, the taper before the transferers %r, in the folder name"
                    % (before, part))
        return _taper_head(part), "%r names the taper(s) in the folder name" % part

    # A name carrying an etree catalogue number is strictly field-structured -
    # band+date . source . taper . id . flags . format - so a single surviving
    # plain word in it is the taper, not a stray place: gd83-10-08.fob-aud.
    # willy.11734.sbeok.shnf.  Only when exactly one word survives; two and we
    # cannot tell a taper from a venue, so we take neither.
    if _ETREE_SOURCE_ID.search(folder_name):
        loose = [p for p in parts[1:] if _TAPER_WORD.match(p) and plausible(p)]
        if len(loose) == 1:
            return (_taper_head(loose[0]),
                    "the one plain word in an etree-catalogued name")

    # Otherwise only a name we already know is a taper.  An unexplained word is
    # far more often the venue - "MMJ-Wiltern" is a theatre, not a person - so
    # it is left for the venue to claim.
    for word in residual_words(cfg, folder_name):
        hit = cfg.canonical_taper(word)
        if hit:
            return hit, "%r is a taper listed in the config" % word
    return None, ""


def broadcast_provenance(cfg: Config, texts) -> tuple[str | None, str]:
    """The station a broadcast came from, if one is named.

    The call sign identifies this copy the way a taper's name does.  "fm" and
    "sxm" never appear in a name - they only tell the source inference that the
    recording came off the board.
    """
    for label, text in texts:
        if not text:
            continue
        for station in cfg.broadcast_stations:
            if has_token(text, station):
                return station, "%s names the station %r" % (label, station)
    return None, ""


def residual_words(cfg: Config, folder_name: str) -> list[str]:
    """Runs of words in a compact name that nothing else explains.

    Word by word rather than segment by segment, because these names glue
    things together with any punctuation to hand: "MMJ-Wiltern" is the band and
    a theatre, "Electric_Factory_WXPN_FM_SBD" is a club, a station and two
    source words.  Consecutive survivors stay together, so the club comes back
    as "Electric Factory" and not as two loose words.
    """
    if not _COMPACT_NAME.match(folder_name):
        return []
    masked = mask_spans(folder_name, [c.span for c in find_dates(folder_name)])

    known: set[str] = set(cfg.format_suffixes)
    known.update(t for toks in cfg.quality_tokens.values() for t in toks)
    known.update(cfg.microphones)
    known.update(cfg.broadcast_stations)
    # "early" and "late" say which show, not where it was.
    known.update(SHOW_WORDS)
    known.update({"unknown", "unk", "various", "misc", "master", "transfer"})
    # Where the taper stood and how the tape was handled - never who taped it.
    # "fob.cohen+vita" is front-of-board, then the tapers.
    known.update({"fob", "ffob", "dfc", "oty", "motb", "sbe", "sbeok",
                  "sbefail", "sbefixed", "shnf", "seekable", "reseed",
                  "remaster", "remastered", "partial", "incomplete"})
    for group in cfg.source_tokens.values():
        known.update(group)
    for band in cfg.bands:
        known.add(band.abbrev)
        for alias in (band.name, *band.aliases, *band.prefixes):
            flat = re.sub(r"[^a-z0-9]", "", alias.lower())
            if flat:
                known.add(flat)

    groups: list[list[str]] = []
    current: list[str] = []
    for word in re.split(r"[^A-Za-z0-9']+", masked):
        low = word.lower()
        explained = (
            not word
            or low in known
            or low.isdigit()
            or len(word) < 3
            or word.upper() in cfg.us_states
            or any(has_token(low, mic) for mic in cfg.microphones)
        )
        if explained:
            if current:
                groups.append(current)
                current = []
            continue
        current.append(word)
    if current:
        groups.append(current)
    return [" ".join(g) for g in groups]


def mic_as_provenance(cfg: Config, folder_name: str) -> tuple[str | None, str]:
    """Fall back to the microphone model as the distinguishing token.

    Only for audience and matrix recordings, and only when no taper is known.
    Two tapers' copies of one night have to be told apart somehow, and the mic
    is frequently the only thing in the name that does it - without it the two
    folders collide and neither can be renamed.
    """
    spans = [c.span for c in find_dates(folder_name)]
    masked = mask_spans(folder_name, spans)
    # "ak40" and "ak-40" are one microphone spelled two ways; the matcher is
    # separator-insensitive, so collapse them before counting.
    slugs = {re.sub(r"[^a-z0-9]", "", hit.lower()) for hit in find_tokens(masked, cfg.microphones)}
    slugs.discard("")
    if len(slugs) != 1:
        return None, ""
    slug = slugs.pop()
    return slug, "microphone model %r, used to tell tapers apart" % slug


def detect_provenance(cfg: Config, texts) -> tuple[str | None, str]:
    """Find a store / label token in any of the given strings."""
    for label, text in texts:
        if not text:
            continue
        low = text.lower()
        for prov in cfg.provenance:
            for alias in prov.aliases:
                if has_phrase(low, alias):
                    return prov.key, "%s mentions %r" % (label, alias)
    return None, ""
