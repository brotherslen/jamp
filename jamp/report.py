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
    try:
        yield
    finally:
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
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
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
    path.write_text(json.dumps(data, indent=2, default=_default), encoding="utf-8")


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
        path.write_text(self.render(), encoding="utf-8")
        return path
