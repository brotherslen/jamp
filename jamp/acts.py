"""`jamp acts`: which folders in the library are which act.

A new library is mostly acts the shipped list has never heard of, and a folder
the config cannot place is either blocked (no band) or, worse, filed under the
folder it happens to sit in.  This lists every top-level folder, says what the
config makes of it, and lets you add the unknown ones as acts or set them
aside - before any plan is made.

What you choose is written to ``acts.yaml`` in your user folder, which is laid
under your own ``jamp.yaml``, so a hand edit there always wins.  Nothing in
the library is read beyond folder names, and nothing in it is written.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import userdir
from .bands import _anywhere_alias_match, _leading_alias_match, suggest_bands
from .config import Config, load_config

KNOWN, INSIDE, IGNORED, UNKNOWN = "act", "acts inside", "ignored", "unknown"

# How many show folders to read the names of, per top-level folder.  Enough to
# tell an act's folder from a mixed container without walking a whole library.
_SAMPLE = 40

_ABBREV = re.compile(r"^[a-z][a-z0-9]{0,11}$")

ACTS_HEADER = """\
# Written by `jamp acts`: the acts you added and the folders you set aside.
# Laid over the shipped config and under your own jamp.yaml, so anything in
# jamp.yaml wins.  Safe to edit by hand; `jamp acts` keeps what is here.
"""


@dataclass
class Row:
    folder: str
    status: str
    subfolders: int
    act: str | None = None
    abbrev: str | None = None
    suggestions: list[str] = field(default_factory=list)
    # For a folder its name does not place: the acts its show folders name.
    inside: dict[str, int] = field(default_factory=dict)
    unplaced: int = 0


def guess(folder: str) -> tuple[str, str]:
    """A starting abbreviation and name for an act, from its folder name."""
    lead = re.split(r"\d", folder, maxsplit=1)[0]
    words = [w for w in re.split(r"[^A-Za-z']+", lead)
             if w and w.lower() not in ("the", "and")]
    name = re.sub(r"\s+", " ", lead).strip(" -_.") or folder.strip()
    if not words:
        return "xx", name
    if len(words) == 1:
        return words[0][:3].lower(), name
    return "".join(w[0] for w in words[:4]).lower(), name


def survey(root: Path, cfg: Config) -> list[Row]:
    ignore = {i.lower() for i in cfg.settings.ignore_folders
              if "/" not in i and "\\" not in i}
    rows = []
    with os.scandir(root) as it:
        entries = sorted((e for e in it if e.is_dir() and not e.name.startswith(".")),
                         key=lambda e: e.name.lower())
    for entry in entries:
        name = entry.name
        if name == cfg.settings.review_folder:
            continue
        try:
            subfolders = sum(1 for e in os.scandir(entry.path) if e.is_dir())
        except OSError:
            subfolders = 0
        if name.lower() in ignore:
            rows.append(Row(name, IGNORED, subfolders))
            continue
        hit = _leading_alias_match(name, cfg) or _anywhere_alias_match(name, cfg)
        if hit:
            band, _ = hit
            rows.append(Row(name, KNOWN, subfolders, band.name, band.abbrev))
        else:
            inside, unplaced = _acts_inside(Path(entry.path), cfg)
            placed = sum(inside.values())
            # Mostly placed by their own names: the folder is a container or a
            # nickname ("King Gizz"), and its shows already resolve.
            status = INSIDE if placed and placed >= 4 * unplaced else UNKNOWN
            rows.append(Row(name, status, subfolders,
                            suggestions=suggest_bands(name, cfg),
                            inside=inside, unplaced=unplaced))
    return rows


def _acts_inside(folder: Path, cfg: Config) -> tuple[dict[str, int], int]:
    """Acts named by the show folders under this one, by their names alone."""
    from .bands import resolve_band
    from .scan import is_year_dir

    names: list[str] = []
    try:
        for e in sorted(os.scandir(folder), key=lambda e: e.name.lower()):
            if not e.is_dir() or e.name.startswith("."):
                continue
            if is_year_dir(e.name):
                try:
                    names.extend(sorted(x.name for x in os.scandir(e.path) if x.is_dir()))
                except OSError:
                    pass
            else:
                names.append(e.name)
            if len(names) >= _SAMPLE:
                break
    except OSError:
        return {}, 0
    inside: dict[str, int] = {}
    unplaced = 0
    for name in names[:_SAMPLE]:
        band = resolve_band(name, cfg).band
        if band is None:
            unplaced += 1
        else:
            inside[band.abbrev] = inside.get(band.abbrev, 0) + 1
    return inside, unplaced


def abbrev_problem(abbrev: str, cfg: Config) -> str | None:
    """Why this abbreviation would misfile shows, or None."""
    if not _ABBREV.match(abbrev):
        return ("use lowercase letters and digits, starting with a letter, "
                "at most 12")
    for band in cfg.bands:
        if abbrev == band.abbrev or abbrev in band.prefixes:
            return "already used by %s" % band.name
    words = set(cfg.format_suffixes)
    for tokens in cfg.source_tokens.values():
        words.update(tokens)
    if abbrev in words:
        return "%r already means something in a folder name (a source or format)" % abbrev
    return None


def load_acts(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def save_acts(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True,
                          default_flow_style=False)
    path.write_text(ACTS_HEADER + "\n" + body, encoding="utf-8")
    if path.parent.resolve() == userdir.user_dir().resolve():
        # A user folder jamp has written always holds a config, as after init;
        # acts.yaml alone looks like settings this window cannot see.
        userdir.ensure_config()


def add_act(data: dict, folder: str, name: str, abbrev: str) -> dict:
    aliases = [name] + ([folder] if folder.lower() != name.lower() else [])
    data.setdefault("bands", []).append(
        {"abbrev": abbrev, "name": name, "prefixes": [abbrev], "aliases": aliases})
    return data


def add_ignore(data: dict, folder: str) -> dict:
    ignored = data.setdefault("settings", {}).setdefault("ignore_folders", [])
    if folder not in ignored:
        ignored.append(folder)
    return data


def _print_rows(rows: list[Row]) -> None:
    width = min(40, max((len(r.folder) for r in rows), default=10))
    for r in rows:
        if r.status == KNOWN:
            what = "%s (%s)" % (r.act, r.abbrev)
        elif r.status == IGNORED:
            what = "ignored - never scanned"
        else:
            what = "UNKNOWN" if r.status == UNKNOWN else "shows name their act"
            if r.inside:
                what += "  inside: " + ", ".join(
                    "%s %d" % (k, v) for k, v in sorted(r.inside.items(),
                                                        key=lambda kv: -kv[1])[:4])
                if r.unplaced:
                    what += ", unplaced %d" % r.unplaced
            if r.status == UNKNOWN and r.suggestions:
                what += "  maybe: %s" % ", ".join(r.suggestions)
        print("  %-*s %4d  %s" % (width, r.folder[:width], r.subfolders, what))


def _ask(prompt: str, default: str = "") -> str:
    try:
        got = input("%s%s: " % (prompt, " [%s]" % default if default else "")).strip()
    except EOFError:
        return default
    return got or default


def run(args) -> int:
    from .cli import load_settings

    root = args.root or (Path(load_settings()["root"])
                         if load_settings().get("root") else None)
    if root is None or not Path(root).is_dir():
        print("acts: no library folder - give it, or run jamp init first",
              file=sys.stderr)
        return 2
    root = Path(root).resolve()
    acts_path = userdir.user_dir() / userdir.ACTS_NAME
    data = load_acts(acts_path)
    cfg = load_config()
    # Before acts.yaml is written: afterwards a user folder this window cannot
    # see would look like one it had set up itself.
    problem = userdir.unseen_settings(cfg.user_path is not None)
    if problem:
        print("acts refused: %s" % problem, file=sys.stderr)
        return 2

    # Scripted answers first: the way a container or a script sets things up.
    for folder in args.ignore or ():
        add_ignore(data, folder)
        print("  ignoring %s" % folder)
    if args.add:
        folder = args.add
        abbrev_default, name_default = guess(folder)
        abbrev = (args.abbrev or abbrev_default).lower()
        problem = abbrev_problem(abbrev, cfg)
        if problem:
            print("acts: %s: %s - choose another with --abbrev" % (abbrev, problem),
                  file=sys.stderr)
            return 2
        add_act(data, folder, args.name or name_default, abbrev)
        print("  added %s as %s (%s)" % (folder, args.name or name_default, abbrev))
    if args.ignore or args.add:
        save_acts(acts_path, data)
        cfg = load_config()

    rows = survey(root, cfg)
    print("Top-level folders in %s" % root)
    print("  (folder, subfolders, what the config makes of it)")
    _print_rows(rows)
    unknown = [r for r in rows if r.status == UNKNOWN]
    print()
    if not unknown:
        print("Every folder is an act or set aside.")
        return 0
    print("%d folder(s) are no act the config knows, and neither are most of "
          "the shows in them. Those shows would be blocked, not guessed."
          % len(unknown))

    if args.ignore or args.add or not (sys.stdin and sys.stdin.isatty()):
        print("Run `jamp acts` in a terminal to go through them, or script it:\n"
              '  jamp acts --add "<folder>" --abbrev <abbrev> [--name "<act>"]\n'
              '  jamp acts --ignore "<folder>"')
        return 0

    print("For each: [a]dd it as an act, [i]gnore it (not live shows), "
          "or [s]kip for now. [q] stops and saves.")
    print("The abbreviation you choose becomes the start of every folder name "
          "for that act (gd1977-05-08...), so pick it with care.")
    changed = False
    for r in unknown:
        print()
        print("%s  (%d subfolders%s)" % (
            r.folder, r.subfolders,
            "; some shows name " + ", ".join(sorted(r.inside)) if r.inside else ""))
        choice = _ask("  [a/i/s/q]", "s").lower()[:1]
        if choice == "q":
            break
        if choice == "i":
            add_ignore(data, r.folder)
            changed = True
        elif choice == "a":
            abbrev_default, name_default = guess(r.folder)
            name = _ask("  act name", name_default)
            while True:
                abbrev = _ask("  abbreviation", abbrev_default).lower()
                problem = abbrev_problem(abbrev, cfg)
                if not problem:
                    break
                print("  %s: %s" % (abbrev, problem))
            add_act(data, r.folder, name, abbrev)
            # Later answers must not reuse this abbreviation either.
            save_acts(acts_path, data)
            cfg = load_config()
            changed = True
    if changed:
        save_acts(acts_path, data)
        print()
        print("Saved to %s" % acts_path)
        print("Next, a dry run for one act:  jamp plan --artist \"<folder>\"")
    return 0
