"""`jamp restore`: put a committed folder back the way it was.

A commit leaves three records behind, and between them they describe the
folder before the tool touched it:

* ``.etree_backup.json`` - every file's original tags and original name, and
  the name the commit gave it;
* ``.etree_state.json`` - the folder's original name, and where it sat;
* ``phase2_committed.csv`` in the reports folder - every rename, in order, if
  it was kept.  Given with ``--log``, it also covers files the backup cannot:
  SHN, which holds no tags, and names changed again by a later commit.

A restore puts back the tags, the track names, the entries in checksum and cue
files, and the folder name, in that order, and undoes all of it if any step
fails - the same rule as a commit.  Like a commit it is a dry run unless given
``--commit``, and the commit needs its dry run.

It refuses a folder it cannot put back completely, rather than restoring part
of it and calling that done: files merged in from another folder, tracks it
cannot match to the backup, a name already taken.  ``--partial`` accepts a
folder whose unmatched files would simply be left as they are.

It does not move a folder back to where it sat before ``--unnest`` lifted it,
does not undo a merge, and does not bring back files set aside by
``--quarantine-lossy``; it says so where it can tell.
"""
from __future__ import annotations

import csv
import datetime as _dt
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .audio import AUDIO_EXTS
from .report import ensure_out_dir, report_file, run_lock, write_csv, write_json
from .sidecars import REWRITE, NO_CHANGE, kind_for, plan_sidecar, propose_sidecar_name
from .state import STATE_NAME, read_state
from .tagwriter import BACKUP_NAME, _prepare_restore, read_all_tags
from .winpath import opener

PLAN_NAME = "restore_plan.json"

# A track name this tool writes: gd1977-05-08d1t01, ph1997-11-22s2t0a.
_OUR_TRACK = re.compile(r"[a-z0-9]+\d{4}-\d{2}-\d{2}[ds]\d+t\d+[a-z]?\.", re.I)


@dataclass
class Step:
    kind: str
    old: Path
    new: Path | None = None
    detail: str = ""
    payload: object = None
    status: str = "planned"
    error: str | None = None
    undo_data: object = None


@dataclass
class FolderRestore:
    folder: Path
    rel: str
    original_name: str | None = None
    steps: list[Step] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)   # stop the restore
    loose: list[str] = field(default_factory=list)      # stop it unless --partial
    notes: list[str] = field(default_factory=list)
    status: str = "planned"
    error: str | None = None

    def blocked(self, partial: bool) -> bool:
        return bool(self.problems) or (bool(self.loose) and not partial)


# -- the commit logs --------------------------------------------------------

def read_logs(paths) -> tuple[dict, dict]:
    """Folder renames and file renames from phase2_committed.csv files.

    Both keyed by the path a thing was given, lower-cased; each value is the
    path it had before.  Only steps that were done and not undone count.
    """
    folders: dict[str, str] = {}
    files: dict[str, str] = {}
    for log in paths or ():
        with open(log, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                if row.get("status") != "done" or not row.get("new"):
                    continue
                if row.get("kind") == "rename_folder":
                    folders[row["new"].lower()] = row["old"]
                elif row.get("kind") in ("rename_file", "rename_sidecar"):
                    files[row["new"].lower()] = row["old"]
    return folders, files


def _folder_chain(folder: Path, folders: dict) -> list[Path]:
    chain = [folder]
    seen = {str(folder).lower()}
    while str(chain[-1]).lower() in folders and len(chain) < 20:
        prev = Path(folders[str(chain[-1]).lower()])
        if str(prev).lower() in seen:
            break
        seen.add(str(prev).lower())
        chain.append(prev)
    return chain


def _name_from_logs(path: Path, folder: Path, chain: list[Path],
                    files: dict) -> str | None:
    """The first name a file had, following renames back through the logs."""
    rel = path.relative_to(folder)
    name = rel.name
    found = False
    for _ in range(20):
        step = None
        for base in chain:
            step = files.get(str(base / rel.parent / name).lower())
            if step:
                break
        if not step:
            break
        name = Path(step).name
        found = True
    return name if found else None


# -- planning ---------------------------------------------------------------

def _walk(folder: Path, keep) -> list[Path]:
    """Files under the folder, by their plain paths, that `keep` wants."""
    out = []
    prefix_len = len(os.fspath(opener(folder))) - len(os.fspath(folder))
    for dirpath, dirnames, filenames in os.walk(opener(folder)):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        base = Path(dirpath[prefix_len:])
        out.extend(base / n for n in filenames if keep(Path(n)))
    return sorted(out)


def _audio_files(folder: Path) -> list[Path]:
    return _walk(folder, lambda p: p.suffix.lower() in AUDIO_EXTS)


def _sidecar_files(folder: Path) -> list[Path]:
    return _walk(folder, lambda p: kind_for(p) is not None)



def _read_backup(folder: Path) -> dict | None:
    p = Path(opener(folder / BACKUP_NAME))
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def plan_folder(folder: Path, root: Path, logs: tuple[dict, dict] = ({}, {})) -> FolderRestore:
    folder = Path(folder)
    try:
        rel = str(folder.relative_to(root))
    except ValueError:
        rel = str(folder)
    fr = FolderRestore(folder=folder, rel=rel)
    if not folder.is_dir():
        fr.problems.append("not a folder")
        return fr

    state = read_state(folder)
    backup = _read_backup(folder)
    from .state import NEWER, newer_format

    if (state or {}).get(NEWER) is not None or newer_format(backup):
        fr.problems.append("written by a newer version of jamp than this one; "
                           "update jamp to restore this folder")
        return fr
    log_folders, log_files = logs
    chain = _folder_chain(folder, log_folders)
    if state is None and backup is None and len(chain) == 1:
        fr.problems.append("nothing here says this tool changed it: no %s, no %s, "
                           "and no rename of it in the logs given"
                           % (STATE_NAME, BACKUP_NAME))
        return fr

    # -- which file had which name --------------------------------------
    entries = (backup or {}).get("files") or {}
    audio = _audio_files(folder)
    by_name: dict[str, list[Path]] = {}
    for p in audio:
        by_name.setdefault(p.name.lower(), []).append(p)

    # The logs first: they follow every rename, where the backup knows only the
    # first one.
    from_logs: dict[str, list[Path]] = {}    # first name -> current paths
    if log_files:
        for path in audio:
            first = _name_from_logs(path, folder, chain, log_files)
            if first:
                from_logs.setdefault(first.lower(), []).append(path)

    original_of: dict[Path, str] = {}       # current path -> first name
    tags_for: dict[Path, dict] = {}         # current path -> backup entry
    for original, entry in entries.items():
        candidates = []
        for name in (entry.get("__renamed_to__"), original):
            if name:
                candidates = by_name.get(name.lower(), [])
                if candidates:
                    break
        if not candidates:
            candidates = from_logs.get(original.lower(), [])
        if len(candidates) > 1:
            fr.problems.append("%r is in the folder %d times, so the backup cannot "
                               "say which is which" % (candidates[0].name, len(candidates)))
            continue
        if not candidates:
            fr.loose.append("in the backup but not found: %s" % original)
            continue
        path = candidates[0]
        original_of[path] = original
        if "__error__" not in entry:
            tags_for[path] = entry

    first_of = {p: n for n, ps in from_logs.items() for p in ps}
    for path in audio:
        from_log = (_name_from_logs(path, folder, chain, log_files)
                    if path in first_of else None)
        if from_log:
            if path in original_of and original_of[path] != from_log:
                fr.notes.append("%s: the log's first name %r is used over the "
                                "backup's %r" % (path.name, from_log, original_of[path]))
            original_of[path] = from_log
        elif path not in original_of:
            if _OUR_TRACK.search(path.name):
                fr.loose.append("named by this tool but in neither the backup nor a "
                                "log: %s%s" % (path.relative_to(folder),
                                               "" if log_files else " (a --log may know it)"))
            else:
                original_of[path] = path.name     # never renamed

    if entries and not tags_for and not original_of:
        fr.problems.append("the backup matches no file in the folder")

    # -- tags --------------------------------------------------------------
    if tags_for:
        fr.steps.append(Step("restore_tags", folder, None,
                             "put back the original tags of %d file(s)" % len(tags_for),
                             payload=tags_for))
    elif backup is None:
        fr.notes.append("no %s, so tags are left as they are" % BACKUP_NAME)

    # -- checksum and cue files -------------------------------------------
    renames = {p: n for p, n in original_of.items() if n != p.name}
    counts: dict[str, int] = {}
    for p in original_of:
        counts[p.name.lower()] = counts.get(p.name.lower(), 0) + 1
    for sc in (_sidecar_files(folder) if renames else ()):
        mapping: dict[str, str] = {}
        for p, n in original_of.items():
            if p.parent == sc.parent:
                mapping[p.name] = n
                continue
            try:
                mapping[p.relative_to(sc.parent).as_posix()] = n
            except ValueError:
                pass
            if counts[p.name.lower()] == 1:
                mapping.setdefault(p.name, n)
        sp = plan_sidecar(sc, mapping)
        if sp.status == REWRITE:
            fr.steps.append(Step("rewrite_sidecar", sc, None,
                                 "point %d entries back at the original names"
                                 % len(sp.referenced),
                                 payload=(sp.new_text, sp.encoding or "utf-8")))
        elif sp.status != NO_CHANGE:
            fr.notes.append("%s left as it is: %s" % (sc.name, "; ".join(sp.notes)))

    # -- track names -------------------------------------------------------
    targets: dict[str, Path] = {}
    for path, name in sorted(renames.items()):
        target = path.with_name(name)
        key = str(target).lower()
        if key in targets:
            fr.problems.append("two files would both become %s" % target.name)
            continue
        targets[key] = path
        fr.steps.append(Step("rename_file", path, target))
    leaving = {str(p).lower() for p in renames}
    for key, path in targets.items():
        if key != str(path).lower() and key not in leaving and os.path.exists(opener(Path(key))):
            fr.problems.append("%s cannot go back to %s: a file of that name is "
                               "already there" % (path.name, Path(key).name))

    # -- the folder --------------------------------------------------------
    original_name = None
    if state:
        original_name = state.get("original_folder_name")
    if not original_name and backup and backup.get("folder"):
        # A state file older than original_folder_name; the backup was written
        # at the first commit, and records where the folder was then.
        original_name = Path(str(backup["folder"]).replace("\\", "/")).name
    if len(chain) > 1:
        original_name = chain[-1].name
    fr.original_name = original_name
    if original_name and original_name != folder.name:
        present = {p.suffix.lower().lstrip(".") for p in audio}
        claimed = {m.group(1).lower() for m in
                   re.finditer(r"[.\s](shn|flac|mp3|m4a|wma|ogg)(?:16|24)?\b", original_name, re.I)}
        stale = sorted(c for c in claimed if c not in present)
        if stale and present:
            fr.notes.append("the original name says %s, but the folder now holds %s: "
                            "the files were converted after it was named, so that "
                            "name no longer describes them"
                            % ("/".join(stale), "/".join(sorted(present))))
    if original_name and original_name != folder.name:
        for sc in _sidecar_files(folder):
            if sc.parent != folder:
                continue
            back = propose_sidecar_name(sc, folder.name, original_name)
            if back and back != sc.name:
                fr.steps.append(Step("rename_sidecar", sc, sc.with_name(back)))
        target = folder.with_name(original_name)
        if str(target).lower() != str(folder).lower() and os.path.exists(opener(target)):
            fr.problems.append("the folder cannot go back to %r: that name is taken"
                               % original_name)
        fr.steps.append(Step("rename_folder", folder, target))
    elif not original_name:
        fr.notes.append("the folder's original name is not recorded, so it keeps "
                        "its name")

    was = (state or {}).get("original_relative_path")
    if was and Path(was).parent != Path(rel).parent:
        fr.notes.append("it was at %s before; restore renames it where it is now - "
                        "move it back yourself if you want it there" % was)

    fr.steps.append(Step("mark_restored", folder, None,
                         "record the restore in %s so the folder is no longer "
                         "treated as settled" % STATE_NAME))
    return fr


# -- doing it ---------------------------------------------------------------

def _safe_rename(src: Path, dst: Path, cfg) -> None:
    from .phase2 import _safe_rename as rename

    rename(src, dst, attempts=cfg.settings.rename_attempts,
           first_delay=cfg.settings.rename_retry_delay)


def _apply(step: Step, fr: FolderRestore, cfg) -> None:
    if step.kind == "restore_tags":
        prepared = [(p, _prepare_restore(p, entry)) for p, entry in step.payload.items()]
        # What they hold now, so a failure later in the folder can put it back.
        now = read_all_tags([p for p, _ in prepared])
        step.undo_data = [(p, now.get(p.name)) for p, _ in prepared]
        for _, apply in prepared:
            apply()
    elif step.kind == "rewrite_sidecar":
        with open(opener(step.old), "rb") as fh:
            step.undo_data = fh.read()
        text, encoding = step.payload
        with open(opener(step.old), "wb") as fh:
            fh.write(text.encode(encoding))
    elif step.kind in ("rename_file", "rename_sidecar", "rename_folder"):
        _safe_rename(step.old, step.new, cfg)
    elif step.kind == "mark_restored":
        folder = fr.folder.with_name(fr.original_name) if (
            fr.original_name and any(s.kind == "rename_folder" for s in fr.steps)) else fr.folder
        path = Path(opener(folder / STATE_NAME))
        before = path.read_bytes() if path.exists() else None
        step.undo_data = (path, before)
        body = json.loads(before.decode("utf-8")) if before else {}
        restored_from = body.pop("folder_name", None) or fr.folder.name
        body.update({
            "restored_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "restored_from": restored_from,
        })
        path.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    step.status = "done"


def _undo(step: Step, cfg) -> None:
    if step.kind == "restore_tags":
        for path, entry in step.undo_data or ():
            if entry and "__error__" not in entry:
                _prepare_restore(path, entry)()
    elif step.kind == "rewrite_sidecar" and step.undo_data is not None:
        with open(opener(step.old), "wb") as fh:
            fh.write(step.undo_data)
    elif step.kind in ("rename_file", "rename_sidecar", "rename_folder"):
        _safe_rename(step.new, step.old, cfg)
    elif step.kind == "mark_restored" and step.undo_data:
        path, before = step.undo_data
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(before)


def execute(fr: FolderRestore, cfg) -> None:
    done: list[Step] = []
    for step in fr.steps:
        try:
            _apply(step, fr, cfg)
            done.append(step)
        except BaseException as exc:
            step.status = "failed"
            step.error = "%s: %s" % (exc.__class__.__name__, exc)
            fr.status, fr.error = "failed", step.error
            for prior in reversed(done):
                try:
                    _undo(prior, cfg)
                    prior.status = "undone"
                except Exception as undo_exc:            # pragma: no cover
                    prior.error = "could not undo: %s" % undo_exc
            if not isinstance(exc, Exception):
                raise
            return
    fr.status = "done"


# -- the command --------------------------------------------------------------

def _plan_check(plan: Path, root: str, folders: list[str]) -> str | None:
    """Why a restore commit should wait for its dry run, or None."""
    if not plan.exists():
        return "no restore dry run in %s" % plan.parent
    try:
        data = json.loads(plan.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "the restore plan cannot be read: %s" % exc
    if data.get("root") != root or data.get("folders") != folders:
        return "the restore dry run in %s was for other folders" % plan.parent
    return None


def _rows(plans: list[FolderRestore]):
    for fr in plans:
        for s in fr.steps:
            yield [s.status, s.kind, fr.rel, str(s.old), str(s.new or ""),
                   s.detail, s.error or ""]


def run(args) -> int:
    from .cli import load_settings
    from .config import load_config

    remembered = load_settings()
    root = args.root or (Path(remembered["root"]) if remembered.get("root") else None)
    out_dir = args.out_dir or (Path(remembered["out_dir"])
                               if remembered.get("out_dir") else None)
    if root is None or out_dir is None:
        print("restore: the library and reports folders are needed - run jamp init, "
              "or give --root and --out-dir", file=sys.stderr)
        return 2
    root = Path(root).resolve()
    try:
        out_dir = ensure_out_dir(out_dir, root)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    cfg = load_config()

    folders = []
    for given in args.folders:
        p = Path(given)
        p = (p if p.is_absolute() else root / p).resolve()
        if p != root and root not in p.parents:
            print("restore: %s is not inside the library %s" % (p, root), file=sys.stderr)
            return 2
        folders.append(p)
    wanted = sorted(str(p) for p in folders)

    if args.commit and not args.skip_plan_check:
        why = _plan_check(out_dir / PLAN_NAME, str(root), wanted)
        if why:
            print("--commit refused: %s.\nRun the same restore without --commit "
                  "first, into the same reports folder, and read "
                  "restore_summary.txt." % why, file=sys.stderr)
            return 2

    try:
        logs = read_logs(args.log)
    except OSError as exc:
        print("restore: cannot read a log: %s" % exc, file=sys.stderr)
        return 2

    with run_lock(out_dir, "restore"):
        plans = [plan_folder(p, root, logs) for p in folders]
        lines = ["Restore %s" % ("COMMITTED" if args.commit else "(dry run)"),
                 "=" * 40,
                 "generated %s" % _dt.datetime.now().isoformat(timespec="minutes"), ""]
        for fr in plans:
            blocked = fr.blocked(args.partial)
            if args.commit and not blocked:
                execute(fr, cfg)
            lines.append(fr.rel)
            if fr.original_name and fr.original_name != fr.folder.name:
                lines.append("  folder -> %s" % fr.original_name)
            renames = [s for s in fr.steps if s.kind == "rename_file"]
            tags = [s for s in fr.steps if s.kind == "restore_tags"]
            sidecars = [s for s in fr.steps if s.kind == "rewrite_sidecar"]
            lines.append("  %s, %d track name(s), %d checksum/cue file(s)"
                         % (tags[0].detail if tags else "no tags", len(renames),
                            len(sidecars)))
            for s in renames[:6]:
                lines.append("    %s -> %s" % (s.old.name, s.new.name))
            if len(renames) > 6:
                lines.append("    ... and %d more, in restore_steps.csv" % (len(renames) - 6))
            for why in fr.problems:
                lines.append("  REFUSED: %s" % why)
            for why in fr.loose:
                lines.append("  %s: %s" % ("left as it is" if args.partial else "REFUSED", why))
            for note in fr.notes:
                lines.append("  note: %s" % note)
            if args.commit:
                if blocked:
                    lines.append("  not restored")
                elif fr.status == "done":
                    lines.append("  RESTORED")
                else:
                    lines.append("  FAILED and put back as it was: %s" % fr.error)
            elif blocked:
                lines.append("  would not be restored")
            lines.append("")
        lines.append("A restored folder is no longer settled: the next dry run will "
                     "propose renaming it again. To keep it as it is, add it to "
                     "overrides.yaml with skip: true.")
        report_file(out_dir / "restore_summary.txt").write_text("\n".join(lines) + "\n",
                                                     encoding="utf-8")
        write_csv(out_dir / ("restore_committed.csv" if args.commit else "restore_steps.csv"),
                  ["status", "kind", "folder", "old", "new", "detail", "error"],
                  _rows(plans))
        if not args.commit:
            write_json(out_dir / PLAN_NAME, {
                "generated": _dt.datetime.now().isoformat(timespec="seconds"),
                "root": str(root), "folders": wanted,
                "restorable": [fr.rel for fr in plans if not fr.blocked(args.partial)]})

    print("\n".join(lines))
    print("reports: %s" % out_dir)
    if args.commit:
        return 1 if any(fr.status == "failed" for fr in plans) else 0
    return 0
