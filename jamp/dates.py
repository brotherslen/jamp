"""Show-date extraction and resolution.

Two layers:

* ``find_dates(text)`` - pure lexical extraction.  Returns every date-shaped
  token in a string, with the pattern that matched, whether the reading is
  ambiguous, and what the alternative readings would be.
* ``resolve_date(evidence)`` - weighs candidates coming from several independent
  places (folder name, nested folder, cue sheet, info file, track filenames) and
  produces one answer plus a 0-100 confidence and a human-readable reason trail.

Rules that are deliberately hard-coded:

* A bare year is never a date.  It is returned separately as year-only evidence
  so a folder like "Umphreys McGee Bonnaroo 2008" is reported as "needs a date"
  rather than silently dated 2008-01-01.
* Nothing is ever inferred from a tag here.  The caller decides which evidence
  is admissible; phase 1 refuses to pass tag evidence for disc rips.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field

EARLIEST_DEFAULT = _dt.date(1960, 1, 1)

# Separators seen between date parts in real folder names: - _ . / and space.
_SEP = r"[-._/ ]"

# Base confidence per pattern, before any corroboration or penalties.
# Overridable from the config file via apply_confidence_overrides().
BASE_CONFIDENCE = {
    "iso_full": 90,
    "iso_compact": 80,
    # A repaired year-day-month typo is a reading, not a fact: scored so it can
    # never reach the commit threshold on its own, only with corroboration.
    "iso_swapped": 45,
    "us_full": 72,
    "us_short": 60,
    # 80-03-01.  Scored like us_short and given the same unambiguous bonus, so
    # a year that cannot be a month scores 72 while "01-02-03" stays low.
    "yy_mm_dd": 60,
    "six_digit": 58,
    # "New Year's Eve 2006" names a night without naming a date, and which
    # night is genuinely ambiguous - 31 Dec 2006, or the show that rang 2006 in.
    # Scored so it can never commit on its own; it exists to turn "no date
    # found" into a suggestion you can confirm.
    "new_years_eve": 40,
}

# Patterns that could in principle be read more than one way.  When only one
# reading survives validation (10-31-91 can only be October), that is real
# evidence and the candidate earns this back.
AMBIGUOUS_BY_NATURE = frozenset({"us_full", "us_short", "six_digit", "yy_mm_dd"})
UNAMBIGUOUS_BONUS = 12


def apply_confidence_overrides(overrides: dict) -> None:
    """Let the config file retune the scoring without touching the code."""
    for key, value in (overrides or {}).items():
        if key == "unambiguous_bonus":
            globals()["UNAMBIGUOUS_BONUS"] = int(value)
        elif key in BASE_CONFIDENCE:
            BASE_CONFIDENCE[key] = int(value)
        else:
            raise KeyError("unknown date confidence key: %r" % key)

# Weight per evidence source.  Multiplied into the base confidence.
SOURCE_WEIGHT = {
    "folder": 1.00,
    "child_folder": 0.95,
    "cue": 0.90,
    "info": 0.85,
    "parent_folder": 0.80,
    # The track filenames are written by the same hand as the folder name, at
    # the same time, and etree convention puts the date in both.  A date on
    # every file is close to as good as one on the folder.
    "tracks": 0.80,
    "tags": 0.55,
    # A store writes the real show date into ALBUM even when DATE holds only the
    # release year, and it is only admitted for official, single-date releases.
    # Weighted like an info file: on a folder called "New Folder" it is the only
    # thing that knows when the show was.
    "tags_album": 0.85,
}


@dataclass(frozen=True)
class DateCandidate:
    date: _dt.date
    raw: str
    pattern: str
    span: tuple[int, int]
    ambiguous: bool = False
    alternatives: tuple[_dt.date, ...] = ()

    @property
    def base_confidence(self) -> int:
        score = BASE_CONFIDENCE.get(self.pattern, 40)
        if self.pattern in AMBIGUOUS_BY_NATURE and not self.ambiguous:
            score += UNAMBIGUOUS_BONUS
        return score

    @property
    def iso(self) -> str:
        return self.date.isoformat()


@dataclass
class DateEvidence:
    """Candidates found in one place, e.g. the folder name."""

    source: str
    text: str
    candidates: list[DateCandidate] = field(default_factory=list)
    years_only: list[int] = field(default_factory=list)
    # Set when this particular evidence is worth more than its source usually
    # is - a date repeated across twenty filenames, say.
    weight_override: float | None = None

    @property
    def weight(self) -> float:
        if self.weight_override is not None:
            return self.weight_override
        return SOURCE_WEIGHT.get(self.source, 0.5)


@dataclass
class DateResolution:
    date: _dt.date | None
    confidence: int
    reasons: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    ambiguous: bool = False
    alternatives: tuple[_dt.date, ...] = ()
    years_only: tuple[int, ...] = ()
    distinct_dates: tuple[_dt.date, ...] = ()

    @property
    def iso(self) -> str | None:
        return self.date.isoformat() if self.date else None


def _valid(y: int, m: int, d: int, earliest: _dt.date, latest: _dt.date) -> _dt.date | None:
    try:
        out = _dt.date(y, m, d)
    except ValueError:
        return None
    if out < earliest or out > latest:
        return None
    return out


def expand_two_digit_year(yy: int, today: _dt.date | None = None) -> int:
    """77 -> 1977, 11 -> 2011.  Pivot on the current year's last two digits."""
    today = today or _dt.date.today()
    pivot = today.year % 100
    return 2000 + yy if yy <= pivot else 1900 + yy


def _iso_builder(m, earliest, latest):
    y, a, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
    primary = _valid(y, a, b, earliest, latest)
    if primary:
        return ("iso", primary, (), False)
    # A year-day-month typo such as 1973-30-05.
    swapped = _valid(y, b, a, earliest, latest)
    if swapped:
        return ("iso_swapped", swapped, (), False)
    return None


def _us_builder(m, earliest, latest, short: bool, today: _dt.date):
    a, b = int(m.group(1)), int(m.group(2))
    y = int(m.group(3))
    if short:
        y = expand_two_digit_year(y, today)
    md = _valid(y, a, b, earliest, latest)  # US convention: month first
    dm = _valid(y, b, a, earliest, latest)  # the day-first reading
    if md and dm and md != dm:
        return ("us", md, (dm,), True)
    if md:
        return ("us", md, (), False)
    if dm:
        return ("us", dm, (), False)
    return None


def _yy_mm_dd_builder(m, earliest, latest):
    """80-03-01 -> 1980-03-01.  Month and day are already pinned by the regex."""
    a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
    got = _valid(expand_two_digit_year(a, latest), b, c, earliest, latest)
    if not got:
        return None
    # When the first pair could itself be a month, "01-02-03" is genuinely two
    # different dates and we say so rather than picking one silently.
    other = _valid(expand_two_digit_year(c, latest), a, b, earliest, latest) if a <= 12 else None
    if other and other != got:
        return ("yy_mm_dd", got, (other,), True)
    return ("yy_mm_dd", got, (), False)


def _six_digit_builder(m, earliest, latest, today: _dt.date):
    a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
    readings: list[_dt.date] = []
    # etree convention first: yymmdd (u111105 -> 2011-11-05, mmj231103 -> 2023-11-03)
    for y, mo, d in (
        (expand_two_digit_year(a, today), b, c),   # yymmdd
        (expand_two_digit_year(c, today), a, b),   # mmddyy
        (expand_two_digit_year(c, today), b, a),   # ddmmyy
    ):
        got = _valid(y, mo, d, earliest, latest)
        if got and got not in readings:
            readings.append(got)
    if not readings:
        return None
    return ("six_digit", readings[0], tuple(readings[1:]), len(readings) > 1)


_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("iso_full", re.compile(r"(?<!\d)((?:19|20)\d{2})" + _SEP + r"(\d{1,2})" + _SEP + r"(\d{1,2})(?!\d)")),
    ("iso_compact", re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)")),
    ("us_full", re.compile(r"(?<!\d)(\d{1,2})" + _SEP + r"(\d{1,2})" + _SEP + r"((?:19|20)\d{2})(?!\d)")),
    ("us_short", re.compile(r"(?<!\d)(\d{1,2})" + _SEP + r"(\d{1,2})" + _SEP + r"(\d{2})(?!\d)")),
    # "80-03-01" - a two-digit year first, the shape etree filenames and some
    # album tags use.  Deliberately AFTER us_short: in
    # "MMJ-Wiltern-9-12-12-01.flac" this pattern would otherwise swallow
    # "12-12-01", the tail of the date plus the track number.  us_short reads
    # the real date first and consumes it; only what us_short cannot make sense
    # of ("80-03-01" has no 80th month) reaches this.
    ("yy_mm_dd", re.compile(r"(?<!\d)(\d{2})" + _SEP + r"(0?[1-9]|1[0-2])" + _SEP
                            + r"(0?[1-9]|[12]\d|3[01])(?!\d)")),
    ("six_digit", re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)")),
]

_YEAR_ONLY = re.compile(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)")

# A date followed by a clock is when a file was made, not when a show was
# played: "XLD extraction logfile from 2015-11-01 12:41:08".
_CLOCK_AFTER = re.compile(r"^[\sT,]*\d{1,2}:\d{2}")

# ...and a compact run of digits straight after "version" is a build number:
# "X Lossless Decoder version 20141129" is not 29 November 2014.
_VERSION_BEFORE = re.compile(r"(?:version|build|rev(?:ision)?|v)\s*$", re.I)


def _is_tool_metadata(text: str, start: int, end: int) -> bool:
    """Is this date-shaped thing a timestamp or a version number?"""
    if _CLOCK_AFTER.match(text[end:end + 12]):
        return True
    return bool(_VERSION_BEFORE.search(text[max(0, start - 12):start]))


def _is_etree_source_id(text: str, start: int, end: int) -> bool:
    """A six-digit field of its own in an etree name is a source ID.

    etree names carry the number the recording is catalogued under, in a field
    of its own: jg83-06-01.010962.jgb.nak701.beach.  Read as a date, 010962
    becomes 1962-01-09 and conflicts with the show date sitting right beside
    it, which is enough to block the whole folder.  The number is delimited on
    both sides, so it is easy to tell from a bare yymmdd.
    """
    if end - start != 6:
        return False
    before = text[start - 1] if start else ""
    after = text[end] if end < len(text) else ""
    return before == "." and after in (".", "")

_NYE = re.compile(
    r"(?:(?:new\s*year'?s?\s*eve|\bnye\b)\W{0,4}(?P<after>(?:19|20)\d{2})"
    r"|(?P<before>(?:19|20)\d{2})\W{0,4}(?:new\s*year'?s?\s*eve|\bnye\b))",
    re.I,
)


def _find_new_years_eve(text, earliest, latest, consumed):
    """"New Year's Eve 2006" -> 31 Dec 2006, with 31 Dec 2005 as the alternative."""
    out: list[DateCandidate] = []
    for m in _NYE.finditer(text):
        start, end = m.span()
        if any(consumed[start:end]):
            continue
        year = int(m.group("after") or m.group("before"))
        readings = [d for d in (_valid(year, 12, 31, earliest, latest),
                                _valid(year - 1, 12, 31, earliest, latest)) if d]
        if not readings:
            continue
        for i in range(start, end):
            consumed[i] = True
        out.append(
            DateCandidate(
                date=readings[0], raw=m.group(0), pattern="new_years_eve",
                span=(start, end), ambiguous=len(readings) > 1,
                alternatives=tuple(readings[1:]),
            )
        )
    return out


def find_dates(
    text: str,
    earliest: _dt.date | None = None,
    today: _dt.date | None = None,
    year_range: tuple[int, int] | None = None,
    allow_six_digit: bool = True,
) -> list[DateCandidate]:
    """Every date-shaped token in `text`, most specific pattern winning.

    `year_range` (a band's active years) only ever demotes: an alternative
    reading inside the range is promoted over a primary reading outside it.
    """
    today = today or _dt.date.today()
    earliest = earliest or EARLIEST_DEFAULT
    consumed = [False] * (len(text) + 1)
    found: list[DateCandidate] = []

    for name, pattern in _PATTERNS:
        # A bare run of six digits means yymmdd in a filename and nothing at all
        # in prose, where it is a CRC, a sector count or a catalogue number.
        if name == "six_digit" and not allow_six_digit:
            continue
        for m in pattern.finditer(text):
            start, end = m.span()
            if any(consumed[start:end]):
                continue
            if name == "six_digit" and _is_etree_source_id(text, start, end):
                for i in range(start, end):
                    consumed[i] = True
                continue
            if _is_tool_metadata(text, start, end):
                for i in range(start, end):
                    consumed[i] = True
                continue
            if name in ("iso_full", "iso_compact"):
                built = _iso_builder(m, earliest, today)
            elif name == "us_full":
                built = _us_builder(m, earliest, today, False, today)
            elif name == "yy_mm_dd":
                built = _yy_mm_dd_builder(m, earliest, today)
            elif name == "us_short":
                built = _us_builder(m, earliest, today, True, today)
            else:
                built = _six_digit_builder(m, earliest, today, today)
            if not built:
                continue
            kind, primary, alts, ambiguous = built
            pattern_name = "iso_swapped" if kind == "iso_swapped" else name

            if year_range:
                lo, hi = year_range
                ordered = [primary, *alts]
                inside = [d for d in ordered if lo <= d.year <= hi]
                if inside:
                    if inside[0] != primary:
                        primary = inside[0]
                    alts = tuple(d for d in ordered if d != primary)
                    if len(inside) == 1:
                        ambiguous = False

            for i in range(start, end):
                consumed[i] = True
            found.append(
                DateCandidate(
                    date=primary,
                    raw=m.group(0),
                    pattern=pattern_name,
                    span=(start, end),
                    ambiguous=ambiguous,
                    alternatives=tuple(alts),
                )
            )
    found.extend(_find_new_years_eve(text, earliest, today, consumed))
    found.sort(key=lambda c: c.span[0])
    return found


def find_date_range(text: str, dates: list[DateCandidate] | None = None):
    """A date written as a span of nights: "2002.07.18-19" is two shows.

    Only the compact form, where a second day is hung straight off a full date.
    One folder cannot be one show if its own name says it holds two, and the
    tail was being read as part of the venue as well - "2002.07.18-19 Mishawaka
    Compilation" was heading for a folder called "19 Mishawaka Compilation".

    Returns (start, end) or None.  The second day has to be a real day later in
    the same month, which is what keeps a disc suffix like "1977-05-08-1" out.
    """
    if not text:
        return None
    for cand in dates if dates is not None else find_dates(text):
        tail = re.match(r"\s*[-–]\s*(\d{1,2})(?![\d])", text[cand.span[1]:])
        if not tail:
            continue
        day = int(tail.group(1))
        if day <= cand.date.day:
            continue
        try:
            end = cand.date.replace(day=day)
        except ValueError:
            continue
        return cand.date, end
    return None


def find_years_only(text: str, consumed_spans: list[tuple[int, int]] | None = None) -> list[int]:
    """Four-digit years that are not part of a full date.

    Evidence that a show exists whose date we do NOT know - never a date.
    """
    blocked: set[int] = set()
    for start, end in consumed_spans or []:
        blocked.update(range(start, end))
    out: list[int] = []
    for m in _YEAR_ONLY.finditer(text):
        if any(i in blocked for i in range(*m.span())):
            continue
        out.append(int(m.group(1)))
    return out


def evidence_from_text(
    source: str,
    text: str,
    earliest: _dt.date | None = None,
    today: _dt.date | None = None,
    year_range: tuple[int, int] | None = None,
    allow_six_digit: bool = True,
) -> DateEvidence:
    cands = find_dates(text, earliest=earliest, today=today, year_range=year_range,
                       allow_six_digit=allow_six_digit)
    years = find_years_only(text, [c.span for c in cands])
    return DateEvidence(source=source, text=text, candidates=cands, years_only=years)


def resolve_date(evidences: list[DateEvidence]) -> DateResolution:
    """Combine evidence from several places into one dated answer."""
    scores: dict[_dt.date, float] = {}
    sources: dict[_dt.date, set[str]] = {}
    best_cand: dict[_dt.date, DateCandidate] = {}
    hits: dict[_dt.date, int] = {}
    reasons: list[str] = []
    years_only: list[int] = []

    for ev in evidences:
        years_only.extend(ev.years_only)
        seen_here: set[_dt.date] = set()
        for cand in ev.candidates:
            hits[cand.date] = hits.get(cand.date, 0) + 1
            score = cand.base_confidence * ev.weight
            if cand.date not in scores or score > scores[cand.date]:
                scores[cand.date] = score
                best_cand[cand.date] = cand
            if cand.date not in seen_here:
                sources.setdefault(cand.date, set()).add(ev.source)
                seen_here.add(cand.date)

    if not scores:
        return DateResolution(
            date=None,
            confidence=0,
            reasons=["no date-shaped token found in any evidence"],
            years_only=tuple(dict.fromkeys(years_only)),
        )

    # An info file, a log or a tag records when the tape was transferred,
    # seeded or checked as well as when the show was.  A date years LATER than
    # one written in the folder or the filenames, and mentioned nowhere but in
    # that prose, is about the copy - so it is set aside before ranking rather
    # than allowed to outscore or conflict with the concert date.
    _COPY_SOURCES = {"info", "tags", "tags_album", "log", "cue"}
    name_dates = [d for d, s in sources.items() if s - _COPY_SOURCES]
    demoted: list[str] = []
    if name_dates:
        newest_named = max(name_dates)
        for d in list(scores):
            if (not (sources[d] - _COPY_SOURCES)
                    and d.year - newest_named.year >= 3):
                demoted.append(
                    "%s in %s is %d years later than the date in the name - "
                    "read as a transfer date, not the show"
                    % (d.isoformat(), sorted(sources[d])[0], d.year - newest_named.year))
                del scores[d]
    if not scores:  # pragma: no cover - defensive; name_dates keeps one alive
        scores = {d: 0.0 for d in best_cand}

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    winner, raw_score = ranked[0]
    cand = best_cand[winner]
    conf = raw_score
    reasons.append(
        "%s from %s '%s' in %s (base %d)"
        % (winner.isoformat(), cand.pattern, cand.raw, sorted(sources[winner])[0], cand.base_confidence)
    )
    reasons.extend(demoted)

    agreeing = len(sources[winner])
    if agreeing > 1:
        bonus = min(10 * (agreeing - 1), 20)
        conf += bonus
        reasons.append("corroborated by %d independent sources (+%d)" % (agreeing, bonus))
    elif hits.get(winner, 0) > 1:
        # Same source, two differently formatted spellings of the same date,
        # e.g. MMJ2012-09-12.MMJ-Wiltern-9-12-12.
        conf += 5
        reasons.append("the same date is written twice in one name (+5)")

    conflicts: list[str] = []
    for other, other_score in ranked[1:]:
        if other_score >= raw_score - 20:
            conflicts.append(
                "%s also found in %s (score %.0f vs %.0f)"
                % (other.isoformat(), sorted(sources[other]), other_score, raw_score)
            )
    if conflicts:
        conf -= 25
        reasons.append("conflicting dates found (-25)")

    ambiguous = cand.ambiguous and agreeing < 2
    if ambiguous:
        conf -= 15
        reasons.append(
            "ambiguous reading, could also be "
            + ", ".join(a.isoformat() for a in cand.alternatives)
            + " (-15)"
        )
    elif cand.ambiguous:
        reasons.append("ambiguity resolved by a second source")

    conf = int(max(0, min(99, round(conf))))
    return DateResolution(
        date=winner,
        confidence=conf,
        reasons=reasons,
        conflicts=conflicts,
        ambiguous=ambiguous,
        alternatives=cand.alternatives,
        years_only=tuple(dict.fromkeys(years_only)),
        distinct_dates=tuple(d for d, _ in ranked),
    )
