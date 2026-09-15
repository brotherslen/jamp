"""A run never destroys the reports of the run before it."""
import pytest

import fixtures
from jamp import cli, report, userdir


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("JAMP_HOME", str(h))
    monkeypatch.setattr(userdir, "LEGACY_OVERRIDES", tmp_path / "none.yaml")
    monkeypatch.setattr(userdir, "LEGACY_PATHS", tmp_path / "none.json")
    return h


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "lib"
    d = root / "Phish" / "Phish 11-22-97 sbd"
    for n in (1, 2):
        fixtures.make_flac(d / ("%02d Song %d.flac" % (n, n)),
                           tags={"ARTIST": "Phish", "TITLE": "Song %d" % n,
                                 "DATE": "1997-11-22", "TRACKNUMBER": str(n)})
    return root


def _history(out):
    base = out / report.HISTORY_NAME
    return sorted(p for p in base.iterdir()) if base.exists() else []


def test_a_second_plan_keeps_the_first_in_history(library, tmp_path, home):
    out = tmp_path / "r"
    assert cli.main(["plan", str(library), "--out-dir", str(out)]) == 0
    first = (out / "phase1_plan.json").read_bytes()
    assert _history(out) == []                          # nothing replaced yet
    assert cli.main(["plan", str(library), "--out-dir", str(out)]) == 0
    runs = _history(out)
    assert len(runs) == 1 and runs[0].name.endswith(" phase1")
    kept = runs[0] / "phase1_plan.json"
    assert kept.read_bytes() == first
    assert (runs[0] / "phase1_summary.txt").exists()
    assert (out / "phase1_plan.json").exists()          # the newest stays in place
    # A cache of reads is only worth its newest copy.
    assert not (runs[0] / "phase1_reads.json.gz").exists()


def test_a_dry_run_after_a_commit_keeps_the_reversal_log(library, tmp_path, home):
    """The case that made this: phase2_committed.csv lost to the next run."""
    out = tmp_path / "r"
    assert cli.main(["plan", str(library), "--out-dir", str(out)]) == 0
    assert cli.main(["apply", str(library), "--out-dir", str(out), "--commit"]) == 0
    log = (out / "phase2_committed.csv").read_text(encoding="utf-8-sig")
    assert "rename_folder" in log
    assert cli.main(["apply", str(library), "--out-dir", str(out)]) == 0
    kept = [r / "phase2_committed.csv" for r in _history(out) if r.name.endswith(" phase2")]
    assert kept and kept[0].read_text(encoding="utf-8-sig") == log


def test_two_runs_in_one_second_do_not_share_a_history_folder(tmp_path, monkeypatch):
    out = tmp_path / "r"
    out.mkdir()
    for text in ("first", "second", "third"):
        with report.run_lock(out, "phase1"):
            report._run["stamp"] = "2026-09-15T12-00-00"     # as if in one second
            (report.report_file(out / "phase1_summary.txt")).write_text(text)
    names = [p.name for p in _history(out)]
    assert names == ["2026-09-15T12-00-00 phase1", "2026-09-15T12-00-00 phase1 (2)"]
    assert (out / "history" / names[0] / "phase1_summary.txt").read_text() == "first"
    assert (out / "history" / names[1] / "phase1_summary.txt").read_text() == "second"
    assert (out / "phase1_summary.txt").read_text() == "third"


def test_a_run_rewriting_its_own_report_does_not_archive_it(tmp_path):
    out = tmp_path / "r"
    out.mkdir()
    with report.run_lock(out, "phase2"):
        report.write_json(out / "phase2_journal.json", {"n": 1})
        report.write_json(out / "phase2_journal.json", {"n": 2})
    assert _history(out) == []


def test_outside_a_run_nothing_moves(tmp_path):
    out = tmp_path / "r"
    out.mkdir()
    (out / "notes.csv").write_text("mine")
    report.write_csv(out / "notes.csv", ["a"], [["b"]])
    assert _history(out) == []


def test_files_that_are_appended_to_stay_where_they_are(tmp_path):
    out = tmp_path / "r"
    out.mkdir()
    (out / "convert_committed.csv").write_text("proof")
    with report.run_lock(out, "convert"):
        report.report_file(out / "convert_committed.csv")
    assert (out / "convert_committed.csv").read_text() == "proof"
    assert _history(out) == []
