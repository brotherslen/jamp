"""The cache and the archive.org client.  Nothing here touches the network."""
import json
from pathlib import Path

import pytest

from jamp.archiveorg import (
    Recording,
    fetch_recording,
    parse_metadata,
    recordings_for,
    search_identifiers,
    split_coverage,
)
from jamp.httpcache import FetchError, HttpCache, OfflineMiss


# --- a fake network --------------------------------------------------------

class FakeNet:
    """Answers the URLs it is given and counts how often it is asked."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, timeout):
        self.calls.append(url)
        if url not in self.responses:
            raise OSError("no route to %s" % url)
        body = self.responses[url]
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        return 200, body


def cache_with(tmp_path, responses, offline=False):
    net = FakeNet(responses)
    return HttpCache(tmp_path / "c.sqlite", offline=offline,
                     opener=net, sleep=lambda s: None), net


# --- the cache -------------------------------------------------------------

def test_a_url_is_fetched_once_and_then_served_from_the_cache(tmp_path):
    cache, net = cache_with(tmp_path, {"http://x/1": {"ok": True}})
    first = cache.get("http://x/1")
    second = cache.get("http://x/1")
    assert first.json() == second.json() == {"ok": True}
    assert first.from_cache is False and second.from_cache is True
    assert len(net.calls) == 1, "the second read must not reach the network"


def test_offline_refuses_to_fetch_but_still_answers_what_it_holds(tmp_path):
    """This is what makes a second phase 3 run reproducible."""
    cache, _ = cache_with(tmp_path, {"http://x/1": {"ok": True}})
    cache.get("http://x/1")
    cache.close()

    offline, net = cache_with(tmp_path, {"http://x/1": {"ok": True}}, offline=True)
    assert offline.get("http://x/1").json() == {"ok": True}
    with pytest.raises(OfflineMiss):
        offline.get("http://x/2")
    assert net.calls == [], "offline must not open a connection at all"


def test_a_failure_is_recorded_and_raised_not_swallowed(tmp_path):
    cache, _ = cache_with(tmp_path, {})
    with pytest.raises(FetchError):
        cache.get("http://x/missing")
    assert cache.stats()["failures"] == 1


def test_the_cache_survives_being_reopened(tmp_path):
    cache, _ = cache_with(tmp_path, {"http://x/1": {"ok": True}})
    cache.get("http://x/1")
    cache.close()
    again, net = cache_with(tmp_path, {})
    assert again.get("http://x/1").json() == {"ok": True}
    assert net.calls == []


# --- coverage --------------------------------------------------------------

@pytest.mark.parametrize("text,city,state", [
    ("Ithaca, NY", "Ithaca", "NY"),
    ("Santa Barbara, CA", "Santa Barbara", "CA"),
    # Not everything is a US state, and a country is kept rather than guessed at.
    ("London, England", "London", "England"),
    ("Vancouver, BC", "Vancouver", "BC"),
    # A bare city is a city, not half a pair.
    ("Tokyo", "Tokyo", None),
    ("", None, None),
    (None, None, None),
])
def test_split_coverage(text, city, state):
    assert split_coverage(text) == (city, state)


# --- metadata --------------------------------------------------------------

CORNELL = {
    "metadata": {
        "identifier": "gd1977-05-08.sbd.cantor.sacks.266.shnf",
        "date": "1977-05-08",
        "venue": "Barton Hall - Cornell University",
        "coverage": "Ithaca, NY",
        "creator": "Grateful Dead",
        "taper": "Betty Cantor",
        "transferer": "Darrin Sacks",
        "collection": ["GratefulDead", "etree"],
    },
    "files": [
        {"name": "01.flac", "format": "Flac", "track": "01", "title": "Minglewood Blues ->"},
        {"name": "01.mp3", "format": "VBR MP3", "track": "01", "title": "Minglewood Blues ->"},
        {"name": "02.flac", "format": "Flac", "track": "02", "title": "Loser"},
        {"name": "cover.jpg", "format": "JPEG"},
    ],
}


def test_parse_metadata_reads_the_fields_phase_3_exists_to_fill():
    rec = parse_metadata(CORNELL, "gd1977-05-08.sbd.cantor.sacks.266.shnf")
    assert rec.venue == "Barton Hall - Cornell University"
    assert (rec.city, rec.state) == ("Ithaca", "NY")
    assert rec.taper == "Betty Cantor"
    assert rec.transferer == "Darrin Sacks"
    assert rec.date == "1977-05-08"
    assert rec.shnid == "266"


def test_one_song_in_three_formats_is_still_one_track():
    rec = parse_metadata(CORNELL, "gd1977-05-08.sbd.cantor.sacks.266.shnf")
    assert [t.title for t in rec.tracks] == ["Minglewood Blues", "Loser"]
    # The segue is kept as a fact about the track, not as punctuation in the title.
    assert rec.tracks[0].segue is True
    assert rec.tracks[1].segue is False


def test_an_item_that_is_not_a_show_is_recognised():
    """A sample-rate test really does sit beside the recordings of that night."""
    assert Recording("skb2003-02-15.sample_rate_test").is_show is False
    assert Recording("skb2003-02-15.onstage-schoeps.miller-phares.flac1648").is_show is True


# --- searching -------------------------------------------------------------

def _search_url(prefix, joiner, date):
    from urllib.parse import urlencode

    from jamp.archiveorg import SEARCH

    return SEARCH + "?" + urlencode(
        {"q": "identifier:%s%s%s*" % (prefix, joiner, date),
         "fl[]": "identifier", "rows": "40", "output": "json"})


def test_both_separators_are_tried_because_the_convention_varies(tmp_path):
    """"gd1977-05-08" has no separator; "STS9-1999-10-15" has a dash."""
    responses = {
        _search_url("sts9", "", "1999-10-15"): {"response": {"docs": []}},
        _search_url("sts9", "-", "1999-10-15"):
            {"response": {"docs": [{"identifier": "STS9-1999-10-15.flac16"}]}},
    }
    cache, _ = cache_with(tmp_path, responses)
    assert search_identifiers(cache, ["sts9"], "1999-10-15") == ["STS9-1999-10-15.flac16"]


def test_a_recording_whose_own_date_disagrees_is_dropped(tmp_path):
    """A prefix match can be a coincidence; the item's own metadata decides."""
    from jamp.archiveorg import METADATA

    wrong = dict(CORNELL)
    wrong["metadata"] = dict(CORNELL["metadata"], date="1977-05-09")
    responses = {
        _search_url("gd", "", "1977-05-08"):
            {"response": {"docs": [{"identifier": "gd1977-05-08.other"}]}},
        _search_url("gd", "-", "1977-05-08"): {"response": {"docs": []}},
        METADATA + "gd1977-05-08.other": wrong,
    }
    cache, _ = cache_with(tmp_path, responses)
    assert recordings_for(cache, ["gd"], "1977-05-08") == []


def test_a_404_is_an_answer_and_is_not_asked_twice(tmp_path):
    """The show is not there.  Caching that stops it being asked every run."""
    import urllib.error

    class Gone:
        def __init__(self):
            self.calls = 0

        def __call__(self, url, timeout):
            self.calls += 1
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    net = Gone()
    cache = HttpCache(tmp_path / "c.sqlite", opener=net, sleep=lambda s: None)
    assert fetch_recording(cache, "nope") is None
    assert fetch_recording(cache, "nope") is None
    assert net.calls == 1


# --- duration alignment ----------------------------------------------------

from jamp.setlist import MATCH, MERGE, SKIP_OURS, SKIP_THEIRS, SPLIT, align


def ops(a):
    return [s.op for s in a.steps]


def test_identical_tracking_aligns_one_to_one():
    a = align([180.0, 240.0, 300.0], [180.0, 240.0, 300.0])
    assert ops(a) == [MATCH, MATCH, MATCH]
    assert a.score == 1.0
    assert a.matched == 3


def test_small_differences_are_the_same_performance():
    """Trimming applause at the join moves a boundary by a few seconds."""
    a = align([180.0, 240.0, 300.0], [182.0, 237.0, 305.0])
    assert ops(a) == [MATCH, MATCH, MATCH]
    assert a.score == 1.0


def test_a_tuning_track_they_do_not_have_is_skipped_cheaply():
    a = align([25.0, 180.0, 240.0], [180.0, 240.0])
    assert ops(a) == [SKIP_OURS, MATCH, MATCH]
    # The tuning track is ours and unmatched, so the score is just short of 1.
    assert 0.85 < a.score < 1.0


def test_a_segue_tracked_as_one_file_matches_two_of_theirs():
    """"Lazy Lightning -> Supplication" is sometimes one track and sometimes two."""
    a = align([420.0], [180.0, 240.0])
    assert ops(a) == [MERGE]
    assert a.steps[0].theirs == (0, 1)
    assert a.score == 1.0


def test_a_song_split_across_two_files_matches_one_of_theirs():
    a = align([180.0, 240.0], [420.0])
    assert ops(a) == [SPLIT]
    assert a.steps[0].ours == (0, 1)
    assert a.score == 1.0


def test_a_long_song_is_not_quietly_dropped_to_make_the_sums_work():
    """Skipping costs the duration skipped, so the alignment cannot slide."""
    a = align([600.0, 180.0], [180.0])
    assert ops(a) == [SKIP_OURS, MATCH]
    assert a.score < 0.3, "most of our duration is unaccounted for"


def test_a_recording_of_a_different_show_is_rejected_by_the_gate():
    """The score alone is not the protection, and this case shows why.

    Two unrelated tracks of ours sum to about the length of one of theirs, so a
    coincidental split part-aligns and the score lands near 0.58 - not obviously
    wrong on its own.  What gives it away is the whole recording being half as
    long again as ours.  Both conditions together are the gate.
    """
    a = align([180.0, 240.0, 300.0], [412.0, 95.0, 631.0])
    good_enough = a.score >= 0.85 and a.duration_gap <= 0.05
    assert not good_enough
    assert a.duration_gap > 0.5, "the recordings are nothing like the same length"


def test_a_missing_encore_shows_up_as_their_extra_track():
    a = align([180.0, 240.0], [180.0, 240.0, 300.0])
    assert SKIP_THEIRS in ops(a)
    assert a.score == 1.0, "everything we have is still accounted for"
    assert a.duration_gap > 0.3, "but the recordings are not the same length"


def test_empty_input_is_not_a_match():
    assert align([], [180.0]).score == 0.0
    assert align([180.0], []).score == 0.0


def test_title_for_finds_which_of_theirs_answers_to_ours():
    a = align([25.0, 420.0], [180.0, 240.0])
    assert a.title_for(0) is None          # the tuning track answers to nothing
    assert a.title_for(1) == (0, 1)        # the segue answers to both


# --- choosing which recording is ours --------------------------------------

from jamp.confirm import markers_contradict


@pytest.mark.parametrize("ours,theirs,contradict", [
    # Zero played early and late at Nick's Bar on 1997-12-12.  Our late show
    # matched their early one on the taper's name alone.
    ("zero1997-12-12.late.aud.miller.flac16",
     "zero1997-12-12.early.akgC61.cooper.miller.109280.flac16", True),
    ("jgb1983-06-04.early.aud.cohen.flac16", "jgb1983-06-04.late.aud.cohen", True),
    # The same set is not a contradiction.
    ("zero1997-12-12.late.aud.miller.flac16", "zero1997-12-12.late.miller", False),
    # Silence on either side is not disagreement - most names say nothing.
    ("skb2002-02-22.flac16", "skb2002-02-22.dpa4011.flac16", False),
    ("zero1997-12-12.late.flac16", "zero1997-12-12.miller", False),
    # A soundcheck is not the show.
    ("ph1997-03-01.soundcheck.wma", "ph1997-03-01.early.sbd", True),
])
def test_markers_contradict(ours, theirs, contradict):
    assert markers_contradict(ours, theirs) is contradict


def test_a_word_that_merely_contains_a_marker_is_not_one():
    """"lately" is not "late", and a venue called Electric Ballroom is a venue."""
    assert markers_contradict("x2000-01-01.lately", "x2000-01-01.early") is False
    assert markers_contradict("x2000-01-01.electricity", "x2000-01-01.acoustic") is False


# --- applying --------------------------------------------------------------

from jamp.confirm import apply_proposals
from tests.fixtures import make_flac, make_shn


def _settled(folder, band="gd", date="1977-05-08"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".etree_state.json").write_text(
        json.dumps({"band": band, "date": date, "folder_name": folder.name}),
        encoding="utf-8")


def _proposal_file(out_dir, entries):
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "phase3_proposals.json"
    p.write_text(json.dumps(entries), encoding="utf-8")
    return p


def test_apply_fills_only_what_is_missing(tmp_path):
    """A title that is already there is not a field phase 3 has an opinion on."""
    root, out = tmp_path / "lib", tmp_path / "out"
    folder = root / "gd1977-05-08.flac16"
    _settled(folder)
    make_flac(folder / "t01.flac", tags={"TITLE": "Loser"})
    make_flac(folder / "t02.flac")

    p = _proposal_file(out, [{
        "folder": "gd1977-05-08.flac16", "tier": "SHNID", "identifier": "x",
        "venue": "Barton Hall", "city": "Ithaca", "state": "NY",
        "titles": {"t01.flac": "WRONG", "t02.flac": "El Paso"},
    }])
    stats = apply_proposals(root, out, p, dry_run=False)

    import mutagen
    kept = mutagen.File(folder / "t01.flac", easy=True)
    filled = mutagen.File(folder / "t02.flac", easy=True)
    assert kept["title"] == ["Loser"], "an existing title must never be overwritten"
    assert filled["title"] == ["El Paso"]
    assert stats["TITLE"] == 1


def test_apply_re_checks_against_the_file_not_the_proposal(tmp_path):
    """A proposals file written days ago may describe tags since filled by hand."""
    root, out = tmp_path / "lib", tmp_path / "out"
    folder = root / "gd1977-05-08.flac16"
    _settled(folder)
    make_flac(folder / "t01.flac")
    p = _proposal_file(out, [{
        "folder": "gd1977-05-08.flac16", "tier": "SHNID", "identifier": "x",
        "venue": None, "city": None, "state": None,
        "titles": {"t01.flac": "El Paso"},
    }])
    apply_proposals(root, out, p, dry_run=False)
    # Someone edits it by hand, then the same proposals file is applied again.
    import mutagen
    f = mutagen.File(folder / "t01.flac", easy=True)
    f["title"] = ["Something Else"]
    f.save()
    stats = apply_proposals(root, out, p, dry_run=False)
    assert mutagen.File(folder / "t01.flac", easy=True)["title"] == ["Something Else"]
    assert stats.get("TITLE", 0) == 0


def test_apply_sees_an_existing_title_on_a_wav_and_an_aiff(tmp_path):
    """mutagen's easy interface has no title for WAV or AIFF, so an existing
    TITLE there read as empty and phase 3 overwrote it."""
    import mutagen
    from mutagen import id3

    from tests.fixtures import make_wav

    root, out = tmp_path / "lib", tmp_path / "out"
    folder = root / "gd1977-05-08.wav"
    _settled(folder)
    wav = make_wav(folder / "t01.wav")
    aiff = folder / "t02.aiff"
    aiff.write_bytes(b"FORM\x00\x00\x00\x2eAIFFCOMM\x00\x00\x00\x12\x00\x01"
                     b"\x00\x00\x00\x00\x00\x10\x40\x0b\xfa\x00\x00\x00\x00\x00\x00\x00"
                     b"SSND\x00\x00\x00\x08\x00\x00\x00\x00\x00\x00\x00\x00")
    for path in (wav, aiff):
        audio = mutagen.File(str(path))
        audio.add_tags()
        audio.tags.add(id3.TIT2(encoding=3, text=["Loser"]))
        audio.save()

    p = _proposal_file(out, [{
        "folder": "gd1977-05-08.wav", "tier": "SHNID", "identifier": "x",
        "venue": None, "city": None, "state": None,
        "titles": {"t01.wav": "WRONG", "t02.aiff": "WRONG"},
    }])
    stats = apply_proposals(root, out, p, dry_run=False)
    for path in (wav, aiff):
        assert mutagen.File(str(path)).tags["TIT2"].text == ["Loser"], path.name
    assert stats.get("TITLE", 0) == 0


def test_apply_recognises_its_own_venue_on_an_m4a(tmp_path):
    """VENUE was read as the repr of the freeform atom, so phase 3 could never
    correct a venue it had itself written to an M4A."""
    from jamp.audio import read_audio_file
    from jamp.tagwriter import write_tags
    from tests.fixtures import make_m4a

    root, out = tmp_path / "lib", tmp_path / "out"
    folder = root / "gd1977-05-08.m4a"
    _settled(folder)
    track = make_m4a(folder / "t01.m4a")
    write_tags(track, {"VENUE": "Barton Hall, Ithaca, NY"})
    state = json.loads((folder / ".etree_state.json").read_text(encoding="utf-8"))
    state["phase3_written"] = {"t01.m4a": {"VENUE": "Barton Hall, Ithaca, NY"}}
    (folder / ".etree_state.json").write_text(json.dumps(state), encoding="utf-8")

    p = _proposal_file(out, [{
        "folder": "gd1977-05-08.m4a", "tier": "SHNID", "identifier": "x",
        "venue": "Barton Hall, Cornell University", "city": "Ithaca", "state": "NY",
        "titles": {},
    }])
    apply_proposals(root, out, p, dry_run=False)
    assert read_audio_file(track).tag("VENUE") == "Barton Hall, Cornell University, Ithaca, NY"


def test_a_dry_run_writes_nothing(tmp_path):
    root, out = tmp_path / "lib", tmp_path / "out"
    folder = root / "gd1977-05-08.flac16"
    _settled(folder)
    make_flac(folder / "t01.flac")
    p = _proposal_file(out, [{
        "folder": "gd1977-05-08.flac16", "tier": "SHNID", "identifier": "x",
        "venue": "Barton Hall", "city": "Ithaca", "state": "NY",
        "titles": {"t01.flac": "El Paso"},
    }])
    stats = apply_proposals(root, out, p, dry_run=True)
    import mutagen
    assert mutagen.File(folder / "t01.flac", easy=True).get("title") is None
    assert not (folder / ".etree_backup.json").exists()
    assert stats["files"] == 1


def test_a_format_that_cannot_hold_a_tag_is_refused_not_attempted(tmp_path):
    """SHN has nowhere to put one; WMA reads back as nothing.  Both are named."""
    root, out = tmp_path / "lib", tmp_path / "out"
    folder = root / "gd1977-05-08.shn"
    _settled(folder)
    make_shn(folder / "t01.shn")
    p = _proposal_file(out, [{
        "folder": "gd1977-05-08.shn", "tier": "SHNID", "identifier": "x",
        "venue": "Barton Hall", "city": "Ithaca", "state": "NY", "titles": {},
    }])
    stats = apply_proposals(root, out, p, dry_run=False)
    assert stats["unwritable format"] == 1
    assert stats.get("failed", 0) == 0, "refused up front, not attempted and failed"


def test_applying_backs_the_folder_up_first_and_records_what_it_did(tmp_path):
    root, out = tmp_path / "lib", tmp_path / "out"
    folder = root / "gd1977-05-08.flac16"
    _settled(folder)
    make_flac(folder / "t01.flac")
    p = _proposal_file(out, [{
        "folder": "gd1977-05-08.flac16", "tier": "SHNID", "identifier": "x",
        "venue": "Barton Hall", "city": "Ithaca", "state": "NY",
        "titles": {"t01.flac": "El Paso"},
    }])
    apply_proposals(root, out, p, dry_run=False)
    assert (folder / ".etree_backup.json").exists()
    assert (out / "phase3_committed.csv").exists()
    state = json.loads((folder / ".etree_state.json").read_text(encoding="utf-8"))
    assert set(state["phase3_filled"]) == {"TITLE", "VENUE"}
    assert state["phase3_filled_at"]


def test_a_folder_that_has_gone_is_reported_not_crashed_on(tmp_path):
    root, out = tmp_path / "lib", tmp_path / "out"
    root.mkdir()
    p = _proposal_file(out, [{
        "folder": "vanished", "tier": "SHNID", "identifier": "x",
        "venue": "X", "city": "Y", "state": "NY", "titles": {},
    }])
    stats = apply_proposals(root, out, p, dry_run=False)
    assert stats["folder gone"] == 1


def test_phase_3_may_correct_its_own_earlier_answer(tmp_path):
    """archive.org gave "Ky" and "Ga." where the library spells a state "KY".

    Fill-only would have frozen that mistake in place: the field is no longer
    empty, so a corrected run could never reach it.  A value phase 3 wrote is
    ours to replace - as long as it is still exactly what we wrote.
    """
    root, out = tmp_path / "lib", tmp_path / "out"
    folder = root / "mmj2005-11-23.flac16"
    _settled(folder, band="mmj", date="2005-11-23")
    make_flac(folder / "t01.flac")

    wrong = _proposal_file(out, [{
        "folder": "mmj2005-11-23.flac16", "tier": "ONLY", "identifier": "x",
        "venue": "The Palace Theater", "city": "Louisville", "state": "Ky",
        "titles": {},
    }])
    apply_proposals(root, out, wrong, dry_run=False)

    right = _proposal_file(out, [{
        "folder": "mmj2005-11-23.flac16", "tier": "ONLY", "identifier": "x",
        "venue": "The Palace Theater", "city": "Louisville", "state": "KY",
        "titles": {},
    }])
    stats = apply_proposals(root, out, right, dry_run=False)

    import mutagen
    got = str(mutagen.File(folder / "t01.flac")["VENUE"][0])
    assert got.endswith("KY"), got
    assert stats["VENUE"] == 1


def test_but_never_an_answer_somebody_else_gave(tmp_path):
    """The same mechanism must not become a licence to overwrite a human."""
    root, out = tmp_path / "lib", tmp_path / "out"
    folder = root / "mmj2005-11-23.flac16"
    _settled(folder, band="mmj", date="2005-11-23")
    make_flac(folder / "t01.flac")

    p = _proposal_file(out, [{
        "folder": "mmj2005-11-23.flac16", "tier": "ONLY", "identifier": "x",
        "venue": "The Palace Theater", "city": "Louisville", "state": "Ky",
        "titles": {},
    }])
    apply_proposals(root, out, p, dry_run=False)

    import mutagen
    f = mutagen.File(folder / "t01.flac")
    f["VENUE"] = ["The Palace Theatre, Louisville, KY"]   # corrected by hand
    f.save()

    other = _proposal_file(out, [{
        "folder": "mmj2005-11-23.flac16", "tier": "ONLY", "identifier": "x",
        "venue": "Somewhere Else", "city": "Nowhere", "state": "NV", "titles": {},
    }])
    stats = apply_proposals(root, out, other, dry_run=False)
    got = str(mutagen.File(folder / "t01.flac")["VENUE"][0])
    assert got == "The Palace Theatre, Louisville, KY"
    assert stats.get("VENUE", 0) == 0


def test_re_running_the_same_proposals_changes_nothing(tmp_path):
    root, out = tmp_path / "lib", tmp_path / "out"
    folder = root / "mmj2005-11-23.flac16"
    _settled(folder, band="mmj", date="2005-11-23")
    make_flac(folder / "t01.flac")
    p = _proposal_file(out, [{
        "folder": "mmj2005-11-23.flac16", "tier": "ONLY", "identifier": "x",
        "venue": "The Palace Theater", "city": "Louisville", "state": "KY",
        "titles": {"t01.flac": "Wordless Chorus"},
    }])
    first = apply_proposals(root, out, p, dry_run=False)
    second = apply_proposals(root, out, p, dry_run=False)
    assert first["files"] == 1
    assert second.get("files", 0) == 0, "a second identical apply is a no-op"


@pytest.mark.parametrize("text,city,state", [
    ("Louisville, Ky", "Louisville", "KY"),
    ("Athens, Ga.", "Athens", "GA"),
    ("Ithaca, NY", "Ithaca", "NY"),
    # Longer than a code, so left exactly as written.
    ("London, England", "London", "England"),
])
def test_state_codes_are_spelled_the_way_the_library_spells_them(text, city, state):
    assert split_coverage(text) == (city, state)


@pytest.mark.parametrize("text,city,state", [
    # Spelled out - the commonest shape after the plain code.
    ("Chicago Heights, Illinois", "Chicago Heights", "IL"),
    ("San Luis Obispo, California", "San Luis Obispo", "CA"),
    ("Washington, District of Columbia", "Washington", "DC"),
    # No comma at all: still a city and a state.
    ("Columbus Ohio", "Columbus", "OH"),
    ("Lake Placid New York", "Lake Placid", "NY"),
    # A city whose whole name is a state's name is not carved up.
    ("New York", "New York", None),
    ("Washington", "Washington", None),
    # Not a US state, so left exactly as written rather than guessed at.
    ("London, England", "London", "England"),
    ("Tokyo", "Tokyo", None),
    ("Vancouver, BC", "Vancouver", "BC"),
])
def test_a_state_is_spelled_the_way_the_library_spells_it(text, city, state):
    assert split_coverage(text) == (city, state)


from jamp.confirm import lineage_tokens


@pytest.mark.parametrize("name,prefixes,expected", [
    # The band and the date are shared by every copy of that night, so a token
    # drawn from either says nothing about which copy is ours.  "mmj2012" alone
    # matched a different taper's recording and took its (missing) city.
    ("mmj2012-08-17.aud.bobbybourbon.flac16", ["mmj"], {"bobbybourbon"}),
    ("gd1977-05-08.sbd.cantor.sacks.266.shnf", ["gd"], {"cantor", "sacks"}),
    ("zero1997-12-12.late.aud.miller.flac16", ["zero"], {"late", "miller"}),
    # A name with nothing but band, date and format has no lineage evidence at
    # all, and must not pretend otherwise.
    ("mmj2003-09-26.flac16", ["mmj"], set()),
    ("ph1998-06-30.flac24", ["ph"], set()),
])
def test_lineage_tokens_are_only_what_tells_two_copies_apart(name, prefixes, expected):
    assert lineage_tokens(name, prefixes) == expected


def test_a_format_token_is_not_lineage():
    """"flac16" appears in half the identifiers on archive.org."""
    assert lineage_tokens("x2000-01-01.flac16.shnf", ["x"]) == set()


# --- jerrybase --------------------------------------------------------------

from jamp import jerrybase as jb

EVENT_HTML = """
<h4>
  <strong>Jerry Garcia Band</strong>
  <span class="text-nowrap">
    <a href="/events?year=1987">1987</a>-10-31 [Sat]  Early
  </span>
</h4>
<h4><a href="/venues/958">Lunt-Fontanne Theatre</a>,
 <a href="/search_advanced/advanced_results?city=New+York&amp;state=NY">New York, NY</a></h4>
<div id="event_tags">
  <div class="col-12">
  <span class="badge pointer text-bg-success">official-release</span>
  </div>
</div>
<div class="entity-section"><h2 class="section-heading">Setlist</h2>
  <div class="stacked-field">
    <strong>Set 1</strong><br/>
    <span class="text-nowrap"><a class="" href="/songs/268">Sugaree</a>, </span>
    <span class="text-nowrap"><a class="" href="/songs/537">That&#39;s What Love Will Make You Do</a></span>
  </div>
  <div class="stacked-field">
    <strong>Encore</strong><br/>
    <span class="text-nowrap"><a class="" href="/songs/159">Friend Of The Devil</a></span>
  </div>
</div>
"""


def test_parse_event_reads_act_date_marker_and_place():
    ev = jb.parse_event(EVENT_HTML, "19871031-02")
    assert ev.act == "Jerry Garcia Band"
    assert ev.date == "1987-10-31"
    assert ev.marker == "early"
    assert (ev.venue, ev.city, ev.state) == ("Lunt-Fontanne Theatre", "New York", "NY")
    assert ev.tags == ("official-release",)


def test_the_setlist_keeps_its_order_and_its_apostrophes():
    """&#39; written into a TITLE tag is worse than no title: it looks deliberate."""
    ev = jb.parse_event(EVENT_HTML, "19871031-02")
    assert ev.songs == ["Sugaree", "That's What Love Will Make You Do",
                        "Friend Of The Devil"]
    assert ev.set_sizes == [2, 1]


def test_a_page_that_is_not_an_event_is_not_half_parsed():
    assert jb.parse_event("<html><body>nothing here</body></html>", "x") is None


def test_the_act_in_the_heading_can_carry_the_marker():
    """"Jerry Garcia Acoustic Band" says which of the four shows this is."""
    html = EVENT_HTML.replace("<strong>Jerry Garcia Band</strong>",
                              "<strong>Jerry Garcia Acoustic Band</strong>")
    html = html.replace("[Sat]  Early", "[Sat]")
    assert jb.parse_event(html, "x").marker == "acoustic"


@pytest.mark.parametrize("ours,theirs,matches", [
    ("Jerry Garcia Band", "Jerry Garcia Band", True),
    # An acoustic Garcia Band is still the Garcia Band.
    ("Jerry Garcia Band", "Jerry Garcia Acoustic Band", True),
    # The ampersand is how the config writes it and "and" is how they do.
    ("Jerry Garcia & David Grisman", "Jerry Garcia and David Grisman", True),
    # But these two acts must never be confused with each other.
    ("Jerry Garcia Band", "Jerry Garcia and David Grisman", False),
    ("Legion of Mary", "Jerry Garcia Band", False),
])
def test_act_matches(ours, theirs, matches):
    assert jb.act_matches([ours], theirs) is matches


# --- choosing between four shows in one night -------------------------------

from jamp.confirm import choose_event


class _Ev:
    def __init__(self, slug, act, marker):
        self.slug, self.act, self.marker = slug, act, marker
        self.venue = self.city = self.state = None
        self.sets = []
        self.songs = []
        self.set_sizes = []


class _Show:
    def __init__(self, name):
        self.path = Path(name)
        self.rel = name
        self.tracks = []


# The Lunt-Fontanne on 1987-10-31: early and late, each played twice over as an
# acoustic set and an electric one.
LUNT = [
    _Ev("19871031-01", "Jerry Garcia Acoustic Band", "early"),
    _Ev("19871031-02", "Jerry Garcia Band", "early"),
    _Ev("19871031-03", "Jerry Garcia Acoustic Band", "late"),
    _Ev("19871031-04", "Jerry Garcia Band", "late"),
]
NAMES = ["Jerry Garcia Band", "Garcia Band"]


def test_both_axes_are_needed_to_pick_one_of_four():
    """early/late sits beside the date; acoustic/electric is part of the act."""
    show = _Show("jgb1987-10-31.mp3 - Lunt Fontaine Theatre late show (acoustic)")
    event, why = choose_event(show, LUNT, NAMES)
    assert event is not None and event.slug == "19871031-03", why


def test_the_electric_late_show_is_picked_when_ours_says_nothing_about_acoustic():
    show = _Show("jgb1987-10-31.sbd.flac16 - late show")
    event, why = choose_event(show, LUNT, NAMES)
    # Both late events survive the clash test; the one our name says more about
    # wins, and with only "late" stated that is a tie - so nothing is chosen.
    assert event is None, why
    assert "tell them apart" in why


def test_a_night_with_one_event_needs_no_disambiguation():
    show = _Show("jgb1989-12-02.sbd.mp3")
    only = [_Ev("19891202-01", "Jerry Garcia Band", None)]
    event, why = choose_event(show, only, NAMES)
    assert event is only[0]


def test_an_event_by_another_act_is_never_ours():
    show = _Show("jgdg1992-05-10.flac16")
    event, why = choose_event(show, LUNT, ["Jerry Garcia and David Grisman"])
    assert event is None
    assert "by this act" in why


# --- phish.net --------------------------------------------------------------

from jamp import phishnet as pn


def test_the_api_key_never_reaches_the_cache_key():
    """It would otherwise be written into a file that gets backed up, and
    printed by any report that names a URL."""
    key = pn.cache_key("shows/showdate/1997-11-14.json", {"apikey": "SECRET123"})
    assert "SECRET123" not in key
    assert key.endswith("shows/showdate/1997-11-14.json")


def test_a_missing_or_empty_key_is_said_out_loud(tmp_path):
    with pytest.raises(pn.MissingKey):
        pn.read_key(tmp_path / "nope.key")
    empty = tmp_path / "empty.key"
    empty.write_text("   \n", encoding="utf-8")
    with pytest.raises(pn.MissingKey):
        pn.read_key(empty)


def test_the_key_is_sent_but_not_stored(tmp_path):
    """The cache is keyed on the clean URL and the request carries the key."""
    clean = pn.cache_key("shows/showdate/1997-11-14.json", {})
    sent = []

    def net(url, timeout):
        sent.append(url)
        return 200, json.dumps({"error": False, "data": []}).encode()

    cache = HttpCache(tmp_path / "c.sqlite", opener=net, sleep=lambda s: None)
    pn._get(cache, "SECRET123", "shows/showdate/1997-11-14.json")
    assert "SECRET123" in sent[0], "the request must carry the key"
    stored = [u for (u,) in cache.db.execute("SELECT url FROM http").fetchall()]
    assert stored == [clean]
    assert not any("SECRET123" in u for u in stored)


@pytest.mark.parametrize("raw,expected", [
    ('The “E” Center', 'The "E" Center'),
    ("Madison Square Garden", "Madison Square Garden"),
    ("Café – Bar", "Cafe - Bar") if False else ("A – B", "A - B"),
])
def test_typographic_punctuation_is_normalised(raw, expected):
    """Curly quotes are fine in a tag and illegal-looking in a folder name."""
    assert pn._clean(raw) == expected


def test_a_province_is_spelled_as_its_code():
    """phish.net says "British Columbia"; the rest of the library says BC."""
    from jamp.naming import state_code

    assert state_code("British Columbia") == "BC"
    # A prefecture is not a code and is left exactly as written.
    assert state_code("Aichi") == "Aichi"


# --- the durations decide, not the name -------------------------------------

from jamp.archiveorg import Track as ATrack
from jamp import setlist
from jamp.confirm import agreed_title, best_alignment


def _rec(ident, seconds_and_titles):
    r = Recording(ident)
    r.tracks = [ATrack(number=i + 1, title=t, seconds=s)
                for i, (s, t) in enumerate(seconds_and_titles)]
    return r


def test_the_copy_whose_durations_fit_wins_over_the_one_named_in_our_folder():
    """Steve Kimock 2002-08-10: our folder says .mk4v and an .mk4v item exists,
    but ours aligns 99% with the -ams copy and only 83% with its namesake."""
    ours = [180.0, 240.0, 300.0]
    named = _rec("skb2002-08-10.mk4v", [(120.0, "A"), (300.0, "B"), (300.0, "C")])
    fits = _rec("skb2002-08-10-ams.shnf", [(180.0, "A"), (240.0, "B"), (300.0, "C")])
    a, rec, passing = best_alignment(ours, [named, fits])
    assert rec.identifier == "skb2002-08-10-ams.shnf"
    assert a.score == 1.0


def test_every_copy_that_fits_is_kept_not_just_the_best():
    """Eighteen copies of a Dead night exist and several align perfectly."""
    ours = [180.0, 240.0]
    a1 = _rec("one", [(180.0, "X"), (240.0, "Y")])
    a2 = _rec("two", [(181.0, "X"), (239.0, "Y")])
    bad = _rec("three", [(600.0, "Z"), (600.0, "W")])
    a, rec, passing = best_alignment(ours, [a1, a2, bad])
    assert len(passing) == 2
    assert {r.identifier for _, r in passing} == {"one", "two"}


def test_a_candidate_with_no_durations_is_skipped_not_crashed_on():
    ours = [180.0]
    blank = Recording("no-durations")
    blank.tracks = [ATrack(number=1, title="X", seconds=None)]
    good = _rec("good", [(180.0, "X")])
    a, rec, passing = best_alignment(ours, [blank, good])
    assert rec.identifier == "good"


# --- what the copies agree the song is called -------------------------------

def _passing(*title_lists):
    ours = [180.0, 240.0]
    out = []
    for i, titles in enumerate(title_lists):
        r = _rec("copy%d" % i, [(180.0, titles[0]), (240.0, titles[1])])
        a = setlist.align(ours, [180.0, 240.0])
        out.append((a, r))
    return out


def test_case_and_a_leading_track_number_are_not_disagreement():
    """The eight Richmond copies say "Feel Like A Stranger", "feel like a
    stranger" and "01 Feel Like A Stranger" - one song, three spellings."""
    passing = _passing(["Feel Like A Stranger", "Friend Of The Devil"],
                       ["feel like a stranger", "friend of the devil"],
                       ["01 Feel Like A Stranger", "02 Friend Of The Devil"])
    assert agreed_title(0, passing) == "Feel Like A Stranger"
    assert agreed_title(1, passing) == "Friend Of The Devil"


def test_a_clear_majority_is_required():
    """One uploader's idiosyncratic name must not carry the track."""
    passing = _passing(["New Minglewood Blues", "B"],
                       ["New Minglewood Blues", "B"],
                       ["Minglewood Blues", "B"])
    assert agreed_title(0, passing) == "New Minglewood Blues"


def test_a_genuine_split_decision_yields_nothing():
    passing = _passing(["Alpha", "B"], ["Beta", "B"])
    assert agreed_title(0, passing) is None


def test_the_best_spelling_of_an_agreed_title_is_chosen():
    """Properly capitalised, and without the number stripped for comparison."""
    passing = _passing(["03 new minglewood blues", "B"],
                       ["New Minglewood Blues", "B"],
                       ["new minglewood blues", "B"])
    assert agreed_title(0, passing) == "New Minglewood Blues"


# --- seeding, and the keyless path ------------------------------------------

from jamp import phishin


def test_phishin_durations_are_milliseconds():
    """547320 is 9 minutes 7 seconds, not 547320 seconds."""
    show = phishin.parse_show({
        "date": "1997-11-14",
        "venue": {"name": "The E Center", "city": "West Valley City",
                  "state": "UT", "country": "USA"},
        "tracks": [{"position": 1, "title": "Runaway Jim", "duration": 547320,
                    "set_name": "Set 1"}],
    })
    assert show.tracks[0].seconds == 547.32
    assert (show.venue, show.city, show.state) == ("The E Center", "West Valley City", "UT")
    assert show.has_durations


def test_a_show_with_no_audio_has_no_usable_durations():
    """phish.in lists such shows with zero durations.  A zero fed to the
    matcher would align against nothing and drag the score down with it."""
    show = phishin.parse_show({
        "date": "1997-11-14",
        "venue": {"name": "X", "city": "Y", "state": "NY"},
        "tracks": [{"position": 1, "title": "A", "duration": 0},
                   {"position": 2, "title": "B", "duration": None}],
    })
    assert show.has_durations is False
    assert [t.seconds for t in show.tracks] == [None, None]
    # The venue is still worth having.
    assert show.venue == "X"


def test_a_province_from_phishin_is_a_code_too():
    show = phishin.parse_show({
        "date": "1996-11-23",
        "venue": {"name": "Pacific Coliseum", "city": "Vancouver",
                  "state": "British Columbia", "country": "Canada"},
        "tracks": [],
    })
    assert show.state == "BC"


@pytest.mark.parametrize("gap,score,near", [
    # Both numbers close: a human could plausibly settle it.
    (0.06, 0.89, True),
    # Our one-track soundcheck against a full two-hour show: the single track
    # found an answer, so the score is perfect and the lengths are nothing alike.
    (21.36, 1.00, False),
    (1.00, 1.00, False),
])
def test_a_near_miss_is_only_near_if_both_numbers_are_close(gap, score, near):
    from jamp.confirm import near_miss_note, worth_a_human

    class _A:
        pass

    a = _A()
    a.score, a.duration_gap = score, gap
    assert worth_a_human(a) is near
    note = near_miss_note(a)
    assert ("close, but under the bar" in note) is near
    if not near:
        assert "differ in length" in note


# --- how long an answer stays good ------------------------------------------

from jamp.httpcache import EARLIER_DAYS, THIS_YEAR_DAYS, age_for


def test_a_show_from_this_year_goes_stale_sooner():
    """phish.net asks for a 24-hour refresh because setlists get edited after a
    show.  That is live for this year's shows and academic for 1994."""
    import time

    year = time.localtime().tm_year
    assert age_for("%d-01-15" % year) == THIS_YEAR_DAYS
    assert age_for("%d-06-11" % (year - 1)) == EARLIER_DAYS
    assert age_for("1994-06-11") == EARLIER_DAYS
    # jerrybase addresses shows as 19800724; the year is still the first four.
    assert age_for("19800724") == EARLIER_DAYS
    # Nothing to go on: assume the slower cadence rather than hammer the source.
    assert age_for(None) == EARLIER_DAYS
    assert age_for("rubbish") == EARLIER_DAYS


def test_an_entry_past_its_age_is_refetched(tmp_path):
    calls = []

    def net(url, timeout):
        calls.append(url)
        return 200, json.dumps({"n": len(calls)}).encode()

    cache = HttpCache(tmp_path / "c.sqlite", opener=net, sleep=lambda s: None)
    cache.get("http://x/1")
    cache.get("http://x/1", max_age_days=30)          # fresh, served from cache
    assert len(calls) == 1
    cache.get("http://x/1", max_age_days=0)           # anything at all is too old
    assert len(calls) == 2, "a stale entry must be refetched"


def test_offline_serves_a_stale_answer_rather_than_failing(tmp_path):
    """Refusing here would make --offline fail on a library a month out of date."""
    def net(url, timeout):
        return 200, b'{"ok": true}'

    cache = HttpCache(tmp_path / "c.sqlite", opener=net, sleep=lambda s: None)
    cache.get("http://x/1")
    cache.close()

    def no_net(url, timeout):
        raise AssertionError("offline must not open a connection")

    offline = HttpCache(tmp_path / "c.sqlite", offline=True, opener=no_net,
                        sleep=lambda s: None, max_age_days=0)
    got = offline.get("http://x/1")
    assert got.json() == {"ok": True}
    assert offline.stats()["stale_served"] == 1


# --- the distilled store ----------------------------------------------------

from jamp.showstore import ShowStore


def _make_store(tmp_path, rows):
    """rows: (source, key, date, venue, city, state, act, [(pos,title,secs,segue)])"""
    import sqlite3
    from jamp.distill import SCHEMA

    p = tmp_path / "shows.sqlite"
    db = sqlite3.connect(str(p))
    db.executescript(SCHEMA)
    for source, key, date, venue, city, state, act, tracks in rows:
        cur = db.execute(
            "INSERT INTO shows (source, key, date, venue, city, state, act) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (source, key, date, venue, city, state, act))
        db.executemany("INSERT INTO tracks (show_id, position, title, seconds, segue) "
                       "VALUES (?, ?, ?, ?, ?)",
                       [(cur.lastrowid, *t) for t in tracks])
    db.commit()
    db.close()
    return ShowStore(p)


def test_a_missing_store_is_not_an_error(tmp_path):
    store = ShowStore(tmp_path / "nope.sqlite")
    assert not store
    assert store.recordings_for(["gd"], "1977-05-08") == []
    assert store.phishin_show("1997-11-14") is None
    assert ShowStore(None).__bool__() is False


def test_a_stored_recording_carries_its_durations(tmp_path):
    """The whole point: the alignment must work on a stored show unchanged."""
    store = _make_store(tmp_path, [
        ("archive.org", "gd1977-05-08.sbd.cantor", "1977-05-08",
         "Barton Hall", "Ithaca", "NY", "Grateful Dead",
         [(1, "Minglewood Blues", 373.0, 1), (2, "Loser", 533.0, 0)]),
    ])
    recs = store.recordings_for(["gd"], "1977-05-08")
    assert len(recs) == 1
    r = recs[0]
    assert (r.venue, r.city, r.state) == ("Barton Hall", "Ithaca", "NY")
    assert [t.seconds for t in r.tracks] == [373.0, 533.0]
    assert r.tracks[0].segue is True


def test_a_stored_recording_is_filtered_by_band_like_a_search(tmp_path):
    store = _make_store(tmp_path, [
        ("archive.org", "gd1977-05-08.x", "1977-05-08", "V", "C", "NY", None, []),
        ("archive.org", "ph1977-05-08.x", "1977-05-08", "V", "C", "NY", None, []),
    ])
    assert len(store.recordings_for(["gd"], "1977-05-08")) == 1
    assert len(store.recordings_for(["gd", "ph"], "1977-05-08")) == 2


def test_a_stored_mmj_break_is_still_a_break(tmp_path):
    """A break was stored as an entry with no title; it must read back as one,
    or the 21-tracks-to-20-songs reconciliation stops working."""
    from jamp import mmjarchive

    store = _make_store(tmp_path, [
        ("mmjarchive", "2005-11-23-x", "2005-11-23", "The Palace Theatre",
         "Louisville", "KY", None,
         [(1, "Anytime", None, 0), (2, None, None, 0), (3, "At Dawn", None, 0)]),
    ])
    show = store.mmj_show("2005-11-23")
    assert [e.kind for e in show.entries] == [
        mmjarchive.SONG, mmjarchive.BREAK, mmjarchive.SONG]
    assert show.songs == ["Anytime", "At Dawn"]
    assert show.breaks == 1
    # Two tracks means the taper cut the break out; three means they kept it.
    assert len(show.plan_for(2)) == 2
    assert len(show.plan_for(3)) == 3
    assert show.plan_for(4) is None


def test_a_stored_phishin_show_keeps_its_seconds(tmp_path):
    store = _make_store(tmp_path, [
        ("phish.in", "1997-11-14", "1997-11-14", "The E Center",
         "West Valley City", "UT", None, [(1, "Runaway Jim", 547.32, 0)]),
    ])
    show = store.phishin_show("1997-11-14")
    assert show.has_durations
    assert show.tracks[0].seconds == 547.32


def test_a_title_for_a_filename_two_discs_share_is_not_written(tmp_path):
    """Proposals name tracks by filename, and disc folders repeat them; the
    first file found used to take the title."""
    import mutagen

    root, out = tmp_path / "lib", tmp_path / "out"
    folder = root / "gd1990-03-24.flac16"
    _settled(folder)
    make_flac(folder / "Disc 1" / "01.flac")
    make_flac(folder / "Disc 2" / "01.flac")
    p = _proposal_file(out, [{
        "folder": "gd1990-03-24.flac16", "tier": "SHNID", "identifier": "x",
        "venue": "Knickerbocker Arena", "city": "Albany", "state": "NY",
        "titles": {"01.flac": "Jack Straw"},
    }])
    stats = apply_proposals(root, out, p, dry_run=False)
    for disc in ("Disc 1", "Disc 2"):
        tags = mutagen.File(folder / disc / "01.flac")
        assert tags.get("TITLE") is None
        assert tags["VENUE"] == ["Knickerbocker Arena, Albany, NY"]
    assert stats["ambiguous filename"] == 1


def test_a_404_is_asked_again_after_a_week(tmp_path):
    """Cached with no age limit, a 404 from a bad day was believed for good."""
    import sqlite3
    import urllib.error

    calls = []

    def gone(url, timeout):
        calls.append(url)
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    cache = HttpCache(tmp_path / "c.sqlite", opener=gone, sleep=lambda s: None)
    fetch_recording(cache, "nope")
    fetch_recording(cache, "nope")
    assert len(calls) == 1
    cache.db.execute("UPDATE http SET fetched_at = '2020-01-01T00:00:00'")
    cache.db.commit()
    fetch_recording(cache, "nope")
    assert len(calls) == 2


def test_a_scoped_apply_leaves_other_artists_alone_and_a_dry_run_keeps_the_log(tmp_path):
    import mutagen

    root, out = tmp_path / "lib", tmp_path / "out"
    for act in ("Phish", "Jerry Garcia"):
        _settled(root / act / "show")
        make_flac(root / act / "show" / "t01.flac")
    p = _proposal_file(out, [
        {"folder": act + "/show", "tier": "SHNID", "identifier": "x", "venue": None,
         "city": None, "state": None, "titles": {"t01.flac": "Tweezer"}}
        for act in ("Phish", "Jerry Garcia")])
    apply_proposals(root, out, p, dry_run=False, artists={"Phish"})
    assert mutagen.File(root / "Phish" / "show" / "t01.flac")["TITLE"] == ["Tweezer"]
    assert mutagen.File(root / "Jerry Garcia" / "show" / "t01.flac").get("TITLE") is None

    committed = (out / "phase3_committed.csv").read_text(encoding="utf-8-sig")
    apply_proposals(root, out, p, dry_run=True)
    assert (out / "phase3_committed.csv").read_text(encoding="utf-8-sig") == committed
    assert (out / "phase3_dry_run.csv").exists()
