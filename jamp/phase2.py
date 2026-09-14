"""Phase 2 - the writer.  The only part of the pipeline that changes anything.

Still a dry run unless you pass --commit.

What it will do:

* rename track files and the show folder to the planned names
* rewrite tags, after dumping the originals to .etree_backup.json
* rewrite .ffp / .md5 / .st5 / .cue so the checksums still name real files

What it will not do, ever:

* touch a folder that phase 1 did not mark PLAN (or MERGE, with --include-merges)
* rename onto an existing name
* delete anything, including the folders left empty by a merge

Each folder is a transaction.  If any step fails, that folder's completed steps
are undone and the run moves on, so a folder is never left half-renamed.
"""
from __future__ import annotations

import datetime as _dt
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .naming import parse_canonical, parse_release_show_name
from .phase1 import (DUPLICATE, MERGE, PLAN, SKIP_BLOCKED, SPLIT_SHOW,
                     UNCHANGED, ShowPlan, build_plans)
from .state import is_settled
from .winpath import opener
from .report import TextReport, ensure_out_dir, run_lock, write_json
from .state import write_state  # noqa: F401  (is_settled imported above)
from .tagwriter import BACKUP_NAME, write_backup, write_tags

# Action kinds, in the order they are applied within a folder.
BACKUP = "backup_tags"
TAGS = "write_tags"
RENAME_FILE = "rename_file"
MOVE_FILE = "move_file"
SIDECAR = "rewrite_sidecar"
RENAME_SIDECAR = "rename_sidecar"
RENAME_FOLDER = "rename_folder"
WRITE_STATE = "write_state"
QUARANTINE = "quarantine_lossy"
CLEAR_STATE = "clear_state"

PLANNED = "planned"
DONE = "done"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass
class Action:
    kind: str
    path: Path
    target: Path | None = None
    detail: str = ""
    payload: object = None
    status: str = PLANNED
    error: str | None = None
    # What a rewrite replaced, captured at the moment it is applied, so a
    # rollback can put it back.  Nothing else holds the original text.
    undo_data: bytes | None = None

    @property
    def describes_a_write(self) -> bool:
        return self.kind in (TAGS, SIDECAR)


@dataclass
class FolderWork:
    plan: ShowPlan
    actions: list[Action] = field(default_factory=list)
    status: str = PLANNED
    error: str | None = None

    @property
    def folder(self) -> Path:
        return self.plan.show.path


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------

def plan_actions(plan: ShowPlan, cfg: Config, include_merges: bool,
                 unnest: bool = False) -> FolderWork:
    work = FolderWork(plan=plan)
    folder = plan.show.path

    taggable = [t for t in plan.tracks if t.tags and t.file.tag_support == "full"]
    if taggable:
        work.actions.append(Action(
            BACKUP, folder, folder / BACKUP_NAME,
            "dump original tags of %d files before changing any" % len(plan.tracks),
            # The names the files are about to be given travel with the backup.
            # Without them the backup is keyed by names that will not exist a
            # few actions later, and restoring finds nothing to restore.
            payload=(
                [t.file.path for t in plan.tracks],
                {t.file.path.name: t.new_name
                 for t in plan.tracks if t.new_name != t.file.path.name},
            ),
        ))
    for t in taggable:
        work.actions.append(Action(
            TAGS, t.file.path, None,
            "; ".join("%s=%r" % (k, v[1]) for k, v in sorted(t.tags.items()))[:200],
            payload={k: v[1] for k, v in t.tags.items()},
        ))

    merging = plan.status == MERGE
    if merging:
        target_folder = plan.merge_target
    else:
        parent = folder.parent
        if unnest:
            lifted, why = lift_target(plan)
            if lifted is not None:
                parent = lifted
            if why:
                work.plan.warnings.append(why)
        target_folder = parent / plan.new_folder_name

    for t in plan.tracks:
        if merging and str(folder) != str(target_folder):
            # The primary folder becomes the target by being renamed, so only
            # the other folders' files actually move between directories.
            if plan.merge_role == "member":
                work.actions.append(Action(
                    MOVE_FILE, t.file.path, target_folder / t.new_name,
                    "move into the merged folder", payload=t.old_name))
                continue
        if t.old_name != t.new_name:
            work.actions.append(Action(
                RENAME_FILE, t.file.path, t.file.path.parent / t.new_name,
                "", payload=t.old_name))

    for sidecar in plan.sidecars:
        if sidecar.status == "rewrite" and sidecar.new_text is not None:
            work.actions.append(Action(
                SIDECAR, sidecar.path, None,
                "rewrite %d entries for the new filenames" % len(sidecar.referenced),
                payload=(sidecar.new_text, sidecar.encoding or "utf-8")))
        if sidecar.new_name and sidecar.new_name != sidecar.path.name:
            work.actions.append(Action(
                RENAME_SIDECAR, sidecar.path, sidecar.path.parent / sidecar.new_name))

    if merging:
        if plan.merge_role == "primary" and str(folder) != str(target_folder):
            work.actions.append(Action(
                RENAME_FOLDER, folder, target_folder,
                "primary folder of a merge becomes the merged folder"))
    elif plan.new_folder_name and str(folder) != str(target_folder):
        work.actions.append(Action(RENAME_FOLDER, folder, target_folder))

    # Last, into the folder under its final name: what was decided, so a later
    # run does not re-derive a different answer from the tags we just wrote.
    a = plan.analysis
    work.actions.append(Action(
        WRITE_STATE, target_folder, target_folder / ".etree_state.json",
        "record the decision so re-runs leave this folder alone",
        payload={
            "band": a.band.abbrev,
            "date": a.date.iso,
            "date_confidence": a.date.confidence,
            "classification": a.classification.kind,
            "shape": a.classification.shape,
            "source": a.source.value,
            "source_inferred": a.source.inferred,
            "provenance": a.provenance,
            "format": a.fmt,
            "mp3_quality": a.quality,
            "previous_folder_name": plan.show.name,
            # Kept only on the first write, as original_relative_path.
            "previous_relative_path": plan.show.rel,
        },
    ))
    return work


def lift_target(plan: ShowPlan) -> tuple[Path | None, str]:
    """Where --unnest moves this show, or None - and what to say about it.

    "Pull it out": a show buried in a container that holds nothing else, or in
    a multi-night bundle that is not a release, comes up a level.  The folder it
    leaves is left behind, not deleted.

    Never to the top of ROOT.  There a show sits outside every act folder, and
    the folder it came out of was the act: that is how STS9 and TAB were emptied
    and had to be put back by hand.
    """
    show = plan.analysis.show
    parent = show.path.parent
    for holder, what in (
        (show.container, "which holds nothing else; that folder is left behind "
                         "empty for you to remove"),
        # A bundle rather than a release: its shows belong beside every other
        # show of that year, not a level down.
        (show.filing_parent, "which holds several shows but is not a release; "
                             "that folder is left behind for you to remove"),
    ):
        if holder is None or str(holder) != str(parent):
            continue
        if str(holder.parent) == str(show.root):
            return None, ("not lifted out of %r: that would leave it loose at the top "
                          "of ROOT, outside every act folder" % holder.name)
        return holder.parent, "lifted out of %r, %s" % (holder.name, what)
    return None, ""


def occupied_merge_targets(merges: list[ShowPlan]) -> set[str]:
    """Merge targets already held by a folder that is no part of the merge."""
    participants: dict[str, set[str]] = {}
    for p in merges:
        participants.setdefault(str(p.merge_target), set()).add(str(p.show.path))
    return {target for target, folders in participants.items()
            if target not in folders and Path(target).exists()}


def eligible_plans(plans: list[ShowPlan], include_merges: bool,
                   unnest: bool = False) -> list[ShowPlan]:
    out = [p for p in plans if p.status == PLAN]
    if unnest:
        # A show can be committed under exactly the right name and still sit a
        # level too deep.  Nothing about its name changes, so it is UNCHANGED
        # and would never be selected - yet moving it up is the whole point of
        # --unnest.  Without this the flag silently does nothing on a library
        # that has already been renamed.
        out.extend(
            p for p in plans
            if p.status == UNCHANGED and p.new_folder_name
            and lift_target(p)[0] is not None
        )
    # Deepest first.  A show can sit inside another show - a Download Series
    # volume misfiled inside a box-set night - and both get renamed.  Renaming
    # the parent first moves the child out from under the path the plan
    # recorded, and the child fails with FileNotFoundError.  A child always has
    # more path parts than its parent, so depth alone orders them safely.
    out.sort(key=lambda p: len(p.show.path.parts), reverse=True)
    if include_merges:
        merges = [p for p in plans if p.status == MERGE]
        # Two shows resolving to one name are both reported, never overwritten -
        # and for a merge that has to be settled before anything moves.  A
        # member moves its files into the target as its own unit of work, so the
        # primary failing to become that target does not stop it: the folder
        # already holding the name ends up with one recording's tracks sitting
        # beside another's, and rolling the primary back does not bring them
        # home.  Trey Anastasio 2002-05-31 is the case - a whole copy of the show
        # under the target name, and a separate split copy of the same date whose
        # audio is nothing like it.
        occupied = occupied_merge_targets(merges)
        merges = [p for p in merges if str(p.merge_target) not in occupied]
        # Primary first: it creates the folder the members move into.
        merges.sort(key=lambda p: (str(p.merge_target), p.merge_role != "primary"))
        out.extend(merges)
    return out


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------

# Windows errors that mean "somebody else is holding this, briefly":
#   5  ERROR_ACCESS_DENIED    - typically an open handle on a directory
#   32 ERROR_SHARING_VIOLATION- the file itself is open elsewhere
# We have just written tags to every file in the folder and renamed them, which
# is exactly what wakes the search indexer and antivirus; they open what changed
# and hold the directory for a moment.  Real permission problems do not clear on
# their own, so after the last attempt the original error is raised unchanged.
_TRANSIENT_WINERRORS = frozenset({5, 32})


def _rename_with_retry(fn, attempts: int, first_delay: float) -> None:
    delay = first_delay
    for remaining in range(attempts - 1, -1, -1):
        try:
            fn()
            return
        except OSError as exc:
            if remaining == 0 or getattr(exc, "winerror", None) not in _TRANSIENT_WINERRORS:
                raise
            time.sleep(delay)
            delay *= 2


def _safe_rename(src: Path, dst: Path, attempts: int = 1,
                 first_delay: float = 0.15, make_parents: bool = False) -> None:
    """Rename, refusing to land on anything that already exists.

    A rename that only changes case goes via a temporary name, because Windows
    treats the two names as the same file and would otherwise do nothing.

    The destination's folder has to exist already unless `make_parents` says
    otherwise.  Creating it on demand let a merge member move its tracks into a
    merged folder its primary had failed to become: the member built the
    folder itself, and one half of the show ended up under the merged name
    while the other half sat where it started.
    """
    # NB: WindowsPath equality is case-insensitive, so "MMJ2006.flac" ==
    # "mmj2006.flac" is True and comparing Paths here would silently skip every
    # case-only rename.  Compare the strings.
    if str(src) == str(dst):
        return
    same_but_case = str(src).lower() == str(dst).lower()
    if not same_but_case and os.path.exists(opener(dst)):
        raise FileExistsError("target already exists: %s" % dst)
    if make_parents:
        os.makedirs(opener(dst.parent), exist_ok=True)
    elif not os.path.isdir(opener(dst.parent)):
        raise FileNotFoundError("destination folder does not exist: %s" % dst.parent)
    if same_but_case:
        interim = src.with_name(src.name + ".jamp-tmp")
        if os.path.exists(opener(interim)):
            raise FileExistsError("interim name already exists: %s" % interim)
        _rename_with_retry(lambda: os.rename(opener(src), opener(interim)),
                           attempts, first_delay)
        try:
            _rename_with_retry(lambda: os.rename(opener(interim), opener(dst)),
                               attempts, first_delay)
        except BaseException:
            # Put it back under its own name.  Left at the interim name, the
            # rollback looks for it under the new name, does not find it, and
            # the file sits as "x.flac.jamp-tmp" where nothing will look.
            os.rename(opener(interim), opener(src))
            raise
    else:
        _rename_with_retry(lambda: os.rename(opener(src), opener(dst)),
                           attempts, first_delay)


def _apply(action: Action, cfg: Config) -> None:
    if action.kind == BACKUP:
        write_backup(action.path, action.payload[0], action.payload[1])
    elif action.kind == TAGS:
        write_tags(action.path, action.payload)
    elif action.kind in (RENAME_FILE, RENAME_SIDECAR, RENAME_FOLDER, MOVE_FILE):
        _safe_rename(action.path, action.target,
                     attempts=cfg.settings.rename_attempts,
                     first_delay=cfg.settings.rename_retry_delay)
    elif action.kind == SIDECAR:
        # The rewrite replaces the only copy of the original checksums.  Keep
        # it until the folder is through, or a rollback restores the audio
        # names and leaves the sidecar describing names that no longer exist.
        with open(opener(action.path), "rb") as fh:
            action.undo_data = fh.read()
        # In the encoding it was read in, BOM and all.  Rewriting a cp1252 .md5
        # as UTF-8 changed the bytes of every accented name in it.
        text, encoding = (action.payload if isinstance(action.payload, tuple)
                          else (action.payload, "utf-8"))
        with open(opener(action.path), "wb") as fh:
            fh.write(text.encode(encoding))
    elif action.kind == WRITE_STATE:
        write_state(action.path, action.payload)
    elif action.kind == QUARANTINE:
        _safe_rename(action.path, action.target, make_parents=True)
    elif action.kind == CLEAR_STATE:
        action.path.unlink(missing_ok=True)
    else:                                                # pragma: no cover
        raise RuntimeError("unknown action %r" % action.kind)
    action.status = DONE


def _undo(action: Action) -> None:
    """Reverse one completed action.  Tag writes are left to the backup file."""
    if action.kind in (RENAME_FILE, RENAME_SIDECAR, RENAME_FOLDER, MOVE_FILE, QUARANTINE):
        _safe_rename(action.target, action.path)
    elif action.kind == SIDECAR and action.undo_data is not None:
        # Actions are undone newest first, so a rename of this sidecar has
        # already been reversed and it is back under action.path.
        with open(opener(action.path), "wb") as fh:
            fh.write(action.undo_data)


def execute_folder(work: FolderWork, cfg: Config) -> None:
    """Apply one folder's actions, undoing them all if any of them fails.

    An interrupt counts as a failure.  Ctrl-C during a long run lands in the
    middle of some folder, and stopping there without undoing it leaves exactly
    the half-renamed folder this function exists to prevent.  The folder is put
    back, then the interrupt carries on and ends the run.
    """
    completed: list[Action] = []
    for action in work.actions:
        try:
            _apply(action, cfg)
            completed.append(action)
        except BaseException as exc:
            action.status = FAILED
            action.error = "%s: %s" % (exc.__class__.__name__, exc)
            work.status = FAILED
            work.error = action.error
            for done in reversed(completed):
                try:
                    _undo(done)
                    done.status = "undone"
                except Exception as undo_exc:            # pragma: no cover
                    done.error = "could not undo: %s" % undo_exc
            if not isinstance(exc, Exception):
                raise
            return
    work.status = DONE


def primary_failed(work: FolderWork, failed_targets: set[str]) -> str | None:
    """Why a merge member must not move, or None.

    A member moves its tracks into the merged folder, and that folder is its
    primary renamed.  If the primary did not get there, the member has nowhere
    to go - and moving anyway splits the show across two folders.
    """
    plan = work.plan
    if plan.status != MERGE or plan.merge_role != "member":
        return None
    target = str(plan.merge_target)
    if target in failed_targets:
        return "not moved: the primary folder of this merge failed, so %r was never made" % (
            Path(target).name)
    if not os.path.isdir(opener(target)):
        return "not moved: the merged folder %r does not exist" % Path(target).name
    return None


class CommitLog:
    """phase2_committed.csv, written as each folder finishes.

    It is the reversal log, so it cannot wait for the end of the run: a run
    that is killed, crashes or is interrupted two hours into Phish would
    otherwise leave the folders it did commit with no record of what changed.
    """

    HEADER = ["status", "kind", "folder", "old", "new", "detail", "error"]

    def __init__(self, path: Path):
        import csv

        self._fh = open(path, "w", newline="", encoding="utf-8-sig")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(self.HEADER)
        self._fh.flush()

    def record(self, work: FolderWork) -> None:
        for a in work.actions:
            self._writer.writerow(
                [a.status, a.kind, work.plan.show.rel, str(a.path),
                 str(a.target) if a.target else "", a.detail, a.error or ""])
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def quarantine_actions(plan: ShowPlan, cfg: Config, root: Path) -> FolderWork:
    """Move MP3s that duplicate a lossless copy into the review folder.

    Moved, never deleted, and the original path is mirrored underneath the
    review folder so it is obvious where each file came from and trivial to put
    back.
    """
    work = FolderWork(plan=plan)
    review = Path(root) / cfg.settings.review_folder
    finding = plan.lossy
    if not finding:
        return work

    source_dir = plan.show.path
    try:
        relative = source_dir.relative_to(root)
    except ValueError:                                   # pragma: no cover
        relative = Path(source_dir.name)
    destination = review / relative

    for f in finding.files:
        work.actions.append(Action(
            QUARANTINE, f.path, destination / f.path.name,
            finding.reason[:160], payload=finding.counterpart))

    # The folder's contents changed, so whatever was decided about it before no
    # longer describes it - let the next run work it out again.
    if not finding.whole_folder and is_settled(source_dir):
        work.actions.append(Action(
            CLEAR_STATE, source_dir / ".etree_state.json", None,
            "the folder lost files, so its recorded decision no longer fits"))
    return work


def settle_actions(plan: ShowPlan) -> FolderWork:
    """Record a folder's current name as the settled answer, renaming nothing."""
    a = plan.analysis
    folder = plan.show.path
    work = FolderWork(plan=plan)
    work.actions.append(Action(
        WRITE_STATE, folder, folder / ".etree_state.json",
        "freeze this folder under the name it already has",
        payload={
            "band": a.band.abbrev,
            "date": a.date.iso,
            "classification": a.classification.kind,
            "source": a.source.value,
            "provenance": a.provenance,
            "format": a.fmt,
            "settled_by": "--settle, from the name already on disk",
        },
    ))
    return work


def settleable(plans: list[ShowPlan]) -> list[ShowPlan]:
    """Folders whose current name is already one of ours."""
    out = []
    for p in plans:
        if is_settled(p.show.path):
            continue
        # Never settle a folder that is still in a dispute.  Freezing one half
        # of a duplicate pair would leave the other half free to rename itself
        # and quietly break the pairing.
        if p.status not in (PLAN, UNCHANGED):
            continue
        name = p.show.name
        # A date and a place is our name for a show inside a release, and only
        # there.  Anywhere else "2023-07-14 Ameris Bank Amphitheatre" is a name
        # someone typed, and freezing it as settled would keep it from ever
        # being renamed.
        if parse_canonical(name) or (p.show.release_dir is not None
                                     and parse_release_show_name(name)):
            out.append(p)
    return out


def run(
    root: Path,
    out_dir: Path,
    cfg: Config,
    commit: bool = False,
    include_merges: bool = False,
    today: _dt.date | None = None,
    reclassify: bool = False,
    settle: bool = False,
    unnest: bool = False,
    quarantine_lossy: bool = False,
    include_top: set[str] | None = None,
    overrides=None,
    until_settled: bool = False,
    max_passes: int = 5,
) -> dict:
    out_dir = ensure_out_dir(out_dir, root)
    with run_lock(out_dir, "phase2"):
        # Committing changes the evidence the next read sees - a folder can
        # only be named correctly once the thing it was nested in has moved,
        # and a venue can only be derived once the tags carry it.  So the
        # answer converges over a few passes rather than arriving at once.
        # Doing that by hand meant a commit, then a whole separate phase 1 to
        # find out whether anything was left, then another commit.
        looping = commit and until_settled and not (settle or quarantine_lossy)
        all_works: list[FolderWork] = []
        # A folder that failed will fail again for the same reason - a
        # duplicate to resolve, a date to settle - so trying it once per pass
        # only multiplies the failure count.  Six blocked folders reported as
        # eighteen failures, which reads like something got worse.
        failed_before: set[str] = set()
        passes = 0
        with CommitLog(out_dir / "phase2_committed.csv") as log:
            while True:
                passes += 1
                plans = build_plans(root, cfg, today=today, reclassify=reclassify,
                                    include_top=include_top, overrides=overrides)
                if quarantine_lossy:
                    works = [quarantine_actions(p, cfg, Path(root)) for p in plans if p.lossy]
                elif settle:
                    works = [settle_actions(p) for p in settleable(plans)]
                else:
                    chosen = [p for p in eligible_plans(plans, include_merges, unnest=unnest)
                              if str(p.show.path) not in failed_before]
                    works = [plan_actions(p, cfg, include_merges, unnest=unnest)
                             for p in chosen]

                failed_targets: set[str] = set()
                for work in works:
                    try:
                        blocked = primary_failed(work, failed_targets) if commit else None
                        if not commit:
                            work.status = PLANNED
                        elif blocked:
                            work.status = FAILED
                            work.error = blocked
                            for a in work.actions:
                                a.status = SKIPPED
                        else:
                            execute_folder(work, cfg)
                    finally:
                        # Also on an interrupt: the folder was rolled back, and
                        # the log should say so rather than stop one folder short.
                        all_works.append(work)
                        log.record(work)
                    if (work.status == FAILED and work.plan.status == MERGE
                            and work.plan.merge_role == "primary"):
                        failed_targets.add(str(work.plan.merge_target))
                failed_before.update(str(w.folder) for w in works if w.status == FAILED)

                if not looping:
                    break
                done = sum(1 for w in works if w.status == DONE)
                # Stop on success, and stop on stalemate: a pass that changed
                # nothing will change nothing next time either, and looping on it
                # would just rewrite the same folders forever.
                if not works or done == 0 or passes >= max_passes:
                    break

        stats = _write_reports(out_dir, plans, all_works, cfg, commit, include_merges)
        stats["passes"] = passes
        if commit:
            # Say plainly whether there is anything left, so finding out does
            # not need a separate phase 1 run.
            after = build_plans(root, cfg, today=today, reclassify=True,
                                include_top=include_top, overrides=overrides)
            stats["remaining"] = len(eligible_plans(after, include_merges, unnest=unnest))
            # "Nothing left to do" is not "everything got renamed".  Folders
            # blocked on a date conflict, waiting on a duplicate decision, or
            # holding other shows are all left alone on purpose, and saying so
            # is the difference between a finished run and a puzzling one.
            held = {}
            for p_ in after:
                if p_.status in (SKIP_BLOCKED, DUPLICATE, MERGE, SPLIT_SHOW):
                    held[p_.status] = held.get(p_.status, 0) + 1
            stats["held"] = held
        return stats


def _write_reports(out_dir: Path, plans, works: list[FolderWork], cfg: Config,
                   commit: bool, include_merges: bool) -> dict:
    counts = {
        "folders_selected": len(works),
        "folders_done": sum(1 for w in works if w.status == DONE),
        "folders_failed": sum(1 for w in works if w.status == FAILED),
        "actions": sum(len(w.actions) for w in works),
        "committed": commit,
        "merges_included": include_merges,
        # Refusing a merge selects nothing, which on its own reads exactly like
        # having nothing to do.  Counted so the run can say which it was.
        "merges_refused": len(occupied_merge_targets(
            [p for p in plans if p.status == MERGE])) if include_merges else 0,
    }

    # The reversal log, phase2_committed.csv, is written by CommitLog as each
    # folder finishes rather than here, so an interrupted run still has one.

    write_json(out_dir / "phase2_journal.json", {
        "generated": _dt.datetime.now().isoformat(timespec="seconds"),
        "committed": commit,
        "merges_included": include_merges,
        "folders": [
            {
                "folder": str(w.folder),
                "status": w.status,
                "error": w.error,
                "proposed": w.plan.new_folder_name,
                "actions": [
                    {"kind": a.kind, "status": a.status, "old": str(a.path),
                     "new": str(a.target) if a.target else None,
                     "detail": a.detail, "error": a.error}
                    for a in w.actions
                ],
            }
            for w in works
        ],
    })

    rep = TextReport("Phase 2 - %s" % ("COMMITTED" if commit else "dry run, nothing written"))
    rep.heading("Scope")
    rep.kv("folders phase 1 marked PLAN", sum(1 for p in plans if p.status == PLAN))
    rep.kv("merges included", "yes" if include_merges else "no (pass --include-merges)")
    rep.kv("folders selected", counts["folders_selected"])
    rep.kv("actions", counts["actions"])
    if commit:
        rep.kv("folders completed", counts["folders_done"])
        rep.kv("folders failed and rolled back", counts["folders_failed"])

    kinds: dict[str, int] = {}
    for w in works:
        for a in w.actions:
            kinds[a.kind] = kinds.get(a.kind, 0) + 1
    rep.heading("Actions by kind")
    rep.histogram(kinds, width=20)

    failures = [w for w in works if w.status == FAILED]
    if failures:
        rep.heading("Folders that failed and were rolled back (%d)" % len(failures))
        for w in failures:
            rep.line("  %s" % w.plan.show.rel)
            rep.line("      %s" % (w.error or "")[:120])

    if include_merges:
        refused = occupied_merge_targets([p for p in plans if p.status == MERGE])
        if refused:
            rep.heading("Merges refused - the name is already taken (%d)" % len(refused))
            rep.line("  Another folder already holds the name these would merge into,")
            rep.line("  and it is not part of the merge.  Nothing was moved: a member")
            rep.line("  moves its files in as its own step, so letting this run would")
            rep.line("  put one recording's tracks inside another's folder.  Decide")
            rep.line("  which copy to keep, then re-run.")
            for target in sorted(refused):
                rep.line("  %s" % Path(target).name)
                for p in plans:
                    if p.status == MERGE and str(p.merge_target) == target:
                        rep.line("      would have merged: %s" % p.show.rel)

        # A member that was refused still holds its files, so it is not listed.
        emptied = [w for w in works
                   if w.plan.status == MERGE and w.plan.merge_role == "member"
                   and (not commit or w.status == DONE)]
        if emptied:
            rep.heading("Folders left empty by a merge (%d)" % len(emptied))
            rep.line("  Nothing is deleted; remove these by hand once you are happy.")
            for w in emptied:
                rep.line("  %s" % w.plan.show.rel)

    rep.heading("How to reverse this")
    rep.line("  phase2_committed.csv lists every change, oldest first: renames can be")
    rep.line("  undone by swapping old and new, and the original tags of every folder")
    rep.line("  are in its %s." % BACKUP_NAME)
    rep.line("  jamp.tagwriter.restore_from_backup(folder) puts a folder's tags back.")

    rep.save(out_dir / "phase2_summary.txt")
    return counts
