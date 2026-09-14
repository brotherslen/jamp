"""Which folders hold the same recording, decided by the audio and nothing else.

Duplicates are reported and never resolved (docs/rules.md), and experience
puts it more bluntly: *never guess
whether two folders are duplicates - every time a pair was characterised from
names, track counts or durations alone, the guess was wrong.*

That rule exists because the instrument was missing, not because the question is
unanswerable.  Every FLAC carries an MD5 of its decoded audio, written by the
encoder, and that number has exactly the properties the question needs:

* it is about the **audio**, so retagging, renaming and refiling do not move it -
  which matters here more than anywhere, since this pipeline's own output is its
  own input;
* it is about the audio **only**, so the same master encoded at two different
  compression levels gives one number from two files sharing no bytes.  That is
  `ph1996-12-04` - 465 MB against 434 MB, bit-identical music, and a duplicate
  that file size, track count and name had all failed to settle;
* and it is **per track**, so two folders can be compared properly: all tracks
  in common is a duplicate, some tracks in common is one copy being a subset of
  the other, which is a different fact and wants saying differently.

What this cannot see: a lossy copy, a different transfer of one master, or two
tapers' recordings of one night.  Those are genuinely different audio and this
will say so, correctly.  The question "is this the same *performance*" is not
the question "is this the same *recording*", and only the second one has a
definite answer.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

IDENTICAL = "IDENTICAL"      # same tracks, same audio, both ways
SUBSET = "SUBSET"            # every track of one is in the other, which has more
OVERLAP = "OVERLAP"          # they share some audio and each has tracks the other lacks


@dataclass
class AudioMatch:
    """Two folders that share audio, and exactly how much."""

    left: str
    right: str
    kind: str
    shared: int = 0
    left_total: int = 0
    right_total: int = 0
    examples: list[str] = field(default_factory=list)

    @property
    def note(self) -> str:
        if self.kind == IDENTICAL:
            return ("the same %d tracks of audio, byte for byte once decoded - "
                    "one of these is redundant" % self.shared)
        if self.kind == SUBSET:
            small, big = sorted((self.left_total, self.right_total))
            return ("all %d tracks of the smaller folder are in the larger one, "
                    "which has %d - a partial copy, not a second recording"
                    % (small, big))
        return ("%d tracks in common, and each folder has audio the other does "
                "not - they overlap rather than duplicate"
                % self.shared)


def index_folders(shows) -> dict:
    """{relative folder path: {audio_md5: [filenames]}}, FLAC only.

    A folder with no FLAC, or whose encoder left the MD5 field zeroed, simply
    does not appear.  Being unable to answer is not the same as answering no,
    and a folder that cannot be checked must not read as a folder that has
    nothing in common with anything.
    """
    out: dict = {}
    for show in shows:
        found: dict = defaultdict(list)
        for f in show.files:
            if f.audio_md5:
                found[f.audio_md5].append(f.name)
        if found:
            out[str(show.path.relative_to(show.root))] = dict(found)
    return out


def find_matches(index: dict, min_shared: int = 1) -> list[AudioMatch]:
    """Every pair of folders sharing audio, described by how they share it.

    Pairs are found through the hashes rather than by comparing every folder
    with every other: the number of folders sharing any given recording is tiny,
    so this stays linear in the sharing rather than quadratic in the library.
    """
    by_hash: dict = defaultdict(set)
    for folder, hashes in index.items():
        for h in hashes:
            by_hash[h].add(folder)

    pairs: dict = defaultdict(set)
    for h, folders in by_hash.items():
        if len(folders) < 2:
            continue
        ordered = sorted(folders)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                pairs[(a, b)].add(h)

    matches = []
    for (a, b), shared in sorted(pairs.items()):
        if len(shared) < min_shared:
            continue
        ha, hb = set(index[a]), set(index[b])
        if ha == hb:
            kind = IDENTICAL
        elif ha <= hb or hb <= ha:
            kind = SUBSET
        else:
            kind = OVERLAP
        examples = sorted(index[a][h][0] for h in sorted(shared))[:3]
        matches.append(AudioMatch(left=a, right=b, kind=kind, shared=len(shared),
                                  left_total=len(ha), right_total=len(hb),
                                  examples=examples))
    # Worst first: an exact duplicate is more actionable than an overlap.
    order = {IDENTICAL: 0, SUBSET: 1, OVERLAP: 2}
    matches.sort(key=lambda m: (order[m.kind], -m.shared, m.left))
    return matches


def repeated_within(index: dict) -> list[tuple[str, str, list[str]]]:
    """One folder holding the same audio twice, under two filenames.

    Distinct from a duplicate folder and easy to miss: a show with its encore
    saved twice, or a disc copied in beside itself, looks like an ordinary long
    show until the hashes are counted.
    """
    out = []
    for folder, hashes in sorted(index.items()):
        for h, names in sorted(hashes.items()):
            if len(names) > 1:
                out.append((folder, h, sorted(names)))
    return out


def summarize(index: dict, matches: list[AudioMatch]) -> dict:
    files = sum(len(n) for h in index.values() for n in h.values())
    return {
        "folders_with_audio_identity": len(index),
        "tracks_identified": files,
        "distinct_recordings": len({h for hs in index.values() for h in hs}),
        "folder_pairs_sharing_audio": len(matches),
        "identical_folders": sum(1 for m in matches if m.kind == IDENTICAL),
        "subset_folders": sum(1 for m in matches if m.kind == SUBSET),
        "overlapping_folders": sum(1 for m in matches if m.kind == OVERLAP),
    }
