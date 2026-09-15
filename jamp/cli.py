"""Command line entry point.

    py -m jamp scan   "D:\\Music\\Live" --out-dir "D:\\jamp-reports"
    py -m jamp plan   "D:\\Music\\Live" --out-dir "D:\\jamp-reports"
    py -m jamp apply  "D:\\Music\\Live" --out-dir "D:\\jamp-reports" --commit
    py -m jamp lookup "D:\\Music\\Live" --out-dir "D:\\jamp-reports"
    py -m jamp check  "D:\\Music\\Live" --out-dir "D:\\jamp-reports"

Each has its older name as well - phase0, phase1, phase2, phase3 and complete -
and both always will: the old names are in scripts, notes and every report
this tool has written (phase1_plan.json, phase2_committed.csv), which keep them.

Dry run is not a flag you remember to pass - it is what every command does
without --commit.  Two things write, and only with --commit: apply, and
lookup --apply, which fills tags named in a report that already exists.
scan, plan and check refuse --commit outright.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

from . import __version__, confirm, phase0, phase1, phase2
from .config import ConfigError, load_config
from .overrides import Overrides
from . import userdir
from .report import ensure_out_dir, run_lock


# The name each command goes by, for the names it started with.  The old ones
# stay accepted for good; reports keep them too (phase1_plan.json).
COMMAND_NAMES = {
    "phase0": "scan",
    "phase1": "plan",
    "phase2": "apply",
    "phase3": "lookup",
    "complete": "check",
}
INTERNAL_NAMES = {new: old for old, new in COMMAND_NAMES.items()}


def settings_path() -> Path:
    """Where remembered paths live: the user folder, not the code.

    Nothing in this tool hardcodes a library location - ROOT and --out-dir are
    arguments - but typing two absolute paths on every run is friction that
    nobody else should inherit.  This remembers them per user, and an explicit
    argument always wins over what is remembered.
    """
    return userdir.resolve(userdir.user_paths_path(), userdir.LEGACY_PATHS)[0]


def load_settings() -> dict:
    p = settings_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(values: dict) -> Path:
    # Always saved to the user folder; a legacy file is read once more here and
    # carried across, then no longer consulted.
    current = load_settings()
    p = userdir.user_paths_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    current.update({k: str(v) for k, v in values.items() if v})
    p.write_text(json.dumps(current, indent=2) + chr(10), encoding="utf-8")
    return p


def _settings_record(cfg, overrides_path: Path | None) -> dict[str, str]:
    """Every settings file that shapes a plan, with a digest of what it held.

    A plan is only as good as the settings it was made with.  A dry run once
    ran in a window that could not see the user folder: no ignore_folders, no
    overrides, fifteen renames nobody wanted - and the commit after it would
    have matched its plan in every respect the check then looked at.
    """
    import hashlib

    from .config import VENUES_NAME

    files = {
        "shipped config": cfg.path,
        "shipped venues": Path(cfg.path).parent / VENUES_NAME,
        "your config": userdir.user_config_path(),
        "your acts": userdir.user_dir() / "acts.yaml",
        "your venues": userdir.user_dir() / VENUES_NAME,
        "your overrides": overrides_path,
    }
    # What each file held, not where it was.  A standalone build unpacks itself
    # into a new temporary folder on every run, so the shipped config's path
    # differed between every dry run and its commit and every commit was
    # refused - found by the first run of a downloaded build, not by the tests,
    # which run from source where the path never moves.
    out = {}
    for label, path in files.items():
        if path is None:
            out[label] = "none"
            continue
        try:
            out[label] = hashlib.sha1(Path(path).read_bytes()).hexdigest()
        except OSError:
            out[label] = "absent"
    return out


def _settings_problem(cfg) -> str | None:
    return userdir.unseen_settings(cfg.user_path is not None)


def _plan_check(out_dir: Path, root: Path, artists: set[str] | None,
                reclassify: bool = False, unnest: bool = False,
                settings: dict[str, str] | None = None) -> str | None:
    """Why a commit should not go ahead yet, or None.

    A commit is meant to follow a dry run somebody read.  Requiring the plan
    for the same library and scope in the same reports folder makes that the
    path of least resistance rather than advice in a document.
    """
    plan = out_dir / "phase1_plan.json"
    if not plan.exists():
        return "no dry run (jamp plan) in %s" % out_dir
    try:
        data = json.loads(plan.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "the dry run's plan in %s cannot be read: %s" % (out_dir, exc)
    # Format 2 records what each folder looked like, which the commit checks.
    if ("root" not in data or "reclassify" not in data or "settings" not in data
            or data.get("plan_format", 1) < 2):
        return ("the dry run's plan in %s predates this check; run jamp plan again"
                % out_dir)
    if data.get("root") != str(root):
        return ("the dry run's plan in %s is for %s, not %s"
                % (out_dir, data.get("root"), root))
    wanted = sorted(artists) if artists else None
    if data.get("scope") != wanted:
        return ("the dry run's plan in %s covered %s, not %s"
                % (out_dir, ", ".join(data["scope"]) if data.get("scope") else "the whole library",
                   ", ".join(wanted) if wanted else "the whole library"))
    for flag, asked in (("--reclassify", reclassify), ("--unnest", unnest)):
        made = bool(data.get(flag.strip("-")))
        if made != bool(asked):
            return ("the dry run's plan in %s was made %s %s, and this commit is %s it"
                    % (out_dir, "with" if made else "without", flag,
                       "without" if made else "with"))
    if settings is not None:
        then = data.get("settings") or {}
        changed = [label for label in sorted(set(then) | set(settings))
                   if then.get(label) != settings.get(label)]
        if changed:
            return ("the dry run's plan in %s was made with different settings: %s "
                    "(was %s, now %s)"
                    % (out_dir, changed[0], then.get(changed[0], "not recorded"),
                       settings.get(changed[0], "not recorded")))
    return None


def _identity_reports(out_dir: Path) -> tuple[Path, Path] | None:
    """The newest pair of durations and folder reports in `out_dir`, or None.

    plan and scan both write one: plan's describes the library as it is after
    any commit since, and is what a normal run leaves; scan's is older news.
    """
    pairs = []
    for prefix in ("phase1", "phase0"):
        identity = out_dir / ("%s_audio_identity.csv" % prefix)
        folders = out_dir / ("%s_folders.csv" % prefix)
        if identity.exists() and folders.exists():
            pairs.append((identity.stat().st_mtime, identity, folders))
    if not pairs:
        return None
    _, identity, folders = max(pairs)
    return identity, folders


def _read_phish_key(args):
    """The phish.net key, or None.

    None is a normal state, not an error: phish.in covers the same catalogue
    without a key, so a user who has not asked phish.net for one still gets
    venues and titles - and a better class of title, since phish.in has track
    durations and phish.net does not.
    """
    if not getattr(args, "phishnet_key", None):
        return None
    from .phishnet import MissingKey, read_key

    try:
        key = read_key(args.phishnet_key)
        print("  phish.net: key loaded from %s" % args.phishnet_key)
        return key
    except MissingKey as exc:
        print("  phish.net: %s - phish.in will be used instead" % exc, file=sys.stderr)
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jamp",
        description="Rename, tag and check a live music archive without destroying "
                    "anything.  Every command is a dry run unless given --commit, "
                    "and reports never go inside ROOT.  The usual order: scan, "
                    "plan, apply --commit, then lookup and check.",
    )
    parser.add_argument("--version", action="version", version="jamp %s" % __version__)
    # A metavar, or the usage line lists every name and alias in one long brace.
    sub = parser.add_subparsers(dest="phase", required=True, metavar="COMMAND")

    for name, help_text in (
        ("phase0", "what is in the library: an inventory, and which folders "
                   "hold the same audio"),
        ("phase1", "the dry run: every proposed rename and tag change"),
        ("phase2", "carry out the plan: renames, tags and checksum files; needs "
                   "--commit"),
        ("phase3", "ask archive.org, phish.in, phish.net, jerrybase and the MMJ "
                   "archive to fill in what the files never said; a report, "
                   "unless --apply --commit"),
        ("complete", "is a recording missing songs? compares the durations plan "
                     "or scan measured against the known setlist; reports only"),
    ):
        p = sub.add_parser(COMMAND_NAMES[name], aliases=[name], help=help_text)
        p.add_argument("root", metavar="ROOT", type=Path, nargs="?", default=None,
                       help="the library (read only). May be omitted once `jamp "
                            "init` or --remember has saved one")
        p.add_argument("--out-dir", type=Path, default=None,
                       help="where reports go; must be outside ROOT")
        p.add_argument("--remember", action="store_true",
                       help="save ROOT, --out-dir, --cache, --shows and "
                            "--phishnet-key as this installation's defaults")
        p.add_argument("--config", type=Path, default=None,
                       help="path to the shipped jamp.yaml to start from (default: the "
                            "one in this package). Your own jamp.yaml in the user "
                            "folder is laid over it either way")
        p.add_argument("--overrides", type=Path, default=None,
                       help="path to overrides.yaml: answers you have given that the "
                            "files do not contain (default: the one in your user folder)")
        p.add_argument("--commit", action="store_true",
                       help="apply only: actually write. Refused by scan, plan and check")
        p.add_argument("--today", type=_dt.date.fromisoformat, default=None,
                       help="pretend today is this date (for reproducible tests)")
        p.add_argument("--artist", action="append", metavar="FOLDER", default=None,
                       help="only look inside this top-level folder of ROOT; repeat "
                            "to name several. ROOT stays the library root, so the "
                            "artist folder is still available as evidence")
        if name == "complete":
            p.add_argument("--shows", type=Path, default=None,
                           help="the distilled show database; this is the only "
                                "reference it reads and it never goes online "
                                "(default: shows.sqlite beside lookup's cache, "
                                "built or refreshed from the cache when needed)")
            p.add_argument("--cache", type=Path, default=None,
                           help="lookup's cache to build the show database "
                                "from (default: the one lookup uses)")
            p.add_argument("--identity", type=Path, default=None,
                           help="an audio identity report (default: the newer "
                                "of phase1_audio_identity.csv and "
                                "phase0_audio_identity.csv in --out-dir)")
            p.add_argument("--folders", type=Path, default=None,
                           help="the folders report written with it (default: "
                                "phase1_folders.csv or phase0_folders.csv, "
                                "whichever goes with that identity report)")
        if name == "phase3":
            p.add_argument("--cache", type=Path, default=None,
                           help="the cache of everything lookup has fetched "
                                "(default: archive.sqlite in your cache folder, "
                                "or one already in <out-dir>/cache)")
            p.add_argument("--shows", type=Path, default=None,
                           help="a distilled show database from "
                                "tools/distill_cache.py. Consulted before the "
                                "network, so a 2 MB file stands in for a 96 MB "
                                "cache and needs no key")
            p.add_argument("--seed", action="store_true",
                           help="fetch what every source knows about every settled "
                                "folder, complete or not, and stop. Warms the cache "
                                "so --offline answers for the whole library rather "
                                "than for the folders that happened to want work")
            p.add_argument("--apply", action="store_true",
                           help="write the tags named in phase3_proposals.json. "
                                "Refused unless that file is already in --out-dir, "
                                "so the report has to exist first. Fill-only is "
                                "re-checked against the file on disk, not trusted "
                                "from the proposal")
            p.add_argument("--phishnet-key", type=Path, default=None,
                           help="file holding the phish.net API key, one line. "
                                "Requested free from https://phish.net/api/keys/ - "
                                "the private key, not the public one. Keep it "
                                "outside the repo; it is never printed, logged or "
                                "stored in the cache")
            p.add_argument("--offline", action="store_true",
                           help="answer only from the cache and never open a "
                                "connection; the second run of a scope is fully "
                                "reproducible this way")
        if name == "phase0":
            p.add_argument("--verify-audio", action="store_true",
                           help="decode every FLAC and compare it against the MD5 "
                                "in its own header. Catches a file that decodes "
                                "cleanly to audio that is not what it claims - "
                                "damage that reads, tags and plays like a healthy "
                                "file. Hours rather than minutes, which is why it "
                                "is opt-in; the identity index it builds on is "
                                "always read and costs nothing")
            p.add_argument("--workers", type=int, default=4,
                           help="parallel decodes for --verify-audio (default 4)")
            p.add_argument("--tag-sample", type=int, default=None,
                           help="read tags from at most this many files per folder "
                                "(0 = all; default comes from the config)")
        if name in ("phase1", "phase2"):
            p.add_argument("--reclassify", action="store_true",
                           help="re-examine folders a previous commit already settled, "
                                "ignoring their .etree_state.json. A commit needs "
                                "it exactly when its dry run had it")
            p.add_argument("--unnest", action="store_true",
                           help="lift a show out of a container folder that holds "
                                "nothing else; the empty container is left behind. "
                                "In plan it lists the lifts; a commit with it "
                                "needs a dry run with it")
        if name == "phase2":
            p.add_argument("--settle", action="store_true",
                           help="rename nothing; record every folder already named by "
                                "this tool as settled, so re-runs leave it alone")
            p.add_argument("--quarantine-lossy", action="store_true",
                           help="move MP3s that duplicate a lossless copy of the same "
                                "recording into the review folder; nothing is deleted")
            p.add_argument("--one-pass", action="store_true",
                           help="commit a single pass. By default a commit keeps "
                                "going until a pass changes nothing: each commit "
                                "changes what the next read sees, so the answer "
                                "converges over a few passes")
            p.add_argument("--until-settled", action="store_true",
                           help="the default for --commit; accepted so older "
                                "commands keep working")
            p.add_argument("--skip-plan-check", action="store_true",
                           help="commit even though --out-dir holds no plan "
                                "dry run of this library and scope. Not "
                                "recommended: reading that plan is the check")
            p.add_argument("--include-merges", action="store_true",
                           help="also apply the split-show merges, which are the only "
                                "changes that move files between folders")

    p = sub.add_parser("init", help="set up: remember your library and reports "
                                    "folders, and create your own config files")
    p.add_argument("--root", type=Path, default=None,
                   help="your live music library; asked for when not given")
    p.add_argument("--out-dir", type=Path, default=None,
                   help="where reports go, outside the library")
    p.add_argument("--phishnet-key", type=Path, default=None,
                   help="file holding a phish.net API key, if you have one")
    p.add_argument("--ffmpeg", default=None, help="path to ffmpeg, if not found")
    p.add_argument("--ask", action="store_true",
                   help="ask again even for paths already remembered")
    p = sub.add_parser("acts", help="which folders in the library are which act; "
                                    "add the unknown ones or set them aside")
    p.add_argument("root", metavar="ROOT", type=Path, nargs="?", default=None,
                   help="the library (default: the one jamp init remembered)")
    p.add_argument("--add", metavar="FOLDER", default=None,
                   help="add this top-level folder as an act")
    p.add_argument("--abbrev", default=None,
                   help="with --add: the act's abbreviation (default: a guess)")
    p.add_argument("--name", default=None,
                   help="with --add: the act's name (default: from the folder)")
    p.add_argument("--ignore", metavar="FOLDER", action="append", default=None,
                   help="never scan this folder; repeat for several")
    p = sub.add_parser("tidy", help="remove folders with nothing at all in them, "
                                    "such as wrappers an unnest or merge emptied")
    p.add_argument("root", metavar="ROOT", type=Path, nargs="?", default=None,
                   help="the library (default: remembered)")
    p.add_argument("--out-dir", type=Path, default=None,
                   help="where reports go (default: remembered)")
    p.add_argument("--artist", action="append", metavar="FOLDER", default=None,
                   help="only inside this top-level folder; repeat for several")
    p.add_argument("--commit", action="store_true",
                   help="actually remove them. Refused without a dry run of the "
                        "same scope in --out-dir")
    p.add_argument("--skip-plan-check", action="store_true",
                   help="commit without that dry run")
    for name, help_text in (
        ("unpack", "extract the ZIP files in the library beside themselves, each "
                   "file checked against the ZIP's CRC"),
        ("convert", "convert SHN files to FLAC with ffmpeg, each proven to decode "
                    "to identical audio"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("root", metavar="ROOT", type=Path, nargs="?", default=None,
                       help="the library (default: remembered)")
        p.add_argument("--out-dir", type=Path, default=None,
                       help="where reports go (default: remembered)")
        p.add_argument("--artist", action="append", metavar="FOLDER", default=None,
                       help="only inside this top-level folder; repeat for several")
        p.add_argument("--commit", action="store_true",
                       help="actually do it. Refused without a dry run of the same "
                            "scope in --out-dir")
        p.add_argument("--skip-plan-check", action="store_true",
                       help="commit without that dry run")
        p.add_argument("--set-aside", action="store_true",
                       help="move originals whose new copy is proven into "
                            "_etree_review/originals/; nothing is deleted")
        if name == "convert":
            p.add_argument("--workers", type=int, default=4,
                           help="files converted at once (default 4)")
            p.add_argument("--ffmpeg", default=None, help="path to ffmpeg, if not found")
    p = sub.add_parser("restore", help="put committed folders back as they were: "
                                       "tags, track names, checksum files, folder name")
    p.add_argument("folders", metavar="FOLDER", nargs="+", type=Path,
                   help="a committed folder, as a path inside the library")
    p.add_argument("--root", type=Path, default=None,
                   help="the library (default: the one jamp init remembered)")
    p.add_argument("--out-dir", type=Path, default=None,
                   help="where reports go (default: remembered)")
    p.add_argument("--commit", action="store_true",
                   help="actually restore. Refused without a dry run of the same "
                        "folders in --out-dir")
    p.add_argument("--skip-plan-check", action="store_true",
                   help="commit without that dry run")
    p.add_argument("--log", action="append", type=Path, default=None,
                   help="a phase2_committed.csv to read renames from, for files the "
                        "backup cannot account for; repeat for several")
    p.add_argument("--partial", action="store_true",
                   help="restore a folder even when some files cannot be matched; "
                        "those are left as they are")
    p = sub.add_parser("doctor", help="check the install, the config and the "
                                      "remembered folders; changes nothing")
    p.add_argument("--ffmpeg", default=None, help="path to ffmpeg, if not found")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # argparse reports whichever name was typed.  Everything below, and every
    # report, speaks the internal name.
    args.phase = INTERNAL_NAMES.get(args.phase, args.phase)

    if args.phase in ("init", "doctor"):
        from . import install

        return install.run_init(args) if args.phase == "init" else install.run_doctor(args)
    if args.phase in ("unpack", "convert"):
        from . import convert, unpack

        return (unpack if args.phase == "unpack" else convert).run(args)
    if args.phase == "tidy":
        from . import tidy

        return tidy.run(args)
    if args.phase == "restore":
        from . import restore

        return restore.run(args)
    if args.phase == "acts":
        from . import acts

        return acts.run(args)

    if args.commit and args.phase != "phase2" and not (
            args.phase == "phase3" and getattr(args, "apply", False)):
        print(
            "--commit is refused here: only apply, and lookup --apply, write.\n"
            "Review the CSVs in --out-dir first, then run apply --commit.",
            file=sys.stderr,
        )
        return 2

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2
    # Before --remember writes anything there: afterwards a user folder this
    # window cannot see would look like one it had just set up.
    problem = _settings_problem(cfg)
    if problem:
        print("refused: %s" % problem, file=sys.stderr)
        return 2

    # An explicit argument always wins; a remembered path fills a blank.
    remembered = load_settings()
    for name in ("root", "out_dir", "cache", "shows", "phishnet_key"):
        if getattr(args, name, None) is None and remembered.get(name):
            setattr(args, name, Path(remembered[name]))
    if getattr(args, "remember", False):
        saved = save_settings({n: getattr(args, n, None)
                               for n in ("root", "out_dir", "cache", "shows",
                                         "phishnet_key")})
        print("  remembered these paths in %s" % saved)
        # A user folder jamp has written always holds a config, as after init.
        created = userdir.ensure_config()
        if created:
            print("  created  %s" % created)
            try:
                cfg = load_config(args.config)
            except ConfigError as exc:
                print("config error: %s" % exc, file=sys.stderr)
                return 2

    if args.root is None or args.out_dir is None:
        print("ROOT and --out-dir are required. Give them once with "
              "--remember and later runs can leave them out:"
              + chr(10) +
              '  jamp plan "<library>" --out-dir "<reports>" --remember',
              file=sys.stderr)
        return 2
    root = args.root.resolve()
    if not root.is_dir():
        print("ROOT is not a directory: %s" % root, file=sys.stderr)
        return 2

    try:
        out_dir = ensure_out_dir(args.out_dir, root)
    except ValueError as exc:
        print("%s" % exc, file=sys.stderr)
        return 2

    print("  config: %s" % cfg.path)
    if cfg.acts_path:
        print("  your acts: %s" % cfg.acts_path)
    if cfg.user_path:
        print("  your config: %s" % cfg.user_path)
    else:
        print("  your config: none yet (%s)" % userdir.user_config_path())
    if args.overrides:
        overrides_path = args.overrides
    else:
        overrides_path, legacy = userdir.resolve(userdir.user_overrides_path(),
                                                 userdir.LEGACY_OVERRIDES)
        if legacy:
            print("  note: overrides are being read from inside the tool (%s); "
                  "move the file to %s" % (overrides_path,
                                           userdir.user_overrides_path()),
                  file=sys.stderr)
    try:
        overrides = Overrides.load(overrides_path)
    except (ValueError, OSError) as exc:
        print("overrides error: %s" % exc, file=sys.stderr)
        return 2
    if len(overrides):
        print("  overrides: %d folder(s) from %s" % (len(overrides), overrides_path))
    settings = _settings_record(cfg, overrides_path)

    artists = set(args.artist) if args.artist else None
    if artists:
        missing = [a for a in artists if not (root / a).is_dir()]
        if missing:
            print("no such folder under ROOT: %s" % ", ".join(missing), file=sys.stderr)
            return 2
        print("  scoped to: %s" % ", ".join(sorted(artists)))

    if args.phase == "complete":
        from .showstore import ShowStore
        from . import complete as _complete

        identity, folders = args.identity, args.folders
        if identity is None or folders is None:
            found = _identity_reports(out_dir)
            if found is None:
                print("no durations to check in %s. Run jamp plan (or jamp scan) "
                      "into it first - check reads their reports rather than the "
                      "library, so the durations are already measured." % out_dir,
                      file=sys.stderr)
                return 2
            identity = identity or found[0]
            folders = folders or found[1]
            print("  durations from %s" % Path(identity).name)
        for needed, what in ((identity, "--identity"), (folders, "--folders")):
            if not Path(needed).exists():
                print("%s does not exist: %s" % (what, needed), file=sys.stderr)
                return 2
        if args.shows is None:
            from . import distill

            cache_path, _why = userdir.cache_path(args.cache, out_dir)
            args.shows = distill.shows_path_for(cache_path)
            if distill.is_stale(cache_path, args.shows):
                if not Path(cache_path).exists():
                    print("no show database yet: lookup has not fetched anything "
                          "(no cache at %s). Run `jamp lookup --seed` first, or "
                          "give --shows." % cache_path, file=sys.stderr)
                    return 2
                print("  building the show database from %s ..." % cache_path, flush=True)
                got = distill.distill(cache_path, args.shows)
                print("  %d shows, %d tracks -> %s" % (got["shows"], got["tracks"], args.shows))
        store = ShowStore(args.shows)
        if not store:
            print("no usable show database at %s. Run `jamp lookup --seed` to "
                  "build one, or point --shows at another." % args.shows,
                  file=sys.stderr)
            return 2
        shows = _complete.in_scope(_complete.load_shows(Path(identity), Path(folders)),
                                   artists)
        print("  %d folders with durations, %d shows in %s"
              % (len(shows), store.stats()["shows"], args.shows))
        # Held like every other command's, so two runs cannot interleave their
        # reports and each keeps the one before it in history/.
        with run_lock(out_dir, "complete"):
            counts = _complete.run(shows, cfg, store, out_dir,
                                   progress=lambda m: print(m, flush=True))
        store.close()
        print("Completeness: %d folders judged" % counts.pop("folders", 0))
        for k in (_complete.TRUNCATED, _complete.SHORT_BY_COUNT,
                  _complete.STATED_PARTIAL, _complete.NOT_COMPARABLE,
                  _complete.REFERENCE_SHORTER, _complete.NO_REFERENCE,
                  _complete.COMPLETE):
            if counts.get(k):
                print("  %-16s %4d" % (k, counts[k]))
        print("  nothing was read from or written to the library")
        print("  reports: %s" % out_dir)
        return 0

    if args.phase == "phase3" and getattr(args, "seed", False):
        from .httpcache import HttpCache

        cache_path, why = userdir.cache_path(args.cache, out_dir)
        print("  cache: %s (%s)" % (cache_path, why))
        phish_key = _read_phish_key(args)
        with HttpCache(cache_path, offline=args.offline) as cache:
            stats = confirm.seed_cache(root, cfg, cache, artists=artists,
                                       phish_key=phish_key,
                                       progress=lambda m: print(m, flush=True))
            c = cache.stats()       # while the database is still open
        print("Phase 3 seed: %d distinct shows" % stats.pop("distinct shows", 0))
        for k, v in sorted(stats.items()):
            print("  %-30s %d" % (k, v))
        print("  cache now: %d URLs, %.1f MB (%d served, %d fetched)"
              % (c["urls"], c["bytes"] / 1048576.0, c["hits"], c["fetches"]))
        # The small database `complete` and `--shows` read, kept beside the cache.
        from . import distill

        shows = distill.shows_path_for(cache_path)
        got = distill.distill(cache_path, shows)
        print("  show database: %d shows, %d tracks, %.1f MB -> %s"
              % (got["shows"], got["tracks"], got["shows_mb"], shows))
        print("  nothing was written inside %s" % root)
        return 0

    if args.phase == "phase3" and args.apply:
        proposals = out_dir / "phase3_proposals.json"
        if not proposals.exists():
            print("--apply is refused: %s does not exist. "
                  "Run jamp lookup without --apply first, and read the report."
                  % proposals, file=sys.stderr)
            return 2
        dry = not args.commit
        with run_lock(out_dir, "phase3"):
            stats = confirm.apply_proposals(root, out_dir, proposals, dry_run=dry,
                                            progress=None, artists=artists)
        head = "Phase 3 apply (dry run)" if dry else "Phase 3 APPLIED"
        print("%s: %d folders, %d files, TITLE %d, VENUE %d"
              % (head, stats.get("folders", 0), stats.get("files", 0),
                 stats.get("TITLE", 0), stats.get("VENUE", 0)))
        for k in ("nothing left to fill", "unwritable format", "unreadable",
                  "ambiguous filename", "failed",
                  "folder gone", "backup failed", "state not recorded"):
            if stats.get(k):
                print("  %s: %d" % (k, stats[k]))
        print("  every change is listed in %s"
              % ("phase3_dry_run.csv" if dry else "phase3_committed.csv"))
        if dry:
            print("  nothing was written. Add --commit to apply.")
        return 0

    if args.phase == "phase3":
        from .httpcache import HttpCache

        cache_path, why = userdir.cache_path(args.cache, out_dir)
        print("  cache: %s (%s)" % (cache_path, why))
        phish_key = _read_phish_key(args)
        with HttpCache(cache_path, offline=args.offline) as cache:
            if args.offline:
                print("  offline: answering from %s only" % cache_path)
            from .showstore import ShowStore

            store = ShowStore(args.shows)
            if args.shows and not store:
                print("  no usable show database at %s" % args.shows, file=sys.stderr)
            elif store:
                st = store.stats()
                print("  shows: %d from %s" % (st["shows"], args.shows))
            with run_lock(out_dir, "phase3"):
                stats = confirm.run(root, out_dir, cfg, cache, artists=artists,
                                    progress=lambda m: print(m, flush=True),
                                    phish_key=phish_key, store=store or None)
            store.close()
        print("Phase 3: %d folders looked at, %d missing something."
              % (stats["folders"], stats["proposals"]))
        print("  place to fill %d   titles to fill %d   needs a human %d"
              % (stats["with_place"], stats["with_titles"], stats["needs_a_human"]))
        print("  shows with paperwork and no audio that archive.org has: %d"
              % stats["downloadable"])
        c = stats["cache"]
        print("  cache: %d URLs, %.1f MB (%d served, %d fetched)"
              % (c["urls"], c["bytes"] / 1048576.0, c["hits"], c["fetches"]))
        if c.get("sites_given_up"):
            print("  stopped asking %s after repeated failures this run; run again "
                  "later to fill what they would have answered"
                  % ", ".join(c["sites_given_up"]))
        print("  nothing was written inside %s" % root)
    elif args.phase == "phase0":
        stats = phase0.run(root, out_dir, cfg, tag_sample=args.tag_sample,
                           today=args.today, include_top=artists,
                           overrides=overrides,
                           verify_audio=args.verify_audio, workers=args.workers,
                           progress=lambda m: print(m, flush=True))
        print("Phase 0: %d show folders across %d directories, %d audio files."
              % (stats["show_folders"], stats["dirs_scanned"], stats["audio_files"]))
        print("  origin: " + ", ".join("%s %d" % (k, v)
                                       for k, v in sorted(stats["classification"].items())))
        print("  undated: %d   multi-date releases: %d   blocked from rename: %d"
              % (stats["undated_folders"], stats["multi_date_releases"],
                 stats["blocked_from_rename"]))
        print("  audio identity: %d tracks, %d distinct recordings, %d folder "
              "pair(s) sharing audio"
              % (stats.get("tags_identified", stats.get("tracks_identified", 0)),
                 stats.get("distinct_recordings", 0),
                 stats.get("folder_pairs_sharing_audio", 0)))
        if stats.get("identical_folders"):
            print("    %d pair(s) are the same recording throughout - see "
                  "phase0_same_audio.csv" % stats["identical_folders"])
        if not stats["show_folders"]:
            print("  no show folders found - check the library path and --artist")
        elif not args.verify_audio:
            print("  audio decode check: not run. It is optional - --verify-audio "
                  "decodes every FLAC to prove it is intact, which takes hours on a "
                  "large library. Everything else works without it.")
        if args.verify_audio:
            print("  verified: %d pass, %d MISMATCH, %d unreadable, %d without an MD5"
                  % (stats.get("verify_pass", 0), stats.get("verify_mismatch", 0),
                     stats.get("verify_unreadable", 0), stats.get("verify_no_md5", 0)))
    elif args.phase == "phase1":
        counts = phase1.run(root, out_dir, cfg, today=args.today,
                            reclassify=args.reclassify, include_top=artists,
                            overrides=overrides, unnest=args.unnest,
                            settings=settings)
        if not counts:
            print("Phase 1 (dry run): no show folders found under %s%s.\n"
                  "  A show folder is one holding audio (FLAC, MP3, M4A, WMA, OGG, "
                  "WAV, AIFF, APE, WV or SHN). Check the library path, the "
                  "--artist name, and ignore_folders in your jamp.yaml."
                  % (root, " in " + ", ".join(sorted(artists)) if artists else ""))
        else:
            print("Phase 1 (dry run): " + ", ".join("%s %d" % (k, v)
                                                    for k, v in sorted(counts.items())))
        print("  nothing was written inside %s" % root)
    else:
        if args.commit and not args.skip_plan_check:
            why = _plan_check(out_dir, root, artists, reclassify=args.reclassify,
                              unnest=args.unnest, settings=settings)
            if why:
                scope = "".join(' --artist "%s"' % a for a in sorted(artists or ()))
                scope += "".join(" --%s" % f for f in ("reclassify", "unnest")
                                 if getattr(args, f))
                print("--commit refused: %s.\n"
                      "Run the dry run first, into the same reports folder, and read "
                      "phase1_summary.txt:\n"
                      '  jamp plan "%s" --out-dir "%s"%s'
                      % (why, root, out_dir, scope), file=sys.stderr)
                return 2
        if args.commit and args.unnest:
            print("  WARNING: --unnest moves shows up out of the folders that hold "
                  "them. Check phase1_summary.txt for every lift before relying on it.")
        if args.commit and args.include_merges:
            print("  WARNING: --include-merges moves files between folders. "
                  "Nothing is deleted, but check each merge in the plan first.")
        stats = phase2.run(root, out_dir, cfg, commit=args.commit,
                           include_merges=args.include_merges, today=args.today,
                           reclassify=args.reclassify, settle=args.settle,
                           unnest=args.unnest,
                           quarantine_lossy=args.quarantine_lossy,
                           include_top=artists, overrides=overrides,
                           until_settled=not args.one_pass,
                           check_plan=args.commit and not args.skip_plan_check)
        if args.commit:
            refused = stats.get("refused_by_plan", 0)
            print("Phase 2 COMMITTED: %d folders, %d actions, %d failed and rolled back."
                  % (stats["folders_done"], stats["actions"],
                     stats["folders_failed"] - refused))
            if refused:
                print("  %d folder(s) NOT committed: their plan is no longer what the "
                      "dry run showed - see phase2_summary.txt, and run jamp plan again"
                      % refused)
            if stats.get("planned_not_offered"):
                print("  %d folder(s) in the dry run's plan are no longer work and were "
                      "left alone - see phase2_summary.txt" % stats["planned_not_offered"])
            if stats.get("passes", 1) > 1:
                print("  took %d passes" % stats["passes"])
            if stats.get("beyond_dry_run"):
                print("  %d folder(s) were committed by a later pass, beyond what the "
                      "dry run showed - listed in phase2_summary.txt"
                      % stats["beyond_dry_run"])
            if stats.get("reads_reused"):
                print("  file reads: %d reused from the dry run or an earlier pass, "
                      "%d read from disk"
                      % (stats["reads_reused"], stats.get("files_read", 0)))
            print("  every change is listed in phase2_committed.csv")
            left = stats.get("remaining")
            if left == 0:
                print("  SETTLED: nothing is left to do for this scope.")
                held = stats.get("held") or {}
                if held:
                    print("  %s are left as they are on purpose - see phase1_summary.txt"
                          % ", ".join("%d %s" % (v, k.lower().replace("_", " "))
                                      for k, v in sorted(held.items())))
            elif left:
                print("  %d folder(s) still need work%s. Re-run to continue."
                      % (left, " - drop --one-pass to keep going automatically"
                         if args.one_pass else ""))
        else:
            print("Phase 2 (dry run): %d folders selected, %d actions planned."
                  % (stats["folders_selected"], stats["actions"]))
            print("  nothing was written. Add --commit to apply.")
        if stats.get("merges_refused"):
            print("  %d merge(s) refused: another folder already holds the name they "
                  "would merge into, and nothing was moved - see phase2_summary.txt"
                  % stats["merges_refused"])
        if not args.include_merges:
            print("  split-show merges were not included (--include-merges)")

    print("  reports: %s" % out_dir)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
