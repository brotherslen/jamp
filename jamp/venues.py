"""The rooms this library knows, and the ways they get written.

**Why this exists.** `overrides.yaml` reached 102 entries, and 90 of them
were not facts about a folder at all - they were the same handful of statements
about sixty rooms, written out once per folder that needed them. Read their own
notes and they say one of three things:

* *"the room is named, the city is not"* - the Bowery Ballroom, Brooklyn Steel,
  Baby's All Right, the Kinetic Playground, Keystone, the Warfield. A venue
  taken from a folder name needs a `City, ST` beside it or the guard in
  section 12 rejects it, and the venue is then lost entirely rather than kept
  without its city.
* *"Port Chester, New York, spelled out rather than as NY"* - the state
  normaliser handles that, but only when something asks it to.
* *"RFK Stadium on one date and Robert F. Kennedy Stadium on another"* - one
  room, two spellings, and phase 3's consensus is taken per night so it cannot
  see that the night before disagreed with it.

All three are answered by knowing what rooms exist. That is a lookup table, and
a lookup table belongs in the config beside the bands and the microphones rather
than repeated per folder in the file reserved for judgements.

**A venue name is not unique, and this must never forget it.** There is an
Orpheum Theatre in Boston and another in San Francisco; a Fox Theatre in Atlanta
and a Fox Theater in Detroit; a Capitol Theatre in Passaic and another in Port
Chester. Filling in a city from a name alone would silently move a show across
the country, which is far worse than the gap it was meant to close. So a lookup
that cannot tell which room is meant **declines and says why**, the same way
`choose_event` declines on a night with two shows.

**It never overrules what a folder actually says.** A city already known is not
replaced, only confirmed; where the two disagree the gazetteer steps aside and
the folder wins, because the folder is evidence about this show and the table is
a general statement. Overrides still beat everything, being applied after.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

VENUES_NAME = "venues.yaml"

# "Theatre" and "Theater" are the same room, and so are "Amphitheater" and
# "Amphitheatre"; a possessive apostrophe may be straight, curly or absent.
# Folding these is the whole reason two spellings can find each other.
_EQUIVALENT = (
    ("amphitheatre", "amphitheater"),
    ("theatre", "theater"),
    ("centre", "center"),
)


def normalise(text: str | None) -> str:
    """A venue name reduced to what two spellings of one room have in common."""
    low = (text or "").lower()
    for a, b in _EQUIVALENT:
        low = low.replace(a, b)
    low = re.sub(r"\bthe\b", " ", low)
    return re.sub(r"[^a-z0-9]", "", low)


@dataclass
class Venue:
    name: str
    city: str | None = None
    state: str | None = None
    aliases: list = field(default_factory=list)
    # The years this name meant this place, for a festival that moved.
    # Lollapalooza toured the country from 1991 and only settled at Grant Park
    # in 2005; All Good was on Marvin's Mountaintop until 2011 and at Legend
    # Valley after it.  Written as [first, last], either end open.  Bands
    # already carry `active_years` for the same reason, so this is the pattern
    # the config already uses rather than a new idea.
    years: tuple | None = None
    # This name is too common to stand alone, whatever this table happens to
    # hold.  The ambiguity guard can only see rooms that are IN the gazetteer,
    # and that is not the same as rooms that exist: there is a State Theatre in
    # a dozen cities, this library had committed exactly one, and a Disco
    # Biscuits show whose tag said only "State Theatre" was therefore about to
    # be placed confidently in Minneapolis.  A generic name resolves only when
    # a city is already in hand - it will still correct a spelling, never invent
    # a place.
    generic: bool = False

    @property
    def keys(self) -> set:
        return {normalise(self.name)} | {normalise(a) for a in self.aliases} - {""}

    def covers(self, year: int | None) -> bool:
        """Was this name this place in that year?

        A venue with no years covers every year - which is the ordinary case, a
        room that does not move.  A year we do not know cannot rule anything
        out, so it matches everything and the caller is left with the ambiguity
        it would have had anyway.
        """
        if not self.years or year is None:
            return True
        first, last = self.years
        if first and year < first:
            return False
        return not (last and year > last)


@dataclass
class Match:
    """What the gazetteer was able to say, and how it knew."""

    venue: Venue
    why: str


# How alike two spellings of a city must be before they are taken for one
# place. "Raliegh" and "Raleigh" score 0.857; "Columbia" and "Columbus" score
# 0.75, and they are two different cities in two different states. The gap
# between those is the whole margin this rule has, so the threshold sits in it
# and the guards below do the rest of the work.
CITY_SIMILARITY = 0.85


def same_city(a: str | None, b: str | None) -> bool:
    """One city written two ways, or two cities?

    Only ever asked once the VENUE name has already matched, which is a strong
    prior that these are the same place - the question is whether somebody
    mistyped it. Three guards, because a false yes here moves a show:

    * the same first letter, so a wrong initial is never a typo;
    * a length within ONE. Two was too loose: "Portland" and "Poland" differ
      by two characters, share a first letter and score 0.857, and they are
      not the same place. Every real case here - Raliegh/Raleigh,
      Pittsburg/Pittsburgh, Cincinatti/Cincinnati, Springfeild/Springfield -
      is a transposition or a doubled letter, which moves the length by at
      most one;
    * and the similarity above.
    """
    x, y = (a or "").strip().lower(), (b or "").strip().lower()
    if not x or not y:
        return False
    if x == y:
        return True
    if x[0] != y[0] or abs(len(x) - len(y)) > 1:
        return False
    import difflib

    return difflib.SequenceMatcher(None, x, y).ratio() >= CITY_SIMILARITY


def _merge_same_room(venues: list) -> list:
    """Fold entries that describe one room into one entry.

    The file is seeded from the library and then added to by hand, so the same
    room arrives twice easily: the seed produced "Loring Commerce Centre,
    Limestone, ME" from a folder, and the festival section then added "Loring
    Air Force Base" in the same town listing that as an alias.  Two entries
    sharing a name in one city are not two rooms - they are one room written
    twice - and left alone they make every lookup on that name ambiguous, so
    the gazetteer would go quiet on exactly the rooms it knows best.

    Rooms of the same name in DIFFERENT cities are untouched.  That is the
    Orpheum case and it must stay ambiguous.
    """
    # Merging is transitive and one pass is not enough. Loring arrived three
    # times: "Loring Commerce Centre" and "Loring Air Force Base" from the
    # library, sharing no name, plus a hand-written entry listing the first as
    # an alias of the second. Only once that third one merges do the first two
    # have a name in common, and a single pass has already walked past them.
    previous = -1
    current = list(venues)
    while len(current) != previous:
        previous = len(current)
        current = _merge_pass(current)
    return current


def _merge_pass(venues: list) -> list:
    merged: list = []
    for v in venues:
        place = ((v.city or "").strip().lower(), (v.state or "").strip().upper())
        for other in merged:
            if ((other.city or "").strip().lower(),
                    (other.state or "").strip().upper()) != place:
                continue
            if not (other.keys & v.keys):
                continue
            # Keep the longer canonical name: "Loring Air Force Base" says more
            # than "Loring", and the shorter form survives as an alias anyway.
            keep, drop = (other, v) if len(other.name) >= len(v.name) else (v, other)
            names = list(dict.fromkeys(
                [*keep.aliases, *drop.aliases, drop.name, keep.name]))
            keep.aliases = [n for n in names if normalise(n) != normalise(keep.name)]
            if keep is v:
                merged[merged.index(other)] = keep
            keep.city = keep.city or drop.city
            keep.state = keep.state or drop.state
            keep.years = keep.years or drop.years
            keep.generic = keep.generic or drop.generic
            break
        else:
            merged.append(v)
    return merged


class Gazetteer:
    """Rooms by name, and by name-and-city where a name is shared."""

    def __init__(self, venues=None, path: Path | None = None):
        self.path = path
        # Copies, because merging assigns to the entries it keeps: building a
        # Gazetteer changed the aliases, city and years of the Venue objects the
        # caller passed in.  load() builds fresh ones, so only a caller holding
        # its own list could see it - which is why no test did.
        self.venues: list = _merge_same_room(
            [replace(v, aliases=list(v.aliases)) for v in (venues or [])])
        self._by_key: dict = {}
        self._by_key_city: dict = {}
        for v in self.venues:
            for key in v.keys:
                self._by_key.setdefault(key, []).append(v)
                if v.city:
                    # A list, not a single entry. Keyed on name and city but
                    # not state, so a Fox Theatre in Springfield IL and another
                    # in Springfield MA collide - and storing one would have
                    # silently overwritten the other and answered confidently
                    # with whichever loaded last.
                    self._by_key_city.setdefault(
                        (key, v.city.lower()), []).append(v)

    def __len__(self) -> int:
        return len(self.venues)

    @classmethod
    def load(cls, path: Path | str | None) -> "Gazetteer":
        """Missing is fine and means an empty table, not an error.

        The pipeline worked before this file existed and must keep working for
        anyone who has not got one.
        """
        if not path:
            return cls()
        p = Path(path)
        if not p.exists():
            return cls(path=p)
        import yaml

        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        venues = []
        for row in data.get("venues") or []:
            if not isinstance(row, dict) or not row.get("name"):
                continue
            venues.append(Venue(
                name=str(row["name"]).strip(),
                city=(str(row["city"]).strip() if row.get("city") else None),
                state=(str(row["state"]).strip().upper() if row.get("state") else None),
                aliases=[str(a) for a in (row.get("aliases") or [])],
                years=(tuple(row["years"])[:2] if row.get("years") else None),
                generic=bool(row.get("generic", False)),
            ))
        return cls(venues, path=p)

    @classmethod
    def load_many(cls, paths) -> "Gazetteer":
        """Several venue files as one table: the shipped one, then the user's.

        Two files naming the same room are merged the way one file listing it
        twice would be, so a user adding an alias need not repeat the room.
        """
        paths = [Path(p) for p in paths if p]
        present = [p for p in paths if p.exists()]
        if len(present) <= 1:
            return cls.load(present[0] if present else (paths[0] if paths else None))
        loaded = [cls.load(p) for p in present]
        venues = [v for g in loaded for v in g.venues]
        first = next((g.path for g in loaded if g.path is not None), None)
        return cls(venues, path=first)

    # -- lookup ----------------------------------------------------------
    def find(self, venue: str | None, city: str | None = None,
             year: int | None = None) -> Match | None:
        """The room this text names, or None.

        With a city in hand the answer is unambiguous and cheap.  Without one,
        a name shared by two rooms is refused: the Orpheum Theatre is in Boston
        and in San Francisco, and picking either would move a show a continent.

        The year is the exception, and only for a name that moved.  Lollapalooza
        names three places across its history and exactly one of them in any
        given year, so a dated folder is not ambiguous at all - it only looks
        that way to a lookup that ignores the date.
        """
        key = normalise(venue)
        if not key:
            return None
        if city:
            exact = self._by_key_city.get((key, city.strip().lower())) or []
            exact = [v for v in exact if v.covers(year)]
            if len(exact) == 1:
                return Match(exact[0],
                             "the gazetteer knows this room in %s" % exact[0].city)
            if len(exact) > 1:
                return None          # same name, same city name, two states
            # The city may simply be mistyped. Only rooms already matching on
            # NAME are considered, so this asks "is this the same city spelled
            # wrong", never "which city might this be".
            near = [v for v in (self._by_key.get(key) or [])
                    if v.covers(year) and same_city(city, v.city)]
            if len(near) == 1:
                return Match(near[0], "the gazetteer knows this room in %s, which "
                                      "is %r spelled correctly" % (near[0].city, city))
        hits = [v for v in (self._by_key.get(key) or []) if v.covers(year)]
        # A name flagged generic never places a show on its own, however few
        # rooms this table happens to list under it.
        if len(hits) == 1 and hits[0].generic and not city:
            return None
        if len(hits) == 1:
            all_named = self._by_key.get(key) or []
            if len(all_named) > 1 and year is not None:
                return Match(hits[0], "the gazetteer knows %d places by this name "
                                      "and only this one in %d" % (len(all_named), year))
            return Match(hits[0], "the gazetteer knows exactly one room by this name")
        return None

    def is_generic(self, venue: str | None) -> bool:
        """Is this a name too common to place a show on its own?"""
        return any(v.generic for v in (self._by_key.get(normalise(venue)) or []))

    def ambiguous(self, venue: str | None, year: int | None = None) -> list:
        """The places sharing this name, when more than one still fits."""
        hits = [v for v in (self._by_key.get(normalise(venue)) or []) if v.covers(year)]
        return hits if len(hits) > 1 else []

    def resolve(self, venue: str | None, city: str | None = None,
                state: str | None = None, year: int | None = None) -> tuple | None:
        """(venue, city, state, why), or None when nothing can be said.

        Two jobs, and it will do either alone:

        * **fill** - a room named with no city keeps its venue instead of losing
          it to the `City, ST` guard;
        * **canonicalise** - one room written two ways becomes one spelling, so
          a box set stops disagreeing with itself between nights.

        It never contradicts the folder.  A city already present and different
        from the table's means one of them is about a different room, and the
        folder is the one that is about *this show*, so the gazetteer withdraws.
        """
        if not venue:
            return None
        match = self.find(venue, city, year)
        if match is None:
            return None
        v = match.venue
        mistyped = False
        if city and v.city and city.strip().lower() != v.city.lower():
            if not same_city(city, v.city):
                return None                  # disagreement: the folder wins
            # Close enough to be the same place typed wrong. This is the one
            # case where the table corrects a folder rather than deferring to
            # it, and it is safe because the venue name already matched: the
            # folder is not naming a different city, it is misspelling this
            # one. "Raliegh" for Raleigh came out of a folder name and into
            # the gazetteer itself before it was caught.
            mistyped = True
        if state and v.state and state.strip().upper() != v.state.upper():
            return None
        new_city = city or v.city
        new_state = state or v.state
        renamed = v.name != venue
        filled = (new_city and not city) or (new_state and not state)
        if mistyped:
            new_city = v.city
        if not renamed and not filled and not mistyped:
            return None                      # nothing to add
        bits = []
        if renamed:
            bits.append("%r is how this library writes %r" % (v.name, venue))
        if filled:
            bits.append("the gazetteer places it in %s"
                        % ", ".join(x for x in (new_city, new_state) if x))
        if mistyped:
            bits.append("%r is how %r is spelled" % (v.city, city))
        return v.name, new_city, new_state, "; ".join(bits)
