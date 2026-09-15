"""A commit carries out the plan that was read, and reads again only what changed."""
import json
import shutil
from pathlib import Path

import pytest

import fixtures
from jamp import cli, phase1, phase2, reads, userdir
from jamp.config import load_config


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("JAMP_HOME", str(h))
    monkeypatch.setattr(userdir, "LEGACY_OVERRIDES", tmp_path / "none.yaml")
    monkeypatch.setattr(userdir, "LEGACY_PATHS", tmp_path / "none.json")
    return h


def _show(root, folder, date):
    d = root / "Phish" / folder
    for n in (1, 2):
        fixtures.make_flac(d / ("%02d Song %d.flac" % (n, n)),
                           tags={"ARTIST": "Phish", "TITLE": "Song %d" % n,
                                 "DATE": date, "TRACKNUMBER": str(n)})
    return d


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "lib"
    _show(root, "Phish 11-22-97 sbd", "1997-11-22")
    _show(root, "Phish 11-23-97 sbd", "1997-11-23")
    for f in root.rglob("*.flac"):
        _age(f)                       # as a library's files are: not just written
    return root


def _settled(root):
    return sorted(p.parent.name for p in root.rglob(".etree_state.json"))


# -- the reads ---------------------------------------------------------------

def _age(path, seconds=3600):
    import os
    import time

    past = time.time() - seconds
    os.utime(path, (past, past))


def test_a_read_is_reused_until_the_file_changes(tmp_path):
    f = fixtures.make_flac(tmp_path / "a.flac", tags={"TITLE": "One"})
    _age(f)
    cache = reads.ReadCache()
    assert cache.read(f).tag("TITLE") == "One"
    assert (cache.hits, cache.misses) == (0, 1)
    cache.read(f).name_info.disc = 9                 # a caller's change stays its own
    again = cache.read(f)
    assert cache.hits == 2 and again.name_info.disc is None

    from mutagen.flac import FLAC
    audio = FLAC(str(f))
    audio["TITLE"] = "A much longer title than before"
    audio.save()
    assert cache.read(f).tag("TITLE") == "A much longer title than before"
    assert cache.misses == 2


def test_a_file_modified_moments_ago_is_read_every_time(tmp_path):
    f = fixtures.make_flac(tmp_path / "a.flac", tags={"TITLE": "One"})
    cache = reads.ReadCache()
    cache.read(f)
    cache.read(f)
    assert (cache.hits, cache.misses, len(cache)) == (0, 2, 0)


def test_saved_reads_come_back_the_same(tmp_path):
    f = fixtures.make_flac(tmp_path / "lib" / "d1t01.flac", tags={"TITLE": "Tweezer"})
    _age(f)
    cache = reads.ReadCache()
    first = cache.read(f)
    cache.save(tmp_path)
    loaded, why = reads.ReadCache.load(tmp_path)
    assert why is None and len(loaded) == 1
    back = loaded.read(f)
    assert loaded.hits == 1
    assert back == first


def test_reads_from_another_version_are_not_used(tmp_path, monkeypatch):
    f = fixtures.make_flac(tmp_path / "a.flac")
    _age(f)
    cache = reads.ReadCache()
    cache.read(f)
    cache.save(tmp_path)
    monkeypatch.setattr(reads, "__version__", "9.9")
    loaded, why = reads.ReadCache.load(tmp_path)
    assert len(loaded) == 0 and "jamp" in why


def test_a_picture_in_a_vorbis_comment_is_not_kept_as_text(tmp_path):
    import base64

    from mutagen.flac import FLAC
    from jamp.audio import read_audio_file

    f = fixtures.make_flac(tmp_path / "a.flac", tags={"TITLE": "One"})
    audio = FLAC(str(f))
    audio["METADATA_BLOCK_PICTURE"] = base64.b64encode(b"\xff\xd8" * 5000).decode()
    audio.save()
    tags = read_audio_file(f).tags
    assert tags["TITLE"] == ["One"]
    assert tags["METADATA_BLOCK_PICTURE"] == ["<picture, 13336 characters of base64>"]


# -- the commit ------------------------------------------------------------------

def test_a_commit_reuses_the_dry_runs_reads(library, tmp_path, home, capsys):
    out = tmp_path / "r"
    assert cli.main(["phase1", str(library), "--out-dir", str(out)]) == 0
    assert (out / reads.READS_NAME).exists()
    assert cli.main(["phase2", str(library), "--out-dir", str(out), "--commit"]) == 0
    printed = capsys.readouterr().out
    assert "reused" in printed
    assert len(_settled(library)) == 2


def test_the_summary_counts_what_the_commit_set_out_to_do(library, tmp_path, home):
    out = tmp_path / "r"
    assert cli.main(["phase1", str(library), "--out-dir", str(out)]) == 0
    assert cli.main(["phase2", str(library), "--out-dir", str(out), "--commit"]) == 0
    summary = (out / "phase2_summary.txt").read_text(encoding="utf-8")
    line = next(l for l in summary.splitlines() if "marked PLAN" in l)
    assert line.split()[-1] == "2"


def test_a_folder_changed_after_the_dry_run_is_not_committed(library, tmp_path, home, capsys):
    out = tmp_path / "r"
    assert cli.main(["phase1", str(library), "--out-dir", str(out)]) == 0
    fixtures.write(library / "Phish" / "Phish 11-23-97 sbd" / "info.txt",
                   "Phish\n1997-11-23\nMcNichols Arena, Denver, CO\n")
    assert cli.main(["phase2", str(library), "--out-dir", str(out), "--commit"]) == 0
    printed = capsys.readouterr().out
    assert "NOT committed" in printed
    assert (library / "Phish" / "Phish 11-23-97 sbd").is_dir()      # untouched
    assert len(_settled(library)) == 1
    summary = (out / "phase2_summary.txt").read_text(encoding="utf-8")
    assert "changed on disk after the dry run" in summary


def test_a_plan_that_no_longer_matches_is_not_committed(library, tmp_path, home):
    out = tmp_path / "r"
    assert cli.main(["phase1", str(library), "--out-dir", str(out)]) == 0
    plan_file = out / "phase1_plan.json"
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    shown = next(s for s in plan["shows"] if "11-22" in s["current_folder"])
    shown["proposed_folder"] = "something nobody would have agreed to"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    assert cli.main(["phase2", str(library), "--out-dir", str(out), "--commit"]) == 0
    assert (library / "Phish" / "Phish 11-22-97 sbd").is_dir()
    assert _settled(library) and len(_settled(library)) == 1
    summary = (out / "phase2_summary.txt").read_text(encoding="utf-8")
    assert "proposed_folder was 'something nobody would have agreed to'" in summary


def test_a_folder_the_dry_run_did_not_see_is_not_committed(library, tmp_path, home):
    out = tmp_path / "r"
    assert cli.main(["phase1", str(library), "--out-dir", str(out)]) == 0
    _show(library, "Phish 11-29-97 sbd", "1997-11-29")
    assert cli.main(["phase2", str(library), "--out-dir", str(out), "--commit"]) == 0
    assert (library / "Phish" / "Phish 11-29-97 sbd").is_dir()
    assert "not in the dry run" in (out / "phase2_summary.txt").read_text(encoding="utf-8")


def test_planned_work_that_is_gone_is_said_to_be_gone(library, tmp_path, home, capsys):
    out = tmp_path / "r"
    assert cli.main(["phase1", str(library), "--out-dir", str(out)]) == 0
    shutil.rmtree(library / "Phish" / "Phish 11-23-97 sbd")
    assert cli.main(["phase2", str(library), "--out-dir", str(out), "--commit"]) == 0
    assert "no longer work" in capsys.readouterr().out
    summary = (out / "phase2_summary.txt").read_text(encoding="utf-8")
    assert "Phish 11-23-97 sbd" in summary.split("no longer work")[1]


def test_a_plan_without_fingerprints_is_refused(library, tmp_path, home, capsys):
    out = tmp_path / "r"
    assert cli.main(["phase1", str(library), "--out-dir", str(out)]) == 0
    plan = json.loads((out / "phase1_plan.json").read_text(encoding="utf-8"))
    del plan["plan_format"]
    (out / "phase1_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    assert cli.main(["phase2", str(library), "--out-dir", str(out), "--commit"]) == 2
    assert "predates" in capsys.readouterr().err
    assert not _settled(library)


# -- settings that shaped the plan ----------------------------------------------

def test_a_commit_with_other_settings_than_its_dry_run_is_refused(library, tmp_path, home,
                                                                  capsys):
    home.mkdir(parents=True)
    (home / "jamp.yaml").write_text("settings:\n  ignore_folders: [Trash]\n", encoding="utf-8")
    out = tmp_path / "r"
    assert cli.main(["phase1", str(library), "--out-dir", str(out)]) == 0
    (home / "jamp.yaml").write_text("settings:\n  ignore_folders: [Trash, Jazz]\n",
                                    encoding="utf-8")
    assert cli.main(["phase2", str(library), "--out-dir", str(out), "--commit"]) == 2
    err = capsys.readouterr().err
    assert "different settings: your config" in err
    assert not _settled(library)


def test_the_same_settings_from_another_folder_still_match(library, tmp_path, home,
                                                          monkeypatch):
    """A standalone build unpacks its shipped config somewhere new on every run."""
    import shutil as _shutil

    from jamp import config as _config

    out = tmp_path / "r"
    assert cli.main(["plan", str(library), "--out-dir", str(out)]) == 0
    moved = tmp_path / "_MEI_second_run" / "data"
    _shutil.copytree(Path(_config.__file__).parent / "data", moved)
    real_load = _config.load_config
    monkeypatch.setattr(cli, "load_config",
                        lambda path=None, **kw: real_load(path or moved / "jamp.yaml", **kw))
    assert cli.main(["apply", str(library), "--out-dir", str(out), "--commit"]) == 0
    assert len(_settled(library)) == 2


def test_changed_overrides_refuse_the_commit_too(library, tmp_path, home, capsys):
    home.mkdir(parents=True)
    (home / "jamp.yaml").write_text("{}\n", encoding="utf-8")
    out = tmp_path / "r"
    assert cli.main(["phase1", str(library), "--out-dir", str(out)]) == 0
    (home / "overrides.yaml").write_text(
        '"Phish 11-22-97 sbd":\n  venue: The Summit\n', encoding="utf-8")
    assert cli.main(["phase2", str(library), "--out-dir", str(out), "--commit"]) == 2
    assert "your overrides" in capsys.readouterr().err


@pytest.mark.parametrize("contents", [(), ("overrides.yaml",), ("paths.json",)])
def test_a_settings_folder_with_no_config_to_be_seen_stops_the_run(
        library, tmp_path, home, capsys, contents):
    """What a window that cannot see the user folder's files looks like."""
    home.mkdir(parents=True)
    for name in contents:
        (home / name).write_text("{}\n", encoding="utf-8")
    assert cli.main(["phase1", str(library), "--out-dir", str(tmp_path / "r")]) == 2
    err = capsys.readouterr().err
    assert "cannot see them" in err and "jamp init" in err
    assert not (tmp_path / "r" / "phase1_plan.json").exists()


def test_a_folder_holding_only_the_cache_is_a_new_user(library, tmp_path, home):
    (home / "cache").mkdir(parents=True)
    assert cli.main(["phase1", str(library), "--out-dir", str(tmp_path / "r")]) == 0


def test_reused_reads_commit_exactly_what_fresh_reads_do(library, tmp_path, home):
    """The before/after: the same library committed with and without saved reads."""
    other = tmp_path / "copy"
    shutil.copytree(library, other, copy_function=shutil.copy2)
    cfg = load_config()

    def commit(root, out, keep_reads):
        phase1.run(root, out, cfg)
        if not keep_reads:
            (out / reads.READS_NAME).unlink()
        return phase2.run(root, out, cfg, commit=True, until_settled=True,
                          check_plan=True)

    with_reads = commit(library, tmp_path / "r1", True)
    without = commit(other, tmp_path / "r2", False)
    assert with_reads["reads_reused"] > 0 and without["reads_saved"] == 0
    assert with_reads["refused_by_plan"] == without["refused_by_plan"] == 0

    def tree(root):
        from jamp.audio import read_audio_file
        return sorted((str(p.relative_to(root)),
                       read_audio_file(p).tags if p.suffix == ".flac" else None)
                      for p in root.rglob("*") if p.is_file()
                      and p.name not in (".etree_state.json", ".etree_backup.json"))

    assert tree(library) == tree(other)
