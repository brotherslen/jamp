"""Line our tracks up against someone else's, without trusting either to agree.

The folders that need titles have none, and their filenames are numeric - all 40
of them, measured, including the original names kept in `.etree_backup.json` and
every `.md5` and `.ffp` sidecar.  So there is no text to match on.  What both
sides do have is **durations**, and those align well even when the tracking does
not.

Tracking is where the disagreement lives, and it is not error - it is taste:

* one taper cuts tuning, crowd noise and stage banter as tracks of their own,
  the next folds them into the song either side;
* a segue can be one file or two, so "Lazy Lightning -> Supplication" is
  sometimes a 14-minute track and sometimes two of seven;
* an encore break may or may not exist as a track at all.

So this aligns rather than zips, and allows a track on either side to answer to
nothing.  **The cost of skipping something is its own duration**, which is the
whole idea: dropping a 20-second tuning track is nearly free, dropping a
10-minute song is not, so the alignment cannot quietly slide out of step to make
the arithmetic work.

Nothing here decides anything.  It returns an alignment and a score; whether
that is good enough to write a tag is `confirm.py`'s judgement.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Seconds of cost added to a split or a merge, so a plain 1:1 reading wins
# whenever one exists.  Small: a genuine segue is still cheaper than
# skipping either track.
STRUCTURAL_PENALTY = 12.0

MATCH = "match"
SPLIT = "split"      # one of theirs is two of ours
MERGE = "merge"      # two of theirs is one of ours
SKIP_OURS = "skip_ours"
SKIP_THEIRS = "skip_theirs"


@dataclass
class Step:
    op: str
    ours: tuple[int, ...] = ()      # indexes into our track list
    theirs: tuple[int, ...] = ()    # indexes into theirs
    drift: float = 0.0              # seconds of disagreement


@dataclass
class Alignment:
    steps: list[Step] = field(default_factory=list)
    score: float = 0.0              # 0..1, share of our duration confidently matched
    matched: int = 0
    ours_total: float = 0.0
    theirs_total: float = 0.0

    @property
    def drift(self) -> float:
        """Total seconds of disagreement across everything that matched.

        The score counts how much of our duration found an answer, which two
        copies can both do perfectly while one of them fits far better. This is
        the tiebreak: of the copies that fit, the one that fits closest.
        """
        return sum(s.drift for s in self.steps if s.op in (MATCH, SPLIT, MERGE))

    @property
    def duration_gap(self) -> float:
        """How far apart the two recordings are overall, as a fraction."""
        if not self.ours_total:
            return 1.0
        return abs(self.ours_total - self.theirs_total) / self.ours_total

    def title_for(self, index: int) -> tuple[int, ...] | None:
        """Which of their tracks answer to our track `index`, if any."""
        for s in self.steps:
            if s.op in (MATCH, SPLIT, MERGE) and index in s.ours:
                return s.theirs
        return None


def tolerance(seconds: float) -> float:
    """How far apart two durations may be and still be the same performance.

    A flat tolerance is wrong at both ends: three seconds is generous on a
    45-second tuning track and mean on a 20-minute Dark Star, where trimming
    applause at the join easily moves half a minute.
    """
    return max(4.0, 0.03 * seconds)


def align(ours: list[float], theirs: list[float]) -> Alignment:
    """Dynamic-programming alignment of two duration sequences.

    Cost is in seconds throughout, so every operation is comparable: a match
    costs the disagreement it leaves behind, and a skip costs the whole of what
    was skipped.
    """
    n, m = len(ours), len(theirs)
    result = Alignment(ours_total=sum(ours), theirs_total=sum(theirs))
    if not n or not m:
        return result

    INF = float("inf")
    # cost[i][j] - best cost having consumed i of ours and j of theirs.
    cost = [[INF] * (m + 1) for _ in range(n + 1)]
    back: dict[tuple[int, int], Step] = {}
    cost[0][0] = 0.0

    for i in range(n + 1):
        for j in range(m + 1):
            here = cost[i][j]
            if here == INF:
                continue
            # 1:1
            if i < n and j < m:
                d = abs(ours[i] - theirs[j])
                pay = d if d <= tolerance(max(ours[i], theirs[j])) else d * 3.0
                if here + pay < cost[i + 1][j + 1]:
                    cost[i + 1][j + 1] = here + pay
                    back[(i + 1, j + 1)] = Step(MATCH, (i,), (j,), d)
            # one of ours answers to two of theirs - a segue tracked as one file.
            # STRUCTURAL_PENALTY makes a straight 1:1 reading win whenever one
            # exists: two unrelated tracks can sum to about the length of a
            # third, and without this a different show could part-align by
            # coincidence.  It is small enough that a real segue still wins.
            if i < n and j + 1 < m:
                d = abs(ours[i] - (theirs[j] + theirs[j + 1]))
                if d <= tolerance(ours[i]) and here + d + STRUCTURAL_PENALTY < cost[i + 1][j + 2]:
                    cost[i + 1][j + 2] = here + d + STRUCTURAL_PENALTY
                    back[(i + 1, j + 2)] = Step(MERGE, (i,), (j, j + 1), d)
            # two of ours answer to one of theirs - the same song split in two
            if i + 1 < n and j < m:
                d = abs((ours[i] + ours[i + 1]) - theirs[j])
                if d <= tolerance(theirs[j]) and here + d + STRUCTURAL_PENALTY < cost[i + 2][j + 1]:
                    cost[i + 2][j + 1] = here + d + STRUCTURAL_PENALTY
                    back[(i + 2, j + 1)] = Step(SPLIT, (i, i + 1), (j,), d)
            # skips: paying the duration of whatever is dropped
            if i < n and here + ours[i] < cost[i + 1][j]:
                cost[i + 1][j] = here + ours[i]
                back[(i + 1, j)] = Step(SKIP_OURS, (i,), (), ours[i])
            if j < m and here + theirs[j] < cost[i][j + 1]:
                cost[i][j + 1] = here + theirs[j]
                back[(i, j + 1)] = Step(SKIP_THEIRS, (), (j,), theirs[j])

    # walk back
    steps: list[Step] = []
    i, j = n, m
    while (i, j) != (0, 0):
        step = back.get((i, j))
        if step is None:                     # unreachable; give up rather than guess
            return result
        steps.append(step)
        i -= len(step.ours)
        j -= len(step.theirs)
    steps.reverse()

    matched_seconds = 0.0
    for s in steps:
        if s.op in (MATCH, SPLIT, MERGE):
            matched_seconds += sum(ours[k] for k in s.ours)
            result.matched += len(s.ours)
    result.steps = steps
    result.score = matched_seconds / result.ours_total if result.ours_total else 0.0
    return result
