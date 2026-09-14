"""The user folder, and the user's config laid over the shipped one."""
from pathlib import Path

import pytest

from jamp import userdir
from jamp.config import DEFAULT_CONFIG_PATH, load_config, merge_layers
from jamp.venues import Gazetteer


def test_jamp_home_names_the_user_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("JAMP_HOME", str(tmp_path))
    assert userdir.user_dir() == tmp_path
    assert userdir.user_overrides_path() == tmp_path / "overrides.yaml"
    assert userdir.user_paths_path() == tmp_path / "paths.json"


def test_platform_default_without_jamp_home(monkeypatch):
    monkeypatch.delenv("JAMP_HOME", raising=False)
    assert userdir.user_dir().name in ("jamp", "etree")


def test_legacy_file_is_read_only_when_the_user_has_none(tmp_path):
    mine, legacy = tmp_path / "mine.yaml", tmp_path / "legacy.yaml"
    assert userdir.resolve(mine, legacy) == (mine, False)
    legacy.write_text("x")
    assert userdir.resolve(mine, legacy) == (legacy, True)
    mine.write_text("y")
    assert userdir.resolve(mine, legacy) == (mine, False)


def test_settings_replace_one_at_a_time_and_lists_add():
    shipped = {"settings": {"genre": "Live", "folder_location_max": 60,
                            "ignore_folders": []},
               "microphones": ["akg 460", "schoeps"]}
    user = {"settings": {"folder_location_max": 80,
                         "ignore_folders": ["Studio"]},
            "microphones": ["schoeps", "neumann km184"]}
    out = merge_layers(shipped, user)
    assert out["settings"] == {"genre": "Live", "folder_location_max": 80,
                               "ignore_folders": ["Studio"]}
    assert out["microphones"] == ["akg 460", "schoeps", "neumann km184"]


def test_a_band_is_replaced_whole_in_place_and_new_ones_are_added():
    shipped = {"bands": [
        {"abbrev": "gd", "name": "Grateful Dead", "aliases": ["GD", "Dead"]},
        {"abbrev": "ph", "name": "Phish", "aliases": ["Phish"]}]}
    user = {"bands": [
        {"abbrev": "GD", "name": "The Grateful Dead", "aliases": ["The Dead"]},
        {"abbrev": "bh", "name": "Bruce Hornsby"}]}
    out = merge_layers(shipped, user)
    assert [b["abbrev"] for b in out["bands"]] == ["GD", "ph", "bh"]
    # Whole, not merged: the shipped alias the user left out is gone.
    assert out["bands"][0]["aliases"] == ["The Dead"]


def test_an_act_moved_to_the_other_list_is_not_duplicated():
    shipped = {"bands": [{"abbrev": "tab", "name": "Trey Anastasio Band"}],
               "side_projects": []}
    user = {"side_projects": [{"abbrev": "tab", "name": "Trey Anastasio Band",
                               "parent": "ph"}]}
    out = merge_layers(shipped, user)
    assert out["bands"] == []
    assert [b["abbrev"] for b in out["side_projects"]] == ["tab"]


def test_series_are_keyed_by_name():
    shipped = {"official_series": [{"name": "Dick's Picks", "patterns": ["dp"]}]}
    user = {"official_series": [{"name": "Dick's Picks", "patterns": ["dicks"]},
                                {"name": "Road Trips", "patterns": ["rt"]}]}
    out = merge_layers(shipped, user)
    assert out["official_series"] == user["official_series"]


def test_merge_leaves_both_inputs_alone():
    shipped = {"settings": {"ignore_folders": ["a"]}, "bands": [{"abbrev": "x", "name": "X"}]}
    user = {"settings": {"ignore_folders": ["b"]}, "bands": [{"abbrev": "y", "name": "Y"}]}
    merge_layers(shipped, user)
    assert shipped == {"settings": {"ignore_folders": ["a"]}, "bands": [{"abbrev": "x", "name": "X"}]}


def test_no_user_file_is_the_shipped_config(tmp_path):
    shipped = load_config(user_path=None)
    missing = load_config(user_path=tmp_path / "jamp.yaml")
    assert missing.user_path is None
    assert [b.abbrev for b in missing.bands] == [b.abbrev for b in shipped.bands]
    assert missing.settings == shipped.settings


def test_user_file_is_laid_over_the_shipped_config(tmp_path):
    (tmp_path / "jamp.yaml").write_text(
        "settings:\n  ignore_folders: [Studio]\n"
        "bands:\n  - abbrev: bh\n    name: Bruce Hornsby\n    prefixes: [bh]\n",
        encoding="utf-8")
    cfg = load_config(user_path=tmp_path / "jamp.yaml")
    assert cfg.user_path == tmp_path / "jamp.yaml"
    assert cfg.settings.ignore_folders == ("Studio",)
    assert cfg.band("bh").name == "Bruce Hornsby"
    assert cfg.band("ph") is not None


def test_user_venues_join_the_shipped_ones(tmp_path):
    (tmp_path / "jamp.yaml").write_text("{}", encoding="utf-8")
    (tmp_path / "venues.yaml").write_text(
        "venues:\n- name: The Smallest Room\n  city: Nowhere\n  state: KS\n",
        encoding="utf-8")
    shipped = load_config(user_path=None)
    cfg = load_config(user_path=tmp_path / "jamp.yaml")
    assert len(cfg.venues) == len(shipped.venues) + 1
    assert cfg.venues.find("The Smallest Room", "Nowhere") is not None


def test_load_many_with_one_file_present_is_that_file(tmp_path):
    shipped = DEFAULT_CONFIG_PATH.parent / "venues.yaml"
    one = Gazetteer.load(shipped)
    many = Gazetteer.load_many([shipped, tmp_path / "absent.yaml"])
    assert len(many) == len(one) and many.path == shipped


def test_a_user_file_that_is_not_a_mapping_is_refused(tmp_path):
    from jamp.config import ConfigError

    (tmp_path / "jamp.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(user_path=tmp_path / "jamp.yaml")


def test_shipped_examples_load(tmp_path):
    examples = DEFAULT_CONFIG_PATH.parent / "examples"
    cfg = load_config(user_path=examples / "jamp.yaml")
    assert cfg.band("bs").name == "Billy Strings"
    from jamp.overrides import Overrides

    assert len(Overrides.load(examples / "overrides.yaml")) == 2


def test_the_shipped_config_names_no_ones_folders():
    assert load_config(user_path=None).settings.ignore_folders == ()
    assert not (Path(DEFAULT_CONFIG_PATH).parent / "overrides.yaml").exists()
    assert not userdir.LEGACY_OVERRIDES.parent.joinpath("overrides.yaml").is_relative_to(
        userdir.SHIPPED_DIR)


def test_remembered_paths_are_saved_to_the_user_folder(tmp_path, monkeypatch):
    from jamp import cli

    home = tmp_path / "home"
    monkeypatch.setenv("JAMP_HOME", str(home))
    monkeypatch.setattr(userdir, "LEGACY_PATHS", tmp_path / "legacy.json")
    saved = cli.save_settings({"root": tmp_path / "library", "out_dir": None})
    assert saved == home / "paths.json"
    assert cli.load_settings() == {"root": str(tmp_path / "library")}


def test_legacy_remembered_paths_are_carried_across(tmp_path, monkeypatch):
    from jamp import cli

    legacy = tmp_path / "legacy.json"
    legacy.write_text('{"root": "E:/old"}', encoding="utf-8")
    monkeypatch.setenv("JAMP_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(userdir, "LEGACY_PATHS", legacy)
    assert cli.load_settings() == {"root": "E:/old"}
    cli.save_settings({"out_dir": tmp_path / "reports"})
    assert cli.settings_path() == tmp_path / "home" / "paths.json"
    assert cli.load_settings() == {"root": "E:/old",
                                   "out_dir": str(tmp_path / "reports")}


def test_the_old_etree_folder_is_used_when_there_is_no_jamp_one(tmp_path, monkeypatch):
    monkeypatch.delenv("JAMP_HOME", raising=False)
    monkeypatch.delenv("ETREE_HOME", raising=False)
    monkeypatch.setattr(userdir, "_config_base", lambda: tmp_path)
    assert userdir.user_dir() == tmp_path / "jamp"
    (tmp_path / "etree").mkdir()
    assert userdir.user_dir() == tmp_path / "etree"
    (tmp_path / "jamp").mkdir()
    assert userdir.user_dir() == tmp_path / "jamp"


def test_etree_home_still_names_the_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("JAMP_HOME", raising=False)
    monkeypatch.setenv("ETREE_HOME", str(tmp_path))
    assert userdir.user_dir() == tmp_path


def test_an_old_etree_yaml_is_read_and_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("JAMP_HOME", str(tmp_path))
    (tmp_path / "etree.yaml").write_text("settings:\n  ignore_folders: [Studio]\n",
                                         encoding="utf-8")
    assert userdir.user_config_path() == tmp_path / "etree.yaml"
    assert load_config().settings.ignore_folders == ("Studio",)
