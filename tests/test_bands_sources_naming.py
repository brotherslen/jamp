"""Band attribution, source inference and the naming scheme."""
import datetime as dt

import pytest

from jamp.bands import resolve_band
from jamp.naming import (
    build_album_tag,
    build_folder_name,
    build_track_name,
    check_windows_name,
    parse_canonical,
)
from jamp.sources import infer_source


# --- bands -----------------------------------------------------------------

@pytest.mark.parametrize("name,expected,matched_by", [
    # With the date blanked, "mmj" and "um" stand alone as tokens, so the
    # alias rule sees them before the prefix rule does.  Same band either way.
    ("mmj2003-09-26.shnf", "mmj", "alias_leading"),
    ("um2004-06-11.mk4minime.flac16", "um", "alias_leading"),
    # Six digits with no separators: nothing to blank, so the prefix rule runs.
    ("u111105", "um", "prefix"),
    # "UM" is a configured alias, so it matches as an alias before the
    # prefix rule ever sees it - same band, stronger evidence.
    ("UM 2012_04_19 Tulsa, OK V0", "um", "alias_leading"),
    ("My Morning Jacket 2023-11-03 Fox Theatre, Atlanta, GA [FLAC24]", "mmj", "alias_leading"),
    ("Umphreys McGee Bonnaroo 2008", "um", "alias_leading"),
    ("Grateful Dead 10-31-91", "gd", "alias_leading"),
    ("Phish - 2012-06-28 Noblesville, IN (v0)", "ph", "alias_leading"),
])
def test_band_from_the_folder_name(cfg, name, expected, matched_by):
    res = resolve_band(name, cfg)
    assert res.abbrev == expected
    assert res.matched_by == matched_by


@pytest.mark.parametrize("name,expected", [
    # Both spellings standardise on "moos".
    ("om2011-07-30.CA-11.flac16", "moos"),
    ("moos2008-09-26", "moos"),
    ("OHMphrey - Posthaste", "ohm"),
    ("Huey Lewis and the rUMors Summer Camp 5-29-11", "hlr"),
])
def test_side_projects_do_not_inherit_the_parent_band(cfg, name, expected):
    res = resolve_band(name, cfg, parent_artist_dir="Umphrey's McGee")
    assert res.abbrev == expected
    assert res.matched_by != "parent_folder"


def test_parent_folder_is_a_last_resort_and_is_flagged(cfg):
    res = resolve_band("2012_04_13 Athens, GA", cfg, parent_artist_dir="Umphrey's McGee")
    assert res.abbrev == "um"
    assert res.matched_by == "parent_folder"
    assert not res.authoritative


def test_unknown_band_gives_suggestions_but_no_answer(cfg):
    res = resolve_band("Grateful Ded 10-31-91", cfg)
    assert res.band is None
    assert any("gd" in s for s in res.suggestions)


# --- sources ---------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("mmj2005-06-04.ak40.flac16", "aud"),
    ("mmj2007-06-07.AT853RX.flac16", "aud"),
    ("um2004-06-11.mk4minime.flac16", "aud"),
    ("om2011-07-30.CA-11.flac16", "aud"),
    ("MMJ2006-06-16..4011s bonaroo", "aud"),
    ("MMJ2005-11-23FMSBD.flacf", "sbd"),
    ("mmj2006-12-01.Electric_Factory_WXPN_FM_SBD", "sbd"),
    ("mmj2004-05-28.flac16 opera house", None),
    ("gd1973-12-10 s1", None),
])
def test_source_from_the_folder_name(cfg, name, expected):
    assert infer_source(cfg, name).value == expected


def test_matrix_beats_a_microphone(cfg):
    """Phish 12-29-18 MTX has an akg414 in its info file and is still a matrix."""
    res = infer_source(cfg, "Phish 12-29-18 MTX",
                       info_text="Source: AKG414 audience blended with the soundboard")
    assert res.value == "mtx"


def test_audience_the_word_is_not_the_token_aud(cfg):
    res = infer_source(cfg, "gd1973-12-10",
                       stated_source="Matrix of audience and board feeds")
    assert res.value == "mtx"


def test_microphone_written_with_a_space_still_matches(cfg):
    res = infer_source(cfg, "um2011-11-05", stated_source="AKG 414 > Sound Devices 744t")
    assert res.value == "aud"


def test_microphone_and_board_with_no_matrix_is_a_conflict(cfg):
    res = infer_source(cfg, "gd1977-05-08.sbd.ak40")
    assert res.value is None
    assert res.conflict


def test_official_source_is_inferred_and_labelled(cfg):
    res = infer_source(cfg, "ph2023-07-14", is_official=True, provenance_key="livephish")
    assert res.value == "sbd"
    assert res.inferred
    assert res.tag_value.startswith("inferred:")


def test_taper_is_read_as_the_token_before_the_microphone(cfg):
    """etree names run band+date . location . taper . mic . format."""
    from jamp.sources import provenance_from_name
    got, why = provenance_from_name(
        cfg, "ph2018-12-28.New.York.NY.padelimike.akg414.flac2496")
    assert got == "padelimike"
    assert "beside the microphone" in why


def test_a_lone_unexplained_token_is_taken_as_the_taper(cfg):
    from jamp.sources import provenance_from_name
    assert provenance_from_name(cfg, "mmj2021-11-04.leary")[0] == "leary"


def test_a_name_of_nothing_but_mic_and_format_yields_no_taper(cfg):
    from jamp.sources import provenance_from_name
    assert provenance_from_name(cfg, "mmj2005-06-04.ak40.flac16")[0] is None


def test_the_microphone_is_the_fallback_distinguisher(cfg):
    from jamp.sources import mic_as_provenance
    assert mic_as_provenance(cfg, "mmj2005-06-04.ak40.flac16")[0] == "ak40"
    assert mic_as_provenance(cfg, "mmj2012-03-29.dpa4023")[0] == "dpa4023"
    # Nothing to go on is fine - the field is simply left out.
    assert mic_as_provenance(cfg, "gd1977-05-08")[0] is None


def test_a_microphone_glued_to_its_brand_is_still_a_microphone(cfg):
    """Boundary matching means "mk4" cannot match inside "schoepsmk4v", so the
    glued spellings are listed too - otherwise both the source and the taper
    standing in front of the mic are lost."""
    from jamp.sources import provenance_from_name

    name = "mmj2012-08-17.bobbybourbon.schoepsmk4v.flac16"
    assert infer_source(cfg, name).value == "aud"
    assert provenance_from_name(cfg, name)[0] == "bobbybourbon"
    assert infer_source(cfg, "mmj2012-08-03.flac16_mk22").value == "aud"


def test_no_evidence_means_no_source(cfg):
    res = infer_source(cfg, "abb2001-06-03")
    assert res.value is None
    assert not res.inferred


# --- naming ----------------------------------------------------------------

@pytest.mark.parametrize("args,expected", [
    (("gd", dt.date(1977, 5, 8), "sbd", "miller", "flac16"), "gd1977-05-08.sbd.miller.flac16"),
    (("ph", dt.date(2023, 7, 14), "sbd", "livephish", "flac24"), "ph2023-07-14.sbd.livephish.flac24"),
    (("wsp", dt.date(2019, 4, 12), "sbd", "nugs", "mp3"), "wsp2019-04-12.sbd.nugs.mp3"),
    (("abb", dt.date(2001, 6, 3), "aud", None, None), "abb2001-06-03.aud"),
    (("abb", dt.date(2001, 6, 3), None, None, None), "abb2001-06-03"),
])
def test_folder_names_match_the_scheme(args, expected):
    assert build_folder_name(*args).name == expected


def test_placeholders_are_never_written():
    proposal = build_folder_name("gd", dt.date(1977, 5, 8), None, "unknown", "flac16")
    assert proposal.name == "gd1977-05-08.flac16"
    assert proposal.dropped


def test_track_names():
    assert build_track_name("ph", dt.date(2023, 7, 14), 1, 3, ".flac", "s").name == "ph2023-07-14s1t03.flac"
    assert build_track_name("ph", dt.date(2023, 7, 14), 1, 3, "flac", "d").name == "ph2023-07-14d1t03.flac"


def test_a_canonical_name_round_trips():
    parsed = parse_canonical("gd1977-05-08.sbd.miller.flac16")
    assert (parsed.band, parsed.source, parsed.provenance, parsed.fmt) == (
        "gd", "sbd", "miller", "flac16")
    assert build_folder_name(parsed.band, parsed.date, parsed.source,
                             parsed.provenance, parsed.fmt).name == "gd1977-05-08.sbd.miller.flac16"


def test_every_configured_band_reads_back_as_one_of_ours(cfg):
    """A writer needs a reader that agrees with it, for every band there is.

    The parser allowed [a-z]{1,6} while the builder writes slug(band, 8), which
    keeps digits, so no STS9 name ever parsed: every guard asking "did we write
    this?" said no, and a settled folder was reclassified from the tags our own
    commit had rewritten.
    """
    from jamp.naming import build_track_name, parse_canonical_track

    date = dt.date(2012, 1, 21)
    for band in cfg.bands:
        name = build_folder_name(band.abbrev, date, "sbd", "miller", "flac16",
                                 location="Congress Theater, Chicago, IL").name
        parsed = parse_canonical(name)
        assert parsed is not None, name
        assert (parsed.band, parsed.date, parsed.source, parsed.provenance,
                parsed.fmt) == (band.abbrev, date, "sbd", "miller", "flac16")
        track = build_track_name(band.abbrev, date, 2, 7, ".flac").name
        assert parse_canonical_track(track[:-len(".flac")]).band == band.abbrev, track


def test_a_band_ending_in_a_digit_does_not_eat_the_year():
    parsed = parse_canonical("sts92012-01-21.sbd.mp3 - Congress Theater, Chicago, IL")
    assert (parsed.band, parsed.date.isoformat(), parsed.fmt) == ("sts9", "2012-01-21", "mp3")
    assert parse_canonical("2012-01-21.sbd.mp3") is None


def test_a_name_with_tokens_to_spare_is_not_one_of_ours():
    """Looking like our output is not the same as being our output.

    Our builder emits band+date, marker, source, provenance, format and nothing
    else, so a leftover token means an original etree name that merely lands in
    the same shape.  These were read back as canonical, which took the first
    spare token as the provenance and silently dropped the rest: the taper
    "zman" and the source "sbd" were lost, and a microphone and a taping
    position were promoted to provenance in their place.
    """
    assert parse_canonical("um2012-04-13.ccm4v.zman.flac16") is None
    assert parse_canonical("jgb1970-10-21.jgf.sbd.cipollina.flac16") is None
    assert parse_canonical("abb1973-07-28.aud.motb.0234.flac16") is None
    assert parse_canonical(
        "zero1997-12-12.late.akgC61.cooper.miller.109291.flac16") is None
    # The scheme's own full house still parses: marker, source, provenance, format.
    assert parse_canonical("jgb1976-03-06.early.sbd.miller.flac16") is not None
    # And a mic standing in as provenance where no taper is known is untouched.
    assert parse_canonical("um2004-06-11.mk4minime.flac16").provenance == "mk4minime"


def test_a_band_written_with_an_ampersand_matches_its_own_alias(cfg):
    """"&" survived tokenising as an empty token that nothing else produced.

    split_tokens left it in the folder's token list while _alias_tokens dropped
    it, so "Medeski, Scofield, Martin & Wood - Out Louder" compared as
    ['medeski','scofield','martin',''] against ['medeski','scofield','martin',
    'wood'] and failed.  No band written with an ampersand could match by its
    leading alias - the whole MMW folder came back NO_BAND despite every ARTIST
    tag naming them.
    """
    from jamp.bands import resolve_band

    assert resolve_band("Medeski, Scofield, Martin & Wood - Out Louder", cfg).abbrev == "mmws"
    assert resolve_band("Medeski, Martin & Wood - Let's Go Everywhere", cfg).abbrev == "mmw"
    assert resolve_band("King Gizzard & the Lizard Wizard 2019-10-10", cfg).abbrev == "kglw"
    # The longest alias still wins, so Scofield's billing is not read as MMW's.
    assert resolve_band("Medeski Scofield Martin & Wood 2011-01-01", cfg).abbrev == "mmws"


def test_a_song_title_in_a_setlist_is_not_a_board_feed(cfg):
    """Alligator is a song this scene plays often, not a lineage.

    It sat in the soundboard token list among sbd, fm, dsbd and the broadcast
    markers, and it matched the setlist line "06 Alligator >" in a Phil &
    Friends info file.  A microphone was named in the same file, so that raised
    a source conflict - and a conflict writes no source at all, so two nights by
    the same taper on the same rig came out with different names.
    """
    from jamp.sources import infer_source

    setlist = ("Schoeps CCM4V (din) > Lunatec V2 > Benchmark AD2K"
               "   05 Viola Lee Blues >   06 Alligator >")
    got = infer_source(cfg, "phil2012-11-15.flac16", info_text=setlist)
    assert got.value == "aud", got.reason
    assert not got.conflict
    # A real board feed in the same shape is still read as one.
    assert infer_source(cfg, "x", info_text="Source: SBD > DAT > CD").value == "sbd"


def test_removing_a_date_does_not_leave_the_brackets_behind():
    """"Garcia Live Vol. 8 (11-23-91)" is a release title with a date in it.

    Taking the date out left "Garcia Live Vol. 8 ( )", and that went into the
    folder name and the ALBUM tag both - then got read back on the next run,
    which is how a name grows punctuation nobody gave it.
    """
    from jamp.naming import strip_dates_from_place

    assert strip_dates_from_place("Garcia Live Vol. 8 (11-23-91)") == "Garcia Live Vol. 8"
    assert strip_dates_from_place("Fillmore West [1969-02-28]") == "Fillmore West"
    # Brackets carrying something other than a date are left alone.
    assert strip_dates_from_place("The Warfield (early show)") == "The Warfield (early show)"


def test_albumartist_names_the_band_when_artist_is_absent(cfg):
    """A release tagged only that way is ordinary, not defective.

    King Gizzard's 2023 shows carry "King Gizzard & the Lizard Wizard" in
    ALBUMARTIST and no ARTIST at all, and their folder names are just a date and
    a town.  Thirteen of them resolved to no band whatever while their dates and
    venues read perfectly.
    """
    from jamp.bands import resolve_band

    assert resolve_band("2023-06-01 - Pelham, TN", cfg).abbrev is None
    got = resolve_band("2023-06-01 - Pelham, TN", cfg,
                       tag_artist="King Gizzard & the Lizard Wizard")
    assert got.abbrev == "kglw"


def test_m4a_earns_a_format_token():
    """A lossy AAC show must not read like a lossless one.

    m4a had no token, so thirteen King Gizzard shows at 319 kbps were heading
    for names with no format at all.  The list lived in two places - naming's
    VALID_FORMATS and a second copy inside format_token - so adding it to one
    of them changed nothing.
    """
    from jamp.audio import AudioFile, format_token
    from jamp.naming import VALID_FORMATS, build_folder_name
    import datetime as dt
    from pathlib import Path

    assert "m4a" in VALID_FORMATS
    # wma had a second cost: with no token canon.fmt stayed None, so a WMA
    # folder could never be recognised as one this pipeline wrote, and Jerry
    # reported "1 folder still needs work" on every --unnest run for ever.
    assert "wma" in VALID_FORMATS
    files = [AudioFile(path=Path("a.m4a"), ext=".m4a", size=1) for _ in range(3)]
    for f in files:
        f.fmt = "m4a"
    assert format_token(files)[0] == "m4a"
    assert build_folder_name("kglw", dt.date(2023, 6, 1), None, None, "m4a").name ==         "kglw2023-06-01.m4a"


def test_windows_hostile_names_are_flagged():
    assert check_windows_name('gd1977-05-08.sbd.mil?ler')
    assert check_windows_name("gd1977-05-08 ")


def test_album_tag_shape():
    assert build_album_tag(dt.date(2023, 11, 3), "Fox Theatre", "Atlanta", "GA") == \
        "2023-11-03: Fox Theatre, Atlanta, GA"
    assert build_album_tag(dt.date(1990, 3, 1)) == "1990-03-01"


def test_album_falls_back_to_the_date_when_the_venue_is_unknown():
    """A city with no venue reads like a fact we do not have, so it is dropped."""
    assert build_album_tag(dt.date(2011, 11, 5), None, "Milwaukee", "WI") == "2011-11-05"
    assert build_album_tag(dt.date(1990, 3, 1), None, "Milwaukee", "WI",
                           release="Dave's Picks 16") == "1990-03-01 [Dave's Picks 16]"


def test_location_text_is_tidied():
    from jamp.naming import make_location
    # A state code must survive being a substring of the city.
    assert make_location("Murat Egyptian Room", "Indianapolis", "IN") == \
        "Murat Egyptian Room, Indianapolis, IN"
    # A city already inside the venue is not repeated.
    assert make_location("Charlotte Coliseum", "Charlotte", "NC") == \
        "Charlotte Coliseum, NC"
    assert make_location("Waterfront Park, Louisville", "Waterfront Park", "KY") == \
        "Waterfront Park, Louisville, KY"
    # The date is already the front of the folder name.
    assert make_location("Oakland 10-31-91") == "Oakland"
    # A stray set numeral from a store's album tag.
    assert make_location("I Noblesville, IN") == "Noblesville, IN"
    # Some stores write the city first; the venue goes back in front.
    assert make_location("Commerce City, CO - Dicks Sporting Goods Park") == \
        "Dicks Sporting Goods Park, Commerce City, CO"
    # A venue-first value is left alone.
    assert make_location("Eagles Ballroom - Milwaukee, WI") == \
        "Eagles Ballroom, Milwaukee, WI"
    # A venue that merely starts with a roman-numeral letter is not truncated.
    assert make_location("Vic Theatre", "Chicago", "IL") == "Vic Theatre, Chicago, IL"
    assert make_location("Live , Kinetic Playground") == "Live, Kinetic Playground"
    # Windows-illegal characters cannot reach a folder name.
    assert ":" not in (make_location("The Fillmore: East", "New York", "NY") or "")
    assert make_location(None, None, None) is None


def test_show_marker_goes_straight_after_the_date():
    from jamp.naming import build_track_name, parse_canonical
    assert build_folder_name("jgb", dt.date(1976, 3, 6), "sbd", "miller", "flac16",
                             marker="early").name == "jgb1976-03-06.early.sbd.miller.flac16"
    assert build_track_name("jgb", dt.date(1976, 3, 6), 1, 3, ".flac", "d",
                            marker="late").name == "jgb1976-03-06lated1t03.flac"
    parsed = parse_canonical("jgb1976-03-06.early.sbd.miller.flac16")
    assert parsed.marker == "early" and parsed.source == "sbd"
    assert parsed.provenance == "miller" and parsed.fmt == "flac16"


def test_taper_is_read_from_either_side_of_the_microphone(cfg):
    """etree names put the taper before OR after the mic; the act is neither.

    jg80-07-27.120085.jgb.nak700.dyche - the token before the mic is "jgb",
    the act, so the taper is the one after it.  Without this the two 1980-07-27
    tapes (dyche's and angus-ladner's) both lose their taper and collide.
    """
    from jamp.sources import provenance_from_name
    got, _ = provenance_from_name(cfg, "jg80-07-27.120085.jgb.nak700.dyche.t-flac16")
    assert got == "dyche"


def test_co_tapers_are_a_taper_field_and_only_the_first_is_kept(cfg):
    """"schillo-shriver" is two tapers, not a place - no venue is spelled so."""
    from jamp.sources import provenance_from_name
    got, _ = provenance_from_name(
        cfg, "jg80-07-24.jgb.aud.schillo-shriver.077929.sbeok.t-flac16")
    assert got == "schillo"


def test_show_marker_next_to_the_mic_is_not_taken_for_a_taper(cfg):
    """"late" says which performance.  The taper is on the mic's other side."""
    from jamp.sources import provenance_from_name
    got, _ = provenance_from_name(
        cfg, "jg83-06-04.078981.jgb.late.senn421.vita.minches.t-flac16")
    assert got == "vita"


def test_the_taper_beats_the_transferers_who_follow(cfg):
    """etree names run taper . transferer: "glassberg.cohen-jupille".

    Glassberg taped it; Cohen and Jupille only moved it around.  Crediting the
    pair would put the wrong name on the folder.
    """
    from jamp.sources import provenance_from_name
    got, _ = provenance_from_name(
        cfg, "jg80-03-01.017928.jgb.early.fm.glassberg.cohen-jupille.sbeok.t-flac16")
    assert got == "glassberg"


def test_unknown_is_not_taken_for_a_taper(cfg):
    """"unknown" is what the name says when nobody knows who taped it."""
    from jamp.sources import provenance_from_name
    got, _ = provenance_from_name(cfg, "jg80-07-19.122980.aud.unknown.moore-berger.t-flac16")
    assert got == "moore"


def test_a_taping_position_is_not_taken_for_a_taper(cfg):
    """"fob" is where the taper stood, not their name.

    In "fob.cohen+vita" the position sits exactly where the rule looks for a
    taper, so without this the folder was credited to "fob".
    """
    from jamp.sources import provenance_from_name
    got, _ = provenance_from_name(
        cfg, "jg83-06-04.015620.jgb.late.fob.cohen+vita.minches.sbeok.t-flac16")
    assert got == "cohen"


def test_track_filenames_name_the_act_the_folder_left_out(cfg):
    """jg80-07-19... with jgb1980-07-19dNtNN tracks is a JGB show.

    "jg" files the whole Garcia family, so it is not an answer on its own.
    Unlike an ordinary band match it must not stop us reading the files, which
    were all named in one go and agree.
    """
    from jamp.bands import resolve_band
    names = ["jgb1980-07-19d1t%02d.flac" % i for i in range(1, 12)]
    res = resolve_band("jg80-07-19.122980.aud.unknown.moore-berger.t-flac16",
                       cfg, track_names=names)
    assert res.abbrev == "jgb"


def test_the_info_file_names_the_act_when_the_filenames_do_not(cfg):
    from jamp.bands import resolve_band
    res = resolve_band("jg80-07-19.122980.aud.unknown.moore-berger.t-flac16", cfg,
                       info_lines=["Jerry Garcia Band", "1980-07-19", "The Stone, SF, CA"])
    assert res.abbrev == "jgb"


def test_an_unrelated_act_in_the_prose_cannot_hijack_the_folder(cfg):
    """Only another member of the same family may override a filing prefix."""
    from jamp.bands import resolve_band
    res = resolve_band("jg80-07-19.122980.aud.unknown.t-flac16", cfg,
                       info_lines=["My Morning Jacket"])
    assert res.abbrev == "jg"


def test_an_explicit_act_in_the_folder_name_still_wins(cfg):
    """The folder name is checked before the files, so jg_dg stays jgdg."""
    from jamp.bands import resolve_band
    res = resolve_band("jg1991-2-3.jg_dg.sbd - warfield, sf, ca", cfg,
                       track_names=["jgb1991-02-03d1t%02d.flac" % i for i in range(1, 12)])
    assert res.abbrev == "jgdg"


def test_a_venue_starting_with_a_short_word_keeps_it(cfg, tmp_path):
    """"New York, NY" must not become "York, NY".

    The leading-token strip exists to drop a band prefix; blindly dropping any
    short leading word quietly corrupts real place names.
    """
    from jamp.analyze import venue_from_folder_name
    from jamp.scan import ShowFolder
    show = ShowFolder(path=tmp_path / "1976-06-14 New York, NY",
                      root=tmp_path, artist_dir=None)
    got, _ = venue_from_folder_name(show, cfg)
    assert got == "New York, NY"


def test_a_real_band_prefix_is_still_stripped(cfg, tmp_path):
    from jamp.analyze import venue_from_folder_name
    from jamp.scan import ShowFolder
    show = ShowFolder(path=tmp_path / "gd 1973-11-09 Winterland Arena San Francisco, CA",
                      root=tmp_path, artist_dir=None)
    got, _ = venue_from_folder_name(show, cfg)
    assert got.startswith("Winterland Arena")


def test_product_words_are_not_part_of_the_venue(cfg, tmp_path):
    """"Box Set" and "CD REL" describe the product, not the place."""
    from jamp.analyze import venue_from_folder_name
    from jamp.scan import ShowFolder
    show = ShowFolder(
        path=tmp_path / "gd 1973-11-09 Box Set Winterland Arena San Francisco, CA CD REL",
        root=tmp_path, artist_dir=None)
    got, _ = venue_from_folder_name(show, cfg)
    assert "Box Set" not in got and "Winterland Arena" in got


def test_a_state_written_without_a_comma_gets_one():
    """"Winterland Arena San Francisco CA" -> "..., CA", as the rest reads."""
    from jamp.analyze import strip_product_words
    assert strip_product_words("Box Set Winterland Arena San Francisco CA CD REL") \
        == "Winterland Arena San Francisco, CA"
    # An already-correct string is left exactly as it is.
    assert strip_product_words("Barton Hall, Cornell University, Ithaca, NY") \
        == "Barton Hall, Cornell University, Ithaca, NY"


def test_a_lone_word_in_a_catalogued_etree_name_is_the_taper(cfg):
    """gd83-10-08.fob-aud.willy.11734.sbeok.shnf - "willy" taped it.

    A catalogue number means the name is strictly field-structured, so the one
    unexplained word is a person.  Without this it was read as a venue.
    """
    from jamp.sources import provenance_from_name
    got, _ = provenance_from_name(cfg, "gd83-10-08.fob-aud.willy.11734.sbeok.shnf")
    assert got == "willy"


def test_two_loose_words_stay_ambiguous(cfg):
    """Two survivors could be a taper and a town; neither is claimed."""
    from jamp.sources import provenance_from_name
    got, _ = provenance_from_name(cfg, "gd83-10-08.sbd.boston.willy.11734.sbeok.shnf")
    assert got is None


# --------------------------------------------------------------------------
# two-digit disc numbering: "01.01 - Promised Land"
# --------------------------------------------------------------------------

@pytest.mark.parametrize("filename,disc,track,title", [
    # the real GD box-set folder that could not commit
    ("01.01 - Promised Land.flac", 1, 1, "Promised Land"),
    ("01.11 - The Music Never Stopped.flac", 1, 11, "The Music Never Stopped"),
    ("02.01 - Bertha.flac", 2, 1, "Bertha"),
    ("02.08 - Johnny B. Goode.flac", 2, 8, "Johnny B. Goode"),
    # the dash spelling of the same scheme
    ("01-01 Bertha.flac", 1, 1, "Bertha"),
    ("01-02 Good Lovin.flac", 1, 2, "Good Lovin"),
])
def test_two_digit_disc_and_track_are_both_read(filename, disc, track, title):
    from jamp.audio import parse_track_name

    got = parse_track_name(filename)
    assert (got.disc, got.track, got.title) == (disc, track, title)


@pytest.mark.parametrize("filename,track,title", [
    # single-digit disc still takes the older pattern
    ("1-01 - Cold Rain and Snow.flac", 1, "Cold Rain and Snow"),
    # a plain track number is not a disc
    ("01 Hell In A Bucket.flac", 1, "Hell In A Bucket"),
    ("10 - Some Song.flac", 10, "Some Song"),
])
def test_plain_track_numbering_is_unchanged(filename, track, title):
    from jamp.audio import parse_track_name

    got = parse_track_name(filename)
    assert (got.track, got.title) == (track, title)


def test_an_implausible_disc_number_falls_back_to_a_flat_track():
    """'45.99 - Weird' is not disc 45.  Discs run low and tracks stay under 30,
    so outside that the leading number is read as a flat track number."""
    from jamp.audio import parse_track_name

    got = parse_track_name("45.99 - Weird.flac")
    assert got.disc is None
    assert got.track == 45


def test_a_two_digit_disc_folder_numbers_without_collisions(tmp_path, cfg):
    """The whole point: eleven files on disc 1 must not all become d1t01.

    Untagged, this folder previously collapsed every '01.xx' onto
    'gd1977-05-05d1t01.flac' and was blocked as a rename collision.
    """
    import fixtures
    from jamp.phase1 import build_plans

    root = tmp_path / "lib"
    show = root / "grateful dead" / "May 1977" / "1977-05-05 GD GSTL BOX FLAC"
    for i in range(1, 12):
        fixtures.make_flac(show / ("01.%02d - Song A%d.flac" % (i, i)), bits=16)
    for i in range(1, 9):
        fixtures.make_flac(show / ("02.%02d - Song B%d.flac" % (i, i)), bits=16)

    plans = {p.show.name: p for p in build_plans(root, cfg, today=dt.date(2026, 9, 7))}
    plan = plans["1977-05-05 GD GSTL BOX FLAC"]
    names = [t.new_name for t in plan.tracks]
    assert len(names) == len(set(names)), "no two tracks may claim the same name"
    assert sum(1 for n in names if "d1t" in n) == 11
    assert sum(1 for n in names if "d2t" in n) == 8


# --------------------------------------------------------------------------
# "behind the board" is where the microphones stood
# --------------------------------------------------------------------------

def test_mic_placement_is_not_read_as_a_soundboard_feed(cfg):
    """An audience tape describing its mic position must stay aud.

    Real case: 'AKG C414xls Hypercardioid > Edirol R4Pro aimed at PA array,
    behind SBD, dead center, 13 feet high by padelimike'.  The bare 'SBD' in
    that sentence is where the taper stood, not where the signal came from,
    and it overrode the '.aud.' written in the folder name.
    """
    from jamp.sources import infer_source

    tag = ("AKG C414xls Hypercardioid > Edirol R4Pro aimed at PA array, "
           "behind SBD, dead center, 13 feet high  by padelimike")
    got = infer_source(cfg, "ph2013-11-01.aud.padelimike", tag_text=tag)
    assert got.value == "aud", got.reason


@pytest.mark.parametrize("name,tag,expected", [
    # a real board feed is still a board feed
    ("ph1997-11-22.sbd.flac16", "", "sbd"),
    ("some show", "Soundboard > DAT > CD", "sbd"),
    ("fm show", "FM broadcast, WNEW", "sbd"),
    # a matrix still wins over both
    ("some show", "SBD > Nak 300 matrix", "mtx"),
    # placement language with a different mic
    ("aud show", "Schoeps mk4 > SD722, 10 feet behind the board", "aud"),
])
def test_real_source_evidence_still_reads_the_same(cfg, name, tag, expected):
    from jamp.sources import infer_source

    assert infer_source(cfg, name, tag_text=tag).value == expected


def test_the_c414_variants_are_recognised_as_microphones(cfg):
    """The C414 ships as XLS / XLII / B-ULS and tapers glue the suffix on,
    so boundary matching never saw the bare 'c414' inside 'C414xls'."""
    from jamp.sources import infer_source

    for spelling in ("AKG C414xls", "akg c414xlii", "C414B-ULS"):
        got = infer_source(cfg, "show", tag_text="%s > SD744" % spelling)
        assert got.value == "aud", "%s -> %s" % (spelling, got.reason)


# --------------------------------------------------------------------------
# track zero: material before the show
# --------------------------------------------------------------------------

@pytest.mark.parametrize("filename,disc,track,suffix", [
    ("ph1992-04-18d1t0a.flac", 1, 0, "a"),
    ("ph1992-04-18d1t0b.flac", 1, 0, "b"),
    ("ph1992-04-18d1t01.flac", 1, 1, ""),
    # a title beginning with a letter must not be read as a suffix
    ("gd1973-12-10d1t01Bertha.flac", 1, 1, ""),
])
def test_a_lettered_track_zero_keeps_its_letter(filename, disc, track, suffix):
    from jamp.audio import parse_track_name

    got = parse_track_name(filename)
    assert (got.disc, got.track, got.suffix) == (disc, track, suffix)


def test_track_zero_does_not_knock_a_folder_off_filename_numbering():
    """Zero is falsy, so `all(f.name_info.track ...)` failed and the whole
    folder fell back to sorted order - which flattened three discs into one
    and shifted every track up by two.

    Phish 1992-04-18 Palo Alto: d1t0a and d1t0b are soundchecks, and folding
    them into the running order made Wilson track 3 instead of track 1.
    """
    from jamp.audio import parse_track_name

    names = ["ph1992-04-18d1t0a.flac", "ph1992-04-18d1t0b.flac",
             "ph1992-04-18d1t01.flac", "ph1992-04-18d2t01.flac"]
    infos = [parse_track_name(n) for n in names]
    assert all(i.disc and i.track is not None for i in infos)
    assert [i.disc for i in infos] == [1, 1, 1, 2]      # discs survive


def test_two_track_zeros_do_not_collide_on_one_name():
    """Without the letter both become d1t00 and one overwrites the other."""
    import datetime as _dt
    from jamp.naming import build_track_name

    date = _dt.date(1992, 4, 18)
    a = build_track_name(band="ph", date=date, number=1, track=0,
                         ext=".flac", kind="d", suffix="a").name
    b = build_track_name(band="ph", date=date, number=1, track=0,
                         ext=".flac", kind="d", suffix="b").name
    assert a != b
    assert a == "ph1992-04-18d1t00a.flac"
    # an ordinary track is unaffected
    assert build_track_name(band="ph", date=date, number=1, track=1,
                            ext=".flac", kind="d").name == "ph1992-04-18d1t01.flac"


# --------------------------------------------------------------------------
# a place that will not fit loses whole pieces, not half a word
# --------------------------------------------------------------------------

def test_a_long_venue_gives_up_its_alias_not_its_city():
    """Real case, Phish 1998-07-24.  The venue is 86 characters, and cutting
    it at 60 produced "Cynthia Mitchell Woods Pavilion (Woodlands Center for
    the" - an unbalanced bracket with the city and state thrown away.  Two
    different transfers of that show then shared one folder name.
    """
    from jamp.naming import make_location

    got = make_location("Cynthia Mitchell Woods Pavilion "
                        "(Woodlands Center for the Performing Arts)", "Houston", "TX")
    assert got == "Cynthia Mitchell Woods Pavilion, Houston, TX"


def test_a_place_that_fits_is_left_alone():
    from jamp.naming import make_location

    assert make_location("Barton Hall", "Ithaca", "NY") == "Barton Hall, Ithaca, NY"


def test_a_trimmed_place_never_ends_on_an_unclosed_bracket():
    """Even when there is nothing left to give up, the result stays readable."""
    from jamp.naming import make_location

    got = make_location(
        "Cornell University Barton Hall Memorial Athletic Complex Annex "
        "(A Very Long Alias Indeed That Cannot Possibly Fit)", "Ithaca", "NY")
    assert got.count("(") == got.count(")")
    assert len(got) <= 60


@pytest.mark.parametrize("filename,set_no,track,title", [
    ("I 01 Jam.mp3", 1, 1, "Jam"),
    ("II 01 The Curtain With.mp3", 2, 1, "The Curtain With"),
    ("III 01 You Enjoy Myself.mp3", 3, 1, "You Enjoy Myself"),
    ("IV 03 Tweezer.mp3", 4, 3, "Tweezer"),
])
def test_a_roman_set_number_is_read_as_a_set(filename, set_no, track, title):
    """Phish 1988-07-23 numbers its sets I, II, III.  All three read as track 1
    and collided on one name, so the folder could not commit at all.

    Two things had to change: the pattern, and the leading band-prefix strip,
    which was swallowing the numeral before any pattern saw it.
    """
    from jamp.audio import parse_track_name

    got = parse_track_name(filename)
    assert (got.set_no, got.track, got.title) == (set_no, track, title)


@pytest.mark.parametrize("filename", [
    "I Am The Walrus.flac",      # a title, not a set number
    "mmj d1t01.flac",            # a real band prefix must still be stripped
    "1-01 Julius.flac",
])
def test_the_roman_rule_does_not_disturb_anything_else(filename):
    from jamp.audio import parse_track_name

    got = parse_track_name(filename)
    if filename.startswith("I Am"):
        assert got.set_no is None and got.track is None
    else:
        assert got.track == 1


# --- a container folder is not an act --------------------------------------

def test_band_resolves_from_the_nearest_parent_not_the_top(cfg):
    """"Live Music" names no act; the folder below it does.

    Passing only the top of the chain left every show under a container folder
    with no band at all - 176 of them in one artist - because the walk stopped
    at the level that says nothing.
    """
    res = resolve_band("4.14.72 Greensboro, NC", cfg,
                       parent_artist_dir=("STS9", "Live Music"))
    assert res.abbrev == "sts9"
    assert res.matched_by == "parent_folder"
    assert not res.authoritative


def test_a_single_parent_string_still_works(cfg):
    """The old call shape is used all over the tests and the reports."""
    res = resolve_band("2012_04_13 Athens, GA", cfg, parent_artist_dir="Umphrey's McGee")
    assert res.abbrev == "um"


def test_container_alone_resolves_nothing(cfg):
    assert resolve_band("05_22_92 Trenton, NJ", cfg,
                        parent_artist_dir=("Live Music",)).band is None


# --- a bracketed source is never the name of the room ----------------------

@pytest.mark.parametrize("text,expected", [
    ("(SBD) Zydeco", "Zydeco"),
    ("Zydeco (SBD)", "Zydeco"),
    ("Crystal Ballroom [AUD]", "Crystal Ballroom"),
    # Bracketed only.  Both of these are real rooms.
    ("The Matrix", "The Matrix"),
    ("The Board Room", "The Board Room"),
])
def test_strip_bracketed_source(text, expected):
    from jamp.naming import strip_bracketed_source
    assert strip_bracketed_source(text) == expected


def test_bracketed_source_does_not_reach_the_place():
    from jamp.naming import make_location
    assert make_location("(SBD) Zydeco", "Birmingham", "AL") == "Zydeco, Birmingham, AL"


def test_a_slashed_date_in_an_album_tag_does_not_become_part_of_the_venue():
    """The order of two cleanups, and why it matters.

    "/" is illegal in a Windows filename and was stripped before dates were
    looked for, so the 8/16/1996 inside a store's ALBUM tag collapsed to the
    digit run 8161996 - which nothing downstream recognised as a date, so it
    was carried into the folder name and written to VENUE as though it were
    part of the place.

    The second case is the one that hid: 7/8/78 collapses to four digits, which
    reads like an ordinary number in a venue name.
    """
    from jamp.naming import make_location

    assert make_location(
        "The Clifford Ball: 8/16/1996 Plattsburgh Air Force Base, "
        "Plattsburgh, NY", limit=120) == \
        "The Clifford Ball Plattsburgh Air Force Base, Plattsburgh, NY"
    assert make_location("Red Rocks Amphitheatre, Morrison, CO 7/8/78",
                         limit=120) == "Red Rocks Amphitheatre, Morrison, CO"


def test_a_dotted_date_is_stripped_even_though_dots_are_legal():
    """Taken out on the second pass, after the illegal characters are gone."""
    from jamp.naming import make_location

    assert make_location("Some Hall 8.16.1996, Plattsburgh, NY", limit=120) == \
        "Some Hall, Plattsburgh, NY"


def test_a_number_that_is_part_of_a_venue_name_survives():
    """The guard on the above: not every run of digits is a date.

    Stripping too eagerly costs a venue its name, which is worse than leaving
    a stray number in one folder.
    """
    from jamp.naming import make_location

    assert make_location("Cafe 1930", "Arcata", "CA") == "Cafe 1930, Arcata, CA"
    assert make_location("Bourbon Street", "New Orleans", "LA") == \
        "Bourbon Street, New Orleans, LA"
