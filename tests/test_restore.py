"""`jamp restore`: a committed folder put back exactly as it was."""
import json
from pathlib import Path

import pytest

import fixtures
from jamp import cli, restore, userdir
from jamp.state import STATE_NAME, read_state
from jamp.tagwriter import BACKUP_NAME

ORIGINAL = "Phish 11-22-97 Hampton Coliseum SBD"
TITLES = ["Emotional Rescue", "AC/DC Bag", "Mike's Song"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("JAMP_HOME", str(h))
    monkeypatch.setattr(userdir, "LEGACY_OVERRIDES", tmp_path / "none.yaml")
    monkeypatch.setattr(userdir, "LEGACY_PATHS", tmp_path / "none.json")
    return h


def _snapshot(folder: Path) -> dict:
    """Every file by relative path: an audio file's tags, anything else's bytes.

    Tags and not bytes, because writing the same tags back can change a FLAC's
    padding without changing a single tag.
    """
    from mutagen.flac import FLAC

    out = {}
    for p in sorted(folder.rglob("*")):
        if not p.is_file() or p.name in (BACKUP_NAME, STATE_NAME):
            continue
        rel = p.relative_to(folder).as_posix()
        if p.suffix == ".flac":
            out[rel] = sorted((k.upper(), tuple(v)) for k, v in FLAC(p).tags.items())
        else:
            out[rel] = p.read_bytes()
    return out


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "lib"
    show = root / "Phish" / ORIGINAL
    for n, title in enumerate(TITLES, 1):
        fixtures.make_flac(show / ("%02d %s.flac" % (n, title.replace("/", "_"))),
                           tags={"ARTIST": "Phish", "TITLE": title})
    fixtures.write(show / "ph97-11-22.md5", "".join(
        "%032x *%02d %s.flac\n" % (n, n, t.replace("/", "_"))
        for n, t in enumerate(TITLES, 1)))
    return root


def _commit(root, out):
    assert cli.main(["phase1", str(root), "--out-dir", str(out)]) == 0
    assert cli.main(["phase2", str(root), "--out-dir", str(out), "--commit"]) == 0
    folders = [p for p in (root / "Phish").iterdir() if p.is_dir()]
    assert len(folders) == 1 and folders[0].name != ORIGINAL
    return folders[0]


def _restore(root, out, *folders, extra=()):
    base = ["restore", *map(str, folders), "--root", str(root), "--out-dir", str(out)]
    return cli.main(base + list(extra))


def test_a_commit_is_put_back_exactly(library, tmp_path, home):
    before = _snapshot(library / "Phish" / ORIGINAL)
    committed = _commit(library, tmp_path / "r")
    assert _snapshot(committed) != before

    assert _restore(library, tmp_path / "rr", committed) == 0          # dry run
    assert committed.exists()                                          # nothing moved
    assert _restore(library, tmp_path / "rr", committed, extra=["--commit"]) == 0

    back = library / "Phish" / ORIGINAL
    assert back.is_dir() and not committed.exists()
    assert _snapshot(back) == before
    state = read_state(back)
    assert state["restored_from"] == committed.name
    assert "folder_name" not in state            # no longer settled


def test_a_restored_folder_is_planned_again(library, tmp_path, home):
    committed = _commit(library, tmp_path / "r")
    _restore(library, tmp_path / "rr", committed)
    _restore(library, tmp_path / "rr", committed, extra=["--commit"])
    assert cli.main(["phase1", str(library), "--out-dir", str(tmp_path / "again")]) == 0
    plan = json.loads((tmp_path / "again" / "phase1_plan.json").read_text(encoding="utf-8"))
    assert [s["status"] for s in plan["shows"]] == ["PLAN"]


def test_commit_needs_its_dry_run(library, tmp_path, home, capsys):
    committed = _commit(library, tmp_path / "r")
    assert _restore(library, tmp_path / "rr", committed, extra=["--commit"]) == 2
    assert "dry run" in capsys.readouterr().err
    assert committed.exists()


def test_a_folder_never_committed_is_refused(library, tmp_path, home):
    folder = library / "Phish" / ORIGINAL
    fr = restore.plan_folder(folder, library)
    assert fr.problems and "nothing here says" in fr.problems[0]


def test_a_file_merged_in_is_refused_unless_partial(library, tmp_path, home):
    committed = _commit(library, tmp_path / "r")
    stranger = committed / "ph1997-11-22d2t01.flac"
    fixtures.make_flac(stranger, tags={"TITLE": "Tweezer"})
    fr = restore.plan_folder(committed, library)
    assert fr.loose and fr.blocked(partial=False) and not fr.blocked(partial=True)

    _restore(library, tmp_path / "rr", committed)
    assert _restore(library, tmp_path / "rr", committed, extra=["--commit"]) == 0
    assert committed.exists()                    # refused, so nothing changed
    _restore(library, tmp_path / "rp", committed, extra=["--partial"])
    assert _restore(library, tmp_path / "rp", committed,
                    extra=["--commit", "--partial"]) == 0
    back = library / "Phish" / ORIGINAL
    assert (back / "ph1997-11-22d2t01.flac").exists()          # left as it is
    assert (back / "01 Emotional Rescue.flac").exists()


def test_a_taken_name_is_refused(library, tmp_path, home):
    committed = _commit(library, tmp_path / "r")
    (library / "Phish" / ORIGINAL).mkdir()
    fr = restore.plan_folder(committed, library)
    assert any("name is taken" in p for p in fr.problems)


def test_a_failure_puts_the_folder_back_as_committed(library, tmp_path, home,
                                                     monkeypatch):
    committed = _commit(library, tmp_path / "r")
    as_committed = _snapshot(committed)
    real = restore._safe_rename

    def fail_on_folder(src, dst, cfg):
        if src == committed:
            raise PermissionError("locked")
        real(src, dst, cfg)

    monkeypatch.setattr(restore, "_safe_rename", fail_on_folder)
    fr = restore.plan_folder(committed, library)
    from jamp.config import load_config

    restore.execute(fr, load_config())
    assert fr.status == "failed"
    assert committed.exists() and _snapshot(committed) == as_committed


def test_the_log_names_files_the_backup_cannot(library, tmp_path, home):
    committed = _commit(library, tmp_path / "r")
    backup = json.loads((committed / BACKUP_NAME).read_text(encoding="utf-8"))
    # A backup written before renames were recorded in it.
    for entry in backup["files"].values():
        entry.pop("__renamed_to__", None)
    (committed / BACKUP_NAME).write_text(json.dumps(backup), encoding="utf-8")

    without = restore.plan_folder(committed, library)
    assert without.blocked(partial=False)
    logs = restore.read_logs([tmp_path / "r" / "phase2_committed.csv"])
    with_log = restore.plan_folder(committed, library, logs)
    assert not with_log.blocked(partial=False)
    names = sorted(s.new.name for s in with_log.steps if s.kind == "rename_file")
    assert names == ["01 Emotional Rescue.flac", "02 AC_DC Bag.flac", "03 Mike's Song.flac"]


def test_a_folder_outside_the_library_is_refused(library, tmp_path, home, capsys):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert _restore(library, tmp_path / "rr", elsewhere) == 2
    assert "not inside the library" in capsys.readouterr().err


def test_an_old_state_file_falls_back_to_the_backups_folder(library, tmp_path, home):
    committed = _commit(library, tmp_path / "r")
    state = read_state(committed)
    state.pop("original_folder_name")
    (committed / STATE_NAME).write_text(json.dumps(state), encoding="utf-8")
    fr = restore.plan_folder(committed, library)
    assert fr.original_name == ORIGINAL


def test_a_converted_folder_says_its_original_name_is_stale(library, tmp_path, home):
    committed = _commit(library, tmp_path / "r")
    state = read_state(committed)
    state["original_folder_name"] = "ph1997-11-22.sbd.shn"
    (committed / STATE_NAME).write_text(json.dumps(state), encoding="utf-8")
    fr = restore.plan_folder(committed, library)
    assert any("converted after it was named" in n for n in fr.notes)
