"""Tests for deciding duplicates from the audio rather than from the names.

The rule these support is in docs/rules.md - duplicates are reported, never
resolved - and the reason it was written is experience:
every time a pair was characterised from names, track counts or durations
alone, the guess was wrong.  So the thing worth testing hardest is that a
folder is never called a duplicate on anything but the audio.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jamp import identity  # noqa: E402
from jamp.audio import AudioFile  # noqa: E402


class FakeShow:
    """Just enough of a ShowFolder for the index to read."""

    def __init__(self, rel, tracks):
        self.root = Path("R:/lib")
        self.path = self.root / rel
        self.files = [
            AudioFile(path=self.path / name, ext=".flac", size=1, audio_md5=md5)
            for name, md5 in tracks
        ]


def show(rel, **tracks):
    return FakeShow(rel, sorted(tracks.items()))


# --- the index --------------------------------------------------------------

def test_only_files_with_a_fingerprint_are_indexed():
    s = FakeShow("a", [])
    s.files = [AudioFile(path=Path("x.flac"), ext=".flac", size=1, audio_md5="aa"),
               AudioFile(path=Path("y.mp3"), ext=".mp3", size=1, audio_md5=None)]
    assert identity.index_folders([s]) == {"a": {"aa": ["x.flac"]}}


def test_a_folder_with_nothing_identifiable_is_absent_not_empty():
    """"Cannot be checked" must not read as "shares nothing with anything".

    An MP3-only folder, or a FLAC whose encoder left the MD5 zeroed, has no
    opinion about duplication.  Recording it as an empty set would let it match
    other unknowable folders and invent pairs out of ignorance.
    """
    s = FakeShow("quiet", [])
    s.files = [AudioFile(path=Path("a.mp3"), ext=".mp3", size=1)]
    assert identity.index_folders([s]) == {}


# --- what kind of sharing ---------------------------------------------------

def test_the_same_audio_throughout_is_identical():
    a = show("1997/livephish", t1="aa", t2="bb")
    b = show("1997/nugs", t1="aa", t2="bb")
    m = identity.find_matches(identity.index_folders([a, b]))
    assert len(m) == 1
    assert m[0].kind == identity.IDENTICAL
    assert m[0].shared == 2
    assert "redundant" in m[0].note


def test_one_folder_wholly_inside_another_is_a_subset():
    """The 1992 case: eight tracks of one night sitting inside another.

    Not a duplicate pair - the folders are different sizes and only one of them
    is wrong - so it must not be described as "the same recording", which would
    invite deleting the larger one.
    """
    big = show("1992/schenectady", d3t04="aa", d3t05="bb", d3t06="cc")
    small = show("1992/burlington", d3t04="aa", d3t05="bb")
    m = identity.find_matches(identity.index_folders([big, small]))
    assert m[0].kind == identity.SUBSET
    assert m[0].shared == 2
    assert "partial copy" in m[0].note


def test_sharing_some_audio_each_way_is_an_overlap():
    a = show("x", t1="aa", t2="bb")
    b = show("y", t2="bb", t3="cc")
    m = identity.find_matches(identity.index_folders([a, b]))
    assert m[0].kind == identity.OVERLAP
    assert "overlap rather than duplicate" in m[0].note


def test_folders_sharing_no_audio_are_not_a_pair():
    a = show("x", t1="aa")
    b = show("y", t1="zz")
    assert identity.find_matches(identity.index_folders([a, b])) == []


def test_two_tapers_of_one_night_are_not_duplicates():
    """The distinction the whole module rests on.

    Same band, same date, same track count, same titles, same length - and
    genuinely different recordings.  Nothing but the audio can tell these apart,
    and reporting them as duplicates is how a unique recording gets deleted.
    """
    a = show("1995/ph1995-06-22.aud.schoeps", t1="aa", t2="bb", t3="cc")
    b = show("1995/ph1995-06-22.aud.nak300", t1="dd", t2="ee", t3="ff")
    assert identity.find_matches(identity.index_folders([a, b])) == []


def test_one_master_at_two_compression_levels_is_still_one_recording():
    """ph1996-12-04: 465 MB against 434 MB, and identical music.

    File size, byte comparison and every name-based heuristic said these were
    different.  The audio MD5 is the only thing that says otherwise, and it is
    right.
    """
    a = show("1996/ph1996-12-04.sbd.set2.flac16", t1="aa", t2="bb")
    b = show("1996/ph1996-12-04.sbd.set2.shn", t1="aa", t2="bb")
    a.files[0].size, b.files[0].size = 465_000_000, 434_000_000
    m = identity.find_matches(identity.index_folders([a, b]))
    assert m[0].kind == identity.IDENTICAL


# --- ordering and shape -----------------------------------------------------

def test_exact_duplicates_are_reported_before_looser_matches():
    """Worst first: an exact duplicate is more actionable than an overlap."""
    dup_a = show("dup/a", t1="aa", t2="bb")
    dup_b = show("dup/b", t1="aa", t2="bb")
    ov_a = show("ov/a", t1="cc", t2="dd")
    ov_b = show("ov/b", t2="dd", t3="ee")
    m = identity.find_matches(identity.index_folders([ov_a, ov_b, dup_a, dup_b]))
    assert [x.kind for x in m] == [identity.IDENTICAL, identity.OVERLAP]


def test_three_copies_of_one_show_are_reported_as_three_pairs():
    shows = [show("c%d" % i, t1="aa", t2="bb") for i in range(3)]
    m = identity.find_matches(identity.index_folders(shows))
    assert len(m) == 3
    assert all(x.kind == identity.IDENTICAL for x in m)


def test_a_pair_is_reported_once_not_twice():
    a, b = show("a", t1="aa"), show("b", t1="aa")
    m = identity.find_matches(identity.index_folders([a, b]))
    assert len(m) == 1
    assert (m[0].left, m[0].right) == ("a", "b")


# --- the same audio twice inside one folder ---------------------------------

def test_one_folder_holding_a_recording_twice_is_found():
    s = show("doubled", t1="aa", t2="bb")
    s.files.append(AudioFile(path=s.path / "t1_copy.flac", ext=".flac",
                             size=1, audio_md5="aa"))
    got = identity.repeated_within(identity.index_folders([s]))
    assert len(got) == 1
    folder, _h, names = got[0]
    assert folder == "doubled"
    assert set(names) == {"t1", "t1_copy.flac"}


def test_a_folder_with_no_repeats_reports_nothing():
    assert identity.repeated_within(identity.index_folders([show("s", t1="aa", t2="bb")])) == []


def test_the_summary_counts_recordings_not_files():
    a = show("a", t1="aa", t2="bb")
    b = show("b", t1="aa", t2="bb")
    index = identity.index_folders([a, b])
    stats = identity.summarize(index, identity.find_matches(index))
    assert stats["tracks_identified"] == 4
    assert stats["distinct_recordings"] == 2
    assert stats["identical_folders"] == 1
