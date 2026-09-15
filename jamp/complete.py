"""Is a recording missing songs?

This is the larger prize, and it is the one
question the rest of the pipeline cannot answer at all. Everything else here
asks *what is this folder* - which band, which night, which recording. Nothing
asks whether the recording is **whole**. A fourteen-track folder of a
twenty-song night is named correctly, tagged correctly, settles cleanly, and is
missing six songs.

**Why the existing alignment cannot see it.** `setlist.align` scores how much of
*our* duration found an answer in theirs. That is the right measure for the job
it was built for - deciding whether a title is safe to write - but it is
deliberately one-sided. A recording missing its last four songs matches
perfectly on every track it does have, and scores about 100%.

The signal is the mirror of it: **their** tracks that answer to nothing of ours,
measured in seconds. High score on our side plus a big leftover on theirs means
we hold a subset of that night. Low on both means it is a different recording
and says nothing about completeness.

**Conservative on purpose.** Among the copies that account for our tracks, the
one with the *smallest* leftover is the claim. If any copy of that night says we
are complete, we are complete; only when every candidate leaves the same songs
over is a shortfall real. That cannot over-claim, which matters because the
answer here is a claim about a recording somebody may go and re-download.

**Two grades of evidence, never mixed.** archive.org and phish.in publish
per-track durations, so a shortfall is measured in seconds. jerrybase, phish.net
and the MMJ archive publish a setlist and no times, so the only honest test is a
count - and a count has a floor rather than a value, because a segue may be one
file or two. Below `songs - segues` a song is genuinely absent; above it, extra
tracks are tuning, banter and filler. The report never prints one as the other.

**A stated shortfall is not a discovery.** A folder whose own name says `set 2`,
`disc 1`, `partial` or `soundcheck` is short because you already know it is
short, and listing those beside real truncations would bury them.
"""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import setlist
from .report import report_file

COMPLETE = "COMPLETE"
TRUNCATED = "TRUNCATED"
SHORT_BY_COUNT = "SHORT_BY_COUNT"      # no durations: fewer tracks than the floor
STATED_PARTIAL = "STATED_PARTIAL"      # the folder says so itself
NO_REFERENCE = "NO_REFERENCE"
NOT_COMPARABLE = "NOT_COMPARABLE"      # nothing aligned well enough to judge
REFERENCE_SHORTER = "REFERENCE_SHORTER"   # the only reference is a part-upload

# Our side must be this well accounted for before their leftover means anything.
# Below it the two are simply different recordings.
MIN_OURS = 0.85
# ...or theirs is, which is the same performance seen from the other end: we
# hold everything they do, plus material they omit.
MIN_THEIRS = 0.85
# Their leftover has to be worth reporting. A few seconds is applause trimmed at
# a join; two minutes is a song.
MIN_MISSING_SECONDS = 100.0
MIN_MISSING_SHARE = 0.04
# Part-uploads are discarded before kindness is considered. Taking the kindest
# of ALL candidates sounds conservative and is the opposite: archive.org is
# full of one-track uploads, and a fragment leaves nothing over, so it wins on
# kindness every time and pronounces every folder complete.
#
# Half the longest candidate is the line, and it is loose on purpose. A
# fragment is a few percent of the night. Two honest copies of one night differ
# by far less - one includes the soundcheck, another starts at the first song -
# and tightening this to exclude those would pick the copy with the MOST extra
# material and report the difference as missing songs, which is the false
# positive this whole check has to avoid.
FULLEST_WITHIN = 0.50
# A reference shorter than our own recording cannot demonstrate a shortfall.
REFERENCE_MUST_REACH = 0.90
# Missing songs means we are a SUBSET. If we also hold material the reference
# does not, in the same order of size, we are not a subset - the two copies are
# tracked differently and the aligner has paired them up as best it can.
#
# The alignment score is no use for spotting this: those cases score 0.85-0.89,
# just above any gate worth setting, because the aligner does find a pairing -
# it is simply the wrong one. The symmetry of what is left over is the tell.
MAX_EXTRA_SHARE_OF_MISSING = 0.5

# The folder saying it is only part of a show.
_PARTIAL = re.compile(
    r"(?<![a-z])(set\s*[123][a-z]?|s[123][a-z]?|disc\s*\d|d[123]|cd\s*\d|"
    r"partial|incomplete|excerpt|excerpts|fragment|soundcheck|encore|filler|"
    r"bonus)(?![a-z0-9])", re.I)


@dataclass
class LocalShow:
    rel: str
    band: str | None
    date: str | None
    seconds: list = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(self.seconds)

    @property
    def stated_partial(self) -> bool:
        return bool(_PARTIAL.search(Path(self.rel).name))


@dataclass
class Finding:
    rel: str
    verdict: str
    band: str | None = None
    date: str | None = None
    ours_tracks: int = 0
    ours_seconds: float = 0.0
    theirs_tracks: int = 0
    theirs_seconds: float = 0.0
    missing_seconds: float = 0.0
    missing_titles: list = field(default_factory=list)
    # Where in the show the missing songs sit. The difference between "this
    # file got cut off" and "you only have set two" is the whole usefulness of
    # the answer, and it is visible in nothing but the shape.
    shape: str = ""
    # Tracks of ours the reference does not account for. Usually filler - a
    # spare track from another night used to fill a disc.
    extra_seconds: float = 0.0
    extra_shape: str = ""
    score: float = 0.0
    source: str = ""
    identifier: str = ""
    evidence: str = ""
    note: str = ""

    @property
    def missing_share(self) -> float:
        return (self.missing_seconds / self.theirs_seconds) if self.theirs_seconds else 0.0


# --- reading what phase 0 already wrote -------------------------------------

def load_shows(identity_csv: Path, folders_csv: Path) -> list:
    """Local durations and each folder's band and date, from phase 0's reports.

    Deliberately reads the CSVs rather than walking the library: phase 0 has
    already opened every file, the numbers do not change between runs, and a
    check that costs nothing to re-run gets re-run.
    """
    meta: dict = {}
    with open(folders_csv, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            meta[row["relative_path"]] = (row.get("band") or None,
                                          row.get("date") or None)
    tracks: dict = defaultdict(list)
    with open(identity_csv, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            try:
                secs = float(row["seconds"])
            except (KeyError, TypeError, ValueError):
                continue
            if secs > 0:
                tracks[row["relative_path"]].append(secs)
    out = []
    for rel, secs in tracks.items():
        band, date = meta.get(rel, (None, None))
        out.append(LocalShow(rel=rel, band=band, date=date, seconds=secs))
    out.sort(key=lambda s: s.rel)
    return out


def in_scope(shows: list, artists) -> list:
    """The folders under the named top-level folders, or all of them.

    --artist was accepted, "scoped to" was printed, and every folder was judged
    anyway.  Paths come from phase 0's report, relative to its ROOT, so the
    artist is the first component, matched the way walk_dirs matches it.
    """
    if not artists:
        return shows
    wanted = [a.replace("\\", "/").strip("/").lower() for a in artists]
    out = []
    for s in shows:
        rel = s.rel.replace("\\", "/").lower()
        if any(rel == w or rel.startswith(w + "/") for w in wanted):
            out.append(s)
    return out


# --- the measurement --------------------------------------------------------

def ours_leftover(a, ours: list) -> tuple:
    """(seconds of OURS that answered to nothing, our indexes).

    Filler is the reason this is worth knowing.  Spare tracks from another
    night are ordinary in trading - the date reader already treats a
    "Filler:" line as somebody else's date - and they answer to nothing in
    this night's reference without meaning anything is wrong.  Reported
    rather than corrected for: a block of unmatched tracks at the end of a
    folder is filler, and the same block in the middle is not.
    """
    unmatched = []
    for step in a.steps:
        if step.op == setlist.SKIP_OURS:
            unmatched.extend(step.ours)
    return sum(ours[i] for i in unmatched if i < len(ours)), unmatched


def leftover(a, theirs: list) -> tuple:
    """(seconds of theirs that answered to nothing, their indexes).

    This is the whole check.  `a.score` says how much of ours found an answer;
    this says how much of theirs did not, which is the question nothing else
    in the pipeline asks.
    """
    unmatched = []
    for step in a.steps:
        if step.op == setlist.SKIP_THEIRS:
            unmatched.extend(step.theirs)
    return sum(theirs[i] for i in unmatched if i < len(theirs)), unmatched


def best_reading(ours: list, candidates: list) -> tuple:
    """The kindest candidate that still accounts for our tracks.

    Kindest on purpose: if any copy of the night says we are complete, we are.
    A shortfall is only real when every copy that fits leaves the same songs
    over, so this cannot over-claim - and over-claiming would send somebody
    re-downloading a show that is already whole.
    """
    fitting = []
    for rec in candidates:
        theirs = [t.seconds for t in rec.tracks]
        if not theirs or not all(theirs):
            continue
        a = setlist.align(list(ours), list(theirs))
        missing, idx = leftover(a, theirs)
        total = sum(theirs)
        their_cover = (total - missing) / total if total else 0.0
        # The two sides are tested separately, because either one being high
        # means these are the same performance.  Our score alone was not
        # enough: a folder holding a soundcheck the reference omits matches
        # only 60% of its own duration, and gating on that dropped the
        # candidate entirely - so "you have extra material" came back as "we
        # cannot compare these", which is a different and much less useful
        # statement.  Both low is the honest incomparable.
        if a.score < MIN_OURS and their_cover < MIN_THEIRS:
            continue
        fitting.append((missing, idx, a, rec, theirs, total))
    if not fitting:
        return None
    # The fullest accounts of the night, and only then the kindest of those.
    longest = max(f[5] for f in fitting)
    full = [f for f in fitting if f[5] >= longest * FULLEST_WITHIN]
    best = min(full, key=lambda f: f[0])
    return best[:5]


TAIL = "cut short at the end"
HEAD = "missing the opening"
BLOCK = "one continuous stretch missing"
SCATTERED = "songs missing throughout"


def shape_of(unmatched: list, total: int) -> str:
    """Where the gap is, which is what says what kind of gap it is.

    A run at the end is a recording that stopped - a dead battery, a tape that
    ran out, a truncated download.  A single block anywhere is far more often
    one set of a two-set night than damage.  Songs missing all through it is
    neither: usually a taper who cut the ones they did not want, and never
    something to go re-downloading over without looking first.
    """
    if not unmatched:
        return ""
    lo, hi = min(unmatched), max(unmatched)
    contiguous = (hi - lo + 1) == len(unmatched)
    if contiguous and hi == total - 1:
        return TAIL
    if contiguous and lo == 0:
        return HEAD
    return BLOCK if contiguous else SCATTERED


def judge(show: LocalShow, candidates: list, source: str) -> Finding:
    """Compare one folder against every copy of its night that has durations."""
    f = Finding(rel=show.rel, verdict=NO_REFERENCE, band=show.band, date=show.date,
                ours_tracks=len(show.seconds), ours_seconds=show.total,
                source=source, evidence="duration alignment")
    if not candidates:
        f.note = "no copy of this night with per-track durations"
        return f

    got = best_reading(show.seconds, candidates)
    if got is None:
        f.verdict = NOT_COMPARABLE
        f.note = ("%d copies of this night, none of which accounts for our "
                  "tracks well enough to judge - so this says nothing about "
                  "completeness" % len(candidates))
        return f

    missing, idx, a, rec, theirs = got
    f.score = a.score
    f.theirs_tracks = len(theirs)
    f.theirs_seconds = sum(theirs)
    f.missing_seconds = missing
    f.identifier = getattr(rec, "identifier", "") or getattr(rec, "date", "") or ""
    f.missing_titles = [rec.tracks[i].title for i in idx
                        if i < len(rec.tracks) and rec.tracks[i].title][:12]
    f.shape = shape_of(idx, len(theirs))
    extra, ours_idx = ours_leftover(a, show.seconds)
    f.extra_seconds = extra
    f.extra_shape = shape_of(ours_idx, len(show.seconds))

    # A reference shorter than our own recording cannot show us to be missing
    # anything, however well it aligns. archive.org is full of part-uploads -
    # one track of a two-hour night - and calling our full copy "complete" on
    # the strength of one is not a finding, it is an absence of one.
    if f.theirs_seconds < f.ours_seconds * REFERENCE_MUST_REACH:
        f.verdict = REFERENCE_SHORTER
        f.note = ("the fullest copy of this night is %.0f minutes against our "
                  "%.0f, so it cannot show a shortfall%s"
                  % (f.theirs_seconds / 60, f.ours_seconds / 60,
                     "; everything it holds is here" if missing < MIN_MISSING_SECONDS
                     else ""))
        return f

    # Missing songs means we are a SUBSET of that night. If we also hold
    # substantial material the reference does not, we are not a subset - we are
    # a different tracking of it, or a different recording, and the two lists
    # simply did not line up. 75 of the first run's 103 truncations were this:
    # tens of minutes unaccounted for in both directions at once, reported as
    # though the shortfall were one-way. The gate accepts either side being
    # high, which is right for deciding these are the same performance and
    # wrong for concluding anything about completeness from it.
    if (missing >= MIN_MISSING_SECONDS
            and f.extra_seconds > missing * MAX_EXTRA_SHARE_OF_MISSING):
        f.verdict = NOT_COMPARABLE
        f.note = ("%.0f minutes of the reference are unaccounted for here and "
                  "%.0f minutes here are unaccounted for in it - the two are "
                  "tracked too differently to say whether anything is missing"
                  % (missing / 60, f.extra_seconds / 60))
        return f

    if missing < MIN_MISSING_SECONDS or f.missing_share < MIN_MISSING_SHARE:
        f.verdict = COMPLETE
        f.note = ("accounts for all but %.0fs of a %.0f-minute reference"
                  % (missing, f.theirs_seconds / 60))
        if f.extra_seconds > MIN_MISSING_SECONDS:
            f.note += ("; %.0f minutes here answer to nothing in it (%s) - "
                       "filler, most likely"
                       % (f.extra_seconds / 60, f.extra_shape or "no clear pattern"))
        return f

    f.verdict = STATED_PARTIAL if show.stated_partial else TRUNCATED
    f.note = ("%.0f minutes of the reference answer to nothing here - %.0f%% of "
              "it - across %d of their %d tracks; %s"
              % (missing / 60, f.missing_share * 100, len(idx), len(theirs),
                 f.shape or "no clear pattern"))
    if show.stated_partial:
        f.note += "; the folder name says it is only part of a show"
    return f


def judge_by_count(show: LocalShow, songs: int, segues: int | None, source: str,
                   identifier: str = "") -> Finding:
    """No durations: a floor, never a value.

    A segue may be one file or two, so the count can honestly land anywhere
    from `songs - segues` upwards.  Below that floor a song is genuinely
    absent; above it, the extra tracks are tuning, banter and filler.  Anything
    more precise than that would be inventing certainty this source cannot give.

    `segues` is None when the source does not say.  jerrybase publishes none,
    and passing 0 for it collapsed the floor to the song count and printed
    "0 segue(s)" as if it had been measured - every SHORT_BY_COUNT the first
    run found was that, including Winterland 1973-11-09, 25 tracks against 28
    songs, which three segues explain.  With the segues unknown a count at or
    above the setlist still says complete; one below it is not judged.
    """
    f = Finding(rel=show.rel, verdict=COMPLETE, band=show.band, date=show.date,
                ours_tracks=len(show.seconds), ours_seconds=show.total,
                theirs_tracks=songs, source=source, identifier=identifier,
                evidence="track count against the setlist; this source "
                         "publishes no durations")
    if segues is None:
        if len(show.seconds) >= songs:
            f.note = ("%d tracks against %d songs - at or above the setlist, "
                      "which is as much as a count can say" % (len(show.seconds), songs))
        else:
            f.verdict = NOT_COMPARABLE
            f.note = ("%d tracks against %d songs, and %s does not say how many of "
                      "them are segues, so a song joined to the next in one file "
                      "cannot be told from a song that is missing - not judged"
                      % (len(show.seconds), songs, source))
        return f
    floor = max(0, songs - segues)
    if len(show.seconds) < floor:
        f.verdict = STATED_PARTIAL if show.stated_partial else SHORT_BY_COUNT
        f.missing_titles = []
        f.note = ("%d tracks against a setlist of %d songs with %d segue(s), so "
                  "at least %d are missing - a count is the whole of the "
                  "evidence here"
                  % (len(show.seconds), songs, segues, floor - len(show.seconds)))
        if show.stated_partial:
            f.note += "; the folder name says it is only part of a show"
    else:
        f.note = ("%d tracks against %d songs (%d segue(s)) - at or above the "
                  "floor, which is as much as a count can say"
                  % (len(show.seconds), songs, segues))
    return f


# --- running it -------------------------------------------------------------

def run(shows: list, cfg, store, out_dir: Path, progress=None) -> dict:
    """Judge every folder against what the reference sources know.

    Reads the stored show database only - no network, no library walk. The
    inputs are phase 0's own reports and a 2 MB sqlite file, so this is cheap
    enough to re-run whenever either changes.
    """
    from . import confirm

    findings = []
    for i, show in enumerate(shows, 1):
        if progress and (i % 100 == 0 or i == len(shows)):
            progress("  %d/%d" % (i, len(shows)))
        if not show.date or not show.band or not show.seconds:
            continue

        # Sources with durations first: a measured shortfall beats a counted one.
        recs = store.recordings_for(confirm.band_prefixes(cfg, show.band),
                                    show.date) or []
        f = judge(show, recs, "archive.org") if recs else None

        if (f is None or f.verdict in (NO_REFERENCE, NOT_COMPARABLE)) \
                and confirm.phishin_covers(cfg, show.band):
            pshow = store.phishin_show(show.date)
            if pshow is not None and getattr(pshow, "has_durations", False):
                f = judge(show, [pshow], "phish.in")

        # Then the setlist-only sources, which can only give a floor.
        if f is None or f.verdict in (NO_REFERENCE, NOT_COMPARABLE):
            counted = _by_count(show, cfg, store)
            if counted is not None:
                f = counted

        if f is None:
            f = Finding(rel=show.rel, verdict=NO_REFERENCE, band=show.band,
                        date=show.date, ours_tracks=len(show.seconds),
                        ours_seconds=show.total,
                        note="no source in the stored database knows this night")
        findings.append(f)

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_reports(out_dir, findings)
    counts: dict = {}
    for f in findings:
        counts[f.verdict] = counts.get(f.verdict, 0) + 1
    counts["folders"] = len(findings)
    return counts


def _by_count(show: LocalShow, cfg, store):
    """jerrybase, then the MMJ archive: a setlist and no times."""
    from . import confirm

    if confirm.jerrybase_covers(cfg, show.band):
        events = store.jerrybase_events(show.date) or []
        chosen, _why = confirm.choose_event(
            _NameOnly(show.rel), events, confirm.act_names(cfg, show.band))
        if chosen is not None and chosen.songs:
            # None, not 0: jerrybase publishes no segues, and 0 claimed there were none.
            return judge_by_count(show, len(chosen.songs), None, "jerrybase",
                                  "jerrybase.com/events/" + (chosen.slug or ""))
    if confirm.mmjarchive_covers(cfg, show.band):
        mshow = store.mmj_show(show.date)
        if mshow is not None and mshow.songs:
            return judge_by_count(show, len(mshow.songs), mshow.breaks,
                                  "mmjarchive", getattr(mshow, "url", ""))
    return None


class _NameOnly:
    """choose_event only reads `.path.name`, and we have a relative path."""

    def __init__(self, rel):
        self.path = Path(rel)


def _write_reports(out_dir: Path, findings: list) -> None:
    import time

    from .report import write_csv

    write_csv(
        out_dir / "completeness.csv",
        ["verdict", "relative_path", "band", "date", "our_tracks", "our_seconds",
         "their_tracks", "their_seconds", "missing_seconds", "missing_share",
         "alignment", "shape", "extra_seconds", "extra_shape", "source",
         "identifier", "evidence", "missing_titles", "note"],
        ([f.verdict, f.rel, f.band or "", f.date or "", f.ours_tracks,
          "%.0f" % f.ours_seconds, f.theirs_tracks or "",
          "%.0f" % f.theirs_seconds if f.theirs_seconds else "",
          "%.0f" % f.missing_seconds if f.missing_seconds else "",
          "%.3f" % f.missing_share if f.missing_share else "",
          "%.3f" % f.score if f.score else "", f.shape,
          "%.0f" % f.extra_seconds if f.extra_seconds else "", f.extra_shape,
          f.source, f.identifier,
          f.evidence, " | ".join(f.missing_titles), f.note]
         for f in findings))

    order = {TRUNCATED: 0, SHORT_BY_COUNT: 1, STATED_PARTIAL: 2,
             NOT_COMPARABLE: 3, REFERENCE_SHORTER: 4, NO_REFERENCE: 5,
             COMPLETE: 6}
    ranked = sorted(findings, key=lambda f: (order.get(f.verdict, 9),
                                             -f.missing_seconds, f.rel))
    L = ["Completeness - is a recording missing songs?",
         "=" * 45,
         "generated %s" % time.strftime("%Y-%m-%d %H:%M"),
         "",
         "Nothing was read from the library and nothing was written to it. The",
         "durations come from phase 0's own identity index and the setlists from",
         "the stored show database, so this costs nothing to re-run.",
         ""]
    counts: dict = {}
    for f in findings:
        counts[f.verdict] = counts.get(f.verdict, 0) + 1
    L.append("%d folders judged" % len(findings))
    for v in sorted(counts, key=lambda k: order.get(k, 9)):
        L.append("  %-16s %4d" % (v, counts[v]))
    L.append("")

    def block(verdict, title, blurb):
        rows = [f for f in ranked if f.verdict == verdict]
        L.append("%s (%d)" % (title, len(rows)))
        L.append("-" * (len(title) + 6))
        for line in blurb:
            L.append("  " + line)
        if not rows:
            L.append("  nothing")
        for f in rows:
            L.append("  %s" % f.rel)
            L.append("      %s" % f.note)
            if f.missing_titles:
                L.append("      missing: %s" % ", ".join(f.missing_titles[:8]))
            if f.identifier:
                L.append("      against %s (%s)" % (f.identifier, f.source))
        L.append("")

    block(TRUNCATED, "Missing songs, measured in seconds",
          ["Our tracks are accounted for and theirs are not. The kindest copy of",
           "the night was used, so this is a floor: every copy that fits leaves",
           "these songs over."])
    block(SHORT_BY_COUNT, "Fewer tracks than the setlist can explain",
          ["No durations from this source, so the test is a count against",
           "`songs - segues`. Below that floor a song is genuinely absent."])
    block(STATED_PARTIAL, "Short, and the folder says so",
          ["A set, a disc or a soundcheck. Listed apart because a stated",
           "shortfall is not a discovery."])
    block(REFERENCE_SHORTER, "No reference long enough to judge against",
          ["archive.org holds only a part-upload of these nights - sometimes a",
           "single track. Our copy may well be complete; nothing here says so."])
    block(NOT_COMPARABLE, "Nothing aligned well enough to judge",
          ["A copy exists but does not account for our tracks, so this says",
           "nothing about completeness either way."])
    report_file(out_dir / "completeness_summary.txt").write_text("\n".join(L) + "\n",
                                                      encoding="utf-8")
