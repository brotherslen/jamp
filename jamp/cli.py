"""Command line entry point.

    py -m jamp phase0   "D:\\Music\\Live" --out-dir "D:\\jamp-reports"
    py -m jamp phase1   "D:\\Music\\Live" --out-dir "D:\\jamp-reports"
    py -m jamp phase2   "D:\\Music\\Live" --out-dir "D:\\jamp-reports" --commit
    py -m jamp phase3   "D:\\Music\\Live" --out-dir "D:\\jamp-reports"
    py -m jamp complete "D:\\Music\\Live" --out-dir "D:\\jamp-reports" --shows <db>

Dry run is not a flag you remember to pass - it is what every phase does
without --commit.  Two things write, and only with --commit: phase2, and
phase3 --apply, which fills tags named in a report that already exists.
Phases 0 and 1 and `complete` refuse --commit outright.
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
from .report import ensure_out_dir


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


def _plan_check(out_dir: Path, root: Path, artists: set[str] | None) -> str | None:
    """Why a commit should not go ahead yet, or None.

    A commit is meant to follow a dry run somebody read.  Requiring the plan
    for the same library and scope in the same reports folder makes that the
    path of least resistance rather than advice in a document.
    """
    plan = out_dir / "phase1_plan.json"
    if not plan.exists():
        return "no phase 1 dry run in %s" % out_dir
    try:
        data = json.loads(plan.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "the phase 1 plan in %s cannot be read: %s" % (out_dir, exc)
    if "root" not in data:
        return ("the phase 1 plan in %s predates this check; run phase 1 again"
                % out_dir)
    if data.get("root") != str(root):
        return ("the phase 1 plan in %s is for %s, not %s"
                % (out_dir, data.get("root"), root))
    wanted = sorted(artists) if artists else None
    if data.get("scope") != wanted:
        return ("the phase 1 plan in %s covered %s, not %s"
                % (out_dir, ", ".join(data["scope"]) if data.get("scope") else "the whole library",
                   ", ".join(wanted) if wanted else "the whole library"))
    return None


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
                    "anything.  Every phase is a dry run unless given --commit, and "
                    "reports never go inside ROOT.",
    )
    parser.add_argument("--version", action="version", version="jamp %s" % __version__)
    sub = parser.add_subparsers(dest="phase", required=True)

    for name, help_text in (
        ("phase0", "walk ROOT and report what is actually there"),
        ("phase1", "produce the full proposed rename and tag plan"),
        ("phase2", "apply the plan: renames, tags and checksum files; needs --commit"),
        ("phase3", "ask archive.org, phish.in, phish.net, jerrybase and the MMJ "
                   "archive to fill in what the files never said; a report, "
                   "unless --apply --commit"),
        ("complete", "is a recording missing songs? compares phase 0's "
                     "durations against the known setlist; reports only"),
    ):
        p = sub.add_parser(name, help=help_text)
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
                       help="phase 2 only: actually write. Refused in phases 0 and 1")
        p.add_argument("--today", type=_dt.date.fromisoformat, default=None,
                       help="pretend today is this date (for reproducible tests)")
        p.add_argument("--artist", action="append", metavar="FOLDER", default=None,
                       help="only look inside this top-level folder of ROOT; repeat "
                            "to name several. ROOT stays the library root, so the "
                            "artist folder is still available as evidence")
        if name == "complete":
            p.add_argument("--shows", type=Path, default=None,
                           help="the distilled show database from "
                                "tools/distill_cache.py; this is the only "
                                "reference it reads and it never goes online")
            p.add_argument("--identity", type=Path, default=None,
                           help="phase0_audio_identity.csv (default: the one "
                                "in --out-dir)")
            p.add_argument("--folders", type=Path, default=None,
                           help="phase0_folders.csv (default: the one in "
                                "--out-dir)")
        if name == "phase3":
            p.add_argument("--cache", type=Path, default=None,
                           help="the cache of everything phase 3 has fetched "
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
                                "ignoring their .etree_state.json")
        if name == "phase2":
            p.add_argument("--settle", action="store_true",
                           help="rename nothing; record every folder already named by "
                                "this tool as settled, so re-runs leave it alone")
            p.add_argument("--quarantine-lossy", action="store_true",
                           help="move MP3s that duplicate a lossless copy of the same "
                                "recording into the review folder; nothing is deleted")
            p.add_argument("--unnest", action="store_true",
                           help="lift a show out of a container folder that holds "
                                "nothing else; the empty container is left behind")
            p.add_argument("--until-settled", action="store_true",
                           help="keep committing until nothing is left to do. Each "
                                "commit changes what the next read sees, so the answer "
                                "converges over a few passes; this does them for you "
                                "and stops as soon as a pass changes nothing")
            p.add_argument("--skip-plan-check", action="store_true",
                           help="commit even though --out-dir holds no phase 1 "
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

    if args.phase in ("init", "doctor"):
        from . import install

        return install.run_init(args) if args.phase == "init" else install.run_doctor(args)
    if args.phase in ("unpack", "convert"):
        from . import convert, unpack

        return (unpack if args.phase == "unpack" else convert).run(args)
    if args.phase == "restore":
        from . import restore

        return restore.run(args)
    if args.phase == "acts":
        from . import acts

        return acts.run(args)

    if args.commit and args.phase != "phase2" and not (
            args.phase == "phase3" and getattr(args, "apply", False)):
        print(
            "--commit is refused here: only phase2, and phase3 --apply, write.\n"
            "Review the CSVs in --out-dir first, then run phase2 --commit.",
            file=sys.stderr,
        )
        return 2

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
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

    if args.root is None or args.out_dir is None:
        print("ROOT and --out-dir are required. Give them once with "
              "--remember and later runs can leave them out:"
              + chr(10) +
              '  py -m jamp phase1 "<library>" --out-dir "<reports>" --remember',
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

        identity = args.identity or (out_dir / "phase0_audio_identity.csv")
        folders = args.folders or (out_dir / "phase0_folders.csv")
        for needed, what in ((identity, "--identity"), (folders, "--folders")):
            if not Path(needed).exists():
                print("%s does not exist: %s" % (what, needed), file=sys.stderr)
                print("Run phase0 first - this reads its reports rather than "
                      "the library, so the durations are already measured.",
                      file=sys.stderr)
                return 2
        store = ShowStore(args.shows)
        if not store:
            print("no usable show database at %s. Point --shows at the file "
                  "tools/distill_cache.py produced." % args.shows, file=sys.stderr)
            return 2
        shows = _complete.in_scope(_complete.load_shows(Path(identity), Path(folders)),
                                   artists)
        print("  %d folders with durations, %d shows in %s"
              % (len(shows), store.stats()["shows"], args.shows))
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
        print("  nothing was written inside %s" % root)
        return 0

    if args.phase == "phase3" and args.apply:
        proposals = out_dir / "phase3_proposals.json"
        if not proposals.exists():
            print("--apply is refused: %s does not exist. "
                  "Run phase3 without --apply first, and read the report."
                  % proposals, file=sys.stderr)
            return 2
        dry = not args.commit
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
                            overrides=overrides)
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
            why = _plan_check(out_dir, root, artists)
            if why:
                scope = "".join(' --artist "%s"' % a for a in sorted(artists or ()))
                print("--commit refused: %s.\n"
                      "Run the dry run first, into the same reports folder, and read "
                      "phase1_summary.txt:\n"
                      '  jamp phase1 "%s" --out-dir "%s"%s'
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
                           until_settled=args.until_settled)
        if args.commit:
            print("Phase 2 COMMITTED: %d folders, %d actions, %d failed and rolled back."
                  % (stats["folders_done"], stats["actions"], stats["folders_failed"]))
            if stats.get("passes", 1) > 1:
                print("  took %d passes" % stats["passes"])
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
                      % (left, "" if args.until_settled else
                         " - or pass --until-settled to keep going automatically"))
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
