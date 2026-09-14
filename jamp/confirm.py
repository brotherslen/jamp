"""Phase 3: fill in what the files never said, from what the reference sources know.

Five sources - archive.org, phish.in, phish.net, jerrybase.com and the My
Morning Jacket archive - asked only about settled folders, through a cache that
makes a second run need no network at all.

Three rules, agreed before any of this was written, and every one of them is a
restriction rather than a capability:

* **Fill only.** A field that already has a value is never touched - unless it
  still holds exactly what phase 3 itself wrote there, recorded per file and
  per field in `.etree_state.json` as `phase3_written`, so its own earlier
  answer can be corrected and nobody else's can.
* **Report first.** A normal run writes a report and a proposals file and
  nothing else. `--apply` writes the tags named in that file, refuses to run
  without it, re-checks fill-only against the file on disk rather than the
  proposal, backs the tags up first (`.etree_backup.json`, never overwritten)
  and logs every change to phase3_committed.csv. Nothing is renamed; the next
  phase 2 pass turns a filled VENUE into a folder name.
* **One source proven before a second was added.** They were added one at a
  time, in that order.

How sure we are is not one number, it is a tier, because the ways of being sure
are different in kind:

    SHNID      our folder name carries an etree catalogue number and an item
               has it. This is identity, not resemblance.
    LINEAGE    band and date agree, and the taper or source in our name is in
               theirs. Two recordings of one night by different tapers are
               different recordings, and this is what tells them apart.
    ONLY       band and date agree and archive.org has exactly one recording.
    CONSENSUS  several recordings, no way to tell which is ours. Their venue and
               city are still worth having - all nineteen copies of 1977-05-08
               agree on Ithaca, NY - but nothing track-level can be taken.
    PHISHIN    phish.in's setlist for the night, with durations.
    JERRYBASE, PHISHNET, MMJARCHIVE
               a setlist with no durations: a title rests on the track count
               agreeing exactly, and the report says so rather than printing an
               alignment score that was never measured.
    NONE       nothing found.

Where a source has durations, a title is written only when our tracks align
with theirs - and alignment runs against every candidate for the night, not the
one our folder name points at, because the durations say which recording a
folder holds and the name only claims to. A place is a claim about the night,
so it can come from consensus; a title is a claim about one performance.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import (archiveorg, dates as _dates, jerrybase, mmjarchive, phishin,
               phishnet, setlist)
from .httpcache import HttpCache
from .winpath import opener

SHNID = "SHNID"
LINEAGE = "LINEAGE"
ONLY = "ONLY"
CONSENSUS = "CONSENSUS"
JERRYBASE = "JERRYBASE"
PHISHNET = "PHISHNET"
MMJARCHIVE = "MMJARCHIVE"
PHISHIN = "PHISHIN"
NONE = "NONE"

# Sources with no durations, where a title rests on the count agreeing and the
# report must never dress that up as an alignment.
COUNT_MATCHED = (JERRYBASE, PHISHNET, MMJARCHIVE)

# A title is written only when the alignment is this good AND the two recordings
# are within this much of each other overall. Neither alone is enough: two
# unrelated tracks can sum to the length of a third and part-align by accident,
# and it is the total duration that gives that away.
MIN_SCORE = 0.85
MAX_DURATION_GAP = 0.05

# Below this, do not even report it as a near miss - it is a different show.
WORTH_REPORTING = 0.55

# ".shn" belongs here even though the library holds none any more: a folder
# that still had one would otherwise not be seen as having tracks at all,
# and would slip past the refusal below rather than being reported by it.
AUDIO = {".flac", ".mp3", ".m4a", ".wma", ".ogg", ".wav", ".aiff", ".aif",
         ".ape", ".wv", ".shn"}


@dataclass
class LocalTrack:
    name: str
    seconds: float | None
    title: str | None


@dataclass
class LocalShow:
    path: Path
    rel: str
    band: str | None
    date: str | None
    tracks: list[LocalTrack] = field(default_factory=list)
    has_venue: bool = False
    # True when the venue currently in the tags is one phase 3 put there. Such a
    # value is ours to improve on; anything else is somebody's answer and is not.
    venue_is_ours: bool = False
    text_files: list[str] = field(default_factory=list)

    @property
    def missing_titles(self) -> bool:
        return bool(self.tracks) and not any(t.title for t in self.tracks)

    @property
    def has_audio(self) -> bool:
        return bool(self.tracks)


@dataclass
class Proposal:
    rel: str
    tier: str
    identifier: str | None = None
    venue: str | None = None
    city: str | None = None
    state: str | None = None
    titles: dict[str, str] = field(default_factory=dict)   # filename -> title
    score: float = 0.0
    duration_gap: float = 0.0
    notes: list[str] = field(default_factory=list)
    needs_a_human: bool = False


def _shnid_in(name: str) -> str | None:
    m = re.search(r"\.(\d{3,6})(?:\.|\s|$)", name)
    return m.group(1) if m else None


# A show played twice in a night is two shows.  These words are how both naming
# conventions say which one, and they are the difference between a match and the
# wrong recording entirely.
_MARKER = re.compile(r"(?<![a-z])(early|late|acoustic|electric|soundcheck)(?![a-z])", re.I)


def _markers(name: str) -> set:
    return {m.group(1).lower() for m in _MARKER.finditer(name)}


def markers_contradict(ours: str, theirs: str) -> bool:
    """True when both names name a set and they are not the same one.

    Steve Kimock's Zero played early and late at Nick's Bar on 1997-12-12, and
    our late show matched their early one on the taper's name alone.  The
    alignment caught it at 18% and took no titles, but the tier said LINEAGE and
    the venue came from the wrong recording.  A silent near-miss like that is
    the thing most likely to be believed.
    """
    a, b = _markers(ours), _markers(theirs)
    return bool(a and b and not (a & b))


def read_local_show(folder: Path, root: Path, light: bool = False) -> LocalShow:
    """One settled folder, as much as can be read without the network.

    `light` reads only the first audio file and stops.  A folder whose first
    track has a title is not missing titles, and the venue this pipeline writes
    goes on every track - so one file answers both questions for the 810 folders
    of 825 that need nothing further.  Durations are what the rest of the read
    is for, and they are only needed once a folder is known to want work, so the
    caller re-reads in full when that turns out to be true.
    """
    import mutagen

    state = {}
    sp = Path(opener(folder / ".etree_state.json"))
    if sp.exists():
        try:
            state = json.loads(sp.read_text(encoding="utf-8"))
        except ValueError:
            state = {}
    from .state import newer_format

    if newer_format(state):
        # From a newer jamp: read nothing from it, so the folder has no band
        # or date here and phase 3 leaves it alone.
        state = {}
    show = LocalShow(path=folder, rel=str(folder.relative_to(root)),
                     band=state.get("band"), date=state.get("date"))
    written = state.get("phase3_written") or {}
    files = []
    for sub, _, names in os.walk(folder):
        for n in names:
            p = Path(sub) / n
            if p.suffix.lower() in AUDIO:
                files.append(p)
            elif p.suffix.lower() in (".txt", ".md5", ".ffp", ".nfo"):
                show.text_files.append(n)
    # One open per file.  This used to open each file up to three times - once
    # through the easy interface for the title, again for the duration when that
    # came back None, and a third time raw to look for a venue - across nearly
    # sixteen thousand files.  The easy interface carries `info` too, so the
    # title and the duration come from the same open, and the venue is a
    # property of the folder rather than the track: once found, stop looking.
    for path in sorted(files):
        seconds = title = None
        try:
            easy = mutagen.File(path, easy=True)
            if easy is not None:
                got = easy.get("title")
                title = (got[0].strip() if got and got[0].strip() else None)
                seconds = getattr(getattr(easy, "info", None), "length", None)
        except Exception:                      # noqa: BLE001 - unreadable is not fatal
            pass
        show.tracks.append(LocalTrack(path.name, seconds, title))
        if not show.has_venue:
            venue = _venue_tag(path)
            if venue:
                show.has_venue = True
                show.venue_is_ours = written.get(path.name, {}).get("VENUE") == venue
        if light:
            break
    return show


def _venue_tag(path: Path) -> str | None:
    """The VENUE tag, which no container exposes through the easy interface."""
    import mutagen

    try:
        raw = mutagen.File(path)
    except Exception:                          # noqa: BLE001
        return None
    if raw is None or raw.tags is None:
        return None
    for key in raw.tags.keys():
        if "venue" in str(key).lower():
            val = str(raw.tags[key]).strip("[]'\" ")
            if val:
                return val
    return None


def settled_folders(root: Path, artists=None) -> list:
    """Every settled folder under ROOT, optionally limited to some artists.

    Paths only.  How much of a folder needs reading depends on whether it turns
    out to want anything, so that decision belongs to the caller.
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        if ".etree_state.json" not in filenames:
            continue
        folder = Path(dirpath)
        rel = folder.relative_to(root)
        if artists and rel.parts and rel.parts[0] not in artists:
            continue
        out.append(folder)
    return out


def scan_library(root: Path, artists=None):
    """Every settled folder, read in full."""
    return [read_local_show(f, root) for f in settled_folders(root, artists)]


# Words that every copy of a night shares, so finding one in common says
# nothing about which copy is ours. The band and the date are masked out
# separately, being different on every folder.
UNINFORMATIVE = {
    "flac", "flac16", "flac24", "flac2496", "flac1644", "shnf", "shn", "mp3",
    "m4a", "wma", "aud", "sbd", "mtx", "dsbd", "soundboard", "audience",
    "matrix", "master", "live", "set1", "set2", "disc", "disk", "cdr", "dat",
}


def lineage_tokens(name: str, prefixes=()) -> set:
    """The words in a folder name that could say WHICH recording this is.

    The band and the date are shared by every candidate for that night, so a
    token drawn from either is not evidence.  "mmj2012-08-17.aud.bobbybourbon"
    matched a different taper's copy on the fragment "mmj2012" alone, which is
    how a tier called LINEAGE ended up choosing arbitrarily among candidates.
    What is left after masking those is the taper, the microphone, the transfer -
    the things that actually tell two copies apart.
    """
    from .tokens import mask_spans

    masked = mask_spans(name.lower(), [c.span for c in _dates.find_dates(name)])
    tokens = {t for t in re.split(r"[^a-z0-9]+", masked) if len(t) > 3}
    out = set()
    for t in tokens:
        if t.isdigit() or t in UNINFORMATIVE:
            continue
        if any(t == p.lower() or t.startswith(p.lower()) and t[len(p):].isdigit()
               for p in prefixes):
            continue
        out.add(t)
    return out


def choose(show: LocalShow, recordings: list[archiveorg.Recording], prefixes=()):
    """Which recording is ours, and how sure is that?  Returns (tier, recording)."""
    if not recordings:
        return NONE, None

    ours = _shnid_in(show.path.name)
    if ours:
        for rec in recordings:
            if rec.shnid == ours:
                return SHNID, rec

    # The taper or the source in our own name, matched against theirs. Our
    # folder name is the etree name it came from, so the words line up.
    tokens = lineage_tokens(show.path.name, prefixes)
    if tokens:
        for rec in recordings:
            theirs = " ".join(filter(None, [rec.identifier, rec.taper, rec.transferer])).lower()
            hits = {t for t in tokens if t in theirs}
            if hits and not markers_contradict(show.path.name, rec.identifier):
                return LINEAGE, rec

    if len(recordings) == 1:
        if markers_contradict(show.path.name, recordings[0].identifier):
            return NONE, None
        return ONLY, recordings[0]
    return CONSENSUS, None


def consensus_place(recordings):
    """What the copies agree on.

    Venue spellings differ between copies of one night - Barton Hall was written
    three ways across nineteen - so the most common spelling wins rather than
    the first. City and state agree far more often, and are worth more.
    """
    def modal(values):
        vals = [v.strip() for v in values if v and v.strip()]
        return Counter(vals).most_common(1)[0][0] if vals else None

    return (modal(r.venue for r in recordings),
            modal(r.city for r in recordings),
            modal(r.state for r in recordings))


def propose_for(show: LocalShow, recordings: list[archiveorg.Recording],
                prefixes=()) -> Proposal:
    """What could be filled in for one folder, and how confident that is."""
    tier, rec = choose(show, recordings, prefixes)
    p = Proposal(rel=show.rel, tier=tier,
                 identifier=rec.identifier if rec else None)
    if tier == NONE:
        p.notes.append("archive.org has no recording of this band on this date")
        return p

    # --- place: safe even when we cannot tell the copies apart -----------
    if show.has_venue and not show.venue_is_ours:
        p.notes.append("venue tag already present - left alone")
    else:
        if rec is not None:
            venue, city, state = rec.venue, rec.city, rec.state
        else:
            venue, city, state = consensus_place(recordings)
            p.notes.append("place agreed by %d recordings" % len(recordings))
        p.venue, p.city, p.state = venue, city, state
        if not any((venue, city, state)):
            p.notes.append("no place in the metadata either")

    # --- titles: only from a recording we have actually identified -------
    if not show.missing_titles:
        p.notes.append("titles already present - left alone")
        return p
    # No early exit for CONSENSUS. Not knowing which copy is ours by NAME does
    # not mean we cannot tell by duration, and it certainly does not mean the
    # copies disagree about what was played - eighteen recordings of one Dead
    # night are eighteen recordings of the same setlist. Alignment decides.
    if rec is None:
        p.notes.append("%d recordings and no way to tell by name which is ours"
                       % len(recordings))
    ours = [t.seconds for t in show.tracks]
    if not all(ours):
        p.notes.append("a duration is missing on our side, so the tracks cannot be aligned")
        return p

    # Align against EVERY candidate, not just the one the name picked. Two
    # findings forced this. Steve Kimock 2002-08-10: our folder says .mk4v and
    # archive.org has an .mk4v item, but ours aligns 99% with the -ams copy and
    # only 83% with the one bearing its own name. And the Dead 1983-10-08: our
    # taper "willy" is not among the eighteen copies at all, yet three of them
    # align at 100%. The durations know which recording this is; the words in
    # the name only claim to.
    a, rec, passing = best_alignment(ours, recordings)
    if a is None or rec is None:
        p.notes.append("no candidate has a full set of durations to align against")
        p.needs_a_human = len(recordings) > 1
        return p
    if rec is not None and p.identifier != rec.identifier:
        p.identifier = rec.identifier
        p.notes.append("the durations pick %s, whatever the name says" % rec.identifier)
    p.score, p.duration_gap = a.score, a.duration_gap
    if len(passing) > 1:
        p.notes.append("%d copies of this night align with ours, so a title is "
                       "taken only where they agree" % len(passing))
    if a.score >= MIN_SCORE and a.duration_gap <= MAX_DURATION_GAP:
        for i, track in enumerate(show.tracks):
            idx = a.title_for(i)
            if not idx:
                continue
            names = [rec.tracks[k].title for k in idx if k < len(rec.tracks)]
            if len(passing) > 1:
                agreed = agreed_title(i, passing)
                if not agreed:
                    continue
                title = agreed
            elif names:
                title = " > ".join(names)
            else:
                continue
            # One of theirs answering to two of ours is one song in two files.
            # Writing the bare title on both says the song was played twice.
            same = [j for j, t in enumerate(show.tracks) if a.title_for(j) == idx]
            if len(same) > 1:
                title = "%s (%d of %d)" % (title, same.index(i) + 1, len(same))
            p.titles[track.name] = title
        skipped = sum(1 for s in a.steps if s.op == setlist.SKIP_OURS)
        if skipped:
            p.notes.append("%d of our tracks answer to nothing and are left untitled"
                           % skipped)
    elif a.score >= WORTH_REPORTING:
        p.needs_a_human = worth_a_human(a)
        p.identifier = rec.identifier
        p.notes.append(near_miss_note(a))
    else:
        p.notes.append("alignment %.0f%% - not the same recording" % (a.score * 100))
    return p


def mmjarchive_covers(cfg, band: str | None) -> bool:
    """The My Morning Jacket live archive, and the acts filed under it."""
    if not band:
        return False
    if band == "mmj":
        return True
    for b in cfg.bands:
        if b.abbrev == band:
            return b.parent == "mmj"
    return False


def propose_from_mmj(show: LocalShow, mshow) -> Proposal:
    """Venue freely; titles when the setlist accounts for every track.

    This is the one source that prints its own gaps, so the count can reconcile
    for a stated reason rather than by assumption: 2005-11-23 lists twenty songs
    with a break notated between the fifteenth and sixteenth, and our folder
    holds twenty-one tracks of which the sixteenth is fifty-six seconds long.
    `plan_for` accepts only the two honest shapes - breaks cut out, or breaks
    left in as untitled tracks.
    """
    p = Proposal(rel=show.rel, tier=MMJARCHIVE, identifier=mshow.url)

    if show.has_venue and not show.venue_is_ours:
        p.notes.append("venue tag already present - left alone")
    else:
        p.venue, p.city, p.state = mshow.venue, mshow.city, mshow.state

    if not show.missing_titles:
        p.notes.append("titles already present - left alone")
        return p

    plan = mshow.plan_for(len(show.tracks))
    if plan is None:
        p.needs_a_human = True
        p.notes.append("we have %d tracks and the setlist accounts for %d songs "
                       "plus %d break(s), which reconciles to neither, so no "
                       "titles are taken"
                       % (len(show.tracks), len(mshow.songs), mshow.breaks))
        return p
    gaps = 0
    for track, entry in zip(show.tracks, plan):
        if entry.kind != mmjarchive.SONG or not entry.title:
            gaps += 1
            continue
        p.titles[track.name] = entry.title
    p.notes.append("%d tracks against %d songs%s - the setlist marks its own "
                   "breaks, so the count reconciles for a stated reason"
                   % (len(show.tracks), len(mshow.songs),
                      " and %d notated break(s) left untitled" % gaps if gaps else ""))
    return p


def phishnet_covers(cfg, band: str | None) -> bool:
    """phish.net is the Phish catalogue, and the acts filed under it."""
    if not band:
        return False
    if band == "ph":
        return True
    for b in cfg.bands:
        if b.abbrev == band:
            return b.parent == "ph"
    return False


def phishin_covers(cfg, band: str | None) -> bool:
    """phish.in carries the same catalogue phish.net does."""
    return phishnet_covers(cfg, band)


def propose_from_phishin(show: LocalShow, pshow) -> Proposal:
    """Venue freely; titles on an alignment, because this source has durations.

    The only secondary source that does.  A Phish title therefore gets the same
    gate as an archive.org one - score and total length both - rather than the
    count rule the others fall back on.
    """
    p = Proposal(rel=show.rel, tier=PHISHIN,
                 identifier="phish.in/%s" % (pshow.date or ""))

    if show.has_venue and not show.venue_is_ours:
        p.notes.append("venue tag already present - left alone")
    else:
        p.venue, p.city, p.state = pshow.venue, pshow.city, pshow.state

    if not show.missing_titles:
        p.notes.append("titles already present - left alone")
        return p
    ours = [t.seconds for t in show.tracks]
    if not all(ours):
        p.notes.append("a duration is missing on our side, so the tracks cannot be aligned")
        return p
    if not pshow.has_durations:
        p.notes.append("phish.in lists this show as %s, with no usable track times"
                       % (pshow.audio_status or "having no audio"))
        return p

    a = setlist.align(list(ours), [t.seconds for t in pshow.tracks])
    p.score, p.duration_gap = a.score, a.duration_gap
    if a.score >= MIN_SCORE and a.duration_gap <= MAX_DURATION_GAP:
        for i, track in enumerate(show.tracks):
            idx = a.title_for(i)
            if not idx:
                continue
            names = [pshow.tracks[k].title for k in idx if k < len(pshow.tracks)]
            if not names:
                continue
            title = " > ".join(names)
            same = [j for j, _ in enumerate(show.tracks) if a.title_for(j) == idx]
            if len(same) > 1:
                title = "%s (%d of %d)" % (title, same.index(i) + 1, len(same))
            p.titles[track.name] = title
        skipped = sum(1 for st in a.steps if st.op == setlist.SKIP_OURS)
        if skipped:
            p.notes.append("%d of our tracks answer to nothing and are left untitled"
                           % skipped)
    elif a.score >= WORTH_REPORTING:
        p.needs_a_human = worth_a_human(a)
        p.notes.append(near_miss_note(a))
    else:
        p.notes.append("alignment %.0f%% - phish.in has a different recording"
                       % (a.score * 100))
    return p


def propose_from_phishnet(show: LocalShow, pshow) -> Proposal:
    """Venue freely, titles only on an exact count match.

    `tracktime` is a column on every setlist row and is empty on all of them, so
    there is no duration to align against and the count is the whole of the
    evidence - the same weaker footing as jerrybase, and gated the same way.
    """
    p = Proposal(rel=show.rel, tier=PHISHNET)
    p.identifier = "phish.net/setlists/%s" % (pshow.date or "")

    if show.has_venue and not show.venue_is_ours:
        p.notes.append("venue tag already present - left alone")
    else:
        p.venue, p.city, p.state = pshow.venue, pshow.city, pshow.state
        if pshow.country and pshow.country.lower() not in ("usa", "united states"):
            p.notes.append("outside the US: phish.net says %s" % pshow.country)

    if not show.missing_titles:
        p.notes.append("titles already present - left alone")
        return p
    titles = pshow.titles
    if titles and len(titles) == len(show.tracks):
        for track, title in zip(show.tracks, titles):
            p.titles[track.name] = title
        p.notes.append("%d tracks and %d songs, taken in order - phish.net has no "
                       "track times, so the count agreeing exactly is the whole of "
                       "the evidence" % (len(show.tracks), len(titles)))
    elif titles:
        p.needs_a_human = True
        segues = sum(1 for s in pshow.songs if s.segue)
        p.notes.append("we have %d tracks and phish.net lists %d songs (%s)%s, so the "
                       "order cannot be trusted and no titles are taken"
                       % (len(show.tracks), len(titles),
                          "+".join(str(n) for n in pshow.set_sizes),
                          " with %d segues, which is often the difference" % segues
                          if segues else ""))
    else:
        p.notes.append("phish.net has the show but no setlist entered")
    return p


def jerrybase_covers(cfg, band: str | None) -> bool:
    """jerrybase is the Grateful Dead and Garcia catalogue, and only that.

    Taken from the config rather than a list kept here: every Garcia act is a
    side project parented to `gd`, so the question answers itself and a new one
    added later is covered without touching this.
    """
    if not band:
        return False
    if band == "gd":
        return True
    # Side projects live in cfg.bands alongside everything else - there is no
    # separate collection - and each carries the parent it is filed under.
    for b in cfg.bands:
        if b.abbrev == band:
            return b.parent == "gd"
    return False


def act_names(cfg, band: str | None) -> list:
    """Every way this library writes the act's name, for matching against theirs."""
    for b in cfg.bands:
        if b.abbrev == band:
            return list(dict.fromkeys([b.name, *b.aliases]))
    return []


def choose_event(show: LocalShow, events, names):
    """Which of a night's events is ours.  Returns (event, why) or (None, why).

    1987-10-31 at the Lunt-Fontanne is four events - early and late, each played
    twice as an acoustic set and an electric one - so the act alone is not
    enough and neither is the date.  Anything still ambiguous after both is left
    for a human rather than guessed at.
    """
    ours = [e for e in events if act_matches_ours(names, e)]
    if not ours:
        return None, "no event that night is by this act"
    if len(ours) > 1:
        kept = [e for e in ours if not _marker_clash(show.path.name, e)]
        if len(kept) == 1:
            return kept[0], ("our folder names the performance, which picks one of "
                             "%d that night" % len(ours))
        # Still more than one, because an event can be silent on an axis rather
        # than disagree: at the Lunt-Fontanne our "late acoustic" clashes with
        # neither the late acoustic set nor the late electric one, which simply
        # does not mention being electric. The event that answers to MORE of
        # what our folder says is the better reading, and only a strict winner
        # counts - a tie is still a tie.
        mine = _markers(show.path.name)
        if mine and len(kept) > 1:
            scored = sorted(((len(mine & _event_markers(e)), e) for e in kept),
                            key=lambda x: x[0], reverse=True)
            if scored[0][0] > scored[1][0]:
                return scored[0][1], ("our folder names %s, matching more of this "
                                      "event than the other %d that night"
                                      % ("/".join(sorted(mine)), len(ours) - 1))
        return None, ("%d events that night by this act and nothing in our folder "
                      "name to tell them apart" % len(ours))
    return ours[0], "the only event that night by this act"


def act_matches_ours(names, event) -> bool:
    return jerrybase.act_matches(names, event.act)


def _event_markers(event) -> set:
    """Every word that says which performance this is.

    jerrybase splits the two axes: early/late sits beside the date, while
    acoustic or electric is part of the act - "Jerry Garcia Acoustic Band".  The
    Lunt-Fontanne on 1987-10-31 needs both to land on one of its four events.
    """
    out = set()
    if event.marker:
        out.add(event.marker)
    out |= _markers(event.act or "")
    return out


def _marker_clash(folder_name: str, event) -> bool:
    """True when our folder names a performance this event is not."""
    mine = _markers(folder_name)
    theirs = _event_markers(event)
    if not mine or not theirs:
        return False
    # Disagreement on either axis is a clash.  Our "late acoustic" is not their
    # late electric, even though both are late.
    for axis in (("early", "late"), ("acoustic", "electric")):
        ours_on = mine & set(axis)
        theirs_on = theirs & set(axis)
        if ours_on and theirs_on and not (ours_on & theirs_on):
            return True
    # We name an axis they are silent on only when some other event that night
    # does speak to it; that comparison belongs to choose_event, not here.
    return False


def propose_from_jerrybase(show: LocalShow, events, names) -> Proposal:
    """Venue freely, titles only on an exact count match.

    There are no durations on jerrybase, so `setlist.align` - the thing that
    makes a title safe to write anywhere else in this file - cannot be used.
    What is left is the track count, which is a far weaker claim: two shows of
    fourteen songs are not the same show. So the bar is exact agreement, and
    anything else is reported rather than written.
    """
    p = Proposal(rel=show.rel, tier=JERRYBASE)
    event, why = choose_event(show, events, names)
    p.notes.append("jerrybase: " + why)
    if event is None:
        p.tier = NONE
        p.needs_a_human = bool(events)
        return p
    p.identifier = "jerrybase.com/events/" + event.slug

    if show.has_venue and not show.venue_is_ours:
        p.notes.append("venue tag already present - left alone")
    else:
        p.venue, p.city, p.state = event.venue, event.city, event.state

    if not show.missing_titles:
        p.notes.append("titles already present - left alone")
        return p
    songs = event.songs
    if len(songs) == len(show.tracks):
        for track, title in zip(show.tracks, songs):
            p.titles[track.name] = title
        p.notes.append("%d tracks and %d songs, taken in order - jerrybase has no "
                       "durations, so the count agreeing exactly is the whole of "
                       "the evidence" % (len(show.tracks), len(songs)))
    else:
        p.needs_a_human = True
        p.notes.append("we have %d tracks and jerrybase lists %d songs (%s), so the "
                       "order cannot be trusted and no titles are taken"
                       % (len(show.tracks), len(songs),
                          "+".join(str(n) for n in event.set_sizes)))
    return p


# A near miss is only near if BOTH numbers are close. Our one-track soundcheck
# scored 100% against a full two-hour show, because the single track found an
# answer - while the two recordings differ in length by 2136%. Reporting that as
# "close, but under the bar" invites somebody to go and loosen the bar.
NEARLY_SAME_LENGTH = 0.25


def near_miss_note(a) -> str:
    """How to describe an alignment that did not clear the gate."""
    if a.duration_gap > NEARLY_SAME_LENGTH:
        return ("alignment %.0f%% but the recordings differ in length by %.0f%% - "
                "not the same thing, whatever the tracks did"
                % (a.score * 100, a.duration_gap * 100))
    return ("alignment %.0f%% with a %.0f%% length difference - close, but under "
            "the bar, so nothing is taken" % (a.score * 100, a.duration_gap * 100))


def worth_a_human(a) -> bool:
    """Only ask for a human when a human could plausibly settle it."""
    return a.score >= WORTH_REPORTING and a.duration_gap <= NEARLY_SAME_LENGTH


def best_alignment(ours, recordings):
    """Align our durations against every candidate.  Returns (best, rec, passing).

    `passing` is every candidate that clears the gate, which matters when more
    than one does: eighteen copies of a Dead night exist and three of them align
    perfectly, because they are all recordings of the same performance. We
    cannot say which is ours, and for a title we do not need to - we need only
    what they agree on.
    """
    scored = []
    for rec in recordings:
        theirs = [t.seconds for t in rec.tracks]
        if not theirs or not all(theirs):
            continue
        a = setlist.align(list(ours), list(theirs))
        scored.append((a, rec))
    if not scored:
        return None, None, []
    scored.sort(key=lambda x: (-x[0].score, x[0].duration_gap, x[0].drift))
    passing = [(a, r) for a, r in scored
               if a.score >= MIN_SCORE and a.duration_gap <= MAX_DURATION_GAP]
    best_a, best_rec = (passing[0] if passing else scored[0])
    return best_a, best_rec, passing


# Uploaders write the same song differently: "Feel Like A Stranger", "feel like
# a stranger" and "01 Feel Like A Stranger" are all the same track of the same
# night. A leading track number in particular has to go before comparing, or
# nothing agrees with anything.
_LEADING_TRACK_NO = re.compile(r"^\s*\d{1,3}\s*[-._)]?\s+")


def _flat(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _LEADING_TRACK_NO.sub("", (text or "")).lower())


def agreed_title(index, passing) -> str | None:
    """What the passing copies call our track `index`, if most of them agree.

    Eight copies of the Dead at Richmond align with ours, and all eight name
    the songs the same way once case and a leading track number are set aside.
    Demanding unanimity on the raw strings found agreement on nothing at all.

    A clear majority is required - more than half of the copies that aligned -
    so one uploader's idiosyncratic name cannot carry a track, and a genuine
    split decision yields nothing rather than a coin toss.
    """
    said: dict = {}
    for a, rec in passing:
        idx = a.title_for(index)
        if not idx:
            continue
        titles = [rec.tracks[k].title for k in idx if k < len(rec.tracks)]
        if not titles:
            continue
        key = "|".join(_flat(t) for t in titles)
        if not key.strip("|"):
            continue
        said.setdefault(key, []).append(" > ".join(titles))
    if not said:
        return None
    best = max(said, key=lambda k: len(said[k]))
    if len(said[best]) * 2 <= len(passing):
        return None
    # Among the spellings that agree, prefer the one that reads like a title:
    # properly capitalised, and without the leading number already stripped for
    # comparison.
    forms = said[best]
    forms.sort(key=lambda f: (f == f.lower(), bool(_LEADING_TRACK_NO.match(f)), -len(f)))
    return forms[0]


def band_prefixes(cfg, band: str | None):
    """Every spelling archive.org might file this band under."""
    if not band:
        return []
    for b in cfg.bands:
        if b.abbrev == band:
            return list(dict.fromkeys([b.abbrev, *b.prefixes]))
    return [band]


@dataclass
class Source:
    """A place to ask when archive.org has not answered.

    Asked in order, and only while something is still missing.  Kept as data
    rather than three near-identical blocks in `run`, which is how they drifted
    apart in the first place: two replaced the proposal wholesale and the third
    merged field by field, so which source won depended on which had been added
    last rather than on what it knew.
    """

    name: str
    covers: object
    propose: object


def _ask_mmj(cache, cfg, show, phish_key, stats, store=None):
    mshow = (store.mmj_show(show.date) if store else None) or         mmjarchive.show_for(cache, show.date)
    return propose_from_mmj(show, mshow) if mshow is not None else None


def _ask_phishin(cache, cfg, show, phish_key, stats, store=None):
    pshow = (store.phishin_show(show.date) if store else None) or         phishin.show_for(cache, show.date)
    return propose_from_phishin(show, pshow) if pshow is not None else None


def _ask_phishnet(cache, cfg, show, phish_key, stats, store=None):
    if phish_key is None:
        stats["phish.net not asked - no key"] += 1
        return None
    pshow = phishnet.show_for(cache, phish_key, show.date)
    return propose_from_phishnet(show, pshow) if pshow is not None else None


def _ask_jerrybase(cache, cfg, show, phish_key, stats, store=None):
    events = (store.jerrybase_events(show.date) if store else None) or         jerrybase.events_for(cache, show.date)
    if not events:
        return None
    return propose_from_jerrybase(show, events, act_names(cfg, show.band))


# Order matters: asked in turn, and each only while something is still missing.
# phish.in comes before phish.net because it has durations and phish.net does
# not - a title on an alignment beats a title on a count - and phish.net stays
# behind it for the shows phish.in has no audio for.
SECONDARY_SOURCES = (
    Source("mmjarchive", mmjarchive_covers, _ask_mmj),
    Source("phish.in", phishin_covers, _ask_phishin),
    Source("phish.net", phishnet_covers, _ask_phishnet),
    Source("jerrybase", jerrybase_covers, _ask_jerrybase),
)


def merge_proposal(p: Proposal, alt: Proposal, show: LocalShow) -> None:
    """Take from `alt` only what `p` is still missing.

    A second source answers a gap, never an answer.  Titles from a recording we
    identified by duration beat titles from a count agreeing, so a proposal that
    already has them keeps them; and a place already found is not replaced by
    another source's spelling of it.  Merging rather than replacing is what
    stops the last source added from quietly winning.
    """
    took = False
    if show.missing_titles and not p.titles and alt.titles:
        p.titles = alt.titles
        p.score, p.duration_gap = alt.score, alt.duration_gap
        took = True
    if not (p.venue or p.city) and not show.has_venue and (alt.venue or alt.city):
        p.venue, p.city, p.state = alt.venue, alt.city, alt.state
        took = True
    if took:
        p.identifier = p.identifier or alt.identifier
        p.tier = alt.tier if p.tier == NONE else "%s+%s" % (p.tier, alt.tier)
        p.notes.extend(alt.notes)
    elif alt.needs_a_human and not p.titles:
        p.needs_a_human = True
        p.notes.extend(alt.notes)


def _fmt(p: Proposal) -> list[str]:
    lines = ["  %s" % p.rel]
    lines.append("      tier %s%s" % (
        p.tier, "  %s" % p.identifier if p.identifier else ""))
    if p.venue or p.city or p.state:
        place = ", ".join(x for x in (p.venue, p.city, p.state) if x)
        lines.append("      VENUE -> %s" % place)
    if p.titles:
        if p.tier in COUNT_MATCHED:
            # Not an alignment, and it must not be printed as one: neither of
            # these sources has durations, so nothing here was measured against
            # anything.  Printing "alignment 0%" was worse than saying nothing.
            lines.append("      %d titles, taken in order on an exact count match"
                         % len(p.titles))
        else:
            lines.append("      %d titles, alignment %.0f%%, length within %.1f%%"
                         % (len(p.titles), p.score * 100, p.duration_gap * 100))
        for name, title in list(p.titles.items())[:4]:
            lines.append("          %-30s %s" % (name[:30], title[:44]))
        if len(p.titles) > 4:
            lines.append("          ... and %d more" % (len(p.titles) - 4))
    for n in p.notes:
        lines.append("      %s" % n)
    return lines


def seed_cache(root: Path, cfg, cache: HttpCache, artists=None,
               phish_key: str | None = None, progress=None) -> dict:
    """Fetch what every source knows about every settled folder, complete or not.

    Ordinary runs only ask about folders missing something, so the cache ends up
    holding a handful of shows.  That is enough to fill gaps and not enough for
    anything else.

    Seeding is for three things the ordinary run cannot serve:

    * **A durable local copy.** Once seeded, `--offline` answers for the whole
      library rather than for the few folders that happened to want work, so the
      library stops depending on any of these sites staying up.
    * **Watching for new shows.** An agent processing a new arrival wants the
      reference data already there, not a first-run network dependency.
    * **The completeness check.** The larger prize:
      comparing our track count against the known setlist is the only way to
      catch a recording that is missing songs, and that needs data for the
      folders that are already *complete* - exactly the ones never fetched.

    Nothing is written to the library and nothing is proposed; this only warms
    the cache.
    """
    folders = settled_folders(root, artists)
    stats = Counter()
    seen: set = set()
    for i, folder in enumerate(folders, 1):
        if progress and (i % 25 == 0 or i == len(folders)):
            progress("  %d/%d folders, %d fetched" % (i, len(folders), cache.fetches))
        try:
            state = json.loads(Path(opener(folder / ".etree_state.json")).read_text(
                encoding="utf-8"))
        except (OSError, ValueError):
            continue
        band, date = state.get("band"), state.get("date")
        if not band or not date:
            stats["no date or band in state"] += 1
            continue
        if (band, date) in seen:
            stats["date already seeded"] += 1
            continue
        seen.add((band, date))

        archiveorg.recordings_for(cache, band_prefixes(cfg, band), date)
        stats["archive.org"] += 1
        if phishin_covers(cfg, band):
            phishin.show_for(cache, date)
            stats["phish.in"] += 1
        if phishnet_covers(cfg, band):
            if phish_key:
                phishnet.show_for(cache, phish_key, date)
                stats["phish.net"] += 1
            else:
                stats["phish.net skipped - no key"] += 1
        if jerrybase_covers(cfg, band):
            jerrybase.events_for(cache, date)
            stats["jerrybase"] += 1
        if mmjarchive_covers(cfg, band):
            mmjarchive.show_for(cache, date)
            stats["mmjarchive"] += 1
    stats["distinct shows"] = len(seen)
    return dict(stats)


def run(root: Path, out_dir: Path, cfg, cache: HttpCache, artists=None,
        progress=None, phish_key: str | None = None, store=None) -> dict:
    """Look every settled folder up, and write the report.  Writes nothing to ROOT."""
    folders = settled_folders(root, artists)
    proposals: list[Proposal] = []
    no_audio: list[LocalShow] = []
    stats = Counter()

    for i, folder in enumerate(folders, 1):
        if progress and (i % 25 == 0 or i == len(folders)):
            progress("  %d/%d folders" % (i, len(folders)))
        # One file first.  Most folders are complete, and this is all they cost.
        show = read_local_show(folder, root, light=True)
        if not show.has_audio:
            no_audio.append(read_local_show(folder, root))
            continue
        if not show.date or not show.band:
            stats["no date or band in state"] += 1
            continue
        # Nothing to ask about if nothing is missing.
        settled_place = show.has_venue and not show.venue_is_ours
        if settled_place and not show.missing_titles:
            stats["already complete"] += 1
            continue
        # It wants something, so now read the whole folder - the durations are
        # what the matching actually runs on.
        show = read_local_show(folder, root)
        if show.has_venue and not show.venue_is_ours and not show.missing_titles:
            stats["already complete"] += 1
            continue
        prefixes = band_prefixes(cfg, show.band)
        # A distilled store is consulted first: whoever holds one already has the
        # answer, and fetching what they were just handed is slower for them and
        # ruder to the archive.
        recordings = (store.recordings_for(prefixes, show.date) if store else None)             or archiveorg.recordings_for(cache, prefixes, show.date)
        p = propose_for(show, recordings, prefixes)
        # jerrybase answers what archive.org left unanswered, and only that. It
        # is a second opinion on a gap, never on an answer: a title from a
        # recording we actually identified is better evidence than a count
        # agreeing, so one is never replaced by the other.
        #
        # The Dead is the case that needs this. archive.org holds many copies of
        # a night, agrees on the venue and cannot say which copy is ours, so it
        # gives a place and no titles - and a rule that only asked jerrybase
        # when nothing at all came back would never have asked for the Dead.
        for src in SECONDARY_SOURCES:
            if not (show.missing_titles and not p.titles) and (p.venue or p.city
                                                               or show.has_venue):
                break                       # nothing left for a second source
            if not src.covers(cfg, show.band):
                continue
            alt = src.propose(cache, cfg, show, phish_key, stats, store)
            if alt is not None:
                merge_proposal(p, alt, show)

        proposals.append(p)
        stats[p.tier] += 1

    # Shows we have the paperwork for but not the audio.
    downloads = []
    for show in no_audio:
        if not show.date or not show.band:
            continue
        recordings = archiveorg.recordings_for(cache, band_prefixes(cfg, show.band), show.date)
        downloads.append((show, recordings))

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_report(out_dir, proposals, downloads, stats, cache, len(folders))
    _write_proposals(out_dir, proposals)
    if store:
        stats["answered from the stored show database"] = store.stats()["hits"]
    return {"folders": len(folders), "proposals": len(proposals),
            "with_titles": sum(1 for p in proposals if p.titles),
            "with_place": sum(1 for p in proposals if p.venue or p.city),
            "needs_a_human": sum(1 for p in proposals if p.needs_a_human),
            "downloadable": sum(1 for _, r in downloads if r),
            "cache": cache.stats()}


def _write_report(out_dir, proposals, downloads, stats, cache, folder_count):
    import time

    L = []
    L.append("Phase 3 - nothing was written to the library")
    L.append("=" * 44)
    L.append("generated %s" % time.strftime("%Y-%m-%d %H:%M"))
    L.append("")
    L.append("Source: archive.org only.  Every response is cached; a second run is")
    L.append("offline and reproducible.  Cache now holds %d URLs (%.1f MB)."
             % (cache.stats()["urls"], cache.stats()["bytes"] / 1048576.0))
    L.append("")
    L.append("%d settled folders looked at, %d of them missing something."
             % (folder_count, len(proposals)))
    L.append("")
    L.append("How confident, per folder")
    L.append("-" * 25)
    for tier in (SHNID, LINEAGE, ONLY, CONSENSUS, NONE):
        n = sum(1 for p in proposals if p.tier == tier)
        if n:
            L.append("  %-10s %4d  %s" % (tier, n, "#" * min(40, n)))
    for k, v in sorted(stats.items()):
        if k not in (SHNID, LINEAGE, ONLY, CONSENSUS, NONE):
            L.append("  %-10s %4d" % (k, v))
    L.append("")

    filled = [p for p in proposals if p.titles or p.venue or p.city]
    L.append("Proposed fills (%d)" % len(filled))
    L.append("-" * 20)
    L.append("  Fill only: a field that already has a value is never touched.")
    for p in filled:
        L.extend(_fmt(p))
    L.append("")

    human = [p for p in proposals if p.needs_a_human]
    L.append("Needs a human (%d)" % len(human))
    L.append("-" * 20)
    if not human:
        L.append("  nothing")
    for p in human:
        L.extend(_fmt(p))
    L.append("")

    nothing = [p for p in proposals if p.tier == NONE]
    L.append("Not on archive.org (%d)" % len(nothing))
    L.append("-" * 24)
    for p in nothing:
        L.append("  %s" % p.rel)
    L.append("")

    L.append("Paperwork but no audio (%d)" % len(downloads))
    L.append("-" * 30)
    L.append("  Folders holding an info file, a checksum or a setlist and no audio.")
    L.append("  Where archive.org has the show, the identifier is the thing to fetch.")
    for show, recs in downloads:
        L.append("  %s" % show.rel)
        if show.text_files:
            L.append("      holds %s" % ", ".join(sorted(show.text_files)[:4]))
        if recs:
            for r in recs[:4]:
                L.append("      https://archive.org/details/%s" % r.identifier)
        else:
            L.append("      archive.org has nothing for this band and date")
    (out_dir / "phase3_summary.txt").write_text("\n".join(L) + "\n", encoding="utf-8")


def _write_proposals(out_dir, proposals):
    payload = []
    for p in proposals:
        if not (p.titles or p.venue or p.city or p.state):
            continue
        payload.append({
            "folder": p.rel, "tier": p.tier, "identifier": p.identifier,
            "venue": p.venue, "city": p.city, "state": p.state,
            "titles": p.titles,
            "alignment": (None if p.tier in COUNT_MATCHED else round(p.score, 3)),
            "duration_gap": (None if p.tier in COUNT_MATCHED
                             else round(p.duration_gap, 4)),
            "evidence": ("exact count match, no durations available"
                         if p.tier in COUNT_MATCHED else "duration alignment"),
            "needs_a_human": p.needs_a_human, "notes": p.notes,
        })
    (out_dir / "phase3_proposals.json").write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")


# --- applying ---------------------------------------------------------------
#
# Separate from the report on purpose, and structurally so: apply refuses to run
# without a proposals file, which means the report has to have been produced and
# could have been read.  It writes tags and nothing else - no renames, no moves,
# no deletions - and it re-checks fill-only against the file on disk rather than
# trusting the proposal, because a proposals file written days ago may describe
# tags that have since been filled in by hand.

# WMA is refused rather than attempted.  tagwriter falls through to a generic
# loop that assigns Vorbis-style keys into an ASF container, where the fields
# readers use are Title, Author and WM/AlbumTitle - so the write appears to
# succeed and reads back as nothing.
UNWRITABLE = {".shn", ".wma"}

FILLABLE = ("TITLE", "VENUE")


def _audio_paths(folder: Path) -> dict:
    """{filename: [paths]} for the audio under a folder, discs included.

    Deliberately cheap: nothing here opens a file.  The apply step only needs
    to know which names exist and where they are.
    """
    out: dict[str, list[Path]] = {}
    for sub, _, names in os.walk(folder):
        for n in names:
            if Path(n).suffix.lower() in AUDIO:
                out.setdefault(n, []).append(Path(sub) / n)
    return out


def _current_tags(path: Path) -> dict | None:
    """TITLE and VENUE as phase 1 reads them, or None if the file cannot be read.

    Fill-only is only as good as this answer, so it comes from the same reader
    as the rest of the pipeline rather than one of its own.  The one it had
    asked mutagen's "easy" interface for the title, which WAV and AIFF do not
    offer - an existing TITLE there read as empty and was overwritten - and
    took VENUE from str() of the raw tag, which for M4A is the repr of a
    freeform atom, so phase 3 could never recognise its own venue there.
    """
    from .audio import read_audio_file

    af = read_audio_file(Path(path), read_tags=True)
    if af.tag_support != "full":
        return None
    out = {}
    for name in FILLABLE:
        value = (af.tag(name) or "").strip()
        if value:
            out[name] = value
    return out


def _phase3_written(folder: Path) -> dict:
    """What phase 3 wrote here: {filename: {FIELD: value}}.

    The values matter, not just the field names.  Recording only "phase 3 filled
    TITLE here" cannot tell our own earlier answer apart from one you typed over
    it afterwards, and the whole point of fill-only is that a human answer wins.
    Keeping the value settles it: we may replace what we wrote if it is still
    exactly what we wrote, and never anything else.
    """
    try:
        state = json.loads(Path(opener(folder / ".etree_state.json")).read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state.get("phase3_written") or {}


def apply_proposals(root: Path, out_dir: Path, proposals_path: Path,
                    dry_run: bool = True, progress=None, artists=None) -> dict:
    """Write the missing tags named in a proposals file.  Nothing else.

    `artists` limits it to folders under those top-level folders, the way
    --artist limits every other phase.  It was accepted on the command line and
    ignored here, so a scoped apply wrote the whole proposals file.
    """
    from . import tagwriter
    from .naming import make_location

    payload = json.loads(proposals_path.read_text(encoding="utf-8"))
    actions = []
    stats = Counter()
    wanted_scopes = ([a.replace("\\", "/").strip("/").lower() for a in artists]
                     if artists else None)

    for entry in payload:
        if wanted_scopes is not None:
            rel = entry["folder"].replace("\\", "/").lower()
            if not any(rel == w or rel.startswith(w + "/") for w in wanted_scopes):
                continue
        folder = root / entry["folder"]
        if not folder.is_dir():
            actions.append(["SKIPPED", "folder", entry["folder"], "", "",
                            "folder is no longer there", ""])
            stats["folder gone"] += 1
            continue
        place = make_location(entry.get("venue"), entry.get("city"),
                              entry.get("state"), limit=120)
        titles = entry.get("titles") or {}
        # Read once per folder, not once per file. Both of these used to be
        # inside the loop: the state file was parsed again for every track, and
        # the filenames came from read_local_show, which opens every audio file
        # with mutagen to collect durations and tags that are then thrown away.
        audio = _audio_paths(folder)
        written = _phase3_written(folder)

        wanted: dict[Path, dict] = {}
        for name in sorted(set(titles) | set(audio)):
            # Disc folders repeat names, and a title is proposed per filename,
            # so a name held by two files cannot say which one it is for.
            paths = audio.get(name) or [folder / name]
            if name in titles and len(paths) > 1:
                stats["ambiguous filename"] += 1
                actions.append(["SKIPPED", "write_tags", entry["folder"], name, "",
                                "TITLE not written: %d files share this name" % len(paths), ""])
            for path in paths:
                if not path.exists():
                    continue
                if path.suffix.lower() in UNWRITABLE:
                    stats["unwritable format"] += 1
                    actions.append(["SKIPPED", "write_tags", entry["folder"], str(path), "",
                                    "%s tags cannot be written" % path.suffix, ""])
                    continue
                have = _current_tags(path)
                if have is None:
                    # Unreadable is not empty.  Treating it as empty is what let a
                    # fill land on a tag that was there all along.
                    stats["unreadable"] += 1
                    actions.append(["SKIPPED", "write_tags", entry["folder"], str(path), "",
                                    "tags could not be read, so fill-only cannot be checked", ""])
                    continue
                # Fill-only means never overwriting an answer that is not ours. A
                # field phase 3 wrote itself IS ours, and the folder's state says
                # which those are - so a correction to our own earlier work can land
                # while a value that came from the files, or from you, still cannot.
                mine = written.get(name, {})

                def may_write(field, value, have=have, mine=mine):
                    """Empty, or still exactly what we put there last time."""
                    if not value:
                        return False
                    current = have.get(field)
                    if current is None:
                        return True
                    if current == value:
                        return False            # already right, nothing to do
                    return current == mine.get(field)

                fill = {}
                if may_write("VENUE", place):
                    fill["VENUE"] = place
                if name in titles and len(paths) == 1 and may_write("TITLE", titles[name]):
                    fill["TITLE"] = titles[name]
                if fill:
                    wanted[path] = fill

        if not wanted:
            stats["nothing left to fill"] += 1
            continue

        if not dry_run:
            # The backup is never overwritten, so a folder phase 2 already
            # backed up keeps its older, more original copy.
            try:
                tagwriter.write_backup(folder, sorted(wanted))
            except Exception as exc:           # noqa: BLE001
                actions.append(["FAILED", "backup_tags", entry["folder"], str(folder),
                                "", "", str(exc)[:200]])
                stats["backup failed"] += 1
                continue

        for path, fill in sorted(wanted.items()):
            detail = ", ".join("%s=%r" % (k, v[:48]) for k, v in sorted(fill.items()))
            if dry_run:
                actions.append(["WOULD_WRITE", "write_tags", entry["folder"],
                                str(path), "", detail, ""])
                stats["files"] += 1
                stats.update({k: 1 for k in fill})
                continue
            try:
                tagwriter.write_tags(path, fill)
            except Exception as exc:           # noqa: BLE001 - recorded, never silent
                actions.append(["FAILED", "write_tags", entry["folder"], str(path),
                                "", detail, str(exc)[:200]])
                stats["failed"] += 1
                continue
            actions.append(["WRITTEN", "write_tags", entry["folder"], str(path),
                            "", detail, ""])
            stats["files"] += 1
            for k in fill:
                stats[k] += 1

        if not dry_run:
            if not _note_state(folder, {q.name: f for q, f in wanted.items()}):
                # The tags are written; what is lost is the record that phase 3
                # wrote them, which is what lets it correct its own answer later.
                stats["state not recorded"] += 1
                actions.append(["FAILED", "note_state", entry["folder"], str(folder), "",
                                "phase3_written could not be recorded", ""])
        stats["folders"] += 1
        if progress:
            progress("  %s" % entry["folder"])

    from .report import write_csv

    # A dry run has its own file.  Written to the same name, it replaced the
    # log of the last real apply - the one record of what was written.
    write_csv(out_dir / ("phase3_dry_run.csv" if dry_run else "phase3_committed.csv"),
              ["status", "kind", "folder", "old", "new", "detail", "error"],
              actions)
    return dict(stats)


def _note_state(folder: Path, written: dict) -> bool:
    """Record in the folder's state that phase 3 filled something.

    Traceable afterwards, and it costs one line.  Written with the same
    read-modify-write the rest of the pipeline uses, and a failure here is not
    worth losing the tags that were just written correctly - but it is worth
    saying, so False comes back and the caller reports it.
    """
    import time

    p = Path(opener(folder / ".etree_state.json"))
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    from .state import newer_format

    if newer_format(state):
        return False
    record = state.get("phase3_written") or {}
    for fname, fields_ in written.items():
        record.setdefault(fname, {}).update(fields_)
    state["phase3_written"] = record
    state["phase3_filled"] = sorted({k for f in record.values() for k in f})
    state["phase3_filled_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        p.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True
