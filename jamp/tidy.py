"""`jamp tidy`: remove folders with nothing at all in them.

`--unnest`, `--include-merges`, `unpack --set-aside` and `convert --set-aside`
all leave the folder they emptied behind, because none of them removes
anything.  This is the separate, explicit step that clears those - on any
system; it used to be a Windows-only PowerShell script outside the tool.

Rules, each a restriction:

* **Only a folder with nothing inside is removed** - no files of any kind,
  hidden ones and ZIPs included - or a folder holding only such folders, from
  the bottom up.  Each is checked again immediately before it goes, and
  removed with a call that the file system itself refuses for anything not
  empty, whatever this module believes.
* **A folder holding files but no audio is listed and never touched.** A
  wrapper's info file may be the only description of the recording that moved
  out of it.
* **A folder that cannot be read is reported, never treated as empty.**
* **The library itself, and each `--artist` folder, are never removed**, and
  ignored folders (Trash, say) are never looked inside.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
from pathlib import Path

from . import batch
from .audio import AUDIO_EXTS
from .report import report_file, run_lock, write_csv, write_json
from .winpath import opener

COMMAND = "tidy"
PLAN_NAME = "tidy_plan.json"


def survey(start: Path, keep: set[str], skip_names: set[str],
           skip_paths: set[str], root: Path):
    """(empty folders deepest first, folders with files but no audio, unreadable)."""
    empty: list[Path] = []
    no_audio: list[tuple[Path, list[str]]] = []
    unreadable: list[tuple[Path, str]] = []

    def visit(folder: Path) -> tuple[bool, bool]:
        """(removable, holds audio anywhere beneath)."""
        try:
            entries = list(os.scandir(opener(folder)))
        except OSError as exc:
            unreadable.append((folder, "%s: %s" % (exc.__class__.__name__, exc)))
            return False, True            # unreadable is not empty, and may hold audio
        files, audio, all_children_removable = [], False, True
        for e in entries:
            child = folder / e.name
            if e.is_dir(follow_symlinks=False):
                rel = child.relative_to(root).as_posix().lower()
                if e.name.lower() in skip_names or rel in skip_paths:
                    all_children_removable = False
                    audio = True          # ignored: treat as holding something
                    continue
                child_removable, child_audio = visit(child)
                audio = audio or child_audio
                all_children_removable = all_children_removable and child_removable
            else:
                files.append(e.name)
                if Path(e.name).suffix.lower() in AUDIO_EXTS:
                    audio = True
        is_removable = not files and all_children_removable
        if is_removable and str(folder).lower() not in keep:
            empty.append(folder)
        elif files and not audio:
            no_audio.append((folder, sorted(files)))
        return is_removable, audio

    visit(start)
    empty.sort(key=lambda p: len(p.parts), reverse=True)
    return empty, no_audio, unreadable


def run(args) -> int:
    try:
        root, out_dir, cfg = batch.resolve(args, COMMAND)
    except batch.Refused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    artists = set(args.artist) if args.artist else None
    wanted = batch.scope_key(root, artists)
    if args.commit and not args.skip_plan_check:
        why = batch.plan_check(out_dir / PLAN_NAME, wanted, COMMAND)
        if why:
            return batch.refuse_commit(why, COMMAND)

    ignore = cfg.settings.ignore_folders
    skip_names = {i.lower() for i in ignore if "/" not in i and "\\" not in i}
    skip_names.add(cfg.settings.review_folder.lower())
    skip_paths = {i.replace("\\", "/").strip("/").lower() for i in ignore
                  if "/" in i or "\\" in i}
    starts = [root / a for a in sorted(artists)] if artists else [root]
    keep = {str(root).lower()} | {str(s).lower() for s in starts}

    with run_lock(out_dir, COMMAND):
        empty, no_audio, unreadable = [], [], []
        for start in starts:
            if not start.is_dir():
                unreadable.append((start, "not a folder"))
                continue
            e, n, u = survey(start, keep, skip_names, skip_paths, root)
            empty += e
            no_audio += n
            unreadable += u
        empty.sort(key=lambda p: len(p.parts), reverse=True)

        removed, skipped = [], []
        if args.commit:
            for folder in empty:
                try:
                    if any(os.scandir(opener(folder))):
                        skipped.append((folder, "no longer empty"))
                        continue
                    os.rmdir(opener(folder))       # refuses anything not empty
                    removed.append(folder)
                except OSError as exc:
                    skipped.append((folder, "%s: %s" % (exc.__class__.__name__, exc)))

        def rel(p):
            try:
                return str(p.relative_to(root))
            except ValueError:
                return str(p)

        lines = ["Tidy %s" % ("COMMITTED" if args.commit
                              else "(dry run - nothing was removed)"),
                 "=" * 60,
                 "generated %s" % _dt.datetime.now().isoformat(timespec="minutes"), ""]
        lines.append("%s (%d)" % ("Removed - nothing at all inside" if args.commit
                                  else "Completely empty - would be removed",
                                  len(removed) if args.commit else len(empty)))
        for folder in (removed if args.commit else empty):
            lines.append("  %s" % rel(folder))
        if skipped:
            lines.append("")
            lines.append("Not removed (%d)" % len(skipped))
            lines.extend("  %s - %s" % (rel(f), why) for f, why in skipped)
        lines.append("")
        lines.append("Holds files but no audio - never removed, look yourself (%d)"
                     % len(no_audio))
        for folder, files in no_audio:
            lines.append("  %s" % rel(folder))
            lines.extend("      %s" % f for f in files[:4])
            if len(files) > 4:
                lines.append("      ... and %d more" % (len(files) - 4))
        if unreadable:
            lines.append("")
            lines.append("Could not be read - treated as not empty (%d)" % len(unreadable))
            lines.extend("  %s - %s" % (rel(f), why) for f, why in unreadable)
        lines.append("")
        lines.append("Only completely empty folders are ever removed; every file stays.")
        text = "\n".join(lines) + "\n"
        report_file(out_dir / "tidy_summary.txt").write_text(text, encoding="utf-8")
        write_csv(out_dir / ("tidy_committed.csv" if args.commit else "tidy_plan.csv"),
                  ["kind", "folder", "detail"],
                  [["removed" if args.commit else "empty", rel(f), ""]
                   for f in (removed if args.commit else empty)]
                  + [["not removed", rel(f), why] for f, why in skipped]
                  + [["files, no audio", rel(f), "; ".join(fs[:10])] for f, fs in no_audio]
                  + [["unreadable", rel(f), why] for f, why in unreadable])
        if not args.commit:
            write_json(out_dir / PLAN_NAME, {
                "generated": _dt.datetime.now().isoformat(timespec="seconds"), **wanted})
    print(text + "reports: %s" % out_dir)
    return 1 if skipped else 0
