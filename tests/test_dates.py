"""Date parsing, using the real folder names rather than tidy examples."""
import datetime as dt

import pytest

from jamp import dates
from jamp.dates import evidence_from_text, expand_two_digit_year, find_dates, resolve_date

TODAY = dt.date(2026, 9, 7)


def only(text, **kw):
    got = find_dates(text, today=TODAY, **kw)
    assert got, "no date found in %r" % text
    return got[0]


@pytest.mark.parametrize("name,expected", [
    ("mmj2003-09-26.shnf", "2003-09-26"),
    ("mmj2004-05-28.flac16 opera house", "2004-05-28"),
    ("MMJ2005-11-23FMSBD.flacf", "2005-11-23"),
    ("MMJ2006-06-16..4011s bonaroo", "2006-06-16"),
    ("mmj-2012-12-27 Port Chester, NY-ldl.flac16", "2012-12-27"),
    ("UM 2012_04_19 Tulsa, OK V0", "2012-04-19"),
    ("2012_04_13 Athens, GA", "2012-04-13"),
    ("Grateful Dead 10-31-91", "1991-10-31"),
    ("UM Summer Camp 5-28-11", "2011-05-28"),
    ("Phish 12-29-18 MTX", "2018-12-29"),
    ("ph2018-12-28.New.York.NY.padelimike.akg414.flac2496", "2018-12-28"),
    ("gd1973-12-10 s1", "1973-12-10"),
    ("u111105", "2011-11-05"),
    ("mmj231103d1_01_Mahgeetah.flac", "2023-11-03"),
])
def test_real_folder_names(name, expected):
    assert only(name).date.isoformat() == expected


def test_two_digit_year_pivot():
    assert expand_two_digit_year(77, TODAY) == 1977
    assert expand_two_digit_year(91, TODAY) == 1991
    assert expand_two_digit_year(11, TODAY) == 2011
    assert expand_two_digit_year(26, TODAY) == 2026


def test_ambiguous_date_keeps_both_readings():
    """05-06-77 is 5 May in US convention, but 6 May is a real possibility."""
    cand = only("gd05-06-77")
    assert cand.date == dt.date(1977, 5, 6)
    assert cand.ambiguous
    assert dt.date(1977, 6, 5) in cand.alternatives


def test_unambiguous_short_date_is_not_flagged():
    cand = only("Grateful Dead 10-31-91")
    assert not cand.ambiguous
    assert cand.base_confidence > dates.BASE_CONFIDENCE["us_short"]


def test_impossible_dates_are_rejected():
    assert find_dates("gd1973-02-30", today=TODAY) == []


def test_month_thirteen_is_repaired_but_never_trusted_alone():
    """1973-13-01 is most likely year-day-month, but acting on that is a guess."""
    cand = only("gd1973-13-01")
    assert cand.pattern == "iso_swapped"
    assert cand.date == dt.date(1973, 1, 13)
    res = resolve_date([evidence_from_text("folder", "gd1973-13-01", today=TODAY)])
    assert res.confidence < 70


def test_future_dates_are_rejected():
    assert find_dates("ph2099-01-01", today=TODAY) == []


def test_dates_before_the_earliest_are_rejected():
    assert find_dates("gd1912-05-08", today=TODAY, earliest=dt.date(1960, 1, 1)) == []


def test_year_only_is_never_a_date():
    res = resolve_date([evidence_from_text("folder", "Umphreys McGee Bonnaroo 2008",
                                           today=TODAY)])
    assert res.date is None
    assert res.confidence == 0
    assert 2008 in res.years_only


def test_bare_year_in_parentheses_is_not_a_date():
    res = resolve_date([evidence_from_text(
        "folder", "Umphrey's McGee Live at Lake Coast (2002)", today=TODAY)])
    assert res.date is None
    assert 2002 in res.years_only


def test_six_digit_prefers_yymmdd_but_keeps_the_alternative():
    cand = only("u111105")
    assert cand.date == dt.date(2011, 11, 5)
    assert cand.ambiguous
    assert dt.date(2005, 11, 11) in cand.alternatives


def test_band_active_years_promote_the_plausible_reading():
    """Grateful Dead stopped in 1995, so a 2005 reading cannot be right."""
    cand = only("gd050677", year_range=(1965, 1995))
    assert cand.date.year == 1977


def test_same_date_written_twice_raises_confidence():
    one = resolve_date([evidence_from_text("folder", "MMJ2012-09-12", today=TODAY)])
    two = resolve_date([evidence_from_text(
        "folder", "MMJ2012-09-12.MMJ-Wiltern-9-12-12", today=TODAY)])
    assert two.date == one.date
    assert two.confidence > one.confidence


def test_corroboration_across_sources_resolves_ambiguity():
    """u111105 alone is ambiguous; the nested folder settles it."""
    alone = resolve_date([evidence_from_text("folder", "u111105", today=TODAY)])
    assert alone.ambiguous
    assert alone.confidence < 70

    together = resolve_date([
        evidence_from_text("folder", "u111105", today=TODAY),
        evidence_from_text("child_folder", "2011_11_05 Eagles Ballroom - Milwaukee, WI",
                           today=TODAY),
    ])
    assert together.date == dt.date(2011, 11, 5)
    assert not together.ambiguous
    assert together.confidence >= 90


def test_two_digit_year_first(cfg=None):
    """"80-03-01" is how etree filenames and some album tags write a date."""
    cand = only("jg80-03-01.jgb.early.fm.17928.shnf")
    assert cand.date == dt.date(1980, 3, 1)
    assert cand.pattern == "yy_mm_dd"
    assert not cand.ambiguous
    assert only("80-03-01 - Capitol Theater").date == dt.date(1980, 3, 1)


def test_year_first_does_not_steal_ordinary_us_dates():
    assert only("Grateful Dead 10-31-91").pattern == "us_short"
    assert only("UM Summer Camp 5-28-11").pattern == "us_short"
    assert only("Phish 12-29-18 MTX").pattern == "us_short"


def test_a_date_readable_both_ways_is_flagged():
    """01-02-03 could be several dates.  Which reading wins matters less than
    that it is marked ambiguous and cannot be committed on."""
    cand = only("gd01-02-03")
    assert cand.ambiguous
    assert cand.alternatives
    res = resolve_date([evidence_from_text("folder", "gd01-02-03", today=TODAY)])
    assert res.confidence < 70


def test_a_track_number_is_not_read_as_part_of_the_date():
    """"MMJ-Wiltern-9-12-12-01.flac" is 9-12-12 track 01, and the tail
    "12-12-01" must not be read as a date of its own."""
    got = find_dates("MMJ-Wiltern-9-12-12-01.flac", today=TODAY)
    assert [c.date.isoformat() for c in got] == ["2012-09-12"]


def test_new_years_eve_is_a_suggestion_not_a_date():
    """"New Year's Eve 2006" names a night without naming a date, and which
    night is genuinely ambiguous."""
    cand = only("My Morning Jacket - New Years Eve 2006")
    assert cand.pattern == "new_years_eve"
    assert cand.date == dt.date(2006, 12, 31)
    assert cand.ambiguous
    assert dt.date(2005, 12, 31) in cand.alternatives

    res = resolve_date([evidence_from_text(
        "folder", "My Morning Jacket - New Years Eve 2006", today=TODAY)])
    # Offered, but nowhere near committable on its own.
    assert res.confidence < 70


def test_new_years_eve_does_not_fire_on_an_ordinary_year():
    assert find_dates("Umphreys McGee Bonnaroo 2008", today=TODAY) == []


def test_conflicting_dates_lose_confidence():
    res = resolve_date([
        evidence_from_text("folder", "gd1973-12-10", today=TODAY),
        evidence_from_text("info", "1974-06-18", today=TODAY),
    ])
    assert res.conflicts
    assert res.confidence < 70


def test_track_numbers_are_not_mistaken_for_dates():
    assert find_dates("101 Cumberland Blues", today=TODAY) == []
    assert find_dates("1-01 Bertha", today=TODAY) == []


def test_track_filenames_must_not_be_run_together():
    """"...Bertha.mp3 1-02 Playing..." reads as 3/1/02 if joined with spaces."""
    names = ["1-01 Bertha.mp3", "1-02 Playing In The Band.mp3", "1-03 Wharf Rat.mp3"]
    assert find_dates(" ".join(names), today=TODAY), "the hazard is real"
    assert find_dates("\n".join(names), today=TODAY) == []


def test_format_tokens_are_not_mistaken_for_dates():
    got = find_dates("ph2018-12-28.padelimike.akg414.flac2496", today=TODAY)
    assert [c.date.isoformat() for c in got] == ["2018-12-28"]


def test_etree_source_id_is_not_read_as_a_date():
    """jg83-06-01.010962 - 010962 is the catalogue number, not 1962-01-09.

    Read as a date it conflicts with the show date two fields to its left,
    and the conflict penalty was enough to block the folder from committing.
    """
    from jamp.dates import find_dates
    got = {c.date.isoformat() for c in find_dates(
        "jg83-06-01.010962.jgb.nak701.beach.sbeok.t-flac16")}
    assert "1983-06-01" in got
    assert "1962-01-09" not in got


def test_a_bare_six_digit_filename_date_still_parses():
    """The guard must not swallow the ordinary yymmdd name it sits next to."""
    from jamp.dates import find_dates
    got = {c.date.isoformat() for c in find_dates("gd770508d1t01.flac")}
    assert "1977-05-08" in got


def test_a_much_later_info_file_date_is_a_transfer_not_a_conflict():
    """An info file says when the tape was transferred; that is not the show."""
    import datetime as dt
    from jamp.dates import DateEvidence, find_dates, resolve_date
    folder = DateEvidence(source="folder", text="jg83-06-01.jgb",
                          candidates=find_dates("jg83-06-01.jgb"))
    info = DateEvidence(source="info", text="transferred 2014-10-18",
                        candidates=find_dates("transferred 2014-10-18"))
    res = resolve_date([folder, info])
    assert res.date == dt.date(1983, 6, 1)
    assert not res.conflicts
    assert any("transfer date" in r for r in res.reasons)


def test_a_filler_date_is_not_the_shows_date(tmp_path):
    """Spare tracks from another night, used to fill out a disc, are ordinary
    in trading - and the date on that line belongs to the other show.

    Phish 2000-09-30 carries "Filler: 2000/10/01 Desert Sky Pavilion, Phoenix,
    AZ".  Both dates scored close (90 to 76), so the folder blocked as a date
    conflict rather than guess.  A filler date that happened to score higher
    would have silently misdated the show instead.
    """
    from jamp.infofile import parse_info_text

    text = (
        "Phish\n"
        "2000/09/30\n"
        "Thomas & Mack Center, Las Vegas, NV\n"
        "Source: DSBD\n"
        "Filler: 2000/10/01 Desert Sky Pavilion, Phoenix, AZ\n"
    )
    info = parse_info_text(tmp_path / "info.txt", text, "utf-8", 10.0)
    found = {c.date.isoformat() for c in info.date_candidates if c.date}
    assert "2000-09-30" in found
    assert "2000-10-01" not in found, "the filler night must not compete"


def test_a_date_only_on_a_filler_line_is_still_used(tmp_path):
    """Better read as the show's own than thrown away: some notes mention
    filler on the same line as the only date in the file."""
    from jamp.infofile import parse_info_text

    text = "Phish\nGorge Amphitheatre\n- 08/08/09 Filler uses the same sources\n"
    info = parse_info_text(tmp_path / "info.txt", text, "utf-8", 10.0)
    found = {c.date.isoformat() for c in info.date_candidates if c.date}
    assert "2009-08-08" in found


@pytest.mark.parametrize("filename,expected", [
    # a song called 2001 pairing with its own track number
    ("2-06 2001.flac", []),
    ("2_06_2001.flac", []),        # underscores, the form that slipped through
    ("1-01 Julius.flac", []),
    ("1-05 1999.flac", []),
    ("2-01 1970.flac", []),
    # a file genuinely named after the show keeps its date
    ("12-31-95 Set 1.flac", ["1995-12-31"]),
    ("1999-07-24 d1t01.flac", ["1999-07-24"]),
    ("ph2013-10-20d1t01.flac", ["2013-10-20"]),
])
def test_a_track_number_cannot_make_a_date_with_the_title(filename, expected):
    """"2-06 2001.flac" is disc 2, track 6, of the song 2001 - and reads as
    2/06/2001.  That cost Phish 2013-10-20 twenty-five points of confidence
    and blocked the folder, while its ALBUM tag plainly said 2013/10/20
    Hampton, VA.  Song titles that are bare years are common: 2001, 1999, 1970.

    The real date at the front of a filename is written in one piece; the false
    one only exists by crossing the space after the track number.
    """
    from jamp.analyze import _drop_track_number
    from jamp.dates import find_dates

    stripped = _drop_track_number(filename)
    got = sorted({c.date.isoformat() for c in find_dates(stripped) if c.date})
    assert got == expected


# --- a name that spans two nights ------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("2002.07.18-19 Mishawaka Compilation", ("2002-07-18", "2002-07-19")),
    ("2002-07-18-19", ("2002-07-18", "2002-07-19")),
    ("1995.06.24-25 Two Nights", ("1995-06-24", "1995-06-25")),
    # A disc suffix is not a second night: day 1 cannot follow day 8.
    ("1977-05-08-1", None),
    # Nothing hangs off the end of a plain date.
    ("ph1997-03-01", None),
    ("abb1973-07-28.aud.motb.0234", None),
    ("cure-the-1984-05-30-netherlands", None),
    ("1999-04-15.paf.sbd.miller.25295", None),
    # A day that does not exist in that month is not a range.
    ("2002-02-27-31", None),
])
def test_find_date_range(text, expected):
    from jamp.dates import find_date_range
    got = find_date_range(text)
    if expected is None:
        assert got is None
    else:
        assert (got[0].isoformat(), got[1].isoformat()) == expected


def test_a_band_prefix_ending_in_a_digit_does_not_hide_the_year(tmp_path, cfg):
    """Every date pattern refuses a year that follows a digit, so the 9 of
    STS9 hid it: "sts92000-02-12" had no date at all."""
    import fixtures
    from jamp.analyze import analyze_show, unglue_band_prefix
    from jamp.scan import scan

    assert unglue_band_prefix("sts92000-02-12.sbd.mp3", cfg) == "sts9 2000-02-12.sbd.mp3"
    assert unglue_band_prefix("ph2000-02-12", cfg) == "ph2000-02-12"
    assert unglue_band_prefix("abcsts92000", cfg) == "abcsts92000"

    root = tmp_path / "lib"
    show = root / "STS9" / "sts92000-02-12.sbd.mp3"
    for i in (1, 2, 3):
        fixtures.make_mp3(show / ("sts92000-02-12d1t%02d.mp3" % i))
    a = analyze_show(scan(root, cfg).shows[0], cfg, today=dt.date(2026, 9, 13))
    assert a.date.iso == "2000-02-12"
    assert a.date.confidence >= 70
