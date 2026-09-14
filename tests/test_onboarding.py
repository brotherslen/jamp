"""Guarding a library the tool has never seen: unknown acts, and commits
that no dry run preceded."""
import json

import pytest

import fixtures
from jamp import acts, cli, phase1, userdir
from jamp.config import load_config


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("JAMP_HOME", str(h))
    monkeypatch.setattr(userdir, "LEGACY_OVERRIDES", tmp_path / "none.yaml")
    monkeypatch.setattr(userdir, "LEGACY_PATHS", tmp_path / "none.json")
    return h


def _show(root, act_dir, folder, artist):
    d = root / act_dir / folder
    for n in (1, 2):
        fixtures.make_flac(d / ("d1t0%d.flac" % n),
                           tags={"ARTIST": artist, "TITLE": "Song %d" % n})
    return d


def _plans(root, cfg):
    return {p.show.path.name: p for p in phase1.build_plans(root, cfg)}


def test_a_show_whose_tags_name_an_unknown_act_is_blocked(tmp_path):
    root = tmp_path / "lib"
    _show(root, "The Allman Brothers Band", "dickey.betts.2002.07.17.sbd.flac16",
          "Dickey Betts & Great Southern")
    plan = _plans(root, load_config(user_path=None))["dickey.betts.2002.07.17.sbd.flac16"]
    codes = {i.code for i in plan.analysis.issues}
    assert "ACT_NOT_CONFIGURED" in codes
    assert plan.analysis.blocked


def test_a_generic_artist_tag_does_not_block_a_parent_filing(tmp_path):
    root = tmp_path / "lib"
    _show(root, "The Allman Brothers Band", "1999-04-09.sbd.flac16", "Unknown Artist")
    plan = _plans(root, load_config(user_path=None))["1999-04-09.sbd.flac16"]
    codes = {i.code for i in plan.analysis.issues}
    assert "ACT_NOT_CONFIGURED" not in codes
    assert "BAND_FROM_PARENT" in codes


def test_adding_the_act_unblocks_it(tmp_path, home):
    root = tmp_path / "lib"
    _show(root, "The Allman Brothers Band", "dickey.betts.2002.07.17.sbd.flac16",
          "Dickey Betts & Great Southern")
    data = acts.add_act({}, "Dickey Betts", "Dickey Betts & Great Southern", "dbgs")
    data["bands"][0]["aliases"].append("Dickey Betts")
    acts.save_acts(home / "acts.yaml", data)
    plan = _plans(root, load_config())["dickey.betts.2002.07.17.sbd.flac16"]
    assert plan.analysis.band.abbrev == "dbgs"
    assert "ACT_NOT_CONFIGURED" not in {i.code for i in plan.analysis.issues}


def test_acts_yaml_sits_under_jamp_yaml(home):
    home.mkdir(parents=True)
    acts.save_acts(home / "acts.yaml",
                   acts.add_act({}, "Billy Strings", "Billy Strings", "bs"))
    (home / "jamp.yaml").write_text(
        "bands:\n  - abbrev: bs\n    name: Billy Strings (hand)\n    prefixes: [bs]\n",
        encoding="utf-8")
    cfg = load_config()
    assert cfg.acts_path == home / "acts.yaml"
    assert cfg.band("bs").name == "Billy Strings (hand)"


def test_survey_places_folders_by_name_and_by_what_is_inside(tmp_path):
    root = tmp_path / "lib"
    (root / "Phish" / "ph1997-11-22").mkdir(parents=True)
    for d in ("kglw2022-10-10", "kglw2023-06-01"):
        (root / "King Gizz" / d).mkdir(parents=True)
    (root / "Billy Strings" / "2021-10-31 Hampton").mkdir(parents=True)
    rows = {r.folder: r for r in acts.survey(root, load_config(user_path=None))}
    assert rows["Phish"].status == acts.KNOWN and rows["Phish"].abbrev == "ph"
    assert rows["King Gizz"].status == acts.INSIDE
    assert rows["Billy Strings"].status == acts.UNKNOWN


def test_abbreviations_that_would_misfile_are_refused():
    cfg = load_config(user_path=None)
    assert acts.abbrev_problem("ph", cfg)          # Phish's
    assert acts.abbrev_problem("sbd", cfg)         # a source token
    assert acts.abbrev_problem("Billy", cfg)       # not lowercase
    assert acts.abbrev_problem("bstr", cfg) is None


def test_scripted_acts_write_acts_yaml_and_nothing_in_the_library(tmp_path, home):
    root = tmp_path / "lib"
    (root / "Billy Strings" / "2021-10-31").mkdir(parents=True)
    (root / "Studio").mkdir()
    before = sorted(p.relative_to(root) for p in root.rglob("*"))
    assert cli.main(["acts", str(root), "--add", "Billy Strings", "--abbrev", "bstr",
                     "--ignore", "Studio"]) == 0
    written = acts.load_acts(home / "acts.yaml")
    assert written["bands"][0]["abbrev"] == "bstr"
    assert written["settings"]["ignore_folders"] == ["Studio"]
    assert sorted(p.relative_to(root) for p in root.rglob("*")) == before
    rows = {r.folder: r for r in acts.survey(root, load_config())}
    assert rows["Billy Strings"].status == acts.KNOWN
    assert rows["Studio"].status == acts.IGNORED


def test_scripted_add_refuses_a_clashing_abbreviation(tmp_path, home):
    root = tmp_path / "lib"
    (root / "Phans").mkdir(parents=True)
    assert cli.main(["acts", str(root), "--add", "Phans", "--abbrev", "ph"]) == 2
    assert not (home / "acts.yaml").exists()


# -- the commit guard ------------------------------------------------------

@pytest.fixture
def small_library(tmp_path):
    root = tmp_path / "lib"
    _show(root, "Phish", "ph1997-11-22.sbd.flac16", "Phish")
    return root


def test_commit_without_a_dry_run_is_refused(small_library, tmp_path, home, capsys):
    out = tmp_path / "reports"
    code = cli.main(["phase2", str(small_library), "--out-dir", str(out), "--commit"])
    assert code == 2
    assert "jamp phase1" in capsys.readouterr().err
    assert not (small_library / "Phish" / "ph1997-11-22.sbd.flac16" /
                ".etree_state.json").exists()


def test_commit_for_another_scope_is_refused(small_library, tmp_path, home, capsys):
    out = tmp_path / "reports"
    assert cli.main(["phase1", str(small_library), "--out-dir", str(out)]) == 0
    code = cli.main(["phase2", str(small_library), "--out-dir", str(out),
                     "--artist", "Phish", "--commit"])
    assert code == 2
    assert "whole library" in capsys.readouterr().err


def test_commit_after_the_matching_dry_run_goes_ahead(small_library, tmp_path, home):
    out = tmp_path / "reports"
    assert cli.main(["phase1", str(small_library), "--out-dir", str(out),
                     "--artist", "Phish"]) == 0
    plan = json.loads((out / "phase1_plan.json").read_text(encoding="utf-8"))
    assert plan["scope"] == ["Phish"] and plan["root"] == str(small_library.resolve())
    assert cli.main(["phase2", str(small_library), "--out-dir", str(out),
                     "--artist", "Phish", "--commit"]) == 0


def test_skip_plan_check_is_the_way_round_it(small_library, tmp_path, home):
    out = tmp_path / "reports"
    assert cli.main(["phase2", str(small_library), "--out-dir", str(out),
                     "--commit", "--skip-plan-check"]) == 0


def test_an_empty_scope_says_so(tmp_path, home, capsys):
    root = tmp_path / "lib"
    (root / "Phish").mkdir(parents=True)
    assert cli.main(["phase1", str(root), "--out-dir", str(tmp_path / "r")]) == 0
    assert "no show folders found" in capsys.readouterr().out
