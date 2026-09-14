"""Tests for the standalone verify_audio command.

The checking itself lives in `jamp.integrity` and is tested there; what is
left here is the part that only the command has - resuming, so that an
interrupted run over a library this size costs the file in flight and nothing
else.
"""
import csv
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location(
    "verify_audio", ROOT / "tools" / "verify_audio.py")
verify_audio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_audio)


# --- resuming ---------------------------------------------------------------

def test_resume_skips_only_files_that_have_not_changed(tmp_path):
    out = tmp_path / "r.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(verify_audio.FIELDS)
        w.writerow(["PASS", "a.flac", 100, 5, "x", "x", "1.0", ""])
    done = verify_audio.already_done(out)
    assert ("a.flac", 100, 5) in done
    # a file replaced since - different size or mtime - is not covered
    assert ("a.flac", 101, 5) not in done
    assert ("a.flac", 100, 6) not in done


def test_a_missing_report_means_nothing_is_done(tmp_path):
    assert verify_audio.already_done(tmp_path / "nope.csv") == set()


def test_an_empty_report_still_gets_its_header(tmp_path):
    """Ctrl-C during the opening scan leaves a 0-byte file, and "it exists" was
    taken to mean the header was already there."""
    out = tmp_path / "r.csv"
    assert verify_audio.needs_header(out)
    out.write_bytes(b"")
    assert verify_audio.needs_header(out)
    out.write_text(",".join(verify_audio.FIELDS) + "\n", encoding="utf-8")
    assert not verify_audio.needs_header(out)
