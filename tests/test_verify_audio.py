"""Resuming the audio check.

The decoding itself lives in `jamp.integrity` and is tested there; what is
tested here is the ledger that `phase0 --verify-audio` and
`tools/verify_audio.py` share, so an interrupted run over a large library costs
the files in flight and nothing else.
"""
import csv
import os

import fixtures
from jamp import verify


def _ledger_with(path, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(verify.FIELDS)
        w.writerows(rows)


def test_recorded_verdicts_are_keyed_by_path_size_and_mtime(tmp_path):
    out = tmp_path / "r.csv"
    _ledger_with(out, [["PASS", "a.flac", 100, 5, "x", "x", "1.0", ""]])
    done = verify.recorded(out)
    assert ("a.flac", 100, 5) in done
    # a file replaced since - different size or mtime - is not covered
    assert ("a.flac", 101, 5) not in done
    assert ("a.flac", 100, 6) not in done


def test_a_missing_ledger_means_nothing_is_done(tmp_path):
    assert verify.recorded(tmp_path / "nope.csv") == {}


def test_an_empty_ledger_still_gets_its_header(tmp_path):
    """Ctrl-C during the first write leaves a 0-byte file, and "it exists" was
    taken to mean the header was already there."""
    out = tmp_path / "r.csv"
    assert verify.needs_header(out)
    out.write_bytes(b"")
    assert verify.needs_header(out)
    out.write_text(",".join(verify.FIELDS) + "\n", encoding="utf-8")
    assert not verify.needs_header(out)


def test_a_second_run_decodes_only_what_changed(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    a = fixtures.make_flac(lib / "show" / "a.flac")
    b = fixtures.make_flac(lib / "show" / "b.flac")
    decoded = []

    def fake_verify(ffmpeg, path):
        decoded.append(path.name)
        return {"status": verify.PASS, "stored_md5": "x", "decoded_md5": "x",
                "seconds": 1.0, "detail": ""}

    monkeypatch.setattr(verify, "verify", fake_verify)
    ledger = tmp_path / "reports" / verify.LEDGER_NAME
    first = verify.verify_files([a, b], lib, ledger, "ffmpeg")
    assert sorted(decoded) == ["a.flac", "b.flac"] and len(first) == 2

    decoded.clear()
    b.write_bytes(b.read_bytes() + b"\0")                # b replaced since
    os.utime(b, (os.path.getatime(b), os.path.getmtime(b) + 10))
    second = verify.verify_files([a, b], lib, ledger, "ffmpeg")
    assert decoded == ["b.flac"]
    assert set(second) == {os.path.join("show", "a.flac"), os.path.join("show", "b.flac")}


def test_phase0_verify_audio_resumes_from_its_ledger(tmp_path, monkeypatch, cfg):
    from jamp import phase0

    lib = tmp_path / "lib"
    fixtures.make_flac(lib / "Phish" / "ph1997-11-22" / "d1t01.flac",
                       tags={"ARTIST": "Phish"})
    calls = []

    def fake_verify(ffmpeg, path):
        calls.append(path.name)
        return {"status": verify.PASS, "stored_md5": "x", "decoded_md5": "x",
                "seconds": 1.0, "detail": ""}

    monkeypatch.setattr(verify, "verify", fake_verify)
    out = tmp_path / "reports"
    phase0.run(lib, out, cfg, verify_audio=True)
    assert calls == ["d1t01.flac"] and (out / verify.LEDGER_NAME).exists()
    calls.clear()
    stats = phase0.run(lib, out, cfg, verify_audio=True)
    assert calls == [] and stats["verify_pass"] == 1
    assert (out / "phase0_verify_audio.csv").exists()
