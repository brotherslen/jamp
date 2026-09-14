"""The dry-run engine: proposals, safety rules and idempotency."""
import datetime as dt

import pytest

import fixtures
from jamp import phase0, phase1
from jamp.analyze import analyze_show
from jamp.phase1 import (
    COLLISION,
    DUPLICATE,
    MERGE,
    PLAN,
    SKIP_BLOCKED,
    SPLIT_SHOW,
    TWO_SHOWS,
    UNCHANGED,
    build_plans,
    plan_show,
)
from jamp.report import ensure_out_dir
from jamp.scan import scan
from jamp.sidecars import plan_sidecar, propose_sidecar_name

TODAY = dt.date(2026, 9, 7)


@pytest.fixture(scope="module")
def plans(library, cfg):
    return {p.old_folder_name: p for p in build_plans(library, cfg, today=TODAY)}


@pytest.mark.parametrize("folder,proposed", [
    ("mmj2003-09-26.shnf",
     "mmj2003-09-26.sbd.miller.shn - Murat Egyptian Room, Indianapolis, IN"),
    # No taper is known, so the mic keeps this copy distinguishable from
    # another taper's recording of the same night.  No info file, so no venue.
    ("mmj2005-06-04.ak40.flac16", "mmj2005-06-04.aud.ak40.flac16"),
    ("MMJ2006-06-16..4011s bonaroo",
     "mmj2006-06-16.aud.miller.flac16 - Bonnaroo Music Festival, Manchester, TN"),
    # WXPN is the station that broadcast it, which identifies this copy;
    # "fm" itself never appears in a name.
    ("mmj2006-12-01.Electric_Factory_WXPN_FM_SBD",
     "mmj2006-12-01.sbd.wxpn.flac16 - Electric Factory"),
    # "MMJ-Wiltern" - the band, then a theatre.
    # The gazetteer supplies the city and state the folder never named. Before
    # it existed this came out as bare "Wiltern", which is the gap it closes.
    ("MMJ2012-09-12.MMJ-Wiltern-9-12-12",
     "mmj2012-09-12.flac24 - Wiltern, Los Angeles, CA"),
    ("My Morning Jacket 2023-11-03 Fox Theatre, Atlanta, GA [FLAC24]",
     "mmj2023-11-03.sbd.nugs.flac24 - Fox Theatre, Atlanta, GA"),
    ("Phish 12-29-18 MTX",
         "ph2018-12-29.mtx.padelimike.flac24 - Madison Square Garden, New York, NY"),
    ("Grateful Dead 10-31-91", "gd1991-10-31.flac16"),
    ("Huey Lewis and the rUMors Summer Camp 5-29-11", "hlr2011-05-29.flac16"),
    ("2011_11_05 Eagles Ballroom - Milwaukee, WI",
     "um2011-11-05.sbd.umlive.mp3 - Eagles Ballroom, Milwaukee, WI"),
])
def test_proposed_folder_names(plans, folder, proposed):
    assert plans[folder].new_folder_name == proposed


def test_umlive_download_is_recognised_from_its_tags(plans, analyses=None):
    """The folder name says nothing, but every track carries COMMENT "UMLive"."""
    plan = plans["2011_11_05 Eagles Ballroom - Milwaukee, WI"]
    a = plan.analysis
    assert a.classification.kind == "OFFICIAL"
    assert a.provenance == "umlive"
    assert a.source.value == "sbd"
    assert a.source.inferred


def test_umlive_date_comes_from_the_name_not_the_year_tag(plans):
    """DATE is the bare year 2011; ALBUM and the folder name hold 2011-11-05."""
    a = plans["2011_11_05 Eagles Ballroom - Milwaukee, WI"].analysis
    assert a.classification.tag_profile.date_values == ("2011",)
    assert not a.classification.tag_date_admissible
    assert a.date.iso == "2011-11-05"


def test_official_genre_is_left_alone(plans):
    """UMLive says "Jam". Overwriting that with "Live" loses real information."""
    plan = plans["2011_11_05 Eagles Ballroom - Milwaukee, WI"]
    assert "GENRE" not in plan.tracks[0].tags
    # A torrent folder still gets the configured genre.
    assert plans["MMJ2006-06-16..4011s bonaroo"].tracks[0].tags["GENRE"][1] == "Live"


def test_official_album_keeps_the_venue_verbatim(plans):
    plan = plans["2011_11_05 Eagles Ballroom - Milwaukee, WI"]
    assert plan.tracks[0].tags["ALBUM"][1] == \
        "2011-11-05: Eagles Ballroom - Milwaukee, WI"


def test_umlive_titles_are_preserved_segues_and_all(plans):
    plan = plans["2011_11_05 Eagles Ballroom - Milwaukee, WI"]
    assert [t.title for t in plan.tracks][:3] == [
        "Catshot >", "All In Time", "Preamble > Mantis >"]


def test_the_venue_reaches_the_folder_but_never_the_filenames(plans):
    plan = plans["MMJ2006-06-16..4011s bonaroo"]
    assert plan.new_folder_name.endswith(" - Bonnaroo Music Festival, Manchester, TN")
    assert all(t.new_name.startswith("mmj2006-06-16d1t") for t in plan.tracks)
    assert not any("Bonnaroo" in t.new_name for t in plan.tracks)


def test_the_machine_part_of_the_name_is_unchanged_by_the_venue(plans):
    from jamp.naming import parse_canonical, split_location
    machine, location = split_location(plans["mmj2003-09-26.shnf"].new_folder_name)
    assert machine == "mmj2003-09-26.sbd.miller.shn"
    assert location == "Murat Egyptian Room, Indianapolis, IN"
    parsed = parse_canonical(plans["mmj2003-09-26.shnf"].new_folder_name)
    assert (parsed.band, parsed.source, parsed.provenance, parsed.fmt) == (
        "mmj", "sbd", "miller", "shn")
    assert parsed.location == "Murat Egyptian Room, Indianapolis, IN"


def test_an_info_file_city_does_not_shut_out_a_folder_name_venue(tmp_path, cfg):
    """The info file names a city but no venue; the venue is in the folder name."""
    root = tmp_path / "lib"
    folder = root / "My Morning Jacket" / "mmj-2012-12-27 Capitol Theatre, Port Chester, NY"
    for i in (1, 2):
        fixtures.make_flac(folder / ("d1t%02d.flac" % i), bits=16)
    fixtures.write(folder / "info.txt",
                   "My Morning Jacket\n2012-12-27\n\nCity: Port Chester\n"
                   "Source: SBD\nLineage: SBD > DAT\n")
    plan = build_plans(root, cfg, today=TODAY)[0]
    assert "Capitol Theatre" in (plan.new_folder_name or "")


def test_a_station_call_sign_becomes_the_provenance(cfg):
    """"this came off WXPN" identifies the copy; "fm" says only that it is a
    board recording, and never appears in a name."""
    from jamp.sources import broadcast_provenance, infer_source

    name = "mmj2006-12-01.Electric_Factory_WXPN_FM_SBD"
    assert broadcast_provenance(cfg, [("folder", name)])[0] == "wxpn"
    assert infer_source(cfg, name).value == "sbd"
    # A satellite broadcast is board-sourced too, and equally unnamed.
    assert infer_source(cfg, "ph2019-12-31.sxm.flac16").value == "sbd"
    assert broadcast_provenance(cfg, [("folder", "ph2019-12-31.sxm.flac16")])[0] is None


def test_an_unexplained_word_is_the_venue_not_a_taper(tmp_path, cfg):
    """"MMJ-Wiltern" is a theatre.  Only a name already in the tapers table is
    read as a person."""
    import fixtures
    from jamp.analyze import analyze_show
    from jamp.scan import scan
    from jamp.sources import provenance_from_name

    assert provenance_from_name(cfg, "MMJ2012-09-12.MMJ-Wiltern-9-12-12")[0] is None
    assert provenance_from_name(cfg, "mmj2021-11-04.leary")[0] == "leary"

    root = tmp_path / "lib"
    folder = root / "My Morning Jacket" / "mmj2012-09-12.Wiltern"
    for i in (1, 2):
        fixtures.make_flac(folder / ("mmj2012-09-12d1t%02d.flac" % i), bits=16)
    a = analyze_show(scan(root, cfg).shows[0], cfg, today=TODAY)
    assert a.venue == "Wiltern"
    assert a.provenance is None


def test_the_venue_is_tagged_even_when_the_name_cannot_carry_it(tmp_path, cfg):
    """The folder name is the nicety; the VENUE tag is the record."""
    import fixtures
    root = tmp_path / "lib"
    folder = root / "My Morning Jacket" / "mmj2012-09-12.Wiltern"
    for i in (1, 2):
        fixtures.make_flac(folder / ("mmj2012-09-12d1t%02d.flac" % i), bits=16)
    plan = build_plans(root, cfg, today=TODAY)[0]
    # The folder names the room and not its city; the gazetteer knows the rest.
    assert plan.tracks[0].tags["VENUE"][1] == "Wiltern, Los Angeles, CA"


def test_venue_only_policy_drops_a_city_with_no_venue(cfg):
    from jamp.phase1 import folder_location
    assert folder_location(cfg, None, "Milwaukee", "WI", None) is None
    assert folder_location(cfg, "The Rave", "Milwaukee", "WI", None) == \
        "The Rave, Milwaukee, WI"


def test_two_tracks_claiming_one_filename_block_the_folder(tmp_path, cfg):
    """Real data: a Phish folder with two files both tagged disc 3 track 1.
    Renaming would destroy one of them, so nothing is renamed."""
    root = tmp_path / "lib"
    folder = root / "phish" / "ph2018-12-31 Madison Square Garden, New York, NY"
    for title in ("Harry_Hood", "Mercury"):
        fixtures.make_flac(
            folder / ("ph181231d3_01_%s.flac" % title), bits=16,
            tags={"ARTIST": "Phish", "ALBUM": "2018-12-31 New York, NY",
                  "TITLE": title.replace("_", " "), "TRACKNUMBER": "1",
                  "DISCNUMBER": "3", "DATE": "2018-12-31"},
        )
    plan = build_plans(root, cfg, today=TODAY)[0]
    assert plan.status == SKIP_BLOCKED
    reasons = " ".join(plan.reasons)
    assert "would become" in reasons and "overwrite" in reasons
    assert "Harry_Hood" in reasons and "Mercury" in reasons


def test_a_duplicate_is_still_a_duplicate_when_only_one_knows_the_venue(tmp_path, cfg):
    """De-duplication compares the machine part, so a venue cannot hide a copy."""
    root = tmp_path / "lib"
    for name in ("ph2018-12-28 one", "ph2018-12-28 two"):
        for i in (1, 2):
            fixtures.make_flac(root / "phish" / name / ("d1t%02d.flac" % i), bits=16)
    fixtures.write(root / "phish" / "ph2018-12-28 one" / "info.txt",
                   "Phish\nMadison Square Garden\nNew York, NY\n2018-12-28\n\n"
                   "Venue: Madison Square Garden\n")
    plans = build_plans(root, cfg, today=TODAY)
    assert {p.status for p in plans} == {DUPLICATE}


def test_a_show_inside_an_official_release_reads_as_a_date_and_a_place(tmp_path, cfg):
    """Spring 1990: the release folder is kept, and the shows inside it are
    named by date and venue rather than by the machine string."""
    root = tmp_path / "lib"
    release = root / "grateful dead" / "Spring 1990 (The Other One) (2014)"
    for name, day in (("1990-03-14 Capital Centre, Landover, MD", 14),
                      ("1990-03-18 Civic Center, Hartford, CT", 18)):
        for i in (1, 2):
            fixtures.make_flac(
                release / name / ("1%02d %s.flac" % (i, "Song")), bits=16,
                tags={"ARTIST": "Grateful Dead", "ALBUM": "Spring 1990 (The Other One)",
                      "TITLE": "Song %d" % i, "TRACKNUMBER": str(i),
                      "DISCNUMBER": "1", "DATE": "2014"},
            )
    plans = {p.old_folder_name: p for p in build_plans(root, cfg, today=TODAY)}
    first = plans["1990-03-14 Capital Centre, Landover, MD"]
    # Date, space, place - the form the box set already uses, so this folder is
    # recognised as correct and left alone rather than churned.
    assert first.new_folder_name == "1990-03-14 Capital Centre, Landover, MD"
    assert first.release_folder == "Spring 1990 (The Other One) (2014)"
    assert not first.folder_changed
    # The release year 2014 is still refused as the show date.
    assert first.analysis.date.iso == "1990-03-14"
    # Track filenames keep the ordinary scheme.
    assert first.tracks[0].new_name.startswith("gd1990-03-14d1t")


def test_a_show_in_a_release_folder_is_not_blocked_as_multi_date(tmp_path, cfg):
    root = tmp_path / "lib"
    release = root / "grateful dead" / "Spring 1990 (The Other One) (2014)"
    for name in ("1990-03-14 Capital Centre, Landover, MD",
                 "1990-03-18 Civic Center, Hartford, CT"):
        for i in (1, 2):
            fixtures.make_flac(release / name / ("d1t%02d.flac" % i), bits=16)
    for p in build_plans(root, cfg, today=TODAY):
        assert "MULTI_DATE_RELEASE" not in p.analysis.issue_codes
        assert "IN_RELEASE_FOLDER" in p.analysis.issue_codes


def test_an_artist_folder_is_not_a_release_folder(plans):
    """Every artist folder holds many shows; that means nothing by itself."""
    assert plans["mmj2005-06-04.ak40.flac16"].release_folder is None
    assert plans["mmj2005-06-04.ak40.flac16"].new_folder_name.startswith("mmj2005-06-04")


def test_unknown_fields_are_left_out_never_filled_in(plans):
    """No source and no provenance evidence: band and date only.

    The place is a separate question from the source and provenance slots this
    test is about - the gazetteer fills in the city the folder never named,
    which is filling in a *place*, not inventing a source.
    """
    assert plans["MMJ2012-09-12.MMJ-Wiltern-9-12-12"].new_folder_name == \
        "mmj2012-09-12.flac24 - Wiltern, Los Angeles, CA"
    assert "unknown" not in plans["Grateful Dead 10-31-91"].new_folder_name


def test_track_names_follow_the_scheme(plans):
    plan = plans["MMJ2012-09-12.MMJ-Wiltern-9-12-12"]
    assert [t.new_name for t in plan.tracks] == [
        "mmj2012-09-12d1t01.flac", "mmj2012-09-12d1t02.flac"]


def test_set_numbering_is_used_only_when_the_set_is_known(plans):
    """gd1973-12-10 s1 has s1t01 filenames; the rest fall back to disc numbering."""
    split = plans["gd1973-12-10 s1"]
    assert all(t.kind == "s" for t in split.tracks)
    assert all(t.kind == "d" for t in plans["mmj2005-06-04.ak40.flac16"].tracks)


def test_split_show_across_sibling_folders_is_merged(plans):
    """gd1973-12-10 s1 and s2 are one show and become one folder."""
    one, two = plans["gd1973-12-10 s1"], plans["gd1973-12-10 s2"]
    assert one.status == two.status == MERGE
    assert one.merge_target == two.merge_target
    # "Charlotte" is already inside "Charlotte Coliseum", so it is not repeated.
    assert one.new_folder_name == two.new_folder_name == \
        "gd1973-12-10.sbd.miller.flac16 - Charlotte Coliseum, NC"
    assert one.merge_role == "primary" and two.merge_role == "member"


def test_merged_tracks_are_numbered_by_set(plans):
    one, two = plans["gd1973-12-10 s1"], plans["gd1973-12-10 s2"]
    assert [t.new_name for t in one.tracks] == [
        "gd1973-12-10s1t01.flac", "gd1973-12-10s1t02.flac"]
    assert [t.new_name for t in two.tracks] == [
        "gd1973-12-10s2t01.flac", "gd1973-12-10s2t02.flac"]
    # No two files in the merged folder can share a name.
    names = [t.new_name for t in one.tracks + two.tracks]
    assert len(names) == len(set(names))


def test_merging_is_flagged_as_the_only_rule_that_moves_files(plans):
    assert any("moves files between folders" in w
               for w in plans["gd1973-12-10 s1"].warnings)


def test_an_audience_show_with_no_mic_and_no_taper_just_says_aud(tmp_path, cfg):
    """Not knowing the mic is fine; the field is left out, never filled in."""
    root = tmp_path / "lib"
    folder = root / "grateful dead" / "gd1977-05-08.aud"
    for i in (1, 2):
        fixtures.make_flac(folder / ("gd1977-05-08d1t%02d.flac" % i), bits=16)
    plan = build_plans(root, cfg, today=TODAY)[0]
    assert plan.analysis.source.value == "aud"
    assert plan.analysis.provenance is None
    assert plan.new_folder_name == "gd1977-05-08.aud.flac16"


def test_the_mic_distinguishes_two_tapers_on_one_night(tmp_path, cfg):
    """Without the mic these two would collide and neither could be renamed."""
    root = tmp_path / "lib"
    for name in ("gd1977-05-08.ak40", "gd1977-05-08.akg414"):
        for i in (1, 2):
            fixtures.make_flac(root / "grateful dead" / name / ("d1t%02d.flac" % i), bits=16)
    plans = {p.old_folder_name: p for p in build_plans(root, cfg, today=TODAY)}
    assert all(p.status == PLAN for p in plans.values())
    assert plans["gd1977-05-08.ak40"].new_folder_name == "gd1977-05-08.aud.ak40.flac16"
    assert plans["gd1977-05-08.akg414"].new_folder_name == "gd1977-05-08.aud.akg414.flac16"


def test_early_and_late_shows_stay_two_folders(tmp_path, cfg):
    """A Jerry Garcia Band speciality: two performances on one date.

    The marker goes straight after the date, in the folder and in the tracks.
    """
    root = tmp_path / "lib"
    for name in ("jgb1976-03-06 early", "jgb1976-03-06 late"):
        for i in (1, 2):
            fixtures.make_flac(root / "grateful dead" / name / ("d1t%02d.flac" % i), bits=16)
    plans = {p.old_folder_name: p for p in build_plans(root, cfg, today=TODAY)}
    assert {p.status for p in plans.values()} == {PLAN}
    assert plans["jgb1976-03-06 early"].new_folder_name == "jgb1976-03-06.early.flac16"
    assert plans["jgb1976-03-06 late"].new_folder_name == "jgb1976-03-06.late.flac16"
    assert plans["jgb1976-03-06 early"].analysis.band.abbrev == "jgb"
    assert [t.new_name for t in plans["jgb1976-03-06 late"].tracks] == [
        "jgb1976-03-06lated1t01.flac", "jgb1976-03-06lated1t02.flac"]


def test_a_bare_ordinal_is_not_a_show_marker(tmp_path, cfg):
    """"second copy" is not the second show of the night."""
    from jamp.phase1 import show_marker
    assert show_marker("ph2018-12-28 second copy") is None
    assert show_marker("ph2018-12-28 second show") == "late"
    assert show_marker("jgb1976-03-06 late") == "late"


def test_early_and_late_are_never_merged(tmp_path, cfg):
    root = tmp_path / "lib"
    for name in ("jgb1976-03-06 early", "jgb1976-03-06 late"):
        for i in (1, 2):
            fixtures.make_flac(root / "grateful dead" / name / ("d1t%02d.flac" % i), bits=16)
    assert all(p.status != MERGE for p in build_plans(root, cfg, today=TODAY))


def test_jerry_garcia_iterations_resolve_to_their_own_bands(cfg):
    from jamp.bands import resolve_band
    for name, expected in (
        ("Jerry Garcia and Merl Saunders 1973-07-10", "jgms"),
        ("Jerry Garcia & David Grisman 1991-02-02", "jgdg"),
        ("jgjk1986-05-30", "jgjk"),
        ("Legion of Mary 1975-06-06", "lom"),
    ):
        assert resolve_band(name, cfg, parent_artist_dir="grateful dead").abbrev == expected


def test_a_split_with_no_set_marker_is_not_merged_silently(tmp_path, cfg):
    """Without a marker we cannot tell two halves from two copies, so we ask."""
    root = tmp_path / "lib"
    for name in ("gd1973-12-10 alpha", "gd1973-12-10 beta"):
        for i in (1, 2):
            fixtures.make_flac(root / "grateful dead" / name / ("d1t%02d.flac" % i), bits=16)
    plans = {p.old_folder_name: p for p in build_plans(root, cfg, today=TODAY)}
    assert {p.status for p in plans.values()} == {DUPLICATE}
    assert all("duplicate of" in " ".join(p.reasons) for p in plans.values())


def test_folders_disagreeing_about_sets_versus_discs_are_not_merged(tmp_path, cfg):
    root = tmp_path / "lib"
    for name in ("gd1973-12-10 s1", "gd1973-12-10 d2"):
        for i in (1, 2):
            fixtures.make_flac(root / "grateful dead" / name / ("d1t%02d.flac" % i), bits=16)
    plans = build_plans(root, cfg, today=TODAY)
    assert {p.status for p in plans} == {SPLIT_SHOW}
    assert all("disagree about whether they are sets or discs" in " ".join(p.reasons)
               for p in plans)


def test_box_set_discs_are_not_merged_across_dates(tmp_path, cfg):
    """Merging needs the same date, so a multi-date box set can never fold."""
    root = tmp_path / "lib"
    for name, day in (("gd1990-03-14 d1", 14), ("gd1990-03-18 d2", 18)):
        for i in (1, 2):
            fixtures.make_flac(root / "grateful dead" / name / ("d1t%02d.flac" % i), bits=16)
    plans = build_plans(root, cfg, today=TODAY)
    assert all(p.status != MERGE for p in plans)


def test_blocked_folders_propose_nothing(plans):
    for name in ("OHMphrey - Posthaste", "UM - Hauntlanta",
                 "Umphreys McGee Bonnaroo 2008"):
        assert plans[name].status == SKIP_BLOCKED
        assert plans[name].new_folder_name is None


def test_official_titles_are_never_overwritten(plans):
    plan = plans["ph2018-12-28 Madison Square Garden, New York, NY [FLAC]"]
    titles = [t.title for t in plan.tracks]
    assert titles == ["Blaze On", "Everything's Right", "Simple"]
    assert all(t.title_source.startswith("existing tag") for t in plan.tracks)


def test_torrent_titles_come_from_the_info_file(plans):
    plan = plans["MMJ2006-06-16..4011s bonaroo"]
    assert [t.title for t in plan.tracks][:2] == ["Wordless Chorus", "It Beats 4 U"]
    assert all(t.title_source.startswith("info file setlist") for t in plan.tracks)


def _title_case(existing):
    """A d1/d2 folder whose info file is laid out by set, numbered straight through."""
    from types import SimpleNamespace as NS

    from jamp.infofile import InfoTrack

    setlist = [InfoTrack(1, None, 1, "Chalkdust"), InfoTrack(1, None, 2, "Reba"),
               InfoTrack(2, None, 3, "Tweezer"), InfoTrack(2, None, 4, "Harry Hood")]
    a = NS(classification=NS(kind="UNOFFICIAL"), info_file=NS(tracks=setlist))
    f = NS(tag=lambda key: existing, name_info=NS(title=None))
    return a, f


def test_a_setlist_found_by_number_alone_never_replaces_a_title():
    """d2t01 looked up (None, 1) and got set 1's first song, ranked above the
    existing tag - so a correct "Tweezer" was replaced with "Chalkdust"."""
    from jamp.phase1 import choose_title

    a, f = _title_case("Tweezer")
    assert choose_title(a, f, "d", 2, 1, groups=2) == ("Tweezer", "existing tag")
    assert choose_title(a, f, "d", 1, 1, groups=1)[0] == "Tweezer"


def test_a_setlist_found_by_number_alone_does_not_cross_discs():
    from jamp.phase1 import choose_title

    a, f = _title_case("")
    assert choose_title(a, f, "d", 2, 1, groups=2) == (None, "none found")
    # One disc: nothing to confuse the number with, so it still fills a gap.
    assert choose_title(a, f, "d", 1, 3, groups=1) == (
        "Tweezer", "info file setlist, by track number")


def test_no_title_is_invented(plans):
    plan = plans["mmj2005-06-04.ak40.flac16"]
    assert all(t.title is None for t in plan.tracks)
    assert all("no title available" in " ".join(t.warnings) for t in plan.tracks)


def test_inferred_source_is_stamped_into_a_tag(plans):
    plan = plans["My Morning Jacket 2023-11-03 Fox Theatre, Atlanta, GA [FLAC24]"]
    tag = plan.tracks[0].tags.get("SOURCE_CONFIDENCE")
    assert tag is not None
    assert tag[1].startswith("inferred:")


def test_stated_source_is_not_stamped(plans):
    plan = plans["MMJ2006-06-16..4011s bonaroo"]
    assert "SOURCE_CONFIDENCE" not in plan.tracks[0].tags


def test_album_tag_shape(plans):
    plan = plans["MMJ2006-06-16..4011s bonaroo"]
    assert plan.tracks[0].tags["ALBUM"][1] == \
        "2006-06-16: Bonnaroo Music Festival, Manchester, TN"


def test_shn_files_are_not_promised_tags(plans):
    plan = plans["mmj2003-09-26.shnf"]
    assert all(not t.tags for t in plan.tracks)
    assert all("cannot be written" in " ".join(t.warnings) for t in plan.tracks)


def test_mp3_dates_are_full_iso_not_just_a_year(plans):
    plan = plans["Phish - 2012-06-28 Noblesville, IN (v0)"]
    proposed = {k: v[1] for k, v in plan.tracks[0].tags.items()}
    assert proposed.get("DATE", "2012-06-28") == "2012-06-28"


# --- safety ----------------------------------------------------------------

def test_phase1_writes_nothing_into_the_library(library, cfg, tmp_path):
    before = {p: p.stat().st_mtime_ns for p in library.rglob("*")}
    phase1.run(library, tmp_path / "out", cfg, today=TODAY)
    after = {p: p.stat().st_mtime_ns for p in library.rglob("*")}
    assert before == after


def test_phase0_writes_nothing_into_the_library(library, cfg, tmp_path):
    before = sorted(str(p) for p in library.rglob("*"))
    phase0.run(library, tmp_path / "out0", cfg, today=TODAY)
    after = sorted(str(p) for p in library.rglob("*"))
    assert before == after


def test_out_dir_inside_root_is_refused(library, tmp_path):
    with pytest.raises(ValueError):
        ensure_out_dir(library / "reports", library)
    assert ensure_out_dir(tmp_path / "elsewhere", library).exists()


def test_running_twice_changes_nothing(tmp_path, cfg):
    """A folder already in the scheme, with correct tags, is UNCHANGED."""
    root = tmp_path / "lib"
    folder = root / "My Morning Jacket" / "mmj2005-06-04.aud.flac16"
    for i in (1, 2):
        fixtures.make_flac(
            folder / ("mmj2005-06-04d1t%02d.flac" % i), bits=16,
            tags={
                "ARTIST": "My Morning Jacket", "ALBUMARTIST": "My Morning Jacket",
                "ALBUM": "2005-06-04", "DATE": "2005-06-04",
                "TRACKNUMBER": str(i), "DISCNUMBER": "1", "GENRE": "Live",
            },
        )
    show = scan(root, cfg).shows[0]
    plan = plan_show(analyze_show(show, cfg, today=TODAY), cfg)
    assert plan.new_folder_name == "mmj2005-06-04.aud.flac16"
    assert plan.status == UNCHANGED
    assert not any(t.changed for t in plan.tracks)


def test_two_copies_of_one_show_are_marked_duplicate(tmp_path, cfg):
    """Same band, date, source and format: two copies, not two shows."""
    root = tmp_path / "lib"
    for name in ("ph2018-12-28 night one", "ph2018-12-28 night two"):
        for i in (1, 2):
            fixtures.make_flac(root / "phish" / name / ("d1t%02d.flac" % i), bits=16)
    plans = build_plans(root, cfg, today=TODAY)
    assert {p.status for p in plans} == {DUPLICATE}
    assert all("delete the copy you do not want" in " ".join(p.reasons) for p in plans)
    # Reported only: nothing is renamed and nothing is deleted.
    assert all(p.new_folder_name for p in plans)


def test_long_paths_are_flagged_not_crashed_on(tmp_path):
    """Windows itself refuses to create a >260 char path here, so the limit is
    lowered rather than the path lengthened - the code path is the same."""
    from jamp.config import load_config

    local_cfg = load_config()
    local_cfg.settings.max_path_length = len(str(tmp_path)) + 40

    deep = tmp_path / "lib" / "phish" / ("ph2018-12-28 " + "x" * 60)
    fixtures.make_flac(deep / "d1t01.flac", bits=16)
    result = scan(tmp_path / "lib", local_cfg)
    assert result.long_paths
    a = analyze_show(result.shows[0], local_cfg, today=TODAY)
    assert "LONG_PATH" in a.issue_codes


# --- sidecars --------------------------------------------------------------

def test_checksum_files_are_rewritten_for_the_new_names(tmp_path):
    ffp = tmp_path / "show.ffp"
    ffp.write_text("old01.flac:%s\nold02.flac:%s\n" % ("a" * 32, "b" * 32), encoding="utf-8")
    plan = plan_sidecar(ffp, {"old01.flac": "ph2018-12-28d1t01.flac",
                              "old02.flac": "ph2018-12-28d1t02.flac"})
    assert plan.status == "rewrite"
    assert "ph2018-12-28d1t01.flac:" + "a" * 32 in plan.new_text
    assert not plan.unresolved


def test_md5_and_cue_are_rewritten(tmp_path):
    md5 = tmp_path / "show.md5"
    md5.write_text("%s *old01.flac\n" % ("c" * 32), encoding="utf-8")
    assert "*gd1977-05-08d1t01.flac" in plan_sidecar(
        md5, {"old01.flac": "gd1977-05-08d1t01.flac"}).new_text

    cue = tmp_path / "show.cue"
    cue.write_text('FILE "old01.flac" WAVE\n  TRACK 01 AUDIO\n', encoding="utf-8")
    assert 'FILE "gd1977-05-08d1t01.flac" WAVE' in plan_sidecar(
        cue, {"old01.flac": "gd1977-05-08d1t01.flac"}).new_text


def test_a_sidecar_with_an_unmappable_entry_is_flagged_not_half_rewritten(tmp_path):
    ffp = tmp_path / "show.ffp"
    ffp.write_text("old01.flac:%s\nmystery.flac:%s\n" % ("a" * 32, "b" * 32), encoding="utf-8")
    plan = plan_sidecar(ffp, {"old01.flac": "ph2018-12-28d1t01.flac"})
    assert plan.status == "unresolved"
    assert plan.new_text is None
    assert plan.unresolved == ["mystery.flac"]


def test_sidecars_named_after_the_folder_are_renamed_too():
    from pathlib import Path
    assert propose_sidecar_name(Path("um2001-06-02d1.md5"), "um2001-06-02.shnf",
                                "um2001-06-02.shn") is None
    assert propose_sidecar_name(Path("um2001-06-02d1.md5"), "um2001-06-02",
                                "um2001-06-02.sbd.shn") == "um2001-06-02.sbd.shnd1.md5"


def test_two_sets_of_one_show_merge_despite_a_trailing_format_note(tmp_path):
    """"... Set 1 (flac16)" and "... Set 2 (flac16)" are one concert.

    The set marker is not last in the name, so the two halves failed to match
    and the pair was reported as two shows resolving to one name.
    """
    from jamp.phase1 import group_stem, split_suffix
    one = "Grateful Dead 2015-07-03 Soldier Field Chicago IL Set 1 (flac16)"
    two = "Grateful Dead 2015-07-03 Soldier Field Chicago IL Set 2 (flac16)"
    assert split_suffix(one) == ("s", 1)
    assert split_suffix(two) == ("s", 2)
    assert group_stem(one) == group_stem(two)


def test_a_release_title_that_is_not_a_place_still_names_the_folder():
    """"Download Series Vol. 08: 1973-12-10" is not a venue, but it is the
    name of the record, and a bare date loses that.  The date inside it is
    dropped because the folder already carries one."""
    from jamp.phase1 import release_tail
    assert release_tail("Download Series Vol. 08: 1973-12-10") == "Download Series Vol. 08"
    assert release_tail("Dave's Picks Volume 16 [FLAC]") == "Dave's Picks Volume 16"
    assert release_tail(None) is None


def test_an_override_key_can_name_a_path_not_just_a_folder(tmp_path):
    """"Without a Net/Disc One" must not answer for every "Disc One".

    Release children carry generic names that repeat all over a library, so a
    bare-name key would silently apply one release's answer to every other.
    """
    from jamp.overrides import Overrides
    path = tmp_path / "o.yaml"
    path.write_text(
        'folders:\n'
        '  "Without a Net/Disc One":\n    skip: true\n'
        '  "Some Unique Folder":\n    skip: true\n', encoding="utf-8")
    ov = Overrides.load(path)
    assert ov.for_folder("Disc One", "Grateful Dead/Without a Net/Disc One") is not None
    assert ov.for_folder("Disc One", "Phish/Some Box/Disc One") is None
    assert ov.for_folder("Disc One") is None
    # A bare-name key still works, and backslashes are accepted in a path key.
    assert ov.for_folder("Some Unique Folder") is not None
    assert ov.for_folder("Disc One", "GD\Without a Net\Disc One") is not None


def test_a_hand_stated_place_beats_the_album_tag_and_the_info_file():
    """An override must reach the name it was written to correct.

    Both routes out of _album_place used to discard it.  An OFFICIAL release
    takes its ALBUM tag verbatim and never reads venue/city/state at all, so a
    nugs.net download whose tag read "Detroit Lakes MN, 10K Lakes Festival" -
    city and state first, no comma, venue last - kept that backwards text in
    its folder name while the override that fixed it was reported as applied.
    The fallback route prefers an info file's venue over the same fields.
    """
    from jamp.classify import OFFICIAL, UNOFFICIAL
    from jamp.phase1 import _album_place

    class _Profile:
        album_values = ["2007-07-19 - Detroit Lakes MN, 10K Lakes Festival"]

    class _Cls:
        def __init__(self, kind):
            self.kind = kind
            self.tag_profile = _Profile()

    class _Info:
        venue, city, state = "Some Other Hall", "Nowhere", "ZZ"

    class _A:
        def __init__(self, kind, place_by_hand, info=None, from_gazetteer=False):
            self.classification = _Cls(kind)
            self.info_file = info
            self.venue, self.city, self.state = "10K Lakes Festival", "Detroit Lakes", "MN"
            self.place_by_hand = place_by_hand
            self.place_from_gazetteer = from_gazetteer

    # Official release, no override: the ALBUM tag is still taken verbatim.
    assert _album_place(_A(OFFICIAL, False))[3] ==         "Detroit Lakes MN, 10K Lakes Festival"
    # Official release with the place stated by hand: the override wins.
    assert _album_place(_A(OFFICIAL, True)) ==         ("10K Lakes Festival", "Detroit Lakes", "MN", None)
    # And it beats an info file on the ordinary route too.
    assert _album_place(_A(UNOFFICIAL, True, _Info())) ==         ("10K Lakes Festival", "Detroit Lakes", "MN", None)
    assert _album_place(_A(UNOFFICIAL, False, _Info()))[0] == "Some Other Hall"
    # A gazetteer answer takes the same route, and for the same reason: without
    # it the tags gain the city and the folder name does not, so the folder
    # replans every run and never settles.
    assert _album_place(_A(OFFICIAL, False, from_gazetteer=True)) ==         ("10K Lakes Festival", "Detroit Lakes", "MN", None)


def test_a_band_or_marker_stated_by_hand_is_actually_applied(cfg):
    """Both were declared override fields and validated, then never applied.

    overrides.FIELDS lists band and marker, so stating either passed validation
    and was reported as OVERRIDDEN - and then silently ignored, which is worse
    than rejecting it.  The case that found this: the 2005 New Orleans SuperJam
    info file is headed "TREY ANASTASIO", but the folder is called
    treysuperjam2005, and defaults_unless_named matches whole tokens, so
    "superjam" cannot be seen inside a run-together name and the family default
    turned it into a TAB show.
    """
    import pytest
    from jamp.analyze import apply_override

    class _A:
        def __init__(self):
            self.band = None
            self.marker_by_hand = None
            self.issues = []
        def add(self, *args, **kwargs):
            self.issues.append(args)

    class _Entry:
        def __init__(self, values):
            self.match = "treysuperjam2005"
            self.values = values

    a = _A()
    apply_override(a, _Entry({"band": "trey", "marker": "late"}), cfg)
    assert a.band.abbrev == "trey"
    assert a.band.confidence == 99 and a.band.matched_by == "overrides.yaml"
    assert a.marker_by_hand == "late"

    # An abbreviation naming no act is a mistake in the override, not something
    # to guess around - the whole point of an override is that it is deliberate.
    with pytest.raises(ValueError):
        apply_override(_A(), _Entry({"band": "no-such-act"}), cfg)


def test_state_remembers_the_name_a_folder_arrived_with(tmp_path):
    """An override is keyed on the original name, so it must survive renaming.

    The first successful rename used to put a folder beyond the reach of its own
    override.  Bad Hat 1994-09-11: an override corrected the town to Northampton,
    the folder was renamed, the override then matched nothing, and the info file
    that spells it "Northhampton" won the next pass and renamed it straight back
    - writing the misspelling into VENUE and ALBUM, so the bad spelling became
    the evidence.  previous_folder_name cannot serve here, because it only ever
    remembers one rename back.
    """
    from jamp.state import read_state, write_state

    d = tmp_path / "bad hat 1994-9-11.flac16"
    d.mkdir()
    write_state(d, {"previous_folder_name": "bad hat 1994-9-11.flac16"})
    assert read_state(d)["original_folder_name"] == "bad hat 1994-9-11.flac16"

    # A later commit renames it again; the original must not drift forward.
    write_state(d, {"previous_folder_name": "badhat1994-09-11.aud.flaschner.flac16"})
    assert read_state(d)["original_folder_name"] == "bad hat 1994-9-11.flac16"

    # A folder with no recorded history answers to the name it has now.
    e = tmp_path / "never touched before"
    e.mkdir()
    write_state(e, {})
    assert read_state(e)["original_folder_name"] == "never touched before"


def test_an_override_key_with_no_value_clears_the_field(cfg):
    """Some of what an override must beat is a value that should not exist.

    An override could only replace a field, never empty one.  "ams" in
    skb2002-08-10-ams.shnf was offered as a venue when it is Amsterdam, and a
    source of "sbd" was inferred from an OFFICIAL classification that the same
    override then overturned - apply_override runs last, so what was derived
    from the old answer stayed behind.  A key written with no value now clears
    the field; a key nobody wrote still leaves it alone.
    """
    from jamp.analyze import apply_override

    class _A:
        def __init__(self):
            self.venue, self.city, self.state = "ams", None, None
            self.provenance, self.fmt = "keepme", "flac16"
            self.source = "sbd"
            self.place_by_hand = False
            self.marker_by_hand = None
            self.band = None
            self.issues = []
        def add(self, *args, **kwargs):
            self.issues.append(args)

    class _Entry:
        def __init__(self, values):
            self.match = "skb2002-08-10-ams.shnf"
            self.values = values

    a = _A()
    apply_override(a, _Entry({"venue": None, "city": "Amsterdam", "source": None}), cfg)
    assert a.venue is None and a.city == "Amsterdam"
    assert a.place_by_hand is True
    assert a.source.value is None and a.source.inferred is False
    # Untouched keys keep what they had.
    assert a.provenance == "keepme" and a.fmt == "flac16"


def test_a_comma_after_the_date_does_not_cost_the_venue(tmp_path):
    """"2004-12-19, Warfield Theater, San Francisco, CA" named a venue.

    The leading date was stripped with " -:", which does not include a comma, so
    what remained began with one and splitting on commas gave an empty head.
    That failed the length check and the venue was lost - and since a city with
    no venue is dropped from the name, the show committed with no place at all
    while its own ALBUM tag said where it was.
    """
    from jamp.analyze import place_from_tags

    class _F:
        def __init__(self, album):
            self.tags = {"ALBUM": [album]}

    class _Show:
        def __init__(self, album):
            self.files = [_F(album)]

    assert place_from_tags(_Show("2004-12-19, Warfield Theater, San Francisco, CA")) ==         ("Warfield Theater", "San Francisco", "CA")
    # The shape that already worked keeps working.
    assert place_from_tags(_Show("2004-12-19 - Warfield Theater, San Francisco, CA")) ==         ("Warfield Theater", "San Francisco", "CA")
    # An ALBUM that is only a date still yields no venue to invent.
    assert place_from_tags(_Show("2006-07-22"))[0] is None


def test_a_tag_that_ends_in_its_lineage_does_not_name_the_room():
    """"2007-05-21 - Mr. Small's Theater sbd" is a venue and a source.

    Ten Disco Biscuits shows were heading for folder names ending in "sbd",
    because the album head is taken whole and these tags append the lineage to
    the venue.  Only a whole trailing word is removed, and only when something
    survives it.
    """
    from jamp.analyze import place_from_tags

    class _F:
        def __init__(self, album):
            self.tags = {"ALBUM": [album]}

    class _Show:
        def __init__(self, album):
            self.files = [_F(album)]

    assert place_from_tags(_Show("2007-05-21 - Mr. Small's Theater sbd"))[0] ==         "Mr. Small's Theater"
    assert place_from_tags(_Show("2009-04-20 - 9:30 Club sbd"))[0] == "9:30 Club"
    # A room whose name merely ends that way is left alone, and a venue that IS
    # the word survives rather than being stripped to nothing.
    assert place_from_tags(_Show("1999-01-01 - The Board Room"))[0] == "The Board Room"
    assert place_from_tags(_Show("1999-01-01 - Soundboard"))[0] == "Soundboard"


def test_duplicate_tag_track_numbers_fall_back_to_the_filenames():
    """Two files tagged with the same track number is a broken numbering.

    Seen on a real disc rip: "20 Around And Around" and "21 One More Saturday
    Night" are both tagged track 21, so both wanted the same new name and the
    whole folder was held back.  The filenames are sequential and unique, so
    they are used instead.
    """
    from pathlib import Path
    from jamp.audio import AudioFile, parse_track_name
    from jamp.classify import OFFICIAL, Classification
    from jamp.phase1 import plan_numbering

    class _A:
        pass

    def mk(name, tagged):
        f = AudioFile(path=Path(name), ext=".flac", size=1,
                      name_info=parse_track_name(name))
        f.tags = {"TRACKNUMBER": [str(tagged)]}
        return f

    a = _A()
    a.info_file = None
    a.show = _A()
    a.show.files = [mk("19 Wharf Rat.flac", 20),
                    mk("20 Around And Around.flac", 21),
                    mk("21 One More Saturday Night.flac", 21)]
    a.classification = Classification(kind=OFFICIAL)
    numbering = plan_numbering(a)
    slots = [(d, n) for _, _, d, n, _ in numbering]
    assert len(set(slots)) == 3, slots
    assert all("filename track number" == src for *_, src in numbering)
    assert [n for _, _, _, n, _ in numbering] == [19, 20, 21]


def test_sets_merge_when_a_format_token_trails_the_marker():
    """"ph2012-06-07s1.mp3" - the set marker is hidden by the format suffix.

    Without stripping it the two sets of one show were reported as two shows
    resolving to the same name.
    """
    from jamp.phase1 import group_stem, split_suffix
    assert split_suffix("ph2012-06-07s1.mp3") == ("s", 1)
    assert split_suffix("ph2012-06-07s2.mp3") == ("s", 2)
    assert group_stem("ph2012-06-07s1.mp3") == group_stem("ph2012-06-07s2.mp3")
    # A marker that is not actually last stays unmatched: this is set 2 of a
    # specific etree source, not a folder waiting to be merged with a sibling.
    assert split_suffix("ph1996-12-04.set2.13831.shnf") is None


def test_set_two_does_not_get_set_ones_titles():
    """A folder numbered by SET looked its titles up under disc 1.

    Every track of set 2 was given the title of the set 1 track with the same
    number: s2t01 came out as "Frankenttein" when it is "Wilson >".  Wrong
    titles written into tags are worse than missing ones, and this would have
    hit 666 tracks across 70 folders.
    """
    from jamp.infofile import InfoTrack
    from jamp.phase1 import _setlist_index

    tracks = [
        InfoTrack(set_no=1, disc=1, number=1, title="Frankenttein"),
        InfoTrack(set_no=1, disc=1, number=2, title="NICU"),
        InfoTrack(set_no=2, disc=2, number=1, title="Wilson >"),
        InfoTrack(set_no=2, disc=2, number=2, title="Simple >"),
        # "Encore:" starts a set of its own in the prose, but the files stay
        # numbered under the last set.
        InfoTrack(set_no=102, disc=2, number=3, title="Sample In A Jar"),
    ]
    index = _setlist_index(tracks)
    assert index[("s", 1, 1)].title == "Frankenttein"
    assert index[("s", 2, 1)].title == "Wilson >"
    # The encore is reachable by disc, which is the fallback choose_title uses.
    assert index[("d", 2, 3)].title == "Sample In A Jar"


def test_a_single_date_multi_date_series_keeps_its_release_name(tmp_path, cfg):
    """"Pure Jerry #4" is one of several installments in a numbered series,
    even though this particular installment has one clean date.  The config's
    own multi_date flag says the release is not one show under one date, so
    the folder keeps its release identity instead of being renamed into the
    machine scheme - the same rule a box set's own container folder gets.
    """
    import fixtures
    root = tmp_path / "lib"
    d = root / "jerry garcia" / "Pure Jerry #4 Garcia Merl Saunders Band - Keystone Berkeley, 1974-09-01"
    for i in (1, 2):
        fixtures.make_flac(
            d / ("%02d Song %d.flac" % (i, i)), bits=16,
            tags={"ARTIST": "Jerry Garcia Band", "ALBUM": "Pure Jerry #4",
                  "TITLE": "Song %d" % i, "TRACKNUMBER": str(i), "DATE": "1974-09-01"},
        )
    plans = {p.old_folder_name: p for p in build_plans(root, cfg, today=TODAY)}
    plan = plans["Pure Jerry #4 Garcia Merl Saunders Band - Keystone Berkeley, 1974-09-01"]
    assert plan.new_folder_name == plan.old_folder_name
    assert plan.tracks[0].new_name.startswith("jgb1974-09-01")


def test_a_reverted_pure_jerry_folder_recovers_its_true_original_name(tmp_path, cfg):
    """A folder already renamed once must revert to what it truly was, not
    freeze at whatever the machine name currently is - .etree_state.json's
    previous_folder_name is what makes that recoverable.
    """
    import json
    import fixtures
    root = tmp_path / "lib"
    d = root / "jerry garcia" / "jgb1978-03-18.sbd.flac16 - Warner Theatre Pure Jerry #6"
    for i in (1, 2):
        fixtures.make_flac(
            d / ("jgb1978-03-18d1t%02d.flac" % i), bits=16,
            tags={"ARTIST": "Jerry Garcia Band", "ALBUM": "Pure Jerry #6",
                  "TITLE": "Song %d" % i, "TRACKNUMBER": str(i), "DATE": "1978-03-18"},
        )
    (d / ".etree_state.json").write_text(json.dumps({
        "previous_folder_name": "Pure Jerry #6 78-3-18 Warner Theatre",
    }), encoding="utf-8")
    plans = {p.old_folder_name: p for p in build_plans(root, cfg, today=TODAY, reclassify=True)}
    plan = plans["jgb1978-03-18.sbd.flac16 - Warner Theatre Pure Jerry #6"]
    assert plan.new_folder_name == "Pure Jerry #6 78-3-18 Warner Theatre"


def test_an_encore_set_number_does_not_misalign_the_whole_numbering():
    """"s102t01" (the +100 encore marker) sorts BEFORE "s1t01"/"s2t04" under
    plain string order, since '0' < 't'.  On a real committed Jerry Garcia
    folder that misaligned every file against the setlist by one and produced
    a closed rename cycle - s102t01 wants to become s1t01, s1t01 wants to
    become s1t02, and so on back around to s102t01 - which fails on its very
    first step because the target is still occupied.
    """
    from jamp.phase1 import _natural_sort_key
    names = ["jgb1983-11-27s1t01.flac", "jgb1983-11-27s1t05.flac",
            "jgb1983-11-27s2t01.flac", "jgb1983-11-27s2t04.flac",
            "jgb1983-11-27s102t01.flac"]
    assert sorted(names, key=_natural_sort_key) == names


def test_our_own_album_suffix_is_not_read_back_as_part_of_the_venue():
    """A box-set ALBUM we already wrote must round-trip to the same name.

    Phase 2 appends " [Release]" to ALBUM.  When that suffix carries a date of
    its own the release-folder branch sliced from the last date in the string,
    cutting into the bracket and leaving "]" stuck on the venue:
        '1990-03-14 Capital Centre, Landover, MD' -> '... Landover, MD]'
    Found on the real Spring 1990 box set, 14 folders.
    """
    import re
    from jamp.phase1 import _ANY_DATE

    album = ("1990-03-14: Capital Centre, Landover, MD "
             "[03/14/1990 Capital Centre, Landover, MD]")
    cleaned = re.sub(r"\s*\[[^\]]*\]\s*$", "", album)
    spans = [m.span() for m in _ANY_DATE.finditer(cleaned)]
    tail = cleaned[spans[-1][1]:] if spans else cleaned
    tail = tail.strip(" -:,")
    assert not tail.endswith("]")
    assert tail == "Capital Centre, Landover, MD"


def test_a_name_sanitised_on_the_way_to_disk_still_matches(tmp_path):
    """A checksum written against the original title must still map.

    Real case, Phish 2000-07-04 Camden: the .ffp says
    "Phish2000-07-04_s01t05_It's Ice_.flac" but the file on disk is
    "..._It_s Ice_.flac" - a downloader replaced the apostrophe, which
    Windows allows but the whole illegal-character class does not.  One
    substituted character left all 21 fingerprints unusable.
    """
    ffp = tmp_path / "camden.ffp"
    ffp.write_text("Phish2000-07-04_s01t05_It's Ice_.flac:%s\n" % ("a" * 32),
                   encoding="utf-8")
    plan = plan_sidecar(ffp, {"Phish2000-07-04_s01t05_It_s Ice_.flac":
                              "ph2000-07-04s1t05.flac"})
    assert plan.status == "rewrite", plan.notes
    assert "ph2000-07-04s1t05.flac:" in plan.new_text


def test_an_ambiguous_fold_is_refused_rather_than_guessed(tmp_path):
    """Two files that differ only in characters the fold erases cannot be
    told apart, so the entry stays unresolved - renaming the wrong line is
    worse than leaving the file alone."""
    ffp = tmp_path / "amb.ffp"
    ffp.write_text("a:b.flac:%s\n" % ("a" * 32), encoding="utf-8")
    plan = plan_sidecar(ffp, {"a?b.flac": "x1.flac", "a*b.flac": "x2.flac"})
    assert plan.status == "unresolved"
    assert plan.new_text is None


def test_the_fold_does_not_loosen_an_ordinary_mismatch(tmp_path):
    """A genuinely absent file is still unresolved; the fold only forgives
    characters that cannot survive a Windows filename."""
    ffp = tmp_path / "show.ffp"
    ffp.write_text("mystery.flac:%s\n" % ("b" * 32), encoding="utf-8")
    plan = plan_sidecar(ffp, {"old01.flac": "ph2018-12-28d1t01.flac"})
    assert plan.status == "unresolved"
    assert plan.unresolved == ["mystery.flac"]


def test_the_same_show_in_two_formats_is_not_a_split_show(tmp_path, cfg):
    """Sibling folders are matched as halves of one show after their format
    suffix is stripped - which is exactly what makes two format twins look
    like a pair.

    "ph1996-12-04.set2.13831.shnf" and "...shnf.FLAC" are one set in SHN and
    in FLAC.  Both say set2; a split needs different markers, not the same one
    twice.  They were reported as a split show and left unnamed, when they
    should simply be named separately - the format token tells them apart.
    """
    import fixtures
    from jamp.phase1 import build_plans, PLAN, SPLIT_SHOW

    root = tmp_path / "lib"
    base = root / "Phish" / "1996"
    for i in range(1, 4):
        fixtures.make_flac(base / "ph1996-12-04.set2.13831.shnf.FLAC" /
                           ("ph1996-12-04s2t%02d.flac" % i), bits=16)
        fixtures.make_shn(base / "ph1996-12-04.set2.13831.shnf" /
                          ("ph1996-12-04s2t%02d.shn" % i))

    plans = {p.show.name: p for p in build_plans(root, cfg, today=dt.date(2026, 9, 7))}
    for name, p in plans.items():
        if "12-04" not in name:
            continue
        assert p.status != SPLIT_SHOW, "%s should not be a split show" % name
    names = [p.new_folder_name for p in plans.values() if p.new_folder_name]
    assert len(names) == len(set(names)), "the format token must keep them apart"


def test_settled_format_twins_stay_settled():
    """The same show in two formats used to be promoted to PLAN whatever it had
    been, so two settled folders side by side became work that did nothing:
    their state was rewritten every run and "remaining" never reached 0."""
    from types import SimpleNamespace as NS

    from jamp.phase1 import UNCHANGED, merge_split_show

    names = ["ph1997-12-30.sbd.nugs.flac16", "ph1997-12-30.sbd.nugs.mp3"]
    group = [NS(old_folder_name=n, new_folder_name=n, status=UNCHANGED, warnings=[],
                analysis=NS(fmt=n.rsplit(".", 1)[1])) for n in names]
    merge_split_show(group, None)
    assert [p.status for p in group] == [UNCHANGED, UNCHANGED]
    assert all("different format" in p.warnings[0] for p in group)


def test_a_city_with_no_state_is_kept():
    """"2002.05.23 Aoyama Cay, Tokyo" is a venue and a city.

    _CITY_STATE wants a two-letter state, so nothing outside the US matched and
    the whole tail went in the bin - the STS9 Tokyo show came out as "Aoyama
    Cay" with no city at all, and the same would have happened to every
    European and Japanese date in the library.
    """
    from jamp.analyze import place_from_tags

    class _F:
        def __init__(self, album):
            self.tags = {"ALBUM": [album]}

    class _Show:
        def __init__(self, album):
            self.files = [_F(album)]

    assert place_from_tags(_Show("2002.05.23 Aoyama Cay, Tokyo")) == (
        "Aoyama Cay", "Tokyo", None)
    assert place_from_tags(_Show("1984-05-30 De Meervaart, Amsterdam")) == (
        "De Meervaart", "Amsterdam", None)
    # A US state still wins, and still comes out as the state rather than a city.
    assert place_from_tags(_Show("2000-03-10 The Brickyard, Vancouver, BC")) == (
        "The Brickyard", "Vancouver", "BC")
    # Only a plain "venue, city": three parts are not this shape, and a tail
    # that is not a place name is not promoted to one.
    assert place_from_tags(_Show("2002.05.23 Aoyama Cay, Disc 2"))[1] is None
    assert place_from_tags(_Show("2002.05.23 Aoyama Cay, 1 of 3"))[1] is None


def test_selections_is_a_release_word_not_a_city():
    """Trey's "9:30 Club - May 11, 1999 - Selections" filed Selections as the city."""
    from jamp.analyze import strip_product_words

    assert strip_product_words("9:30 Club - May 11, 1999 - Selections") == (
        "9:30 Club - May 11, 1999")


@pytest.mark.parametrize("text,expected", [
    # A double space is doing the comma's job between the room and the city.
    ("Congress Theater  Chicago IL", "Congress Theater, Chicago, IL"),
    ("Crystal Ballroom  Portland  OR", "Crystal Ballroom, Portland, OR"),
    # One word on the left is a name that happens to contain two spaces, not a
    # venue and a city: "The  Fillmore" must not become "The, Fillmore".
    ("The  Fillmore", "The Fillmore"),
    # Lower case on the right is not a new field either.
    ("9:30 Club  washington dc", "9:30 Club washington dc"),
    # Already punctuated, and single-spaced, text is left as it is.
    ("Barton Hall, Ithaca, NY", "Barton Hall, Ithaca, NY"),
    ("Mangos", "Mangos"),
])
def test_a_double_space_can_stand_in_for_the_comma(text, expected):
    from jamp.analyze import strip_product_words

    assert strip_product_words(text) == expected


@pytest.mark.parametrize("album,expected", [
    # "::" separates the fields; without that the room and the city ran together
    # and the venue came out "Congress Theater Chicago".
    ("2012.01.21 :: Congress Theater :: Chicago, IL",
     ("Congress Theater", "Chicago", "IL")),
    ("2009-08-01 | The Fillmore | San Francisco, CA",
     ("The Fillmore", "San Francisco", "CA")),
    # A single colon is part of the name of the room, not a separator.
    ("2009-04-20 - 9:30 Club, Washington, DC",
     ("9:30 Club", "Washington", "DC")),
])
def test_double_colon_and_pipe_separate_the_fields(album, expected):
    from jamp.analyze import place_from_tags

    class _F:
        def __init__(self, album):
            self.tags = {"ALBUM": [album]}

    class _Show:
        def __init__(self, album):
            self.files = [_F(album)]

    assert place_from_tags(_Show(album)) == expected


@pytest.mark.parametrize("album,expected_city", [
    # A real city with no state after it is kept.
    ("2002.05.23 Aoyama Cay, Tokyo", "Tokyo"),
    ("1984-05-30 De Meervaart, Amsterdam", "Amsterdam"),
    # A tail that names a campus or a building continues the venue.  A Disco
    # Biscuits album reads "Irvine Auditorium, University of Pennsylvania", and
    # taking that as the city turned a settled folder into a rename.
    ("1999-10-29 - Irvine Auditorium, University of Pennsylvania", None),
    ("1999-01-01 Foo Bar, Memorial Hall", None),
    ("1999-01-01 Foo Bar, Riverside Amphitheatre", None),
])
def test_a_venue_continuation_is_not_a_city(album, expected_city):
    from jamp.analyze import place_from_tags

    class _F:
        def __init__(self, album):
            self.tags = {"ALBUM": [album]}

    class _Show:
        def __init__(self, album):
            self.files = [_F(album)]

    assert place_from_tags(_Show(album))[1] == expected_city


def test_a_whole_place_string_does_not_gain_a_second_city():
    """"Walnut Creek Amphitheatre, Raliegh, NC" already says the city.

    venue_from_folder_name returns the whole place as one string by design. The
    info file for that Phish show gives only a city, "Raleigh", and adding it
    beside the venue produced "Walnut Creek Amphitheatre, Raliegh, NC, Raleigh" -
    the same city twice, in two spellings, which dedupe cannot catch.
    """
    from jamp.analyze import place_is_whole_string

    assert place_is_whole_string("Walnut Creek Amphitheatre, Raliegh, NC")
    assert place_is_whole_string("Barton Hall, Ithaca, NY")
    # The name of a room on its own is not a whole place.
    assert not place_is_whole_string("Walnut Creek Amphitheatre")
    assert not place_is_whole_string("The Cat's Cradle")
    assert not place_is_whole_string(None)


@pytest.mark.parametrize("text,expected", [
    ("KSU MAC Center (16/44.1)", "KSU MAC Center"),
    ("Venue (24bit/96kHz)", "Venue"),
    # The "/" cannot survive in a folder name, so a name already committed
    # carries the mangled form and must be cleaned up too.
    ("Venue (1644.1)", "Venue"),
    ("Venue (96kHz)", "Venue"),
    # A bracketed year is not a transfer spec; dates are stripped elsewhere.
    ("Wembley (1985)", "Wembley (1985)"),
    # Numbers that belong to the name of the room.
    ("The 40 Watt Club", "The 40 Watt Club"),
    ("Studio 54", "Studio 54"),
    ("Hall (Upstairs)", "Hall (Upstairs)"),
])
def test_strip_audio_spec(text, expected):
    from jamp.naming import strip_audio_spec

    assert strip_audio_spec(text) == expected


def _analyse_one(root, cfg, overrides):
    from jamp.analyze import analyze_show
    from jamp.scan import scan

    shows = scan(root, cfg, read_tags=True).shows
    assert len(shows) == 1
    return analyze_show(shows[0], cfg, today=dt.date(2026, 9, 13), overrides=overrides)


def test_a_path_keyed_override_still_reaches_a_renamed_folder(tmp_path, cfg):
    """The original-name fallback asked by name only, so a key written as a
    path went dormant after the first rename: "Here Comes Sunshine (1973)/
    1973-06-10" stopped reaching "1973-06-10 Robert F. Kennedy Stadium, ..."."""
    import json

    import fixtures
    from jamp.overrides import Override, Overrides

    root = tmp_path / "lib"
    box = root / "The Grateful Dead" / "Here Comes Sunshine (1973)"
    child = box / "1973-06-10 RFK Stadium, Washington, DC"
    fixtures.make_flac(child / "gd1973-06-10d1t01.flac", tags={"ARTIST": "Grateful Dead"})
    (child / ".etree_state.json").write_text(json.dumps(
        {"folder_name": child.name, "original_folder_name": "1973-06-10"}), encoding="utf-8")
    ov = Overrides([Override("Here Comes Sunshine (1973)/1973-06-10",
                             {"venue": "Robert F. Kennedy Stadium"})])

    a = _analyse_one(root, cfg, ov)
    assert "OVERRIDDEN" in a.issue_codes
    assert a.venue == "Robert F. Kennedy Stadium"


def test_a_lifted_and_renamed_folder_is_found_by_the_path_it_arrived_at(tmp_path, cfg):
    """--unnest moved the Lugano night up a level and renamed it; no path built
    from where it sits now can reach a key written against where it was."""
    import json

    import fixtures
    from jamp.overrides import Override, Overrides

    root = tmp_path / "lib"
    now = root / "Medeski Martin & Wood" / "mmwnc2014-04-07.mp3 - Lugano"
    fixtures.make_mp3(now / "mmwnc2014-04-07d1t01.mp3")
    (now / ".etree_state.json").write_text(json.dumps({
        "folder_name": now.name,
        "original_folder_name": "04-07-14 Lugano, Switzerland",
        "original_relative_path": "Medeski Martin & Wood/MMW 04-07-14 Lugano, Switzerland/"
                                  "04-07-14 Lugano, Switzerland"}), encoding="utf-8")
    ov = Overrides([Override("Medeski Martin & Wood/MMW 04-07-14 Lugano, Switzerland/"
                             "04-07-14 Lugano, Switzerland", {"venue": "Auditorio Stelio Molo RSI"})])

    assert _analyse_one(root, cfg, ov).venue == "Auditorio Stelio Molo RSI"


def test_the_original_path_is_recorded_once_and_only_when_it_is_known(tmp_path):
    from jamp.state import read_state, write_state

    d = tmp_path / "gd1977-05-08.sbd.flac16"
    d.mkdir()
    write_state(d, {"previous_folder_name": "Cornell 77",
                    "previous_relative_path": "Grateful Dead/Cornell 77"})
    assert read_state(d)["original_relative_path"] == "Grateful Dead/Cornell 77"
    assert "previous_relative_path" not in read_state(d)
    write_state(d, {"previous_folder_name": d.name,
                    "previous_relative_path": "Grateful Dead/" + d.name})
    assert read_state(d)["original_relative_path"] == "Grateful Dead/Cornell 77"

    # Settled before the path was recorded: it has already moved, so its
    # current path is not an original and is not written as one.
    e = tmp_path / "jgb1990-01-01.sbd.flac16"
    e.mkdir()
    write_state(e, {"previous_folder_name": "whatever"})
    write_state(e, {"previous_folder_name": e.name,
                    "previous_relative_path": "Jerry Garcia/" + e.name})
    assert "original_relative_path" not in read_state(e)


def test_and_friends_survives_a_second_rename(tmp_path, cfg):
    """previous_folder_name is one rename back; after a second rename it holds
    our own machine name, and the word that stops the family default is gone."""
    import json

    import fixtures

    root = tmp_path / "lib"
    folder = root / "Jerry Garcia" / "jg1990-05-10.sbd.flac16"
    fixtures.make_flac(folder / "jg1990-05-10d1t01.flac", tags={"ARTIST": "Jerry Garcia"})
    (folder / ".etree_state.json").write_text(json.dumps({
        "folder_name": folder.name,
        "original_folder_name": "Jerry Garcia & Friends 1990-05-10",
        "previous_folder_name": "jg1990-05-10.flac16"}), encoding="utf-8")

    a = _analyse_one(root, cfg, None)
    assert "FAMILY_DEFAULT_NOT_APPLIED" in a.issue_codes
    assert a.band.abbrev == "jg"


def test_disc_folders_that_repeat_a_filename_keep_their_checksums_apart(tmp_path):
    """One rename map for the whole show, keyed by bare filename, collapsed
    "Disc 1/01.flac" and "Disc 2/01.flac" into one entry: a disc 1 checksum line
    could be rewritten to disc 2's new name."""
    from types import SimpleNamespace as NS

    from jamp.phase1 import rename_map_for
    from jamp.sidecars import REWRITE, UNRESOLVED, plan_sidecar

    show = tmp_path / "Dozin at the Nick"
    tracks = []
    for disc in (1, 2):
        for n in (1, 2):
            path = show / ("Disc %d" % disc) / ("%02d.flac" % n)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
            tracks.append(NS(file=NS(path=path), old_name=path.name,
                             new_name="gd1990-03-24d%dt%02d.flac" % (disc, n)))

    inside = show / "Disc 2" / "disc2.ffp"
    inside.write_text("01.flac:%s\n02.flac:%s\n" % ("0" * 32, "1" * 32), encoding="utf-8")
    plan = plan_sidecar(inside, rename_map_for(inside, tracks))
    assert plan.status == REWRITE
    assert plan.new_text.startswith("gd1990-03-24d2t01.flac:")

    top = show / "show.md5"
    top.write_text("%s *Disc 1/01.flac\n%s *Disc 2/01.flac\n" % ("0" * 32, "1" * 32),
                   encoding="utf-8")
    plan = plan_sidecar(top, rename_map_for(top, tracks))
    assert plan.status == REWRITE
    assert "Disc 1/gd1990-03-24d1t01.flac" in plan.new_text
    assert "Disc 2/gd1990-03-24d2t01.flac" in plan.new_text
    # The Windows separator names the same file.
    top.write_text("%s *Disc 2\\01.flac\n" % ("1" * 32), encoding="utf-8")
    plan = plan_sidecar(top, rename_map_for(top, tracks))
    assert plan.status == REWRITE and "Disc 2\\gd1990-03-24d2t01.flac" in plan.new_text

    # A bare name at the top cannot say which disc it means: flagged, not guessed.
    loose = show / "loose.ffp"
    loose.write_text("01.flac:%s\n" % ("0" * 32), encoding="utf-8")
    assert plan_sidecar(loose, rename_map_for(loose, tracks)).status == UNRESOLVED


def test_a_later_decision_keeps_what_phase3_and_the_title_repair_recorded(tmp_path):
    """write_state rebuilt the file from phase 2's payload alone, so a rename
    after phase 3 had filled a folder dropped phase3_written, and phase 3 could
    no longer correct its own answer there."""
    import json

    from jamp.state import read_state, write_state

    d = tmp_path / "gd1977-05-08.sbd.flac16"
    d.mkdir()
    write_state(d, {"previous_folder_name": "Cornell 77"})
    state = read_state(d)
    state["phase3_written"] = {"d1t01.flac": {"VENUE": "Barton Hall"}}
    state["titles_restored"] = {"d1t02.flac": {"was": "x", "now": "Loser"}}
    (d / ".etree_state.json").write_text(json.dumps(state), encoding="utf-8")

    write_state(d, {"previous_folder_name": d.name, "classification": "UNOFFICIAL"})
    again = read_state(d)
    assert again["phase3_written"] == {"d1t01.flac": {"VENUE": "Barton Hall"}}
    assert again["titles_restored"]["d1t02.flac"]["now"] == "Loser"
    assert again["classification"] == "UNOFFICIAL"


def _disc_title_case(existing, setlist_title):
    from types import SimpleNamespace as NS

    from jamp.infofile import InfoTrack

    setlist = [InfoTrack(None, 1, 1, "Chalk Dust Torture"), InfoTrack(None, 2, 1, setlist_title)]
    a = NS(classification=NS(kind="UNOFFICIAL"), info_file=NS(tracks=setlist))
    f = NS(tag=lambda key: existing, name_info=NS(title=None))
    return a, f


def test_the_setlists_wording_wins_once_its_damage_is_cleaned():
    """The folder's own setlist is the authority on what a track is called.
    Its lines are cleaned as they are parsed, so what wins is its name for the
    song, not "Dear //Prudence [12:#53] ->"."""
    from jamp.infofile import parse_setlist
    from jamp.phase1 import choose_title

    for existing, line, want in [
        ("Dear Prudence", "1. Dear //Prudence [12:#53] ->", "Dear Prudence ->"),
        ("Suzy Greenberg", "1. Suzie Greenberg", "Suzie Greenberg"),
        ("It Ain't No Use", "1. It's No Use [8:52]", "It's No Use"),
    ]:
        listed = parse_setlist([line])[0].title
        a, f = _disc_title_case(existing, listed)
        assert choose_title(a, f, "d", 2, 1, groups=2)[0] == want, existing


def test_a_setlist_still_replaces_a_title_that_names_another_song():
    from jamp.phase1 import choose_title

    a, f = _disc_title_case("Taper Talk, Crowd & Tuning", "Crowd & Tuning")
    assert choose_title(a, f, "d", 2, 1, groups=2) == ("Crowd & Tuning", "info file setlist")
