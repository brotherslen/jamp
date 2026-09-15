"""Writing reports.

Everything lands in --out-dir, never in the library.  CSVs are written with a
UTF-8 BOM so Excel opens venue names with accents correctly.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import datetime as _dt
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path


LOCK_NAME = ".jamp_run.lock"
HISTORY_NAME = "history"

# Kept where they are rather than put in history: a ledger and a log that are
# appended to, and a cache of reads that is only ever worth its newest copy.
NEVER_ARCHIVED = frozenset({"verify_audio_ledger.csv", "convert_committed.csv",
                            "phase1_reads.json.gz", LOCK_NAME})

# The run holding the lock in this process: its reports folder, when it started,
# what it is, and the reports it has written so far.
_run: dict | None = None


def report_file(path: Path) -> Path:
    """`path`, after moving an earlier run's file of that name into history.

    Every run used to replace the reports of the run before it, and the one
    lost was always the one needed: phase2_committed.csv - the reversal log - was
    overwritten by the next dry run into the same folder, and so was a tag
    survey.  Now whatever a run is about to replace goes first to
    history/<when this run started> <what it is>/ in the same reports folder.
    The newest reports stay exactly where they always were, so nothing that
    reads them (the commit check, check, restore --log) needs to change.

    Only inside a run that holds the reports folder's lock, only for a file
    directly in that folder, and only once per file per run - a run rewriting its
    own report (a summary saved twice) is not replacing anyone's.
    """
    path = Path(path)
    run = _run
    if run is None or path.parent.resolve() != run["out_dir"]:
        return path
    key = str(path.resolve()).lower()
    if key in run["written"]:
        return path
    run["written"].add(key)
    if path.name in NEVER_ARCHIVED or not path.is_file():
        return path
    dest_dir = run.get("history_dir")
    if dest_dir is None:
        # Chosen on first use and kept: two runs of one command in the same
        # second must not share a folder and overwrite each other's history.
        base = run["out_dir"] / HISTORY_NAME / ("%s %s" % (run["stamp"], run["what"]))
        dest_dir, n = base, 1
        while dest_dir.exists():
            n += 1
            dest_dir = base.with_name("%s (%d)" % (base.name, n))
        dest_dir.mkdir(parents=True)
        run["history_dir"] = dest_dir
    os.replace(path, dest_dir / path.name)
    return path
# Long enough that a slow library scan never trips it, short enough that a lock
# left behind by a killed run does not block work for the rest of the day.
LOCK_STALE_AFTER = _dt.timedelta(hours=2)


class OutDirBusy(RuntimeError):
    """Another run is already writing this report directory."""


@contextmanager
def run_lock(out_dir: Path, what: str = "a run"):
    """Hold a report directory for the duration of one run.

    Two runs writing the same directory interleave their rows and leave a CSV
    that still parses but is quietly wrong - torn lines, half-written values.
    That is far worse than an error, because it looks like data.
    """
    lock = Path(out_dir) / LOCK_NAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    mine = "%s pid %d | %s" % (what, os.getpid(),
                               _dt.datetime.now().isoformat(timespec="seconds"))
    # Two attempts: the second only after a lock left by a dead run is cleared.
    for attempt in (1, 2):
        try:
            # Created exclusively, so two runs starting together cannot both
            # read "no lock" and both go ahead.
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                existing = lock.read_text(encoding="utf-8")
            except OSError:
                existing = ""
            if attempt == 1 and _lock_is_stale(existing):
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
            raise OutDirBusy(
                "%s is already being written by %s.\n"
                "Reports from two runs at once interleave and the CSVs come out "
                "corrupt but still readable, so this one is refused.\n"
                "If no run is active, delete %s and try again."
                % (out_dir, existing.strip() or "another run", lock)
            )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(mine)
        break
    global _run
    outer = _run
    _run = {"out_dir": Path(out_dir).resolve(), "what": what,
            "stamp": _dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S"),
            "written": set()}
    try:
        yield
    finally:
        _run = outer
        # Only our own lock.  If ours was taken over as stale while we ran,
        # the lock there now belongs to the run that is still writing.
        try:
            if lock.read_text(encoding="utf-8") == mine:
                lock.unlink()
        except OSError:                                  # pragma: no cover
            pass


def _pid_alive(pid: int) -> bool | None:
    """Is that process still running?  None when it cannot be told."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == 259                          # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return None
    return True


def _lock_is_stale(existing: str) -> bool:
    """A lock whose run is gone.

    Judged by whether the process that wrote it is still alive.  Age alone was
    the rule, and two hours is shorter than phase 0 --verify-audio: a second run
    took over a live run's lock, and the first run's cleanup then deleted the
    second's.  Age is only the fallback, for a lock that names no process.
    """
    m = re.search(r"pid (\d+)", existing)
    if m:
        alive = _pid_alive(int(m.group(1)))
        if alive is not None:
            return not alive
    try:
        started = _dt.datetime.fromisoformat(existing.split("|")[1].strip())
    except (IndexError, ValueError):
        return False
    return _dt.datetime.now() - started >= LOCK_STALE_AFTER


def ensure_out_dir(out_dir: Path, root: Path) -> Path:
    """Reports must not be dropped inside the library being scanned."""
    out_dir = Path(out_dir).resolve()
    root = Path(root).resolve()
    if out_dir == root or root in out_dir.parents:
        raise ValueError(
            "--out-dir must be outside ROOT so the library never gets new files: "
            "%s is inside %s" % (out_dir, root)
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def write_csv(path: Path, header: list[str], rows) -> int:
    count = 0
    with open(report_file(path), "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for row in rows:
            writer.writerow(["" if v is None else v for v in row])
            count += 1
    return count


def _default(obj):
    if isinstance(obj, (_dt.date, _dt.datetime)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    return str(obj)


def write_json(path: Path, data) -> None:
    report_file(path).write_text(json.dumps(data, indent=2, default=_default),
                                 encoding="utf-8")


@dataclass
class TextReport:
    title: str
    lines: list[str] = field(default_factory=list)

    def heading(self, text: str) -> None:
        self.lines.append("")
        self.lines.append(text)
        self.lines.append("-" * len(text))

    def line(self, text: str = "") -> None:
        self.lines.append(text)

    def kv(self, key: str, value, width: int = 34) -> None:
        self.lines.append("  %-*s %s" % (width, key, value))

    def histogram(self, counts: dict, width: int = 34, limit: int | None = None,
                  total: int | None = None) -> None:
        items = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
        if limit:
            items = items[:limit]
        if not items:
            self.lines.append("  (none)")
            return
        biggest = max(v for _, v in items) or 1
        for key, value in items:
            bar = "#" * max(1, round(20 * value / biggest))
            pct = " %5.1f%%" % (100 * value / total) if total else ""
            self.lines.append("  %-*s %6d%s  %s" % (width, str(key)[:width], value, pct, bar))

    def render(self) -> str:
        head = [self.title, "=" * len(self.title),
                "generated %s" % _dt.datetime.now().strftime("%Y-%m-%d %H:%M")]
        return "\n".join(head + self.lines) + "\n"

    def save(self, path: Path) -> Path:
        report_file(path).write_text(self.render(), encoding="utf-8")
        return path
