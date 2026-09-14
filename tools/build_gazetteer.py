"""Seed jamp/data/venues.yaml from what the library and the overrides already know.

The gazetteer is a maintained config file, not a generated artefact - but typing
two hundred venues by hand to start it would be silly when every one of them is
already written down somewhere.  This reads them out, groups the spellings of
each room together, and writes a first version to read and correct.

Two sources, weighted differently on purpose:

* **overrides.yaml** (the user folder's), counted triple.  Those are places stated by hand,
  and where a hand-stated spelling disagrees with the library's it is the one to
  keep - that is what stating it meant.
* **the settled folder names**, counted once each.  Between them they cover far
  more rooms than the overrides do, and the commonest spelling of a room across
  many folders is a good default for its canonical form.

Rerunning it does not overwrite a file that exists: this bootstraps, and after
that the config is edited like any other.

    py tools/build_gazetteer.py <ROOT> --out jamp/data/venues.yaml
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from jamp import userdir  # noqa: E402
from jamp.naming import state_code  # noqa: E402
from jamp.venues import normalise  # noqa: E402

OVERRIDE_WEIGHT = 3


def triples_from_overrides(path: pathlib.Path) -> collections.Counter:
    out: collections.Counter = collections.Counter()
    if not path.exists():
        return out
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for v in (data.get("folders") or {}).values():
        if not isinstance(v, dict):
            continue
        venue, city, st = v.get("venue"), v.get("city"), v.get("state")
        if venue and city and st:
            out[(str(venue).strip(), str(city).strip(), str(st).strip().upper())] \
                += OVERRIDE_WEIGHT
    return out


def triples_from_library(root: pathlib.Path) -> collections.Counter:
    """Every "Venue, City, ST" this pipeline has already committed.

    Only three-part places count.  Two parts is a city and a state with no
    venue, which says nothing about any room.
    """
    out: collections.Counter = collections.Counter()
    for state_file in root.rglob(".etree_state.json"):
        name = state_file.parent.name
        if " - " not in name:
            continue
        parts = [p.strip() for p in name.split(" - ", 1)[1].split(",") if p.strip()]
        if len(parts) < 3:
            continue
        st = state_code(parts[-1])
        if not st or len(st) != 2 or not st.isalpha():
            continue
        out[(", ".join(parts[:-2]), parts[-2], st)] += 1
    return out


def group(triples: collections.Counter) -> list:
    """One entry per room: the commonest spelling, and the rest as aliases.

    Keyed on the normalised venue AND the city, because a venue name is not
    unique - there is an Orpheum Theatre in Boston and another in San Francisco,
    and a Fox Theatre in Atlanta and a Fox Theater in Detroit.  Merging those on
    name alone would put a show in the wrong city, which is worse than leaving
    the gazetteer silent about it.
    """
    rooms: dict = collections.defaultdict(collections.Counter)
    cities: dict = collections.defaultdict(collections.Counter)
    for (venue, city, st), n in triples.items():
        key = (normalise(venue), city.lower(), st)
        rooms[key][venue] += n
        cities[key][city] += n              # keep the city's own capitalisation

    entries = []
    for key, spellings in rooms.items():
        city_now = cities[key].most_common(1)[0][0]
        # A "venue" that is just the city again is not a room. These come from
        # two-part places that happened to parse into three.
        if normalise(list(spellings)[0]) == normalise(city_now):
            continue
        # Capitalisation first, then frequency. "orpheum theatre" appearing
        # four times and "Orpheum Theatre" once does not make the lower-case one
        # the name of the room - it makes it the spelling four folders happened
        # to use. After that the commonest wins, then the longer, which is
        # usually the fuller name ("Great Woods Amphitheatre" over "Great
        # Woods").
        def rank(kv):
            name, n = kv
            all_lower = name == name.lower()
            return (all_lower, -n, -len(name), name)
        ranked = sorted(spellings.items(), key=rank)
        canonical = ranked[0][0]
        entries.append({
            "name": canonical,
            "city": city_now,
            "state": key[2],
            # Every other spelling seen. The canonical one is not repeated here;
            # Gazetteer matches on it anyway.
            "aliases": sorted({v for v, _ in ranked[1:]}),
        })
    entries.sort(key=lambda e: (e["state"], e["city"], e["name"]))
    return entries


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("root", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path,
                    default=pathlib.Path("jamp/data/venues.yaml"))
    ap.add_argument("--overrides", type=pathlib.Path,
                    default=userdir.resolve(userdir.user_overrides_path(),
                                            userdir.LEGACY_OVERRIDES)[0])
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing file; by default this refuses, "
                         "because the gazetteer is maintained after it is seeded")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if args.out.exists() and not args.force:
        print("%s already exists. It is maintained by hand once seeded, so this "
              "refuses to overwrite it. Pass --force if that is really what you "
              "want." % args.out, file=sys.stderr)
        return 2

    triples = triples_from_overrides(args.overrides)
    triples += triples_from_library(args.root.resolve())
    entries = group(triples)
    multi = [e for e in entries if e["aliases"]]

    head = (
        "# Venues: the rooms this library knows, and the ways they get written.\n"
        "#\n"
        "# This exists because the same room is spelled several ways - Alpine\n"
        "# Valley Music Theater and Music Theatre, Riverport Amphitheater and\n"
        "# Amphitheatre, RFK Stadium and Robert F. Kennedy Stadium - and because\n"
        "# a folder often names a room without naming its city, which the\n"
        "# City, ST guard then rejects, losing the venue entirely.\n"
        "#\n"
        "# A name here is NOT assumed unique. There is an Orpheum Theatre in\n"
        "# Boston and another in San Francisco. A lookup that cannot tell which\n"
        "# room is meant declines rather than guessing - see jamp/venues.py.\n"
        "#\n"
        "# Seeded by tools/build_gazetteer.py from the settled library and from\n"
        "# the places stated by hand in overrides.yaml; maintained here after.\n"
        "\n")
    args.out.write_text(
        head + yaml.safe_dump({"venues": entries}, allow_unicode=True,
                              sort_keys=False, width=100),
        encoding="utf-8")
    print("%d venues written to %s" % (len(entries), args.out))
    print("  %d of them are written more than one way in the library" % len(multi))
    for e in multi[:12]:
        print("    %-34s %-18s %s" % (e["name"][:34], "%s, %s" % (e["city"], e["state"]),
                                      " | ".join(e["aliases"][:3])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
