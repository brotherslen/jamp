"""Tests for the completeness check.

The thing this must get right is the asymmetry. `setlist.align` scores how much
of OUR duration found an answer, and a recording missing its last four songs
scores about 100% on that measure - which is why nothing in the pipeline could
see truncation before. Every test here is really about the other side of the
alignment: what of THEIRS answered to nothing.

Second, it must not over-claim. A finding here sends somebody off to
re-download a show, so "we cannot tell" has to be a distinct answer from "you
are missing six songs".
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jamp import complete  # noqa: E402


class T:
    def __init__(self, seconds, title=""):
        self.seconds = seconds
        self.title = title


class Rec:
    """Just enough of an archive.org Recording."""

    def __init__(self, seconds, identifier="ref"):
        self.identifier = identifier
        self.tracks = [T(s, "song%d" % i) for i, s in enumerate(seconds)]


def show(seconds, rel="Phish/1997/ph1997-01-01.flac16 - Somewhere"):
    return complete.LocalShow(rel=rel, band="ph", date="1997-01-01",
                              seconds=list(seconds))


FULL = [300, 420, 250, 600, 380, 290]      # six songs, 37 minutes


# --- the asymmetry, which is the whole point --------------------------------

def test_a_recording_missing_its_last_songs_is_found():
    """The case the existing alignment scores at 100%.

    Every track we hold matches perfectly. It is what we do NOT hold that says
    the recording stopped early.
    """
    f = complete.judge(show(FULL[:3]), [Rec(FULL)], "archive.org")
    assert f.verdict == complete.TRUNCATED
    assert f.score > 0.95, "our side really does align perfectly"
    assert 1250 < f.missing_seconds < 1290
    assert f.shape == complete.TAIL


def test_a_complete_recording_is_left_alone():
    f = complete.judge(show(FULL), [Rec(FULL)], "archive.org")
    assert f.verdict == complete.COMPLETE
    assert f.missing_seconds < complete.MIN_MISSING_SECONDS


def test_a_trimmed_join_is_not_a_missing_song():
    """Applause cut at a join costs seconds, not songs."""
    f = complete.judge(show([298, 417, 248, 596, 377, 288]), [Rec(FULL)],
                       "archive.org")
    assert f.verdict == complete.COMPLETE


# --- never over-claiming ----------------------------------------------------

def test_the_kindest_copy_of_the_night_decides():
    """If any copy says we are complete, we are.

    Eighteen copies of a Dead night exist and they do not all carry the same
    material - one includes the soundcheck, another does not. A shortfall is
    only real when every copy that fits leaves the same songs over.
    """
    generous = Rec(FULL + [900], "has-a-soundcheck")
    exact = Rec(FULL, "just-the-show")
    f = complete.judge(show(FULL), [generous, exact], "archive.org")
    assert f.verdict == complete.COMPLETE
    assert f.identifier == "just-the-show"


def test_a_different_recording_says_nothing_either_way():
    """Low on our side too - so it is not a reference, it is another show."""
    f = complete.judge(show([111, 222, 333]), [Rec(FULL)], "archive.org")
    assert f.verdict == complete.NOT_COMPARABLE
    assert "says nothing about completeness" in f.note


def test_no_candidate_with_durations_is_not_a_verdict():
    f = complete.judge(show(FULL), [], "archive.org")
    assert f.verdict == complete.NO_REFERENCE


def test_a_reference_whose_durations_are_missing_is_skipped():
    """A candidate with no times cannot support a claim about seconds."""
    blank = Rec([0, 0, 0])
    assert complete.best_reading(FULL, [blank]) is None


def test_a_reference_shorter_than_us_cannot_show_a_shortfall():
    """archive.org is full of part-uploads - sometimes one track of a night.

    Our copy may well be complete, but a reference shorter than it cannot say
    so, and calling that COMPLETE would be an absence of a finding dressed up
    as one.
    """
    f = complete.judge(show(FULL + [900, 700]), [Rec(FULL)], "archive.org")
    assert f.verdict == complete.REFERENCE_SHORTER
    assert "cannot show a shortfall" in f.note


def test_a_one_track_fragment_never_pronounces_a_folder_complete():
    """The bug the real run exposed.

    Kindness alone picks the fragment every time - it leaves nothing over - so
    a folder missing half its night was being called complete on the strength
    of a 34-second upload sitting beside a full copy.
    """
    fragment = Rec([34], "part-upload")
    whole = Rec(FULL, "the-whole-night")
    f = complete.judge(show(FULL[:3]), [fragment, whole], "archive.org")
    assert f.verdict == complete.TRUNCATED
    assert f.identifier == "the-whole-night"


# --- where the gap is -------------------------------------------------------

def test_the_shape_says_what_kind_of_gap_it_is():
    """"Cut off at the end" and "you only have set two" are different facts."""
    assert complete.shape_of([4, 5], 6) == complete.TAIL
    assert complete.shape_of([0, 1], 6) == complete.HEAD
    assert complete.shape_of([2, 3], 6) == complete.BLOCK
    assert complete.shape_of([1, 4], 6) == complete.SCATTERED
    assert complete.shape_of([], 6) == ""


def test_a_folder_holding_only_the_second_set_reads_as_one_block():
    f = complete.judge(show(FULL[3:]), [Rec(FULL)], "archive.org")
    assert f.verdict == complete.TRUNCATED
    assert f.shape == complete.HEAD


# --- a stated shortfall is not a discovery ----------------------------------

@pytest.mark.parametrize("name", [
    "ph1997-01-01.sbd.flac16 - Set 2",
    "ph1997-01-01.s2b.flac",
    "ph1997-01-01 Disc 1",
    "ph1997-01-01.sbd.flac16 soundcheck",
    "ph1997-01-01.partial.flac16",
])
def test_a_folder_that_says_it_is_partial_is_filed_apart(name):
    f = complete.judge(show(FULL[:3], rel="Phish/1997/" + name), [Rec(FULL)],
                       "archive.org")
    assert f.verdict == complete.STATED_PARTIAL
    assert "the folder name says" in f.note


def test_an_ordinary_name_is_not_read_as_partial():
    assert not show(FULL, rel="Phish/1997/ph1997-01-01.sbd.flac16 - Somewhere") \
        .stated_partial


# --- counting, where there are no durations ---------------------------------

def test_a_count_is_a_floor_not_a_value():
    """A segue may be one file or two, so the count can honestly land anywhere
    from `songs - segues` upwards."""
    s = show([1] * 18)
    assert complete.judge_by_count(s, 20, 2, "mmjarchive").verdict == complete.COMPLETE
    assert complete.judge_by_count(s, 20, 0, "mmjarchive").verdict == \
        complete.SHORT_BY_COUNT


def test_extra_tracks_above_the_floor_are_not_a_finding():
    """Tuning, banter and filler are ordinary and are not songs."""
    assert complete.judge_by_count(show([1] * 24), 20, 0, "mmjarchive").verdict == \
        complete.COMPLETE


def test_a_counted_verdict_says_it_was_only_counted():
    f = complete.judge_by_count(show([1] * 10), 20, 0, "mmjarchive")
    assert "publishes no durations" in f.evidence
    assert f.missing_seconds == 0, "a count cannot produce seconds"


def test_a_source_that_does_not_count_segues_is_not_read_as_zero():
    """jerrybase publishes no segues.  Passing 0 turned Winterland 1973-11-09,
    25 tracks against 28 songs, into three missing songs - three segues explain
    it just as well, and a count cannot say which."""
    short = complete.judge_by_count(show([1] * 25), 28, None, "jerrybase")
    assert short.verdict == complete.NOT_COMPARABLE
    assert "0 segue" not in short.note and "does not say" in short.note
    assert complete.judge_by_count(show([1] * 28), 28, None, "jerrybase").verdict == \
        complete.COMPLETE


def test_the_check_can_be_scoped_to_an_artist():
    """--artist was accepted, "scoped to" was printed, and every folder was judged."""
    rels = ["The Grateful Dead/x", "Phish/1997/y", "Phish Side Project/z"]
    shows = [show([1.0], rel=r) for r in rels]
    assert [s.rel for s in complete.in_scope(shows, {"Phish"})] == ["Phish/1997/y"]
    assert [s.rel for s in complete.in_scope(shows, {"phish"})] == ["Phish/1997/y"]
    assert complete.in_scope(shows, None) == shows


# --- reading phase 0's reports ----------------------------------------------

def test_durations_and_metadata_are_joined_by_relative_path(tmp_path):
    ident = tmp_path / "id.csv"
    ident.write_text(
        "relative_path,file,audio_md5,samples,seconds,bits,rate,channels,bytes\n"
        "A/show,t1.flac,aa,1,300.5,16,44100,2,1\n"
        "A/show,t2.flac,bb,1,420.0,16,44100,2,1\n"
        "A/other,t1.flac,cc,1,0,16,44100,2,1\n", encoding="utf-8")
    folders = tmp_path / "f.csv"
    folders.write_text("relative_path,band,date\nA/show,ph,1997-01-01\n",
                       encoding="utf-8")
    got = complete.load_shows(ident, folders)
    assert len(got) == 1, "a folder whose durations are all zero has none"
    assert got[0].rel == "A/show"
    assert got[0].band == "ph"
    assert round(got[0].total, 1) == 720.5


# --- extra material on our side ---------------------------------------------

def test_material_missing_both_ways_is_not_a_truncation():
    """A shortfall means we are a SUBSET of that night.

    75 of the first real run's 103 truncations had tens of minutes
    unaccounted for in BOTH directions - two differently-tracked copies the
    aligner could not reconcile - and were reported as though the shortfall
    were one-way. Filler is the everyday version of this: a spare track from
    another night, which the date reader already treats as somebody
    else's date.
    """
    # Shaped like the real ones: their side is well covered, so the pair passes
    # the "same performance" gate - and ours is not, because we hold ten
    # minutes they never had.
    # Shaped like the real ones, which is fiddlier than it looks. The pair has
    # to clear the "same performance" gate - so most of both sides must match,
    # and the real cases score 0.85-0.89 - while still leaving material
    # unaccounted for in both directions. Their side must also be no shorter
    # than ours, or REFERENCE_SHORTER answers first.
    # Fiddlier to construct than it looks, and every constraint is real.
    # Their side must be no shorter than ours or REFERENCE_SHORTER answers
    # first. The pair must clear the "same performance" gate - the real cases
    # score 0.85-0.89, and this scores 0.857. And the extra track must be a
    # length nothing on their side explains: 600s would merge into two of
    # their 300s as a segue, correctly, and leave nothing over.
    theirs = [300] * 22
    ours = [300] * 18 + [900]
    f = complete.judge(complete.LocalShow("x", "ph", "1997-01-01", ours),
                       [Rec(theirs)], "archive.org")
    assert f.verdict == complete.NOT_COMPARABLE
    assert "tracked too differently" in f.note


def test_a_true_subset_is_still_called_truncated():
    """The guard must not swallow the case the check exists for."""
    f = complete.judge(show(FULL[:3]), [Rec(FULL)], "archive.org")
    assert f.verdict == complete.TRUNCATED


def test_filler_is_reported_beside_a_complete_verdict():
    """A complete show with a spare track from another night is complete.

    The extra is worth saying out loud - it is why the totals disagree - but
    it is not a defect and must not change the verdict.
    """
    f = complete.judge(show(FULL + [700]), [Rec(FULL + [690])], "archive.org")
    assert f.verdict == complete.COMPLETE
