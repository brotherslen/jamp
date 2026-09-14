"""`jamp init` and `jamp doctor`."""
import json

import pytest

from jamp import cli, userdir


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("JAMP_HOME", str(h))
    monkeypatch.setattr(userdir, "LEGACY_OVERRIDES", tmp_path / "no-legacy.yaml")
    monkeypatch.setattr(userdir, "LEGACY_PATHS", tmp_path / "no-legacy.json")
    return h


@pytest.fixture
def library(tmp_path):
    lib = tmp_path / "Live Music"
    (lib / "Phish").mkdir(parents=True)
    return lib


def test_init_creates_the_user_folder_and_remembers_paths(home, library, tmp_path):
    reports = tmp_path / "reports"
    code = cli.main(["init", "--root", str(library), "--out-dir", str(reports)])
    assert code == 0
    assert (home / "jamp.yaml").exists() and (home / "overrides.yaml").exists()
    saved = json.loads((home / "paths.json").read_text(encoding="utf-8"))
    assert saved["root"] == str(library.resolve())
    assert saved["out_dir"] == str(reports.resolve())
    assert reports.is_dir()
    # Nothing was written into the library.
    assert [p.name for p in library.iterdir()] == ["Phish"]


def test_the_templates_load_as_config_and_overrides(home, library, tmp_path):
    cli.main(["init", "--root", str(library), "--out-dir", str(tmp_path / "r")])
    from jamp.config import load_config
    from jamp.overrides import Overrides

    cfg = load_config()
    assert cfg.user_path == home / "jamp.yaml"
    assert cfg.settings.ignore_folders == ()
    assert len(Overrides.load(home / "overrides.yaml")) == 0


def test_init_again_keeps_your_answers(home, library, tmp_path):
    cli.main(["init", "--root", str(library), "--out-dir", str(tmp_path / "r")])
    (home / "overrides.yaml").write_text("folders:\n  x:\n    skip: true\n",
                                         encoding="utf-8")
    cli.main(["init"])      # both paths come from what was remembered
    assert "skip: true" in (home / "overrides.yaml").read_text(encoding="utf-8")
    saved = json.loads((home / "paths.json").read_text(encoding="utf-8"))
    assert saved["root"] == str(library.resolve())


def test_init_copies_overrides_left_in_an_old_checkout(home, library, tmp_path,
                                                       monkeypatch):
    legacy = tmp_path / "legacy-overrides.yaml"
    legacy.write_text("folders:\n  mine:\n    skip: true\n", encoding="utf-8")
    monkeypatch.setattr(userdir, "LEGACY_OVERRIDES", legacy)
    cli.main(["init", "--root", str(library), "--out-dir", str(tmp_path / "r")])
    assert (home / "overrides.yaml").read_text(encoding="utf-8") == legacy.read_text(
        encoding="utf-8")


def test_init_refuses_reports_inside_the_library(home, library):
    code = cli.main(["init", "--root", str(library),
                     "--out-dir", str(library / "reports")])
    assert code == 2
    assert not (library / "reports").exists()


def test_init_without_a_library_and_no_terminal_says_what_to_pass(home, capsys):
    assert cli.main(["init"]) == 2
    assert "--root" in capsys.readouterr().err


def test_doctor_is_ready_after_init(home, library, tmp_path, capsys):
    cli.main(["init", "--root", str(library), "--out-dir", str(tmp_path / "r")])
    capsys.readouterr()
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "1 top-level folders" in out and "Ready." in out


def test_doctor_fails_when_the_library_is_unreachable(home, tmp_path):
    home.mkdir(parents=True)
    (home / "paths.json").write_text(json.dumps(
        {"root": str(tmp_path / "unplugged"), "out_dir": str(tmp_path / "r")}),
        encoding="utf-8")
    assert cli.main(["doctor"]) == 1


def test_doctor_fails_on_a_broken_user_config(home, capsys):
    home.mkdir(parents=True)
    (home / "jamp.yaml").write_text("- not\n- a mapping\n", encoding="utf-8")
    assert cli.main(["doctor"]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_ffmpeg_from_jamp_ffmpeg(monkeypatch, tmp_path):
    from jamp.integrity import locate_ffmpeg

    monkeypatch.setenv("JAMP_FFMPEG", str(tmp_path / "ff"))
    assert locate_ffmpeg() == (str(tmp_path / "ff"), "JAMP_FFMPEG")
    assert locate_ffmpeg("given")[1] == "given on the command line"


def test_init_does_not_shadow_an_old_etree_yaml(home, library, tmp_path):
    home.mkdir(parents=True)
    (home / "etree.yaml").write_text("settings:\n  ignore_folders: [Studio]\n",
                                     encoding="utf-8")
    cli.main(["init", "--root", str(library), "--out-dir", str(tmp_path / "r")])
    assert not (home / "jamp.yaml").exists()
