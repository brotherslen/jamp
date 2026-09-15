"""Decode FLAC files against their own fingerprint, resumably.

Shared by `phase0 --verify-audio` and `tools/verify_audio.py`, so the two cannot
drift apart.  It used to be two copies: phase 0 decoded everything on every
run and remembered nothing, while only the tool could pick up where an
interrupted run stopped - and a standalone install has no tools.

A full pass over a large library is measured in hours, so results go to a
ledger as they are produced, and a later run skips any file already recorded
at the same size and modification time.  Interrupting it costs only the files
in flight.  A file that has since been replaced is checked again rather than
inheriting an old verdict.
"""
from __future__ import annotations

import csv
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .integrity import MISMATCH, NO_MD5, PASS, UNREADABLE, verify
from .winpath import opener

FIELDS = ["status", "file", "bytes", "mtime", "stored_md5", "decoded_md5",
          "seconds", "detail"]
LEDGER_NAME = "verify_audio_ledger.csv"


def needs_header(ledger: Path) -> bool:
    """Missing or empty.  Ctrl-C during the first write can leave a 0-byte file;
    a resume that then read its first data row as the header lost everything."""
    return not ledger.exists() or ledger.stat().st_size == 0


def recorded(ledger: Path) -> dict[tuple, dict]:
    """Every verdict in the ledger, keyed by (relative path, size, mtime)."""
    out: dict[tuple, dict] = {}
    if not ledger.exists():
        return out
    try:
        with ledger.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    out[(row["file"], int(row["bytes"]), int(row["mtime"]))] = row
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        return {}
    return out


def _check(ffmpeg: str, path: Path, rel: str) -> dict:
    st = os.stat(opener(path))
    v = verify(ffmpeg, path)
    return {"status": v["status"], "file": rel, "bytes": st.st_size,
            "mtime": int(st.st_mtime), "stored_md5": v["stored_md5"] or "",
            "decoded_md5": v["decoded_md5"] or "",
            "seconds": "%.1f" % v["seconds"] if v.get("seconds") else "",
            "detail": v["detail"] or ""}


def verify_files(files: list[Path], root: Path, ledger: Path, ffmpeg: str,
                 workers: int = 4, progress=None) -> dict[str, dict]:
    """A verdict for every file, by path relative to `root`.

    Files already in the ledger at the same size and mtime are not decoded
    again; everything decoded now is appended to it as it finishes.
    """
    root = Path(root)
    ledger = Path(ledger)
    done = recorded(ledger)
    verdicts: dict[str, dict] = {}
    todo = []
    for path in files:
        rel = str(Path(path).relative_to(root))
        try:
            st = os.stat(opener(path))
        except OSError:
            verdicts[rel] = {"status": UNREADABLE, "file": rel, "bytes": 0, "mtime": 0,
                             "stored_md5": "", "decoded_md5": "", "seconds": "",
                             "detail": "the file could not be read"}
            continue
        row = done.get((rel, st.st_size, int(st.st_mtime)))
        if row is not None:
            verdicts[rel] = row
        else:
            todo.append((path, rel))
    if progress:
        progress("  %d FLAC files: %d already checked (%s), %d to decode"
                 % (len(files), len(files) - len(todo), ledger.name, len(todo)))
    if not todo:
        return verdicts

    ledger.parent.mkdir(parents=True, exist_ok=True)
    fresh = needs_header(ledger)
    with ledger.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if fresh:
            writer.writeheader()
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(_check, ffmpeg, p, rel): rel for p, rel in todo}
            for i, fut in enumerate(as_completed(futures), 1):
                rel = futures[fut]
                try:
                    row = fut.result()
                except Exception as exc:            # noqa: BLE001 - never silent
                    row = {"status": UNREADABLE, "file": rel, "bytes": 0, "mtime": 0,
                           "stored_md5": "", "decoded_md5": "", "seconds": "",
                           "detail": "verifier raised: %s" % str(exc)[:200]}
                writer.writerow(row)
                verdicts[rel] = row
                if row["status"] in (MISMATCH, UNREADABLE) or i % 50 == 0:
                    fh.flush()
                if progress and (i % 50 == 0 or i == len(todo)):
                    progress("  decoded %d/%d" % (i, len(todo)))
    return verdicts


__all__ = ["FIELDS", "LEDGER_NAME", "MISMATCH", "NO_MD5", "PASS", "UNREADABLE",
           "recorded", "verify_files"]
