"""Working out which band a folder actually belongs to.

The parent artist directory is explicitly NOT authoritative: side projects
(Omega Moos, OHMphrey, Jerry Garcia Band) live under their parent band's folder.
Evidence is ranked, and a parent-folder match is always flagged so you can see
which shows were attributed by location rather than by name.

Fuzzy matches are only ever returned as `suggestions`.  They are never applied.
"""
from __future__ import annotations

import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from .config import Band, Config
from .dates import find_dates
from .tokens import mask_spans, split_tokens

_PREFIX_RE = re.compile(r"^([A-Za-z]{1,6})[-._ ]?\d")

CONFIDENCE = {
    "alias_leading": 95,
    "prefix": 90,
    "tag_artist": 80,
    "alias_anywhere": 70,
    "info_file": 65,
    "parent_folder": 50,
}


@dataclass
class BandResolution:
    band: Band | None
    matched_by: str | None
    confidence: int
    evidence: str = ""
    authoritative: bool = True
    suggestions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def abbrev(self) -> str | None:
        return self.band.abbrev if self.band else None


def _norm_token(tok: str) -> str:
    return re.sub(r"[^a-z0-9]", "", tok.lower())


def _alias_tokens(alias: str) -> list[str]:
    return [t for t in (_norm_token(x) for x in split_tokens(alias)) if t]


def _leading_alias_match(name: str, cfg: Config) -> tuple[Band, str] | None:
    """Longest alias whose tokens are the leading tokens of `name`."""
    # Drop what normalises away to nothing.  "&" survives split_tokens as an
    # empty token while _alias_tokens discards it, so the leading comparison
    # for "Medeski, Scofield, Martin & Wood" was ['medeski','scofield',
    # 'martin',''] against ['medeski','scofield','martin','wood'] and no band
    # written with an ampersand could ever match by its leading alias.
    name_tokens = [t for t in (_norm_token(t) for t in split_tokens(name)) if t]
    best: tuple[int, Band, str] | None = None
    for band in cfg.bands:
        for alias in (band.name, *band.aliases):
            at = _alias_tokens(alias)
            if not at or len(at) > len(name_tokens):
                continue
            if name_tokens[: len(at)] == at:
                score = sum(len(t) for t in at)
                if best is None or score > best[0]:
                    best = (score, band, alias)
    return (best[1], best[2]) if best else None


def _anywhere_alias_match(name: str, cfg: Config) -> tuple[Band, str] | None:
    # Drop what normalises away to nothing.  "&" survives split_tokens as an
    # empty token while _alias_tokens discards it, so the leading comparison
    # for "Medeski, Scofield, Martin & Wood" was ['medeski','scofield',
    # 'martin',''] against ['medeski','scofield','martin','wood'] and no band
    # written with an ampersand could ever match by its leading alias.
    name_tokens = [t for t in (_norm_token(t) for t in split_tokens(name)) if t]
    best: tuple[int, Band, str] | None = None
    for band in cfg.bands:
        for alias in (band.name, *band.aliases):
            at = _alias_tokens(alias)
            if not at or len(at) > len(name_tokens):
                continue
            # Multi-word aliases only; a bare abbreviation anywhere in a name is
            # too noisy to trust.
            if len(at) < 2:
                continue
            for i in range(len(name_tokens) - len(at) + 1):
                if name_tokens[i : i + len(at)] == at:
                    score = sum(len(t) for t in at)
                    if best is None or score > best[0]:
                        best = (score, band, alias)
                    break
    return (best[1], best[2]) if best else None


def _prefix_match(name: str, cfg: Config) -> tuple[Band, str] | None:
    """A letter prefix glued to a date, e.g. mmj2003-09-26 / u111105 / UM 2012_04_19."""
    m = _PREFIX_RE.match(name)
    if not m:
        return None
    raw = m.group(1).lower()
    best: tuple[int, Band, str] | None = None
    for band in cfg.bands:
        for pfx in band.prefixes:
            if raw == pfx and (best is None or len(pfx) > best[0]):
                best = (len(pfx), band, pfx)
    return (best[1], best[2]) if best else None


def _family_member_from_track_names(
    names: list[str], cfg: Config, band: Band
) -> tuple[Band, str] | None:
    """The act named by the track filenames, when most of them agree.

    A folder called jg80-07-19... whose every track is jgb1980-07-19dNtNN is a
    Jerry Garcia Band show: the files were named once, together, by whoever
    prepared them, and there are a lot of them saying the same thing.
    """
    counts: dict[tuple[Band, str], int] = {}
    for name in names:
        found = _prefix_match(name, cfg)
        if not found:
            continue
        other, pfx = found
        if (other is band or not other.is_side_project
                or other.parent != band.parent):
            continue
        counts[(other, pfx)] = counts.get((other, pfx), 0) + 1
    if not counts:
        return None
    (other, pfx), agreeing = max(counts.items(), key=lambda kv: kv[1])
    if agreeing >= 3 and agreeing >= 0.6 * len(names):
        return (other, pfx)
    return None


def _prefer_family_member(
    masked: str,
    cfg: Config,
    hit: tuple[Band, str],
    info_lines: list[str] | None = None,
    tag_artist: str | None = None,
    track_names: list[str] | None = None,
) -> tuple[Band, str]:
    """"jg1989-1-27.jgb - orpheum" is a Jerry Garcia BAND show, not a solo one.

    When the name opens with a family filing prefix, a more specific act named
    anywhere in the evidence is the real answer.  A filing prefix is by design
    unspecific - "jg" files the whole Garcia family - so unlike an ordinary
    band match it does NOT stop us reading the info file and the tags, which
    routinely open by naming the act the folder name left out.
    """
    band, alias = hit
    if not band.family_prefix:
        return hit

    tokens = [_norm_token(t) for t in split_tokens(masked)]
    # A longer alias spelled out later ("jg_dg" -> jg dg) beats the prefix.
    longer = _anywhere_alias_match(masked, cfg)
    if longer and longer[0] is not band:
        return longer
    # ...as does a bare abbreviation of another act ("jgb").
    for token in tokens[1:]:
        for other in cfg.bands:
            if other is band or not token:
                continue
            if token == other.abbrev or token in other.prefixes:
                return (other, token)

    # Nothing in the FOLDER name is more specific.  The track filenames often
    # are, and they are the best evidence there is - many files, named in one
    # go, all saying the same thing.
    from_tracks = _family_member_from_track_names(track_names or [], cfg, band)
    if from_tracks:
        return from_tracks

    # Then the info file and the ARTIST tag: jg80-07-19.122980.aud.unknown.moore-berger opens its text file with
    # "Jerry Garcia Band".  Only another act in the SAME family may win this
    # way, so an unrelated name in the prose cannot hijack the folder.
    for label, text in ((("the ARTIST tag"), tag_artist),
                        *(("the info file", line) for line in (info_lines or []))):
        if not text:
            continue
        found = _leading_alias_match(text, cfg) or _anywhere_alias_match(text, cfg)
        if (found and found[0] is not band and found[0].is_side_project
                and found[0].parent == band.parent):
            return found
    return hit


def suggest_bands(name: str, cfg: Config, limit: int = 3) -> list[str]:
    """Fuzzy suggestions for the report only - never applied automatically."""
    lead = re.split(r"\d", name, maxsplit=1)[0].strip(" -_.")
    if len(lead) < 3:
        return []
    pool: dict[str, str] = {}
    for band in cfg.bands:
        for alias in (band.name, *band.aliases, band.abbrev):
            pool[alias.lower()] = band.abbrev
    hits = difflib.get_close_matches(lead.lower(), list(pool), n=limit, cutoff=0.72)
    return ["%s -> %s" % (h, pool[h]) for h in hits]


def resolve_band(
    folder_name: str,
    cfg: Config,
    parent_artist_dir: str | Sequence[str] | None = None,
    tag_artist: str | None = None,
    info_lines: list[str] | None = None,
    track_names: list[str] | None = None,
) -> BandResolution:
    notes: list[str] = []

    # Blank the date first: in "jg+dg92-05-10" the date is glued to the band,
    # so the token reads "dg92" and no alias can match it.
    masked = mask_spans(folder_name, [c.span for c in find_dates(folder_name)])

    hit = _leading_alias_match(masked, cfg)
    if hit:
        hit = _prefer_family_member(masked, cfg, hit, info_lines, tag_artist,
                                    track_names)
        band, alias = hit
        if band.is_side_project:
            notes.append("side project (%s) - parent folder is not authoritative" % band.name)
        return BandResolution(band, "alias_leading", CONFIDENCE["alias_leading"],
                              "folder name starts with '%s'" % alias, notes=notes)

    hit = _prefix_match(folder_name, cfg)
    if hit:
        hit = _prefer_family_member(masked, cfg, hit, info_lines, tag_artist,
                                    track_names)
        band, pfx = hit
        if band.is_side_project:
            notes.append("side project (%s) - parent folder is not authoritative" % band.name)
        return BandResolution(band, "prefix", CONFIDENCE["prefix"],
                              "prefix '%s' before the date" % pfx, notes=notes)

    if tag_artist:
        hit = _leading_alias_match(tag_artist, cfg) or _anywhere_alias_match(tag_artist, cfg)
        if hit:
            band, alias = hit
            return BandResolution(band, "tag_artist", CONFIDENCE["tag_artist"],
                                  "ARTIST tag '%s'" % tag_artist, notes=notes)

    hit = _anywhere_alias_match(masked, cfg)
    if hit:
        band, alias = hit
        return BandResolution(band, "alias_anywhere", CONFIDENCE["alias_anywhere"],
                              "'%s' appears inside the folder name" % alias, notes=notes)

    if info_lines:
        # An info file usually opens by naming the act, before any "Key: value"
        # lines start.  On a folder whose tags are empty this is the only place
        # the band is written down.
        for line in info_lines:
            hit = _leading_alias_match(line, cfg) or _anywhere_alias_match(line, cfg)
            if hit:
                band, alias = hit
                if band.is_side_project:
                    notes.append("side project (%s)" % band.name)
                return BandResolution(band, "info_file", CONFIDENCE["info_file"],
                                      "the info file says %r" % line.strip()[:60],
                                      notes=notes)

    # A string or a chain of them, nearest directory first.  A container folder
    # ("Live Music") names no act, so the nearest folder that does is the answer;
    # trying only the top of the chain left every show under a container with no
    # band at all.
    chain = ([parent_artist_dir] if isinstance(parent_artist_dir, str)
             else list(parent_artist_dir or ()))
    for parent in chain:
        if not parent:
            continue
        hit = _leading_alias_match(parent, cfg) or _anywhere_alias_match(parent, cfg)
        if hit:
            band, alias = hit
            notes.append("attributed from the parent artist folder, which is not authoritative")
            return BandResolution(band, "parent_folder", CONFIDENCE["parent_folder"],
                                  "parent directory '%s'" % parent,
                                  authoritative=False, notes=notes)

    return BandResolution(None, None, 0, "no band evidence",
                          suggestions=suggest_bands(folder_name, cfg), notes=notes)
