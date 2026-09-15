"""`jamp tidy`, and `complete` finding its show database by itself."""
import json
import sqlite3

import pytest

import fixtures
from jamp import cli, distill, userdir


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("JAMP_HOME", str(h))
    monkeypatch.setattr(userdir, "LEGACY_OVERRIDES", tmp_path / "none.yaml")
    monkeypatch.setattr(userdir, "LEGACY_PATHS", tmp_path / "none.json")
    return h


def _run(command, root, out, *extra):
    return cli.main([command, str(root), "--out-dir", str(out), *extra])


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "lib"
    (root / "Phish" / "wrapper emptied by unnest").mkdir(parents=True)
    (root / "Phish" / "nested" / "empty" / "deeper").mkdir(parents=True)
    fixtures.make_flac(root / "Phish" / "ph1997-11-22" / "d1t01.flac")
    info = root / "Phish" / "old wrapper" / "info.txt"
    info.parent.mkdir(parents=True)
    info.write_text("the only description of the show")
    (root / "Trash" / "left empty").mkdir(parents=True)
    (root / "Grateful Dead").mkdir()
    return root


def test_tidy_dry_run_removes_nothing(library, tmp_path, home):
    before = sorted(p for p in library.rglob("*"))
    assert _run("tidy", library, tmp_path / "r") == 0
    assert sorted(p for p in library.rglob("*")) == before
    summary = (tmp_path / "r" / "tidy_summary.txt").read_text(encoding="utf-8")
    assert "wrapper emptied by unnest" in summary and "old wrapper" in summary


def test_tidy_removes_only_empty_folders(library, tmp_path, home):
    home.mkdir(parents=True)
    (home / "jamp.yaml").write_text("settings:\n  ignore_folders: [Trash]\n",
                                    encoding="utf-8")
    _run("tidy", library, tmp_path / "r")
    assert _run("tidy", library, tmp_path / "r", "--commit") == 0
    assert not (library / "Phish" / "wrapper emptied by unnest").exists()
    assert not (library / "Phish" / "nested").exists()             # emptied bottom up
    assert (library / "Phish" / "old wrapper" / "info.txt").exists()  # files: never
    assert (library / "Phish" / "ph1997-11-22" / "d1t01.flac").exists()
    assert (library / "Trash" / "left empty").is_dir()             # ignored: not looked in
    assert library.is_dir()


def test_tidy_never_removes_the_artist_folder_it_was_given(library, tmp_path, home):
    _run("tidy", library, tmp_path / "r", "--artist", "Grateful Dead")
    assert _run("tidy", library, tmp_path / "r", "--artist", "Grateful Dead",
                "--commit") == 0
    assert (library / "Grateful Dead").is_dir()


def test_tidy_commit_needs_its_dry_run(library, tmp_path, home, capsys):
    assert _run("tidy", library, tmp_path / "r", "--commit") == 2
    assert "dry run" in capsys.readouterr().err
    assert (library / "Phish" / "wrapper emptied by unnest").is_dir()


def test_a_folder_that_fills_up_before_the_commit_is_kept(library, tmp_path, home):
    _run("tidy", library, tmp_path / "r")
    (library / "Phish" / "wrapper emptied by unnest" / "new.flac").write_bytes(b"x")
    assert _run("tidy", library, tmp_path / "r", "--commit") == 0
    assert (library / "Phish" / "wrapper emptied by unnest" / "new.flac").exists()


# -- the show database ------------------------------------------------------

def _cache(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.executescript("CREATE TABLE http (url TEXT PRIMARY KEY, status INTEGER, "
                     "body BLOB, fetched_at TEXT); CREATE TABLE failures (url TEXT, "
                     "reason TEXT, failed_at TEXT);")
    body = json.dumps({"data": {"date": "1997-11-22", "venue": {"name": "Hampton Coliseum",
                       "location": "Hampton, VA"}, "tracks": [
                           {"position": 1, "title": "Mike's Song", "duration": 600000}]}})
    db.execute("INSERT INTO http VALUES (?, 200, ?, '2026-09-14T00:00:00')",
               ("https://phish.in/api/v2/shows/1997-11-22", body.encode()))
    db.commit()
    db.close()


def test_the_show_database_is_rebuilt_only_when_the_cache_is_newer(tmp_path):
    cache = tmp_path / "cache" / "archive.sqlite"
    _cache(cache)
    shows = distill.shows_path_for(cache)
    assert shows.name == "shows.sqlite" and distill.is_stale(cache, shows)
    distill.distill(cache, shows)
    assert not distill.is_stale(cache, shows)
    import os
    os.utime(cache, (os.path.getatime(cache), os.path.getmtime(shows) + 5))
    assert distill.is_stale(cache, shows)


def test_complete_without_a_cache_says_to_seed_first(tmp_path, home, capsys):
    lib = tmp_path / "lib"
    lib.mkdir()
    out = tmp_path / "r"
    out.mkdir()
    (out / "phase0_audio_identity.csv").write_text("x\n")
    (out / "phase0_folders.csv").write_text("x\n")
    assert _run("complete", lib, out) == 2
    assert "lookup --seed" in capsys.readouterr().err


def test_check_with_no_durations_says_to_run_plan(tmp_path, home, capsys):
    lib = tmp_path / "lib"
    lib.mkdir()
    out = tmp_path / "r"
    out.mkdir()
    assert _run("check", lib, out) == 2
    assert "jamp plan" in capsys.readouterr().err


def test_check_reads_the_newer_of_plans_and_scans_reports(tmp_path, home, monkeypatch, capsys):
    import os
    import time

    from jamp import complete as _complete

    lib = tmp_path / "lib"
    lib.mkdir()
    out = tmp_path / "r"
    out.mkdir()
    for prefix, age in (("phase0", 3600), ("phase1", 0)):
        for name in ("audio_identity", "folders"):
            path = out / ("%s_%s.csv" % (prefix, name))
            path.write_text("x\n")
            os.utime(path, (time.time() - age, time.time() - age))
    _cache(home / "cache" / "archive.sqlite")
    seen = {}
    monkeypatch.setattr(_complete, "load_shows",
                        lambda identity, folders: seen.update(i=identity, f=folders) or [])
    assert _run("check", lib, out) == 0
    assert seen["i"].name == "phase1_audio_identity.csv"
    assert seen["f"].name == "phase1_folders.csv"


def test_complete_builds_the_show_database_from_the_cache(tmp_path, home, capsys, monkeypatch):
    from jamp import complete as _complete

    lib = tmp_path / "lib"
    lib.mkdir()
    out = tmp_path / "r"
    out.mkdir()
    (out / "phase0_audio_identity.csv").write_text("x\n")
    (out / "phase0_folders.csv").write_text("x\n")
    cache = home / "cache" / "archive.sqlite"
    _cache(cache)
    monkeypatch.setattr(_complete, "load_shows", lambda identity, folders: [])
    assert _run("complete", lib, out) == 0
    assert (home / "cache" / "shows.sqlite").exists()
    assert "building the show database" in capsys.readouterr().out
