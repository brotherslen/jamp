"""Walking the library, choosing the info file, and classifying origin."""
import datetime as dt

import pytest

from jamp.analyze import analyze_show
from jamp.classify import DISC_RIP, OFFICIAL, STORE, UNOFFICIAL
from jamp.infofile import parse_setlist, select_info_file, taper_slug
from jamp.scan import scan

TODAY = dt.date(2026, 9, 7)


@pytest.fixture(scope="module")
def shows(library, cfg):
    return {s.name: s for s in scan(library, cfg).shows}


@pytest.fixture(scope="module")
def analyses(shows, cfg):
    return {name: analyze_show(s, cfg, today=TODAY) for name, s in shows.items()}


def test_show_folders_are_found_at_varying_depth(shows):
    assert "mmj2003-09-26.shnf" in shows                       # directly under the artist
    assert "2011_11_05 Eagles Ballroom - Milwaukee, WI" in shows  # one level deeper


def test_a_single_child_container_is_recorded_not_treated_as_a_show(shows):
    assert "u111105" not in shows
    nested = shows["2011_11_05 Eagles Ballroom - Milwaukee, WI"]
    assert nested.container is not None
    assert nested.container.name == "u111105"


def test_the_container_name_corroborates_the_date(analyses):
    a = analyses["2011_11_05 Eagles Ballroom - Milwaukee, WI"]
    assert a.date.iso == "2011-11-05"
    assert a.date.confidence >= 90


JG_DG_INFO = """Jerry Garcia and David Grisman
12/08/91
Warfield Theater
San Francisco, CA

source:  sbd > dat > cdr > eac > shn

Disc 1 - Set One
01 Shady Grove
02 Sitting Here In Limbo
03 Rosalie McFall

Disc 2 - Set Two
01 Red Rockin' Chair (1)
02 Troubled In Mind (1)
"""


def test_a_setlist_without_punctuation_after_the_number(tmp_path, cfg):
    """"01 Shady Grove" is a track line; it does not need a dot after the 01."""
    from jamp.infofile import select_info_file

    import fixtures
    folder = tmp_path / "jg1991-12-8.jg_dg - warfield"
    fixtures.write(folder / "jg+dg91-12-08.txt", JG_DG_INFO)
    info = select_info_file([folder / "jg+dg91-12-08.txt"], folder_name=folder.name).chosen
    assert info is not None
    assert [t.title for t in info.tracks][:3] == [
        "Shady Grove", "Sitting Here In Limbo", "Rosalie McFall"]
    # "Disc 1 - Set One" says both things at once.
    assert (info.tracks[0].disc, info.tracks[0].set_no) == (1, 1)
    assert info.tracks[-1].set_no == 2
    assert info.venue == "Warfield Theater"
    assert (info.city, info.state) == ("San Francisco", "CA")
    assert [c.date.isoformat() for c in info.date_candidates] == ["1991-12-08"]


def test_the_band_can_come_from_the_info_file(tmp_path, cfg):
    """Empty tags, an unhelpful folder name, but the info file opens by
    naming the act."""
    import fixtures
    from jamp.analyze import analyze_show
    from jamp.scan import scan

    root = tmp_path / "lib"
    # A folder name that says nothing about who played: only the info file does.
    folder = root / "Jerry Garcia" / "1991-12-8 - warfield"
    for i in (1, 2, 3):
        fixtures.make_mp3(folder / ("d1t%02d.mp3" % i))
    fixtures.write(folder / "info.txt", JG_DG_INFO)
    a = analyze_show(scan(root, cfg).shows[0], cfg, today=TODAY)
    assert a.band.abbrev == "jgdg"
    assert a.band.matched_by == "info_file"
    assert a.date.iso == "1991-12-08"
    assert a.source.value == "sbd"


@pytest.mark.parametrize("name,expected", [
    # "jg" is Jerry Garcia solo...
    ("jg1982-03-13 - keystone", "jg"),
    # ...and also the family filing prefix, where the act follows the date.
    ("jg1989-1-27.jgb - orpheum theatre, sf, ca sbd", "jgb"),
    ("jg1989-12-2.jgb with clemons - warfield, sf, ca - sbd", "jgb"),
    ("jg1980-02-02.jgb.fob.menke.d2t05", "jgb"),
    ("jg1991-12-8.jg_dg - warfield", "jgdg"),
    ("jg+dg92-05-10.shnf", "jgdg"),
    # A name that says the band outright still wins on its own terms.
    ("JGB1991-11-09-FLAC", "jgb"),
    ("jgb1976-05-21 - Don't Let Go - D1", "jgb"),
    # The other two Garcia side projects, as they are actually filed.
    ("Reconstruction1979-07-07", "rec"),
    ("Reconstruction_1979-06-16_SBD", "rec"),
    ("rec1979-07-07.sbd", "rec"),
    ("Legion of Mary 1975-06-06", "lom"),
    ("lom1975-06-06", "lom"),
])
def test_the_jerry_garcia_family_prefix(cfg, name, expected):
    from jamp.bands import resolve_band
    assert resolve_band(name, cfg, parent_artist_dir="Jerry Garcia").abbrev == expected


def test_set_and_track_spelled_out_in_the_filename(tmp_path, cfg):
    """Set1T03 must keep its numbering: renumbering it from its position would
    invent a running order the recording does not have."""
    import fixtures
    from jamp.analyze import analyze_show
    from jamp.audio import parse_track_name
    from jamp.phase1 import plan_numbering
    from jamp.scan import scan

    assert (parse_track_name("Set1T03.flac").set_no,
            parse_track_name("Set1T03.flac").track) == (1, 3)
    assert (parse_track_name("Disc2 T04.flac").disc,
            parse_track_name("Disc2 T04.flac").track) == (2, 4)

    root = tmp_path / "lib"
    folder = root / "Jerry Garcia" / "JGB1991-11-09-FLAC"
    for name in ("Set1T03", "Set1T04", "Set1T06", "Set2T03", "Set2T05"):
        fixtures.make_flac(folder / (name + ".flac"), bits=16)
    a = analyze_show(scan(root, cfg).shows[0], cfg, today=TODAY)
    numbering = [(f.path.name, kind, number, track)
                 for f, kind, number, track, _ in plan_numbering(a)]
    assert ("Set1T06.flac", "s", 1, 6) in numbering
    assert ("Set2T05.flac", "s", 2, 5) in numbering
    assert "INCOMPLETE_SHOW" in a.issue_codes
    detail = " ".join(i.detail for i in a.issues if i.code == "INCOMPLETE_SHOW")
    assert "set 1 is missing 1, 2, 5" in detail


def test_a_folder_of_only_disc_folders_is_one_show(tmp_path, cfg):
    """"Roadsongs/Disc 1, Disc 2" is one release, not two concerts.  The
    container holds no audio itself, so nothing folded the discs together."""
    import fixtures
    from jamp.scan import scan

    root = tmp_path / "lib"
    release = root / "Derek Trucks" / "Roadsongs"
    for disc in (1, 2):
        for i in (1, 2):
            fixtures.make_flac(release / ("Disc %d" % disc) / ("%02d Song.flac" % i),
                               bits=16)
    shows = scan(root, cfg).shows
    assert len(shows) == 1
    assert shows[0].path == release
    assert sorted(p.name for p in shows[0].disc_dirs) == ["Disc 1", "Disc 2"]
    assert len(shows[0].files) == 4


def test_disc_folders_beside_a_dated_sibling_still_separate(tmp_path, cfg):
    """A container mixing disc folders with a real show folder is not folded."""
    import fixtures
    from jamp.scan import scan

    root = tmp_path / "lib"
    box = root / "grateful dead" / "Spring 1990"
    for name in ("1990-03-14 Landover", "1990-03-18 Hartford"):
        for i in (1, 2):
            fixtures.make_flac(box / name / ("%02d Song.flac" % i), bits=16)
    shows = scan(root, cfg).shows
    assert len(shows) == 2


def test_a_year_folder_is_filing_not_a_release(tmp_path, cfg):
    """Phish is filed by year.  Phish/2012/ is not a box set, and its shows
    keep their ordinary names rather than being renamed as release tracks."""
    import fixtures
    from jamp.phase1 import build_plans
    from jamp.scan import is_year_dir

    for name in ("2012", "1980s", "90s", "1972-1974"):
        assert is_year_dir(name), name
    for name in ("Spring 1990 (The Other One) (2014)", "Pure Jerry #4", "Disc 1"):
        assert not is_year_dir(name), name

    root = tmp_path / "lib"
    for day in ("2012.06.16 -- Bader Field -- Atlantic City, NJ",
                "2012.06.17 -- Bader Field -- Atlantic City, NJ"):
        for i in (1, 2):
            fixtures.make_flac(root / "Phish" / "2012" / day / ("ph120616d1_%02d.flac" % i),
                               bits=16)
    plans = build_plans(root, cfg, today=TODAY)
    assert len(plans) == 2
    for p in plans:
        assert p.show.release_dir is None
        assert p.new_folder_name.startswith("ph2012-06-1"), p.new_folder_name


def test_ffp_hiding_behind_a_txt_extension_is_found(shows):
    show = shows["MMJ2006-06-16..4011s bonaroo"]
    assert [p.name for p in show.sidecars["ffp"]] == ["fingerprint.ffp.txt"]


def test_per_disc_md5_is_found(shows):
    show = shows["um2001-06-02.shnf"]
    assert [p.name for p in show.sidecars["md5"]] == ["um2001-06-02d1.md5"]


# --- info file selection ---------------------------------------------------

def test_a_news_clipping_is_not_used_as_the_info_file(shows):
    show = shows["Grateful Dead 10-31-91"]
    sel = select_info_file(show.texts, folder_name=show.name)
    assert sel.chosen is None


def test_the_real_info_file_wins_over_the_clutter(shows, analyses):
    a = analyses["MMJ2006-06-16..4011s bonaroo"]
    assert a.info.chosen is not None
    assert a.info.chosen.path.name == "info.txt"
    ignored = {p.name for p, _ in a.info.considered if p != a.info.chosen.path}
    assert "band comments.txt" in ignored


def test_a_torrent_marker_file_is_recorded(analyses):
    a = analyses["MMJ2006-06-16..4011s bonaroo"]
    assert a.info.torrent_markers
    assert "TORRENT_MARKER" in a.issue_codes


def test_info_file_fields_and_setlist(analyses):
    info = analyses["MMJ2006-06-16..4011s bonaroo"].info.chosen
    assert info.fields["taper"] == "Charlie Miller"
    assert info.city == "Manchester"
    assert info.state == "TN"
    assert [t.title for t in info.tracks][:2] == ["Wordless Chorus", "It Beats 4 U"]


def test_setlist_sets_are_tracked():
    tracks = parse_setlist([
        "Set 1:", "01. Wordless Chorus", "02. Gideon",
        "Set 2:", "01. Dondante (12:04)",
    ])
    assert [(t.set_no, t.number, t.title) for t in tracks] == [
        (1, 1, "Wordless Chorus"), (1, 2, "Gideon"), (2, 1, "Dondante")]


def test_taper_slug_heuristic():
    """Without the config table: surname for a name, the handle otherwise."""
    assert taper_slug("Charlie Miller") == ("miller", False)
    assert taper_slug("padelimike") == ("padelimike", False)
    assert taper_slug("Charlie Miller / DAT master") == ("miller", False)
    assert taper_slug("") == (None, False)


def test_equipment_is_never_mistaken_for_a_taper():
    """A transfer line is a deck chain, not a person; it must not become a slug."""
    assert taper_slug("Tascam DA-20MKII > HHb CDR-830,") == (None, False)
    assert taper_slug("Schoeps mk4 > kc5 > cmc6 > Sound Devices 722") == (None, False)
    assert taper_slug("recorded on a very nice sounding rig indeed") == (None, False)


def test_the_config_taper_table_pins_the_spelling(cfg):
    """The table is authoritative, so one taper gets exactly one slug."""
    assert taper_slug("Charlie Miller", cfg) == ("miller", True)
    assert taper_slug("charlie miller/ dat", cfg) == ("miller", True)
    assert taper_slug("PADeliMike", cfg) == ("padelimike", True)
    assert taper_slug("VWMULE", cfg) == ("vwmule", True)
    # Not in the table: still resolved, but flagged as a guess.
    assert taper_slug("Some Unknown Taper", cfg) == ("taper", False)


# --- classification --------------------------------------------------------

@pytest.mark.parametrize("folder,kind", [
    ("mmj2003-09-26.shnf", UNOFFICIAL),
    ("mmj2005-06-04.ak40.flac16", UNOFFICIAL),
    ("MMJ2006-06-16..4011s bonaroo", UNOFFICIAL),
    ("ph2018-12-28.New.York.NY.padelimike.akg414.flac2496", UNOFFICIAL),
    ("Grateful Dead 10-31-91", UNOFFICIAL),
    ("My Morning Jacket 2023-11-03 Fox Theatre, Atlanta, GA [FLAC24]", OFFICIAL),
    ("ph2018-12-28 Madison Square Garden, New York, NY [FLAC]", OFFICIAL),
    ("Dave's Picks 16 [FLAC]", OFFICIAL),
])
def test_origin(analyses, folder, kind):
    assert analyses[folder].classification.kind == kind


def test_two_folders_for_the_same_show_are_told_apart(analyses):
    """Same date, same venue: one is a torrent, one is the official download."""
    torrent = analyses["ph2018-12-28.New.York.NY.padelimike.akg414.flac2496"]
    official = analyses["ph2018-12-28 Madison Square Garden, New York, NY [FLAC]"]
    assert torrent.classification.kind == UNOFFICIAL
    assert official.classification.kind == OFFICIAL
    assert torrent.date.date == official.date.date


def test_store_download_is_not_mistaken_for_a_disc_rip(analyses):
    a = analyses["Phish - 2012-06-28 Noblesville, IN (v0)"]
    assert a.classification.shape == STORE


def test_disc_rip_is_detected(analyses):
    a = analyses["Dave's Picks 16 [FLAC]"]
    assert a.classification.shape == DISC_RIP
    assert not a.classification.tag_date_admissible


def test_per_set_album_tags_are_one_release(tmp_path):
    """A store tags each set separately: "2011/10/29 I Atlanta, GA" and
    "... II Atlanta, GA" are one show, not two albums."""
    import fixtures
    from jamp.audio import read_audio_file
    from jamp.classify import album_key, profile_tags

    assert album_key("2011/10/29 I Atlanta, GA") == album_key("2011/10/29 II Atlanta, GA")
    files = []
    for i, album in enumerate(["2011/10/29 I Atlanta, GA", "2011/10/29 II Atlanta, GA"], 1):
        path = fixtures.make_flac(
            tmp_path / ("um111029d%d_01.flac" % i), bits=16,
            tags={"ARTIST": "Umphrey's McGee", "ALBUM": album,
                  "TITLE": "Song %d" % i, "TRACKNUMBER": str(i)},
        )
        files.append(read_audio_file(path))
    assert profile_tags(files).complete_and_consistent


def test_a_set_marker_is_not_part_of_the_place():
    from jamp.naming import strip_set_marker
    assert strip_set_marker("I Atlanta, GA") == "Atlanta, GA"
    assert strip_set_marker("II Noblesville, IN") == "Noblesville, IN"
    assert strip_set_marker("Set 2 Chicago, IL") == "Chicago, IL"
    # A venue that merely begins with one of those letters is untouched.
    assert strip_set_marker("Vic Theatre, Chicago, IL") == "Vic Theatre, Chicago, IL"
    assert strip_set_marker("Irving Plaza, New York, NY") == "Irving Plaza, New York, NY"


def test_numeric_song_titles_are_real_titles(tmp_path):
    """"555" and "2001" are Phish songs; demanding letters convicted a whole
    folder of having machine-generated titles."""
    import fixtures
    from jamp.audio import read_audio_file
    from jamp.classify import profile_tags

    names = ["The Moma Dance", "555", "Tube", "2001", "Character Zero"]
    files = []
    for i, title in enumerate(names, start=1):
        path = fixtures.make_flac(
            tmp_path / ("ph181103d1_%02d.flac" % i), bits=16,
            tags={"ARTIST": "Phish", "ALBUM": "2018/11/03 Las Vegas, NV",
                  "TITLE": title, "TRACKNUMBER": str(i)},
        )
        files.append(read_audio_file(path))
    assert profile_tags(files).titles_look_real


def test_machine_made_titles_are_not_real_titles(tmp_path):
    import fixtures
    from jamp.audio import read_audio_file
    from jamp.classify import profile_tags

    files = []
    for i, title in enumerate(["d1t01", "Track 02", "03"], start=1):
        path = fixtures.make_flac(
            tmp_path / ("x%02d.flac" % i), bits=16,
            tags={"ARTIST": "Phish", "ALBUM": "x", "TITLE": title,
                  "TRACKNUMBER": str(i)},
        )
        files.append(read_audio_file(path))
    assert not profile_tags(files).titles_look_real


def test_the_release_year_trap_does_not_fire(analyses):
    """Dave's Picks 16 is tagged DATE 2015. That is the release year, not a show."""
    a = analyses["Dave's Picks 16 [FLAC]"]
    assert a.classification.tag_profile.date_values == ("2015",)
    assert "TAG_DATE_REFUSED" in a.issue_codes
    assert a.date.date != dt.date(2015, 1, 1)


def test_a_rip_log_names_the_show_without_its_own_dates_leaking(analyses):
    """The XLD log holds the real show date - and a version string and an
    extraction timestamp that are date-shaped and are not the show."""
    a = analyses["Dave's Picks 16 [FLAC]"]
    assert a.date.iso == "1973-03-28"
    assert dt.date(2014, 11, 29) not in a.date.distinct_dates   # version 20141129
    assert dt.date(2015, 11, 1) not in a.date.distinct_dates    # logfile timestamp
    assert not a.date.conflicts


def test_one_volume_of_a_multi_date_series_is_still_one_show(analyses):
    """Dave's Picks usually spans several nights; volume 16 does not."""
    a = analyses["Dave's Picks 16 [FLAC]"]
    assert "MULTI_DATE_RELEASE" not in a.issue_codes
    assert "SINGLE_DATE_OF_A_SERIES" in a.issue_codes


def test_a_release_really_spanning_dates_is_still_blocked(tmp_path, cfg):
    import fixtures

    root = tmp_path / "lib"
    d = root / "grateful dead" / "Road Trips Vol 2"
    for i, day in enumerate(("1974-06-16", "1974-06-18", "1974-06-20"), start=1):
        fixtures.make_flac(d / ("%d01 Song.flac" % i), bits=16,
                           tags={"ARTIST": "Grateful Dead", "ALBUM": "Road Trips Vol 2",
                                 "TITLE": "Song %d" % i, "TRACKNUMBER": "1",
                                 "DISCNUMBER": str(i), "DATE": "2008"})
    fixtures.write(d / "info.txt",
                   "Road Trips Vol 2\n1974-06-16 Des Moines\n1974-06-18 Louisville\n"
                   "1974-06-20 Miami\n")
    from jamp.analyze import analyze_show
    from jamp.scan import scan
    a = analyze_show(scan(root, cfg).shows[0], cfg, today=TODAY)
    assert "MULTI_DATE_RELEASE" in a.issue_codes


def test_undated_folders_are_never_given_a_date(analyses):
    for name in ("UM - Hauntlanta", "OHMphrey - Posthaste",
                 "Umphreys McGee Bonnaroo 2008",
                 "My Morning Jacket- iTunes Session(Christmas)"):
        a = analyses[name]
        assert a.date.date is None
        assert a.blocked


def test_a_studio_record_is_recognised_but_a_real_run_is_not(analyses):
    """Both are undated. One has album tags and a give-away name; the other is
    a real Halloween run that simply has no date in it."""
    from jamp.analyze import ALBUM, UNDATED

    itunes = analyses["My Morning Jacket- iTunes Session(Christmas)"]
    hauntlanta = analyses["UM - Hauntlanta"]
    assert itunes.category == ALBUM
    assert "ALBUM_NOT_A_SHOW" in itunes.issue_codes
    assert hauntlanta.category == UNDATED
    assert "ALBUM_NOT_A_SHOW" not in hauntlanta.issue_codes


def test_a_live_release_is_not_mistaken_for_a_studio_album(analyses):
    """Dave's Picks has album-shaped tags, but it is a concert recording."""
    from jamp.analyze import ALBUM

    a = analyses["Dave's Picks 16 [FLAC]"]
    assert a.category != ALBUM
    assert "ALBUM_NOT_A_SHOW" not in a.issue_codes


def test_a_side_project_studio_album_is_recognised(analyses):
    from jamp.analyze import ALBUM

    a = analyses["OHMphrey - Posthaste"]
    assert a.category == ALBUM
    assert a.blocked


def test_shn_is_reported_as_untaggable(analyses):
    a = analyses["mmj2003-09-26.shnf"]
    assert "SHN_NO_TAGS" in a.issue_codes
    assert all(f.tag_support == "none" for f in a.show.files if f.ext == ".shn")


def test_format_token_comes_from_the_stream_not_the_name(analyses):
    assert analyses["mmj2005-06-04.ak40.flac16"].fmt == "flac16"
    assert analyses["MMJ2012-09-12.MMJ-Wiltern-9-12-12"].fmt == "flac24"
    assert analyses["Phish - 2012-06-28 Noblesville, IN (v0)"].fmt == "mp3"
    assert analyses["mmj2003-09-26.shnf"].fmt == "shn"


def test_an_etree_catalogue_number_settles_the_origin(cfg, tmp_path):
    """jg80-08-09.029088.jgb... - no store or label numbers a folder that way.

    These folders are otherwise so sparse that neither side reached the score
    the classifier needs, and the whole show was held back as UNKNOWN_ORIGIN.
    """
    from jamp.classify import UNOFFICIAL, classify
    res = classify(cfg, "jg80-08-09.029088.jgb.partial.sbd.jupille.sbeok.t-flac16",
                   files=[], sidecars={})
    assert res.kind == UNOFFICIAL
    assert any(s.name.endswith("etree_source_id") for s in res.signals)


def test_a_plain_number_in_a_name_is_not_a_catalogue_number(cfg, tmp_path):
    """The signal needs the etree field shape, not any digits in the name."""
    from jamp.classify import classify
    res = classify(cfg, "GarciaLive Volume 20", files=[], sidecars={})
    assert not any(s.name.endswith("etree_source_id") for s in res.signals)


def test_ignored_folders_are_never_scanned(cfg, tmp_path):
    """Scope lives in the config, not in whichever CLI flag was used.

    A bare name matches any folder so called; a path matches only that one, so
    "Phish/Studio, Collections & Bonus Discs" does not also skip "GD/Studio".
    """
    from dataclasses import replace as _replace
    import fixtures
    from jamp.scan import scan

    for rel in ("Phish/2012/ph2012-06-19",
                "Phish/Collections/Live Bait 01",
                "Phish/Jazz/whatever",
                "GD/Collections/keep me"):
        fixtures.make_flac(tmp_path / rel / "01 Song.flac", bits=16, tags={})

    cfg2 = _replace(cfg, settings=_replace(
        cfg.settings, ignore_folders=("Jazz", "Phish/Collections")))
    names = {s.name for s in scan(tmp_path, cfg2, read_tags=False).shows}
    assert "ph2012-06-19" in names
    assert "Live Bait 01" not in names   # ignored by path
    assert "whatever" not in names       # ignored by bare name
    assert "keep me" in names            # same name, different artist: kept


def test_an_etree_catalogue_number_settles_it_however_well_tagged(cfg):
    """etree hosts no official releases, so a shnid proves this is a fan copy.

    "tags_complete_consistent" is tied for the biggest official signal, and on
    a carefully tagged audience tape it was enough to stall the folder at
    UNKNOWN despite the catalogue number sitting in its name.
    """
    from jamp.classify import UNOFFICIAL, classify
    res = classify(cfg, "ph1991-03-22.23192.flac16", files=[], sidecars={})
    assert res.kind == UNOFFICIAL
    assert any("etree hosts no official releases" in n for n in res.notes)


def test_a_lineage_line_alone_does_not_settle_it(cfg):
    """An official release can be ripped, documented and seeded like anything
    else, so how a copy travelled says nothing about who made the recording."""
    from jamp.classify import classify
    res = classify(cfg, "Dave's Picks 16", files=[], sidecars={},
                   info_has_lineage=True, torrent_marker=True)
    assert not any("etree hosts no official" in n for n in res.notes)


def test_an_official_series_is_not_flipped_by_a_stray_info_file(cfg):
    """Live Bait ships correctly tagged; an info file must not override that."""
    from jamp.classify import classify
    res = classify(cfg, "Dave's Picks 16", files=[], sidecars={},
                   info_has_lineage=True)
    assert res.kind != "UNOFFICIAL" or res.series is None


def test_a_store_marker_is_not_overridden_by_a_lineage_line(cfg):
    """A matrix of the LivePhish feed carries both; the lineage decides nothing."""
    from jamp.classify import classify
    res = classify(cfg, "ph2003-07-15", files=[], sidecars={},
                   info_has_lineage=True, store_marker="livephish")
    assert not any("etree hosts no official" in n for n in res.notes)


def test_an_unsettled_folder_with_no_series_or_store_reads_as_a_torrent(cfg):
    """An official release names a series or a store.  Neither means fan copy."""
    from jamp.classify import UNOFFICIAL, classify
    res = classify(cfg, "Phish 1993-03-28", files=[], sidecars={})
    assert res.kind == UNOFFICIAL
    assert any("names a series or a store" in n for n in res.notes)


def test_an_unsettled_official_series_is_never_defaulted_away(cfg):
    """Live Bait and Dave's Picks stay out of the torrent default."""
    from jamp.classify import UNOFFICIAL, classify
    res = classify(cfg, "Dave's Picks 16", files=[], sidecars={})
    assert res.kind != UNOFFICIAL


def test_a_source_named_in_the_folder_is_taper_vocabulary(cfg):
    """No label ships a product called "4-17-82 Grateful Dead sbd flac".

    This folder scored 41 official against 0 torrent - carried entirely by
    having tidy tags - and was called an official release.
    """
    from jamp.classify import OFFICIAL, classify
    res = classify(cfg, "4-17-82 Grateful Dead sbd flac", files=[], sidecars={})
    assert res.kind != OFFICIAL
    assert any(s.name.endswith("source_token_in_name") for s in res.signals)


def test_tidy_tags_alone_cannot_make_a_folder_official(cfg):
    """Careful tagging identifies a careful person, not a label."""
    from jamp.classify import OFFICIAL, classify
    res = classify(cfg, "Phish 1993-02-27", files=[], sidecars={})
    assert res.kind != OFFICIAL


def test_a_date_first_name_does_not_earn_the_full_band_name_signal(cfg):
    """"4-17-82 Grateful Dead sbd flac" does not open with the band name.

    The band resolver works on the date-masked name and reports the alias as
    leading, which had the classifier crediting a taper's folder with the shape
    of a store download.
    """
    from jamp.classify import classify
    res = classify(cfg, "4-17-82 Grateful Dead sbd flac", files=[], sidecars={},
                   band_matched_by="alias_leading")
    assert not any(s.name.endswith("full_band_name") for s in res.signals)
    res2 = classify(cfg, "Grateful Dead 1982-04-17 Hartford", files=[], sidecars={},
                    band_matched_by="alias_leading")
    assert any(s.name.endswith("full_band_name") for s in res2.signals)


def test_a_store_marker_beside_audience_evidence_is_lineage_not_a_publisher(cfg):
    """There has never been a LivePhish audience release.

    A folder naming the store AND carrying a mic is a fan matrix built on the
    board feed, quoting the store in its lineage.  Paying the store its full
    30 points there made every such matrix look like an official release.
    """
    from jamp.classify import classify
    res = classify(cfg, "ph2003-07-15 matrix", files=[], sidecars={},
                   store_marker="livephish", mic_token=True)
    assert not any(s.name == "official.store_marker" for s in res.signals)
    assert any(s.name == "official.store_marker_ignored" for s in res.signals)
    # ...but a store marker on its own is still the strong signal it was.
    plain = classify(cfg, "ph2003-07-15", files=[], sidecars={},
                     store_marker="livephish")
    assert any(s.name == "official.store_marker" for s in plain.signals)


def test_a_second_run_will_not_write_the_same_report_directory(tmp_path):
    """Two runs at once leave a CSV that parses but is quietly wrong."""
    import pytest
    from jamp.report import OutDirBusy, run_lock
    with run_lock(tmp_path, "phase1"):
        with pytest.raises(OutDirBusy):
            with run_lock(tmp_path, "phase1"):
                pass
    # The lock is released afterwards, so the next run proceeds normally.
    with run_lock(tmp_path, "phase1"):
        pass


def test_a_long_run_keeps_its_lock_however_old_it_is(tmp_path):
    """Two hours was the stale limit, and phase 0 --verify-audio takes longer:
    a second run took the live run's lock over."""
    import os

    import pytest
    from jamp.report import LOCK_NAME, OutDirBusy, run_lock

    (tmp_path / LOCK_NAME).write_text(
        "phase0 pid %d | 2020-01-01T00:00:00" % os.getpid(), encoding="utf-8")
    with pytest.raises(OutDirBusy):
        with run_lock(tmp_path, "phase1"):
            pass


def test_a_lock_left_by_a_run_that_has_ended_is_taken_over(tmp_path):
    import os
    import subprocess
    import sys

    from jamp.report import LOCK_NAME, run_lock

    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended.wait()
    from datetime import datetime
    (tmp_path / LOCK_NAME).write_text(
        "phase1 pid %d | %s" % (ended.pid, datetime.now().isoformat(timespec="seconds")),
        encoding="utf-8")
    with run_lock(tmp_path, "phase1"):
        assert "pid %d" % os.getpid() in (tmp_path / LOCK_NAME).read_text(encoding="utf-8")
    assert not (tmp_path / LOCK_NAME).exists()


def test_a_run_does_not_delete_a_lock_that_is_no_longer_its_own(tmp_path):
    from jamp.report import LOCK_NAME, run_lock

    with run_lock(tmp_path, "phase1"):
        (tmp_path / LOCK_NAME).write_text("phase0 pid 1 | now", encoding="utf-8")
    assert (tmp_path / LOCK_NAME).read_text(encoding="utf-8") == "phase0 pid 1 | now"


def test_our_own_canonical_name_does_not_look_unofficial(cfg):
    """Every name this pipeline writes states the source.

    Counting that as taper vocabulary means each folder we rename gains an
    unofficial signal the next time it is read, so official releases drift out
    of OFFICIAL one commit at a time.  It flipped seven committed My Morning
    Jacket folders, which then wanted their GENRE rewritten.
    """
    from jamp.classify import classify
    ours = classify(cfg, "mmj2023-11-09.sbd.flac16 - Chicago, IL",
                    files=[], sidecars={})
    assert not any(s.name.endswith("source_token_in_name") for s in ours.signals)
    # A name we did not write still earns it: no format token, so not ours.
    theirs = classify(cfg, "4-17-82 Grateful Dead sbd flac", files=[], sidecars={})
    assert any(s.name.endswith("source_token_in_name") for s in theirs.signals)


def test_venue_and_city_on_one_line_is_not_confused_with_the_band_above():
    """"Jerry Garcia Band\nOrpheum Theater, Boston, MA" - real shape, real bug.

    The venue picker assumed venue and city/state always sit on separate
    lines and took whatever was directly above a "City, ST" match.  Here the
    venue carries its own city and state on one line, so the line above is
    just the band's name - and "Jerry Garcia Band, Boston, MA" was written as
    the venue on a real committed folder.
    """
    from jamp.infofile import parse_info_text
    from pathlib import Path
    text = (
        "This is flac encoded & tagged version of shnid: 17069\n\n"
        "Jerry Garcia Band\n"
        "Orpheum Theater, Boston, MA\n"
        "July 25, 1980 (Early Show)\n"
    )
    info = parse_info_text(Path("jg.txt"), text, "utf-8", score=1.0)
    assert info.venue == "Orpheum Theater"
    assert info.city == "Boston"
    assert info.state == "MA"


def test_a_true_two_line_venue_still_works():
    """Venue alone on one line, city/state alone on the next - the old shape."""
    from jamp.infofile import parse_info_text
    from pathlib import Path
    text = "Grateful Dead\nBarton Hall\nIthaca, NY\n1977-05-08\n"
    info = parse_info_text(Path("gd.txt"), text, "utf-8", score=1.0)
    assert info.venue == "Barton Hall"


def test_band_date_venue_city_state_packed_onto_one_line():
    """"Jerry Garcia Band, 24-July-1980, Bushnell Auditorium, Hartford, CT".

    Only the segment immediately before the city/state is the venue - not
    everything on the line.  A real committed folder had this collapse to
    "Jerry Garcia Band, 24-July-1980, Bushnell Auditorium" as its "venue".
    """
    from jamp.infofile import parse_info_text
    from pathlib import Path
    text = ("shnid 21536\n\n"
           "Jerry Garcia Band, 24-July-1980, Bushnell Auditorium, Hartford, CT\n")
    info = parse_info_text(Path("a.txt"), text, "utf-8", score=1.0)
    assert info.venue == "Bushnell Auditorium"
    assert info.city == "Hartford" and info.state == "CT"


def test_a_semicolon_separates_venue_from_city_too():
    """"Roseland Ballroom; New York, NY" - not every file uses a comma."""
    from jamp.infofile import parse_info_text
    from pathlib import Path
    text = "shnid 10962\n\nJerry Garcia Band\nRoseland Ballroom; New York, NY\n6/1/83\n"
    info = parse_info_text(Path("b.txt"), text, "utf-8", score=1.0)
    assert info.venue == "Roseland Ballroom"


def test_wma_is_recognised_as_audio():
    """.wma was missing from AUDIO_EXTS, so a WMA-only show was invisible to
    the scanner - not blocked, not reported, just silently skipped, with no
    trace in any report.  mutagen already reads and writes ASF tags through
    the same generic path FLAC/Vorbis use, so only the extension list needed
    fixing.
    """
    from jamp.audio import AUDIO_EXTS
    assert ".wma" in AUDIO_EXTS


# --------------------------------------------------------------------------
# a container is not a show
# --------------------------------------------------------------------------

def test_a_stray_file_does_not_turn_a_year_folder_into_a_show(tmp_path, cfg):
    """One loose file in Phish/1997 must not rename the year folder after it.

    Found on the real library: a single soundcheck sitting beside 32 show
    folders proposed 'Phish/1997' -> 'ph1997-03-01', burying every show
    underneath one show's name.
    """
    import fixtures
    from jamp.phase1 import build_plans
    from jamp.phase1 import SKIP_BLOCKED

    root = tmp_path / "lib"
    year = root / "Phish" / "1997"
    for day in ("1997-02-16 Amsterdam", "1997-02-17 Cologne", "1997-03-01 Hamburg"):
        for i in (1, 2):
            fixtures.make_flac(year / day / ("ph1997d1t%02d.flac" % i), bits=16)
    fixtures.make_flac(year / "Soundcheck Bass Jam Hamburg 1997-03-01 MK4.flac", bits=16)

    plans = {p.show.name: p for p in build_plans(root, cfg, today=TODAY)}
    assert "1997" in plans, "the stray file must still be reported, never silently skipped"
    year_plan = plans["1997"]
    assert year_plan.status == SKIP_BLOCKED
    codes = [i.code for i in year_plan.analysis.issues]
    assert "CONTAINER_NOT_A_SHOW" in codes
    # the real shows underneath are untouched by the guard
    assert len(plans) == 4


def test_a_show_with_one_misfiled_show_inside_is_still_a_show(tmp_path, cfg):
    """The box-set case that must not regress.

    'Spring 1990 Box Set/19900326 Knickerbocker Arena' holds 26 tracks of its
    own plus one unrelated Download Series show misfiled inside it.  It is a
    genuine show and has to keep being renamed as one.
    """
    import fixtures
    from jamp.phase1 import build_plans, SKIP_BLOCKED

    root = tmp_path / "lib"
    show = root / "grateful dead" / "Spring 1990 Box Set" / "19900326 Knickerbocker Arena Albany, NY"
    for i in range(1, 11):
        fixtures.make_flac(show / ("%02d Song %d.flac" % (i, i)), bits=16)
    nested = show / "Grateful Dead Download Series Vol. 08 1973-12-10"
    for i in (1, 2):
        fixtures.make_flac(nested / ("1-%02d Bertha.flac" % i), bits=16)

    plans = {p.show.name: p for p in build_plans(root, cfg, today=TODAY)}
    parent = plans["19900326 Knickerbocker Arena Albany, NY"]
    assert [i.code for i in parent.analysis.issues].count("CONTAINER_NOT_A_SHOW") == 0
    assert parent.status != SKIP_BLOCKED
    assert "1990-03-26" in parent.new_folder_name
    assert parent.show.child_show_dirs, "the misfiled show inside is still seen"


def test_a_multi_disc_show_is_never_read_as_a_container(tmp_path, cfg):
    """Disc folders fold into their parent, so three discs plus a stray
    bonus track is still one show, not a container with loose audio."""
    import fixtures
    from jamp.scan import scan

    root = tmp_path / "lib"
    show = root / "Phish" / "1998" / "1998-06-30 Den Gra Hal"
    for disc in (1, 2, 3):
        for i in (1, 2):
            fixtures.make_flac(show / ("Disc %d" % disc) / ("%02d Song.flac" % i), bits=16)
    fixtures.make_flac(show / "bonus.flac", bits=16)

    shows = scan(root, cfg).shows
    assert len(shows) == 1
    assert shows[0].child_show_dirs == []


# --------------------------------------------------------------------------
# a box set and a taper's bundle are the same shape
# --------------------------------------------------------------------------

def test_a_listed_box_set_is_a_release_and_a_bundle_is_filing(tmp_path, cfg):
    """Holding several shows is not what makes something a release.

    "Europe '72 Complete Recordings" and "Phish Gorge 1998 gotfob" are
    identical in shape - one folder per night, no audio of their own - so
    shape alone gave a taper's two-night bundle a release's naming and left
    its shows a level down, out of step with every other show of that year.
    Being a release is now a stated fact, from config official_series.
    """
    import fixtures
    from jamp.scan import scan

    root = tmp_path / "lib"
    box = root / "grateful dead" / "Europe '72 Complete Recordings"
    for night in ("1972-04-07 Wembley", "1972-04-08 Wembley"):
        for i in (1, 2):
            fixtures.make_flac(box / night / ("gd1972d1t%02d.flac" % i), bits=16)
    bundle = root / "Phish" / "1998" / "Phish Gorge 1998 gotfob"
    for night in ("1998-07-16 The Gorge", "1998-07-17 The Gorge"):
        for i in (1, 2):
            fixtures.make_flac(bundle / night / ("ph1998d1t%02d.flac" % i), bits=16)

    result = scan(root, cfg)
    releases = {p for p, _ in result.multi_show_containers}
    filing = {p for p, _ in result.filing_containers}
    assert box in releases, "a listed box set is a release"
    assert bundle in filing, "an unlisted multi-night bundle is filing"
    assert box not in filing and bundle not in releases

    for s in result.shows:
        if s.path.parent == bundle:
            assert s.filing_parent == bundle, "its shows can be lifted out"
            assert s.release_dir is None
        if s.path.parent == box:
            assert s.release_dir == box
            assert s.filing_parent is None

def test_a_disc_folder_may_be_named_for_what_is_on_it():
    """"Disc 1_Improvisations" is still disc 1.

    The pattern required the name to end at the disc number, so MMW's Ljubljana
    2009 - three folders called Disc 1_Improvisations, Disc 2_Dreamers and
    Disc 3_MMW - was not read as one show in three discs.  Dating each disc by
    hand made each its own show and all three then read as duplicates of one
    another, sharing a band, a date and a format.  This is the same rule that
    already finds a set marker with something after it.
    """
    from jamp.scan import _DISC_DIR

    for name in ("Disc 1_Improvisations", "Disc 3_MMW", "Disc 1", "d2",
                 "Set 1 (flac16)", "CD 2 - encores"):
        assert _DISC_DIR.match(name), name
    # A separator is required after the number, so a volume is not a disc and a
    # release whose name merely ends in a digit is left alone.
    for name in ("Disc 12ABC", "Dave's Picks 16", "Steve Kimock LIVE 2012",
                 "Zero 1993-03-19"):
        assert not _DISC_DIR.match(name), name

def test_a_file_that_is_dead_on_disk_is_caught_before_anything_parses_it(tmp_path):
    """ph1999-07-25 was 855 MB of zeros and settled quietly.

    read_audio_file returned early for .shn without reading a byte, because SHN
    cannot be tagged - so eighteen files containing nothing at all were renamed
    as though they held a show, and only an attempt to convert them noticed.
    The check runs for every container now, costs sixteen bytes, and reports the
    file as unreadable so the folder is held back.
    """
    from jamp.audio import read_audio_file, structural_problem

    empty = tmp_path / "a.shn"
    empty.write_bytes(b"")
    zeros = tmp_path / "b.shn"
    zeros.write_bytes(bytes(4096))
    wrong = tmp_path / "c.shn"
    wrong.write_bytes(b"fLaC" + bytes(64))
    good = tmp_path / "d.shn"
    good.write_bytes(b"ajkg" + bytes(64))

    assert "0 bytes" in structural_problem(empty, ".shn", 0)
    assert "zero bytes" in structural_problem(zeros, ".shn", zeros.stat().st_size)
    assert "not shn data" in structural_problem(wrong, ".shn", wrong.stat().st_size)
    assert structural_problem(good, ".shn", good.stat().st_size) is None

    # And it reaches the folder: an unreadable file is what blocks a show.
    assert read_audio_file(zeros).tag_support == "unreadable"
    assert read_audio_file(good).tag_support == "none"   # SHN: readable, untaggable


def test_a_disc_folder_may_be_only_its_number():
    """jgb91-04-21.dnk holds two folders called "1" and "2".

    Read as separate shows they resolved to one name and were reported as
    duplicates of each other, when they are the two discs of a single night.
    A year or a date must not be swept up with them.
    """
    from jamp.scan import _DISC_DIR

    for name in ("1", "2", "12"):
        assert _DISC_DIR.match(name), name
    for name in ("1999", "2005-03-24", "Vol. 8", "123"):
        assert not _DISC_DIR.match(name), name


def test_an_id3_tag_in_front_of_a_flac_is_not_damage(tmp_path):
    """Plenty of taggers put an ID3v2 tag on a FLAC; mutagen reads it fine.

    The structural check wanted "fLaC" at offset zero and so called 45 good
    files across two Phish shows damaged, which blocked both folders.
    """
    from jamp.audio import structural_problem

    good = tmp_path / "a.flac"
    good.write_bytes(b"ID3" + bytes(7) + bytes(32) + b"fLaC")
    assert structural_problem(good, ".flac", good.stat().st_size) is None

    # Genuinely dead files are still caught.
    dead = tmp_path / "b.flac"
    dead.write_bytes(bytes(4096))
    assert structural_problem(dead, ".flac", dead.stat().st_size)
    wrong = tmp_path / "c.flac"
    wrong.write_bytes(b"OggS" + bytes(64))
    assert structural_problem(wrong, ".flac", wrong.stat().st_size)


@pytest.mark.parametrize("line,venue,city,state", [
    # "Venue - City, ST" on one line is the commonest shape an info file uses,
    # and the greedy city class swallowed the venue with it: the city matched as
    # "Red Rocks Amphitheatre - Morrison", failed the real-city check, and all
    # three fields were dropped together.  Four settled folders were carrying no
    # venue at all because of it.
    ("Red Rocks Amphitheatre - Morrison, CO", "Red Rocks Amphitheatre", "Morrison", "CO"),
    ("Cafe Tomo - Arcata, CA", "Cafe Tomo", "Arcata", "CA"),
    ("The Cat's Cradle - Carrboro, NC", "The Cat's Cradle", "Carrboro", "NC"),
    # A hyphen with no spaces around it belongs to the city's own name.
    ("Ziggy's - Winston-Salem, NC", "Ziggy's", "Winston-Salem", "NC"),
    # The shapes that already worked keep working.
    ("Barton Hall, Ithaca, NY", "Barton Hall", "Ithaca", "NY"),
    ("Roseland Ballroom; New York, NY", "Roseland Ballroom", "New York", "NY"),
])
def test_a_venue_and_city_on_one_line_survive_the_dash(tmp_path, line, venue, city, state):
    from jamp.infofile import parse_info_text

    p = tmp_path / "show.txt"
    text = "Some Band\n1/1/99\n%s\n\nsource: sbd\n" % line
    p.write_text(text, encoding="utf-8")
    info = parse_info_text(p, text, "utf-8", 3)
    assert (info.venue, info.city, info.state) == (venue, city, state)


@pytest.mark.parametrize("line,venue,city", [
    # A spaced dash outranks the commas: it separates the venue block from the
    # city block, and commas inside the venue block belong to the venue.  Taking
    # the last comma segment threw the theatre away and kept the campus.
    ("Anaconda Theater, UCSB - Santa Barbara, CA", "Anaconda Theater, UCSB", "Santa Barbara"),
    ("Red Rocks Amphitheatre - Morrison, CO", "Red Rocks Amphitheatre", "Morrison"),
    # No dash: the last comma segment is still the venue.
    ("Jerry Garcia Band, 24-July-1980, Bushnell Auditorium, Hartford, CT",
     "Bushnell Auditorium", "Hartford"),
    ("Roseland Ballroom; New York, NY", "Roseland Ballroom", "New York"),
    ("Barton Hall, Ithaca, NY", "Barton Hall", "Ithaca"),
])
def test_a_spaced_dash_outranks_the_commas(tmp_path, line, venue, city):
    from jamp.infofile import parse_info_text

    p = tmp_path / "show.txt"
    text = "Some Band\n1/1/99\n%s\n\nsource: sbd\n" % line
    p.write_text(text, encoding="utf-8")
    info = parse_info_text(p, text, "utf-8", 3)
    assert (info.venue, info.city) == (venue, city)


# --- a rip log's track table is not a setlist ---------------------------------

XLD_LOG = """X Lossless Decoder version 20181125 (151.1)

XLD extraction logfile from 2019-03-02 12:30:44 -0500

Grateful Dead / 1972.05.11 - Grote Zaal De Doelen - Rotterdam - HOL (Disc 1)

TOC of the extracted CD
     Track |   Start  |  Length  | Start sector | End sector
    ---------------------------------------------------------
        1  | 00:00:00 | 10:52:66 |         0    |    48965
        2  | 10:52:66 | 07:23:00 |     48966    |    82190
        4  | 22:07:10 | 06:21:27 |     99535    |   128136
"""


def test_a_rip_logs_track_table_is_not_read_as_a_setlist(tmp_path):
    """Every row of the TOC matched the setlist pattern, and 1,010 of them were
    written into the 30 Trips and Europe '72 box sets as titles -
    "22:07:10 | 06:21:27 | 99535 |" where the file had said "Casey Jones"."""
    from jamp.infofile import parse_info_text

    info = parse_info_text(tmp_path / "disc1.log", XLD_LOG, "utf-8", 6.0)
    assert info.tracks == []
    # The table alone, with no ripper header to recognise, is refused as well.
    table = XLD_LOG.split("TOC of the extracted CD")[1].splitlines()
    assert parse_setlist(table) == []


def test_durations_and_question_marks_are_not_titles():
    tracks = parse_setlist([
        "1. Chalk Dust Torture",
        "2. 7:51",
        "3. 2:26-2:28 (spotty)",
        "4. ???",
        "5. >>",
        "6. 2001",
        "7. 12:00 Noon Jam",
    ])
    assert [t.title for t in tracks] == ["Chalk Dust Torture", "2001", "12:00 Noon Jam"]


def test_a_folder_that_cannot_be_listed_is_reported_not_dropped(tmp_path, cfg, monkeypatch):
    """os.walk was told to ignore listing errors, so a folder it could not read
    vanished from every phase with no line in any report."""
    import os

    from jamp import phase1, scan as scan_mod

    root = tmp_path / "lib"
    (root / "Phish" / "locked").mkdir(parents=True)
    real_walk = os.walk

    def walk(top, topdown=True, onerror=None, followlinks=False):
        for item in real_walk(top, topdown=topdown, onerror=onerror, followlinks=followlinks):
            if item[0].endswith("locked"):
                onerror(PermissionError(13, "Access is denied", item[0]))
                continue
            yield item

    monkeypatch.setattr(scan_mod.os, "walk", walk)
    result = scan_mod.scan(root, cfg)
    assert [p for p, _ in result.errors] == [str(root / "Phish" / "locked")]

    phase1.run(root, tmp_path / "out", cfg)
    summary = (tmp_path / "out" / "phase1_summary.txt").read_text(encoding="utf-8")
    assert "could not be read (1)" in summary and "locked" in summary


def test_a_disc_header_spelled_as_a_word_numbers_the_disc():
    """Disc headers took digits only, so "Disc One" / "Disc Two" were not
    headers: the setlist lost its discs, and jgb1976-04-03's second disc was
    given the first disc's titles."""
    tracks = parse_setlist([
        "Disc One (5) 50:14",
        "01. Taper Talk, Crowd & Tuning [1:40]",
        "02. Don't Let Go [18:27] [0:40]",
        "03. [06:49] Chalk Dust Torture",
        "Disc Two (4) 41:52",
        "01. Crowd & Tuning [0:20]",
        "02. A Strange Man [6:20] [1:58]",
        "CD III",
        "01. Deal",
        "Disc 4 - Set Two",
        "01. Mission In The Rain",
    ])
    assert [(t.disc, t.set_no, t.number, t.title) for t in tracks] == [
        (1, None, 1, "Taper Talk, Crowd & Tuning"),
        (1, None, 2, "Don't Let Go"),
        (1, None, 3, "Chalk Dust Torture"),
        (2, None, 1, "Crowd & Tuning"),
        (2, None, 2, "A Strange Man"),
        (3, None, 1, "Deal"),
        (4, 2, 1, "Mission In The Rain"),
    ]


def test_a_word_that_merely_starts_like_a_header_is_not_one():
    tracks = parse_setlist(["Discovery Channel", "1. Song", "Disco Inferno", "2. Other"])
    assert [(t.disc, t.set_no) for t in tracks] == [(None, None), (None, None)]


def test_setlist_lines_lose_their_damage_but_not_their_names():
    tracks = parse_setlist([
        "1. /Catfish John [#10:43]",
        "2. Heavy Metal Jam*->",
        "3. Sittin' In Limbo,",
        "4. AC/DC Bag",
        "5. Gomorrah [5:47] ->",
    ])
    assert [t.title for t in tracks] == [
        "Catfish John", "Heavy Metal Jam->", "Sittin' In Limbo", "AC/DC Bag", "Gomorrah ->"]


def test_cleaning_keeps_what_is_part_of_a_track_name():
    tracks = parse_setlist([
        "1. Speech w/ Gloria Steinem",
        '2. Tuning/"Please turn Merl\'s mic on..."',
        "3. Star (v1) >>",
        "4. Red Rockin' Chair (1)",
        "5. Harry Hood > (Moby Dick) >",
        "6. My Life As a Pez > (02:34) [1]",
    ])
    assert [t.title for t in tracks] == [
        "Speech w/ Gloria Steinem", 'Tuning "Please turn Merl\'s mic on..."', "Star (v1) >>",
        "Red Rockin' Chair", "Harry Hood > (Moby Dick) >", "My Life As a Pez >"]
