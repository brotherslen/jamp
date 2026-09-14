"""Tests for the venue gazetteer.

Most of these are about what it must NOT say. Filling in a city from a venue
name is the one operation here that can move a show a thousand miles, and the
library really does hold an Orpheum Theatre in two cities, a Fox Theatre in two
more, and a Keystone in Berkeley and Palo Alto. A table that answers confidently
on those is worse than no table.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jamp.venues import Gazetteer, Venue, normalise, same_city  # noqa: E402


@pytest.fixture
def gaz():
    return Gazetteer([
        Venue("Alpine Valley Music Theatre", "East Troy", "WI",
              ["Alpine Valley Music Theater", "Alpine Valley"]),
        Venue("The Warfield", "San Francisco", "CA",
              ["Warfield", "Warfield Theater", "The Warfield Theater"]),
        Venue("Orpheum Theatre", "San Francisco", "CA"),
        Venue("Orpheum Theatre", "Boston", "MA", ["Orpheum Theater"]),
        Venue("Keystone", "Berkeley", "CA"),
        Venue("Keystone", "Palo Alto", "CA"),
        Venue("Red Rocks Amphitheatre", "Morrison", "CO"),
    ])


# --- folding spellings ------------------------------------------------------

def test_theatre_and_theater_are_one_room():
    assert normalise("Alpine Valley Music Theatre") == normalise("Alpine Valley Music Theater")
    assert normalise("Riverport Amphitheater") == normalise("Riverport Amphitheatre")


def test_a_leading_the_does_not_make_a_different_room():
    assert normalise("The Warfield") == normalise("Warfield")


def test_punctuation_and_case_are_not_the_name():
    assert normalise("Dick's Sporting Goods Park") == normalise("Dicks Sporting Goods Park")
    assert normalise("9:30 Club") == normalise("930 Club")


# --- what it refuses to say -------------------------------------------------

def test_a_name_two_rooms_share_is_refused(gaz):
    """The Orpheum is in San Francisco AND Boston.

    Answering either would be right half the time and would move a show across
    the country the other half.  Silence is the only honest answer.
    """
    assert gaz.resolve("Orpheum Theatre") is None
    shared = gaz.ambiguous("Orpheum Theatre")
    assert len(shared) == 2
    assert {v.city for v in shared} == {"San Francisco", "Boston"}


def test_a_shared_name_resolves_once_the_city_is_known(gaz):
    got = gaz.resolve("Orpheum Theatre", "Boston")
    assert got[:3] == ("Orpheum Theatre", "Boston", "MA")


def test_two_rooms_of_one_name_in_one_state_are_still_ambiguous(gaz):
    """Keystone Berkeley and Keystone Palo Alto are both in California.

    A state in hand is not enough; it takes the city.
    """
    assert gaz.resolve("Keystone", None, "CA") is None
    assert len(gaz.ambiguous("Keystone")) == 2


def test_a_room_it_does_not_know_gets_no_opinion(gaz):
    assert gaz.resolve("Somewhere Nobody Has Played") is None
    assert gaz.ambiguous("Somewhere Nobody Has Played") == []


def test_it_never_contradicts_the_folder(gaz):
    """A city already in hand that disagrees means one of them is another room.

    The folder is evidence about *this show*; the table is a general statement.
    So the folder wins and the gazetteer withdraws rather than arguing.
    """
    assert gaz.resolve("Red Rocks Amphitheatre", "Denver") is None
    assert gaz.resolve("Red Rocks Amphitheatre", "Morrison", "UT") is None


def test_nothing_to_add_is_not_an_answer(gaz):
    """Already canonical, already placed - there is no change to report."""
    assert gaz.resolve("Red Rocks Amphitheatre", "Morrison", "CO") is None


# --- what it is for ---------------------------------------------------------

def test_a_room_with_no_city_gets_one(gaz):
    """The case that cost fifteen folders their venue.

    Without a `City, ST` the venue guard throws the room away entirely, so
    "warfield" became the whole of the place and then no place at all.
    """
    venue, city, state, why = gaz.resolve("Warfield")
    assert (venue, city, state) == ("The Warfield", "San Francisco", "CA")
    assert "San Francisco" in why


def test_one_room_written_two_ways_becomes_one(gaz):
    venue, city, state, _why = gaz.resolve("Alpine Valley Music Theater", "East Troy")
    assert venue == "Alpine Valley Music Theatre"


def test_the_reason_is_always_given(gaz):
    """Every answer says how it was reached; nothing here is silent."""
    for args in (("Warfield",), ("Alpine Valley", "East Troy")):
        got = gaz.resolve(*args)
        assert got and got[3]


# --- loading ----------------------------------------------------------------

def test_a_missing_file_is_an_empty_table_not_an_error(tmp_path):
    """The pipeline worked before this existed and must work without it."""
    g = Gazetteer.load(tmp_path / "nope.yaml")
    assert len(g) == 0
    assert g.resolve("Anywhere") is None


def test_no_path_at_all_is_also_fine():
    assert len(Gazetteer.load(None)) == 0


def test_the_shipped_gazetteer_loads_and_is_self_consistent():
    """Every alias must point at exactly one room, or at rooms in different
    cities - never at two rooms in the same city, which would be a duplicate
    entry rather than a shared name."""
    g = Gazetteer.load(ROOT / "jamp" / "data" / "venues.yaml")
    if not len(g):
        pytest.skip("no gazetteer in this checkout")
    seen = {}
    for v in g.venues:
        for key in v.keys:
            place = (key, (v.city or "").lower(), (v.state or "").upper())
            assert place not in seen, "duplicate entry for %r in %s" % (v.name, v.city)
            seen[place] = v


# --- one room arriving twice ------------------------------------------------

def test_two_entries_for_one_room_become_one():
    """The file is seeded and then added to by hand, so this happens easily.

    Two entries sharing a name in one city are not two rooms. Left alone they
    make every lookup on that name ambiguous, so the gazetteer would go quiet
    on exactly the rooms it knows best.
    """
    g = Gazetteer([
        Venue("Magnaball", "Watkins Glen", "NY"),
        Venue("Magnaball", "Watkins Glen", "NY", ["Watkins Glen International"]),
    ])
    assert len(g) == 1
    assert g.resolve("Magnaball")[:3] == ("Magnaball", "Watkins Glen", "NY")


def test_merging_is_transitive():
    """Loring arrived three times and only a repeated pass links all three.

    "Loring Commerce Centre" and "Loring Air Force Base" share no name at all.
    It takes the third entry, which lists one as an alias of the other, to
    connect them - and by then a single pass has already walked past the first.
    """
    g = Gazetteer([
        Venue("Loring Commerce Centre", "Limestone", "ME"),
        Venue("Loring Air Force Base", "Limestone", "ME"),
        Venue("Loring Air Force Base", "Limestone", "ME",
              ["Loring Commerce Centre", "Loring"]),
    ])
    assert len(g) == 1
    for name in ("Loring", "Loring Air Force Base", "Loring Commerce Centre"):
        assert g.resolve(name) is not None, name


def test_the_same_name_in_two_cities_is_never_merged():
    """The Orpheum case. Merging these would put a show in the wrong city."""
    g = Gazetteer([
        Venue("Orpheum Theatre", "San Francisco", "CA"),
        Venue("Orpheum Theatre", "Boston", "MA"),
    ])
    assert len(g) == 2
    assert g.resolve("Orpheum Theatre") is None


# --- festivals --------------------------------------------------------------

def test_a_festival_places_a_show_the_way_a_room_does():
    g = Gazetteer([Venue("The Clifford Ball", "Plattsburgh", "NY",
                         ["Clifford Ball", "Plattsburgh Air Force Base"])])
    assert g.resolve("Plattsburgh Air Force Base")[:3] == \
        ("The Clifford Ball", "Plattsburgh", "NY")


def test_a_festival_that_moved_is_told_apart_by_the_year():
    """Lollapalooza toured before Grant Park, so the name alone says nothing.

    With a date in hand it is not ambiguous at all - it only looks that way to
    a lookup that ignores the year.
    """
    g = Gazetteer([
        Venue("Lollapalooza", "Chicago", "IL", years=(2005, None)),
        Venue("Lollapalooza", "Irvine", "CA", years=(1991, 1997)),
    ])
    assert g.resolve("Lollapalooza") is None
    assert g.resolve("Lollapalooza", year=2015)[:3] == ("Lollapalooza", "Chicago", "IL")
    assert g.resolve("Lollapalooza", year=1993)[:3] == ("Lollapalooza", "Irvine", "CA")


def test_a_room_that_never_moved_ignores_the_year():
    g = Gazetteer([Venue("Red Rocks Amphitheatre", "Morrison", "CO")])
    assert g.resolve("Red Rocks Amphitheatre", year=1978)[:3] == \
        ("Red Rocks Amphitheatre", "Morrison", "CO")


# --- names too common to stand alone ----------------------------------------

def test_a_generic_name_never_places_a_show_on_its_own():
    """The guard the ambiguity check could not provide.

    Ambiguity can only see rooms that are IN the table, which is not the same
    as rooms that exist.  There is a State Theatre in a dozen cities; this
    library had committed exactly one, so the lookup was unambiguous and wrong
    - a Disco Biscuits show whose tag said only "State Theatre" was about to be
    placed in Minneapolis.
    """
    g = Gazetteer([Venue("State Theater", "Minneapolis", "MN", generic=True)])
    assert g.resolve("State Theatre") is None
    assert g.resolve("State Theater") is None


def test_a_generic_name_still_resolves_once_the_city_is_known():
    g = Gazetteer([Venue("State Theater", "Minneapolis", "MN", generic=True)])
    assert g.resolve("State Theatre", "Minneapolis")[:3] == \
        ("State Theater", "Minneapolis", "MN")


def test_a_distinctive_name_is_unaffected_by_the_flag():
    g = Gazetteer([
        Venue("State Theater", "Minneapolis", "MN", generic=True),
        Venue("Bonnaroo", "Manchester", "TN"),
    ])
    assert g.resolve("Bonnaroo")[:3] == ("Bonnaroo", "Manchester", "TN")


def test_merging_keeps_the_generic_flag():
    """A room merged from two entries must not lose the warning on one of them."""
    g = Gazetteer([
        Venue("Fox Theatre", "Atlanta", "GA"),
        Venue("Fox Theatre", "Atlanta", "GA", ["Fox Theater"], generic=True),
    ])
    assert len(g) == 1
    assert g.venues[0].generic
    assert g.resolve("Fox Theatre") is None


# --- a city typed wrong -----------------------------------------------------

def test_a_mistyped_city_is_recognised_as_the_same_place():
    """Raliegh for Raleigh - which the gazetteer itself was seeded with.

    This is the one case where the table corrects a folder rather than
    deferring to it, and it is safe only because the VENUE NAME already
    matched: the folder is not naming a different city, it is misspelling
    this one.
    """
    g = Gazetteer([Venue("Walnut Creek Amphitheatre", "Raleigh", "NC")])
    venue, city, state, why = g.resolve("Walnut Creek Amphitheatre", "Raliegh", "NC")
    assert (city, state) == ("Raleigh", "NC")
    assert "Raliegh" in why and "Raleigh" in why


@pytest.mark.parametrize("wrong,right", [
    ("Raliegh", "Raleigh"),
    ("Pittsburg", "Pittsburgh"),
    ("Cincinatti", "Cincinnati"),
    ("Springfeild", "Springfield"),
    ("San Franciso", "San Francisco"),      # archive.org's own typo
])
def test_real_misspellings_are_matched(wrong, right):
    assert same_city(wrong, right)


@pytest.mark.parametrize("a,b", [
    ("Portland", "Poland"),        # two characters apart and not the same place
    ("Columbia", "Columbus"),      # different cities, different states
    ("Athens", "Athena"),
    ("Charlotte", "Charleston"),
    ("Newark", "New York"),
    ("Boston", "Austin"),          # different first letter
])
def test_different_cities_are_never_folded_together(a, b):
    assert not same_city(a, b)


def test_a_typo_does_not_rescue_a_genuinely_wrong_city():
    """The folder still wins when the two are actually different places."""
    g = Gazetteer([Venue("Red Rocks Amphitheatre", "Morrison", "CO")])
    assert g.resolve("Red Rocks Amphitheatre", "Denver") is None


def test_a_mistyped_city_still_finds_a_room_it_shares_a_name_with():
    """find() has to tolerate the typo too, or a generic name stays unresolved."""
    g = Gazetteer([Venue("State Theater", "Minneapolis", "MN", generic=True)])
    got = g.resolve("State Theatre", "Minneapolos")
    assert got[:3] == ("State Theater", "Minneapolis", "MN")


def test_one_name_in_two_identically_named_cities_is_still_refused():
    """Springfield IL and Springfield MA.

    The city index is keyed on name and city but not state, so these two
    collide. Storing one entry per key would have silently overwritten the
    other and answered with whichever happened to load last.
    """
    g = Gazetteer([
        Venue("Fox Theatre", "Springfield", "IL"),
        Venue("Fox Theatre", "Springfield", "MA"),
    ])
    assert len(g) == 2, "different states, so not one room"
    assert g.resolve("Fox Theatre", "Springfield") is None


def test_a_state_in_hand_settles_two_identically_named_cities():
    g = Gazetteer([
        Venue("Fox Theatre", "Springfield", "IL"),
        Venue("Fox Theatre", "Springfield", "MA"),
    ])
    assert g.resolve("Fox Theatre", "Springfield", "MA") is None,         "nothing to add - the folder already says both"


# --- learning a room's city from the rest of the library -------------------

def _a(venue, city=None, state=None, by_hand=False):
    from types import SimpleNamespace as NS

    notes = []
    return NS(venue=venue, city=city, state=state, place_by_hand=by_hand,
              place_from_gazetteer=False, date=NS(date=None), notes=notes,
              add=lambda *issue: notes.append(issue))


def test_the_library_fills_a_room_it_agrees_on():
    from jamp.phase1 import learn_venues_from_the_library

    bare = _a("Wiltern")
    learn_venues_from_the_library([_a("Wiltern", "Los Angeles", "CA"),
                                   _a("Wiltern", "Los Angeles"), bare])
    assert (bare.city, bare.state) == ("Los Angeles", "CA")


def test_the_library_does_not_move_a_room_to_another_city():
    """Keyed on the name alone, "Fox Theatre, Atlanta, GA" placed a bare Fox
    Theatre in Atlanta and gave "Fox Theatre, Oakland" the state GA."""
    from jamp.phase1 import learn_venues_from_the_library

    atlanta = _a("Fox Theatre", "Atlanta", "GA")
    oakland = _a("Fox Theatre", "Oakland")
    bare = _a("Fox Theatre")
    learn_venues_from_the_library([atlanta, oakland, bare])
    assert (oakland.city, oakland.state) == ("Oakland", None)
    assert (bare.city, bare.state) == (None, None)


def test_a_state_is_only_added_to_its_own_city():
    from jamp.phase1 import learn_venues_from_the_library

    elsewhere = _a("Fox Theatre", "Oakland")
    learn_venues_from_the_library([_a("Fox Theatre", "Atlanta", "GA"), elsewhere])
    assert elsewhere.state is None


def test_a_typo_in_the_city_is_still_agreement():
    from jamp.phase1 import learn_venues_from_the_library

    bare = _a("Raleigh Amphitheater")
    learn_venues_from_the_library([_a("Raleigh Amphitheater", "Raleigh", "NC"),
                                   _a("Raleigh Amphitheater", "Raliegh"), bare])
    assert (bare.city, bare.state) == ("Raleigh", "NC")


def test_the_library_defers_to_the_gazetteer_on_a_shared_or_generic_name():
    from jamp.phase1 import learn_venues_from_the_library

    gaz = Gazetteer([Venue("Orpheum Theatre", "Boston", "MA"),
                     Venue("Orpheum Theatre", "San Francisco", "CA"),
                     Venue("State Theatre", "Minneapolis", "MN", generic=True)])
    orpheum, state = _a("Orpheum Theatre"), _a("State Theatre")
    learn_venues_from_the_library(
        [_a("Orpheum Theatre", "Boston", "MA"), orpheum,
         _a("State Theatre", "Minneapolis", "MN"), state], gaz)
    assert orpheum.city is None and state.city is None


def test_a_place_set_by_hand_is_not_added_to():
    from jamp.phase1 import learn_venues_from_the_library

    hand = _a("Wiltern", by_hand=True)
    learn_venues_from_the_library([_a("Wiltern", "Los Angeles", "CA"), hand])
    assert hand.city is None


def test_a_blocked_folder_can_learn_a_place_but_not_teach_one():
    """An unconfigured act's folder, blocked with no band, placed Phish's
    "Cologne" in a city called "Germany"."""
    from jamp.phase1 import learn_venues_from_the_library

    blocked = _a("Cologne", "Germany")
    blocked.blocked = True
    phish = _a("Cologne")
    learn_venues_from_the_library([blocked, phish])
    assert (phish.city, phish.state) == (None, None)

    learner = _a("Wiltern")
    learner.blocked = True
    learn_venues_from_the_library([_a("Wiltern", "Los Angeles", "CA"), learner])
    assert learner.city == "Los Angeles"


def test_building_a_gazetteer_leaves_the_callers_venues_alone():
    """Merging assigned to the entries it kept, so the Venue objects passed in
    came back with other rooms' aliases and cities folded into them."""
    a = Venue("Loring Commerce Centre", "Limestone", "ME")
    b = Venue("Loring Air Force Base", "Limestone", "ME", aliases=["Loring Commerce Centre"])
    Gazetteer([a, b])
    assert a.aliases == [] and b.aliases == ["Loring Commerce Centre"]
    assert (a.name, b.name) == ("Loring Commerce Centre", "Loring Air Force Base")
