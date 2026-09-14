"""Phase 2: the writer.  Every test here commits to a throwaway library."""
import datetime as dt
import json
import sys

import pytest

import fixtures
from jamp import phase1, phase2
from jamp.audio import INTERESTING_TAGS
from jamp.phase1 import MERGE, PLAN, build_plans
from jamp.tagwriter import BACKUP_NAME, restore_from_backup

TODAY = dt.date(2026, 9, 7)


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "lib"
    fixtures.build_library(root)
    return root


def plans_by_name(root, cfg):
    return {p.old_folder_name: p for p in build_plans(root, cfg, today=TODAY)}


# --- the default is still not writing ---------------------------------------

def test_without_commit_nothing_changes(library, cfg, tmp_path):
    before = {p: p.stat().st_mtime_ns for p in library.rglob("*")}
    stats = phase2.run(library, tmp_path / "out", cfg, commit=False, today=TODAY)
    assert stats["actions"] > 0
    assert {p: p.stat().st_mtime_ns for p in library.rglob("*")} == before


def test_only_plan_rows_are_touched(library, cfg, tmp_path):
    plans = plans_by_name(library, cfg)
    phase2.run(library, tmp_path / "out", cfg, commit=True, today=TODAY)
    # A blocked folder is still exactly where it was.
    assert (library / "Umphrey's McGee" / "UM - Hauntlanta").is_dir()
    # A duplicate pair is left alone too.
    assert plans["gd1973-12-10 s1"].status == MERGE
    assert (library / "grateful dead" / "gd1973-12-10 s1").is_dir()


def test_merges_are_held_back_unless_asked_for(library, cfg, tmp_path):
    phase2.run(library, tmp_path / "out", cfg, commit=True, today=TODAY)
    assert (library / "grateful dead" / "gd1973-12-10 s1").is_dir()
    assert (library / "grateful dead" / "gd1973-12-10 s2").is_dir()


# --- what a commit actually does --------------------------------------------

def test_a_folder_and_its_tracks_are_renamed(library, cfg, tmp_path):
    phase2.run(library, tmp_path / "out", cfg, commit=True, today=TODAY)
    mmj = library / "My Morning Jacket"
    renamed = mmj / "mmj2005-06-04.aud.ak40.flac16"
    assert renamed.is_dir()
    assert not (mmj / "mmj2005-06-04.ak40.flac16").exists()
    assert sorted(p.name for p in renamed.glob("*.flac")) == [
        "mmj2005-06-04d1t01.flac", "mmj2005-06-04d1t02.flac",
        "mmj2005-06-04d1t03.flac"]


def test_tags_are_written_and_backed_up_first(library, cfg, tmp_path):
    import mutagen

    phase2.run(library, tmp_path / "out", cfg, commit=True, today=TODAY)
    folder = (library / "My Morning Jacket" /
              "mmj2006-06-16.aud.miller.flac16 - Bonnaroo Music Festival, Manchester, TN")
    assert folder.is_dir()

    backup = folder / BACKUP_NAME
    assert backup.exists(), "the originals must be saved before anything is written"
    payload = json.loads(backup.read_text(encoding="utf-8"))
    assert len(payload["files"]) == 4

    track = folder / "mmj2006-06-16d1t01.flac"
    tags = mutagen.File(track).tags
    assert tags["ARTIST"] == ["My Morning Jacket"]
    assert tags["ALBUM"] == ["2006-06-16: Bonnaroo Music Festival, Manchester, TN"]
    assert tags["DATE"] == ["2006-06-16"]
    assert tags["TITLE"] == ["Wordless Chorus"]


def test_mp3_tags_are_written_as_id3v2_3(library, cfg, tmp_path):
    import mutagen

    phase2.run(library, tmp_path / "out", cfg, commit=True, today=TODAY)
    folder = library / "phish" / "ph2012-06-28.sbd.mp3 - Klipsch, Noblesville, IN"
    if not folder.is_dir():                     # venue text may differ; find it
        folder = next(p for p in (library / "phish").iterdir()
                      if p.name.startswith("ph2012-06-28"))
    track = next(folder.glob("*.mp3"))
    assert mutagen.File(track).tags.version == (2, 3, 0)


def test_shn_is_renamed_but_never_tagged(library, cfg, tmp_path):
    phase2.run(library, tmp_path / "out", cfg, commit=True, today=TODAY)
    folder = next(p for p in (library / "My Morning Jacket").iterdir()
                  if p.name.startswith("mmj2003-09-26"))
    assert sorted(p.name for p in folder.glob("*.shn")) == [
        "mmj2003-09-26d1t01.shn", "mmj2003-09-26d1t02.shn"]
    assert not (folder / BACKUP_NAME).exists(), "nothing taggable, so nothing to back up"


def test_checksum_files_follow_the_rename(library, cfg, tmp_path):
    phase2.run(library, tmp_path / "out", cfg, commit=True, today=TODAY)
    folder = next(p for p in (library / "My Morning Jacket").iterdir()
                  if p.name.startswith("mmj2006-06-16"))
    ffp = (folder / "fingerprint.ffp.txt").read_text(encoding="utf-8")
    assert "mmj2006-06-16d1t01.flac:" in ffp


# --- safety -----------------------------------------------------------------

def test_running_phase2_twice_is_a_no_op(library, cfg, tmp_path):
    phase2.run(library, tmp_path / "out1", cfg, commit=True, today=TODAY)
    after_first = {str(p.relative_to(library)) for p in library.rglob("*")}
    second = phase2.run(library, tmp_path / "out2", cfg, commit=True, today=TODAY)
    assert {str(p.relative_to(library)) for p in library.rglob("*")} == after_first
    assert second["folders_failed"] == 0


def test_a_rename_never_lands_on_an_existing_name(tmp_path):
    a = tmp_path / "a.flac"
    b = tmp_path / "b.flac"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    with pytest.raises(FileExistsError):
        phase2._safe_rename(a, b)
    assert a.read_bytes() == b"a" and b.read_bytes() == b"b"


def test_a_path_over_the_windows_limit_can_still_be_read(tmp_path):
    """A show nested a few levels deep with long names exceeds MAX_PATH, and
    Python then reports "No such file" for a file that is plainly there."""
    import mutagen

    from jamp.audio import read_audio_file
    from jamp.winpath import extended, opener

    deep = tmp_path
    for i in range(4):
        deep = deep / ("A very long folder name for a live recording %d" % i)
    path = deep / ("A rather long track title indeed number one.flac")
    fixtures.make_flac(path, bits=16, tags={"TITLE": "Deep Track"})
    assert len(str(path)) > 240
    if sys.platform == "win32":
        assert extended(path).startswith("\\\\?\\")
    else:
        assert extended(path) == str(path)        # only Windows has the limit

    assert mutagen.File(opener(path)) is not None
    af = read_audio_file(path)
    assert af.tag_support == "full"
    assert af.tag("TITLE") == "Deep Track"
    assert af.size > 0


def test_short_paths_are_left_alone(tmp_path):
    from jamp.winpath import extended
    assert not extended(tmp_path / "x.flac").startswith("\\\\?\\")


def test_a_case_only_rename_works_on_windows(tmp_path):
    src = tmp_path / "MMJ2006.flac"
    src.write_bytes(b"x")
    phase2._safe_rename(src, tmp_path / "mmj2006.flac")
    assert [p.name for p in tmp_path.iterdir()] == ["mmj2006.flac"]


def test_a_failing_folder_is_rolled_back(library, cfg, tmp_path, monkeypatch):
    """If any step fails, that folder is put back the way it was."""
    plans = plans_by_name(library, cfg)
    plan = plans["mmj2005-06-04.ak40.flac16"]
    work = phase2.plan_actions(plan, cfg, include_merges=False)
    assert len(work.actions) >= 3

    real_apply = phase2._apply
    calls = {"n": 0}

    def flaky(action, cfg_):
        calls["n"] += 1
        if calls["n"] == len(work.actions):       # fail on the folder rename
            raise OSError("simulated failure")
        return real_apply(action, cfg_)

    monkeypatch.setattr(phase2, "_apply", flaky)
    phase2.execute_folder(work, cfg)

    assert work.status == phase2.FAILED
    # Every track is back under its original name, in the original folder.
    original = library / "My Morning Jacket" / "mmj2005-06-04.ak40.flac16"
    assert original.is_dir()
    assert sorted(p.name for p in original.glob("*.flac")) == [
        "mmj2005-06-04d1t01.flac", "mmj2005-06-04d1t02.flac",
        "mmj2005-06-04d1t03.flac"]


def _sidecar_folder(tmp_path):
    """A folder whose checksum file is rewritten and renamed, then the folder
    rename fails - the WinError 5 case, which is the one that really happens."""
    folder = tmp_path / "lib" / "show"
    folder.mkdir(parents=True)
    (folder / "01 Bertha.flac").write_bytes(b"audio")
    original = "01 Bertha.flac:%s\r\n" % ("0" * 32)
    (folder / "show.ffp").write_bytes(original.encode("cp1252"))
    (tmp_path / "lib" / "taken").mkdir()
    work = phase2.FolderWork(plan=None, actions=[
        phase2.Action(phase2.RENAME_FILE, folder / "01 Bertha.flac",
                      folder / "gd1977-05-08d1t01.flac"),
        phase2.Action(phase2.SIDECAR, folder / "show.ffp", None,
                      payload="gd1977-05-08d1t01.flac:%s\r\n" % ("0" * 32)),
        phase2.Action(phase2.RENAME_SIDECAR, folder / "show.ffp",
                      folder / "gd1977-05-08.ffp"),
        phase2.Action(phase2.RENAME_FOLDER, folder, tmp_path / "lib" / "taken"),
    ])
    return folder, original, work


def test_a_rolled_back_folder_gets_its_checksum_file_back(tmp_path, cfg):
    folder, original, work = _sidecar_folder(tmp_path)
    phase2.execute_folder(work, cfg)

    assert work.status == phase2.FAILED
    assert sorted(p.name for p in folder.iterdir()) == ["01 Bertha.flac", "show.ffp"]
    # Byte for byte: the rewrite had replaced the only copy of these lines.
    assert (folder / "show.ffp").read_bytes() == original.encode("cp1252")


def test_an_interrupt_rolls_the_folder_back_and_still_stops_the_run(tmp_path, cfg,
                                                                    monkeypatch):
    """Ctrl-C is not an Exception.  Stopping mid-folder without undoing it left
    exactly the half-renamed folder the transaction exists to prevent."""
    folder, original, work = _sidecar_folder(tmp_path)
    real_apply = phase2._apply

    def interrupted(action, cfg_):
        if action.kind == phase2.RENAME_FOLDER:
            raise KeyboardInterrupt
        return real_apply(action, cfg_)

    monkeypatch.setattr(phase2, "_apply", interrupted)
    with pytest.raises(KeyboardInterrupt):
        phase2.execute_folder(work, cfg)
    assert sorted(p.name for p in folder.iterdir()) == ["01 Bertha.flac", "show.ffp"]
    assert (folder / "show.ffp").read_bytes() == original.encode("cp1252")


def test_an_interrupted_run_still_leaves_its_reversal_log(library, cfg, tmp_path,
                                                          monkeypatch):
    """The log used to be written at the very end, so a killed run had none."""
    out = tmp_path / "out"
    real_execute = phase2.execute_folder
    seen = {"n": 0}

    def execute_then_interrupt(work, cfg_):
        seen["n"] += 1
        if seen["n"] == 2:
            raise KeyboardInterrupt
        return real_execute(work, cfg_)

    monkeypatch.setattr(phase2, "execute_folder", execute_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        phase2.run(library, out, cfg, commit=True, today=TODAY)

    rows = (out / "phase2_committed.csv").read_text(encoding="utf-8-sig").splitlines()
    assert rows[0].startswith("status,kind,folder")
    assert any(r.startswith("done,") for r in rows[1:])
    assert not (out / ".jamp_run.lock").exists()


def test_a_rename_does_not_create_the_folder_it_lands_in(tmp_path):
    src = tmp_path / "a.flac"
    src.write_bytes(b"a")
    with pytest.raises(FileNotFoundError):
        phase2._safe_rename(src, tmp_path / "missing" / "a.flac")
    assert src.exists() and not (tmp_path / "missing").exists()


def test_a_merge_member_stays_put_when_its_primary_fails(library, cfg, tmp_path,
                                                        monkeypatch):
    """The member used to create the merged folder itself and move in, leaving
    one half of the show under the merged name and the other where it was."""
    real_apply = phase2._apply

    def primary_rename_fails(action, cfg_):
        if (action.kind == phase2.RENAME_FOLDER
                and action.path.name == "gd1973-12-10 s1"):
            raise PermissionError(5, "simulated: the folder is held open")
        return real_apply(action, cfg_)

    monkeypatch.setattr(phase2, "_apply", primary_rename_fails)
    stats = phase2.run(library, tmp_path / "out", cfg, commit=True,
                       include_merges=True, today=TODAY)

    gd = library / "grateful dead"
    assert sorted(p.name for p in (gd / "gd1973-12-10 s2").glob("*.flac"))
    assert sorted(p.name for p in (gd / "gd1973-12-10 s1").glob("*.flac"))
    assert not [p for p in gd.iterdir() if p.name.startswith("gd1973-12-10.")]
    assert stats["folders_failed"] >= 2
    summary = (tmp_path / "out" / "phase2_committed.csv").read_text(encoding="utf-8-sig")
    assert "skipped,move_file" in summary


def test_tags_can_be_restored_from_the_backup(library, cfg, tmp_path):
    import mutagen

    folder_before = (library / "My Morning Jacket" /
                     "My Morning Jacket 2023-11-03 Fox Theatre, Atlanta, GA [FLAC24]")
    original = {p.name: dict(mutagen.File(p).tags) for p in folder_before.glob("*.flac")}

    phase2.run(library, tmp_path / "out", cfg, commit=True, today=TODAY)
    folder = next(p for p in (library / "My Morning Jacket").iterdir()
                  if p.name.startswith("mmj2023-11-03"))
    assert (folder / BACKUP_NAME).exists()

    # Names changed, so restore by matching the backup's own filenames.
    restore_from_backup(folder)
    payload = json.loads((folder / BACKUP_NAME).read_text(encoding="utf-8"))
    assert set(payload["files"]) == set(original)


def test_the_reversal_log_lists_every_change(library, cfg, tmp_path):
    out = tmp_path / "out"
    phase2.run(library, out, cfg, commit=True, today=TODAY)
    text = (out / "phase2_committed.csv").read_text(encoding="utf-8-sig")
    assert "rename_folder" in text and "write_tags" in text and "backup_tags" in text
    journal = json.loads((out / "phase2_journal.json").read_text(encoding="utf-8"))
    assert journal["committed"] is True


# --- merges -----------------------------------------------------------------

def test_a_merge_gathers_both_folders_into_one(library, cfg, tmp_path):
    phase2.run(library, tmp_path / "out", cfg, commit=True,
               include_merges=True, today=TODAY)
    gd = library / "grateful dead"
    merged = next(p for p in gd.iterdir() if p.name.startswith("gd1973-12-10."))
    names = sorted(p.name for p in merged.glob("*.flac"))
    assert names == ["gd1973-12-10s1t01.flac", "gd1973-12-10s1t02.flac",
                     "gd1973-12-10s2t01.flac", "gd1973-12-10s2t02.flac"]


def test_a_merge_leaves_the_emptied_folder_in_place(library, cfg, tmp_path):
    """Nothing is ever deleted, so the drained folder stays for you to remove."""
    phase2.run(library, tmp_path / "out", cfg, commit=True,
               include_merges=True, today=TODAY)
    leftover = library / "grateful dead" / "gd1973-12-10 s2"
    assert leftover.is_dir()
    assert not list(leftover.glob("*.flac"))


def test_unnest_lifts_a_show_out_of_its_container(library, cfg, tmp_path):
    """u111105/ holds one show and nothing else, so the show comes up a level."""
    phase2.run(library, tmp_path / "out", cfg, commit=True, unnest=True, today=TODAY)
    um = library / "Umphrey's McGee"
    lifted = um / "um2011-11-05.sbd.umlive.mp3 - Eagles Ballroom, Milwaukee, WI"
    assert lifted.is_dir()
    assert list(lifted.glob("*.mp3"))
    # The container is left behind, empty; nothing is ever deleted.
    assert (um / "u111105").is_dir()
    assert not list((um / "u111105").iterdir())


def test_without_unnest_the_show_is_renamed_where_it_sits(library, cfg, tmp_path):
    phase2.run(library, tmp_path / "out", cfg, commit=True, today=TODAY)
    nested = (library / "Umphrey's McGee" / "u111105" /
              "um2011-11-05.sbd.umlive.mp3 - Eagles Ballroom, Milwaukee, WI")
    assert nested.is_dir()


def test_the_same_copy_in_two_places_is_pointed_out(tmp_path, cfg):
    """Same band, date and provenance: one of these is redundant."""
    root = tmp_path / "lib"
    for name in ("um2007-07-19", "odds and ends/New Folder"):
        folder = root / "Umphrey's McGee" / name
        for i in (1, 2):
            fixtures.make_mp3(
                folder / ("%02d Song.mp3" % i),
                tags={"ARTIST": "Umphrey's McGee",
                      "ALBUM": "2007-07-19 - Detroit Lakes MN, 10K Lakes Festival",
                      "TITLE": "Song %d" % i, "TRACKNUMBER": str(i), "DATE": "2007",
                      "COMMENT": "powered by nugs.net"},
            )
    plans = build_plans(root, cfg, today=TODAY)
    assert all("the same copy appears elsewhere" in " ".join(p.warnings) for p in plans)


def test_two_tapers_of_one_night_are_not_called_redundant(tmp_path, cfg):
    """Different provenance means genuinely different recordings - keep both."""
    root = tmp_path / "lib"
    for name in ("ph2018-12-28.padelimike.akg414", "ph2018-12-28.ldl.mk4"):
        for i in (1, 2):
            fixtures.make_flac(root / "phish" / name / ("d1t%02d.flac" % i), bits=16)
    plans = build_plans(root, cfg, today=TODAY)
    assert not any("the same copy appears elsewhere" in " ".join(p.warnings)
                   for p in plans)


def _mixed_format_show(root, titles=("Utopian Fir", "40s Theme", "JaJunk")):
    """A folder holding the same recording as both FLAC and MP3.

    The FLAC set splits at disc 2 where the MP3 set runs straight through, as a
    real store's does, so the filenames disagree while the music is identical.
    """
    folder = root / "Umphrey's McGee" / "um2007-07-19.sbd.nugs"
    for i, title in enumerate(titles, start=1):
        disc = 1 if i <= 1 else 2
        tags = {"ARTIST": "Umphrey's McGee", "ALBUM": "2007-07-19 - Detroit Lakes MN",
                "TITLE": title, "TRACKNUMBER": str(i), "DATE": "2007",
                "COMMENT": "powered by nugs.net"}
        fixtures.make_flac(folder / ("um2007-07-19d%dt%02d.flac" % (disc, i)),
                           bits=16, tags=tags)
        fixtures.make_mp3(folder / ("um2007-07-19d1t%02d.mp3" % i), tags=tags)
    return folder


def test_mp3s_duplicating_flacs_in_one_folder_are_found(tmp_path, cfg):
    folder = _mixed_format_show(tmp_path / "lib")
    plan = build_plans(tmp_path / "lib", cfg, today=TODAY)[0]
    assert plan.lossy
    assert len(plan.lossy.files) == 3
    assert all(f.ext == ".mp3" for f in plan.lossy.files)
    assert "matched on title" in plan.lossy.reason


def test_quarantine_moves_the_mp3s_and_deletes_nothing(tmp_path, cfg):
    root = tmp_path / "lib"
    folder = _mixed_format_show(root)
    phase2.run(root, tmp_path / "out", cfg, commit=True,
               quarantine_lossy=True, today=TODAY)

    assert sorted(p.suffix for p in folder.glob("*.*") if p.suffix in (".flac", ".mp3")) \
        == [".flac", ".flac", ".flac"]
    review = root / cfg.settings.review_folder
    moved = sorted(review.rglob("*.mp3"))
    assert len(moved) == 3
    # The original path is mirrored, so it is obvious where each file came from.
    assert "um2007-07-19.sbd.nugs" in str(moved[0])


def test_the_review_folder_is_not_scanned_as_music(tmp_path, cfg):
    root = tmp_path / "lib"
    _mixed_format_show(root)
    phase2.run(root, tmp_path / "out", cfg, commit=True,
               quarantine_lossy=True, today=TODAY)
    plans = build_plans(root, cfg, today=TODAY)
    assert all(cfg.settings.review_folder not in p.show.rel for p in plans)


def test_a_partial_match_moves_nothing(tmp_path, cfg):
    """One MP3 without a lossless twin means we do not understand the folder."""
    root = tmp_path / "lib"
    folder = _mixed_format_show(root)
    fixtures.make_mp3(folder / "um2007-07-19d1t09.mp3",
                      tags={"ARTIST": "Umphrey's McGee", "ALBUM": "2007-07-19",
                            "TITLE": "An Encore Nobody Kept", "TRACKNUMBER": "9"})
    plan = build_plans(root, cfg, today=TODAY)[0]
    assert not plan.lossy
    assert any("not a track-for-track copy" in w for w in plan.warnings)


def test_tracks_are_paired_on_duration_not_on_wording(tmp_path, cfg):
    """A store's FLAC titles are often tidier than its MP3 titles for the same
    performance: "Plunger" against "Plunger>Jimmy Stewart>Plunger"."""
    from jamp.dupes import _titles_compatible, _track_key

    assert _titles_compatible("plunger", "plungerjimmystewartplunger")
    assert _titles_compatible("nemo", "enemo")
    assert _titles_compatible("makingflippyfloppy",
                              "makingflippyfloppyhaveacigartease")
    assert not _titles_compatible("glory", "wizardburialground")


def test_an_all_mp3_folder_duplicating_a_flac_folder_is_found(tmp_path, cfg):
    root = tmp_path / "lib"
    _mixed_format_show(root)
    stray = root / "Umphrey's McGee" / "odds" / "New Folder"
    for i, title in enumerate(("Utopian Fir", "40s Theme", "JaJunk"), start=1):
        fixtures.make_mp3(stray / ("%02d %s.mp3" % (i, title)),
                          tags={"ARTIST": "Umphrey's McGee",
                                "ALBUM": "2007-07-19 - Detroit Lakes MN",
                                "TITLE": title, "TRACKNUMBER": str(i),
                                "DATE": "2007", "COMMENT": "powered by nugs.net"})
    plans = {p.show.path.name: p for p in build_plans(root, cfg, today=TODAY)}
    assert plans["New Folder"].lossy.whole_folder
    assert "losslessly" in plans["New Folder"].lossy.reason


def test_a_flac_only_show_is_never_quarantined(library, cfg, tmp_path):
    plans = build_plans(library, cfg, today=TODAY)
    assert not any(p.lossy for p in plans)


def test_a_committed_folder_records_what_was_decided(library, cfg, tmp_path):
    from jamp.state import STATE_NAME, read_state

    phase2.run(library, tmp_path / "out", cfg, commit=True, today=TODAY)
    folder = next(p for p in (library / "My Morning Jacket").iterdir()
                  if p.name.startswith("mmj2005-06-04"))
    state = read_state(folder)
    assert state is not None
    assert state["folder_name"] == folder.name
    assert state["date"] == "2005-06-04"
    assert state["source"] == "aud"
    assert (folder / STATE_NAME).exists()


def test_a_settled_folder_is_not_reclassified(library, cfg, tmp_path):
    """Phase 2 changes the evidence phase 1 reads: once an unofficial folder has
    been given clean tags it starts to look like a store download.  The state
    file is what stops that turning into a second rename."""
    phase2.run(library, tmp_path / "out", cfg, commit=True, today=TODAY)
    folder = next(p for p in (library / "My Morning Jacket").iterdir()
                  if p.name.startswith("mmj2005-06-04"))

    settled = {p.old_folder_name: p for p in build_plans(library, cfg, today=TODAY)}
    assert settled[folder.name].status == "UNCHANGED"
    assert "settled by a previous commit" in " ".join(settled[folder.name].reasons)

    # --reclassify makes it think again.
    fresh = {p.old_folder_name: p
             for p in build_plans(library, cfg, today=TODAY, reclassify=True)}
    assert "settled by a previous commit" not in " ".join(fresh[folder.name].reasons)


def test_settle_records_names_without_renaming_anything(library, cfg, tmp_path):
    from jamp.state import is_settled

    before = {p: p.stat().st_mtime_ns for p in library.rglob("*")}
    phase2.run(library, tmp_path / "out", cfg, commit=True, settle=True, today=TODAY)
    after = set(library.rglob("*"))
    # Only state files were added; nothing was renamed.
    added = {p for p in after if p.name == ".etree_state.json"}
    assert added
    assert {p for p in after if p not in added and p.name != ".etree_state.json"} == set(before)


def test_settle_leaves_disputed_folders_alone(tmp_path, cfg):
    """Freezing one half of a duplicate pair would let the other half rename
    itself and quietly break the pairing."""
    from jamp.state import is_settled

    root = tmp_path / "lib"
    for name in ("ph2018-12-28", "ph2018-12-28 alt"):
        for i in (1, 2):
            fixtures.make_flac(root / "phish" / name / ("d1t%02d.flac" % i), bits=16)
    phase2.run(root, tmp_path / "out", cfg, commit=True, settle=True, today=TODAY)
    assert not is_settled(root / "phish" / "ph2018-12-28")


def test_a_folder_with_an_unreadable_file_is_reported_not_renamed(tmp_path, cfg):
    """A zero-byte FLAC cannot be tagged, and would otherwise leave the folder
    looking unfinished on every future run."""
    root = tmp_path / "lib"
    folder = root / "My Morning Jacket" / "mmj2010-05-01"
    for i in (1, 2):
        fixtures.make_flac(folder / ("mmj2010-05-01d1t%02d.flac" % i), bits=16)
    (folder / "mmj2010-05-01d1t09.flac").write_bytes(b"")
    plan = build_plans(root, cfg, today=TODAY)[0]
    assert plan.status == "SKIP_BLOCKED"
    assert "UNREADABLE_AUDIO" in plan.analysis.issue_codes


def test_an_even_format_split_gets_no_format_token(tmp_path, cfg):
    """12 FLAC and 12 MP3: whichever wins would depend on directory order."""
    root = tmp_path / "lib"
    folder = root / "Umphrey's McGee" / "um2007-07-19"
    for i in (1, 2):
        fixtures.make_flac(folder / ("um2007-07-19d1t%02d.flac" % i), bits=16)
        fixtures.make_mp3(folder / ("um2007-07-19d2t%02d.mp3" % i))
    plan = build_plans(root, cfg, today=TODAY)[0]
    assert plan.analysis.fmt is None
    assert any("even split" in " ".join(i.detail for i in plan.analysis.issues)
               for _ in [0])


def test_after_committing_phase1_reports_nothing_left_to_do(library, cfg, tmp_path):
    """Rule 6, end to end: a second pass finds no work."""
    phase2.run(library, tmp_path / "out", cfg, commit=True,
               include_merges=True, today=TODAY)
    again = build_plans(library, cfg, today=TODAY)
    assert not [p for p in again if p.status == phase1.PLAN]


def test_the_backup_can_still_restore_after_the_files_are_renamed(tmp_path, cfg):
    """The undo has to survive the rename that happens right after it.

    Tags are written first and files renamed a few actions later, so a backup
    keyed only by the original names matches nothing by the time anyone wants
    it - and it failed silently, reporting zero files restored as if that were
    a success.
    """
    import fixtures
    from jamp.phase2 import run as phase2_run
    from jamp.tagwriter import BACKUP_NAME, restore_from_backup
    from jamp.audio import read_audio_file

    lib = tmp_path / "lib"
    show = lib / "My Morning Jacket" / "mmj2005-06-04.ak40.flac16"
    for i in (1, 2):
        fixtures.make_flac(show / ("%02d Some Song.flac" % i), bits=16,
                           tags={"ARTIST": "My Morning Jacket", "TITLE": "Song %d" % i,
                                 "TRACKNUMBER": str(i), "ALBUM": "ORIGINAL ALBUM"})
    phase2_run(lib, tmp_path / "out", cfg, commit=True)

    folder = next(p for p in (lib / "My Morning Jacket").iterdir() if p.is_dir())
    assert (folder / BACKUP_NAME).exists()
    # The tracks really were renamed, so this is the case that used to fail.
    assert sorted(p.name for p in folder.glob("*.flac")) == [
        "mmj2005-06-04d1t01.flac", "mmj2005-06-04d1t02.flac"]

    assert restore_from_backup(folder) == 2
    assert read_audio_file(sorted(folder.glob("*.flac"))[0]).tag("ALBUM") == "ORIGINAL ALBUM"


def test_a_restore_that_matches_nothing_is_an_error_not_a_quiet_zero(tmp_path):
    """Reporting "0 files restored" as success is the worst failure for an undo."""
    import json
    import pytest
    from jamp.tagwriter import BACKUP_NAME, TagWriteError, restore_from_backup

    folder = tmp_path / "show"
    folder.mkdir()
    (folder / BACKUP_NAME).write_text(json.dumps(
        {"folder": str(folder), "files": {"gone.flac": {"fields": {"ALBUM": ["x"]}}}}),
        encoding="utf-8")
    with pytest.raises(TagWriteError, match="nothing was restored"):
        restore_from_backup(folder)


def test_a_transient_access_denied_is_retried(tmp_path):
    """Windows holds a directory for a moment after its files are rewritten.

    Phase 2 writes tags to every file in a folder and renames them, which wakes
    the indexer and antivirus; they open what changed and the folder rename
    fails with WinError 5 for a second or two.
    """
    from jamp.phase2 import _rename_with_retry

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            exc = PermissionError("access is denied")
            exc.winerror = 5
            raise exc

    _rename_with_retry(flaky, attempts=5, first_delay=0.001)
    assert calls["n"] == 3


def test_a_real_permission_error_is_raised_unchanged(tmp_path):
    """Retrying must not bury a problem that will never clear."""
    import pytest
    from jamp.phase2 import _rename_with_retry

    calls = {"n": 0}

    def always():
        calls["n"] += 1
        exc = PermissionError("access is denied")
        exc.winerror = 5
        raise exc

    with pytest.raises(PermissionError):
        _rename_with_retry(always, attempts=3, first_delay=0.001)
    assert calls["n"] == 3


def test_an_unrelated_oserror_is_not_retried(tmp_path):
    """Only the two "somebody is holding this" errors are worth waiting on."""
    import pytest
    from jamp.phase2 import _rename_with_retry

    calls = {"n": 0}

    def missing():
        calls["n"] += 1
        exc = FileNotFoundError("no such file")
        exc.winerror = 2
        raise exc

    with pytest.raises(FileNotFoundError):
        _rename_with_retry(missing, attempts=5, first_delay=0.001)
    assert calls["n"] == 1


def test_write_tags_reaches_a_path_past_windows_max_path(tmp_path):
    """A track title long enough to push the full path over 260 characters.

    write_tags called mutagen with the raw path and nothing else in the module
    went through winpath.opener() either, unlike every other file access in
    the pipeline.  It failed with "No such file or directory" on a file that
    plainly existed - on a real Jerry Garcia commit, the very track this test
    is modeled on ("That's What Love Will Make You Do") sat at 264 characters
    and aborted the whole folder mid-write.
    """
    import fixtures
    from jamp.tagwriter import write_tags
    from jamp.audio import read_audio_file

    deep = tmp_path
    for part in ("a" * 50, "b" * 50, "c" * 50, "d" * 50, "e" * 50):
        deep = deep / part
    path = deep / "03 That's What Love Will Make You Do.flac"
    assert len(str(path)) > 260
    fixtures.make_flac(path, bits=16, tags={})

    write_tags(path, {"ALBUM": "Kean College", "TRACKNUMBER": "3"})
    assert read_audio_file(path).tag("ALBUM") == "Kean College"


def test_restore_from_backup_also_reaches_a_long_path(tmp_path):
    """The undo path needs the same long-path handling as the write path."""
    import fixtures
    from jamp.tagwriter import BACKUP_NAME, write_backup, write_tags, restore_from_backup
    from jamp.audio import read_audio_file

    deep = tmp_path
    for part in ("a" * 50, "b" * 50, "c" * 50, "d" * 50, "e" * 50):
        deep = deep / part
    path = deep / "03 Some Very Long Track Title Indeed.flac"
    assert len(str(path)) > 260
    fixtures.make_flac(path, bits=16, tags={"ALBUM": "ORIGINAL"})

    write_backup(deep, [path])
    write_tags(path, {"ALBUM": "CHANGED"})
    assert read_audio_file(path).tag("ALBUM") == "CHANGED"

    assert restore_from_backup(deep) == 1
    assert read_audio_file(path).tag("ALBUM") == "ORIGINAL"


def test_a_show_nested_inside_another_show_is_renamed_first(tmp_path, cfg):
    """Both get renamed, and the parent must not move first.

    Real failure on the Grateful Dead commit: 'Spring 1990 Box Set/19900326
    Knickerbocker Arena' held a misfiled Download Series volume.  The parent
    renamed, then the child failed with FileNotFoundError because its plan
    still pointed at the old parent path.
    """
    import fixtures
    from jamp.phase1 import build_plans
    from jamp.phase2 import eligible_plans

    root = tmp_path / "lib"
    parent = root / "grateful dead" / "Spring 1990 Box Set" / "19900326 Knickerbocker Arena Albany, NY"
    for i in range(1, 11):
        fixtures.make_flac(parent / ("%02d Song %d.flac" % (i, i)), bits=16)
    child = parent / "Grateful Dead Download Series Vol. 08 1973-12-10"
    for i in (1, 2):
        fixtures.make_flac(child / ("1-%02d Bertha.flac" % i), bits=16)

    plans = build_plans(root, cfg, today=dt.date(2026, 9, 7))
    order = [p.show.path for p in eligible_plans(plans, include_merges=False)]
    assert child in order and parent in order
    assert order.index(child) < order.index(parent), \
        "the nested show must be renamed before its parent moves"


# --------------------------------------------------------------------------
# containers phase 2 could read but not write
# --------------------------------------------------------------------------

def test_a_bare_boolean_tag_does_not_break_the_backup(tmp_path):
    """MP4 stores cpil and pgap as bools, not lists.

    The backup runs before anything is written, so iterating one of those
    took the whole folder down before a tag had been touched - 5 Phish
    folders rolled back on it.
    """
    from jamp.tagwriter import _tag_values

    assert _tag_values(True) == ["True"]
    assert _tag_values(0) == ["0"]
    assert _tag_values(["a", "b"]) == ["a", "b"]
    assert _tag_values([(1, 18)]) == ["(1, 18)"]


def test_a_wav_carrying_id3_is_written_as_id3(tmp_path):
    """A WAV's tags are _WaveID3, which is not "ID3", so it fell through to
    the generic branch and assigned a list into a frame dictionary."""
    import wave
    import mutagen
    from jamp.tagwriter import write_tags

    p = tmp_path / "t.wav"
    w = wave.open(str(p), "wb")
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
    w.writeframes(b"\x00\x00\x00\x00" * 100)
    w.close()

    write_tags(p, {"ARTIST": "Phish", "ALBUM": "1998-06-30: Den Gra Hal",
                   "VENUE": "Den Gra Hal"})
    tags = mutagen.File(str(p)).tags
    assert tags.__class__.__name__ == "_WaveID3"
    assert str(tags["TPE1"]) == "Phish"
    assert str(tags["TXXX:VENUE"]) == "Den Gra Hal"


def test_mp4_track_and_disc_keep_the_total(tmp_path):
    """trkn and disk are (number, total) pairs.  Writing a bare number would
    throw the total away, so an 18-track show would forget it had 18."""
    from jamp.tagwriter import _apply_mp4

    tags = {"trkn": [(1, 18)], "disk": [(1, 2)]}
    _apply_mp4(tags, {"TRACKNUMBER": "3", "DISCNUMBER": "2"})
    assert tags["trkn"] == [(3, 18)]
    assert tags["disk"] == [(2, 2)]


def test_mp4_takes_a_tag_outside_its_fixed_vocabulary(tmp_path):
    """MP4 has no VENUE atom, so it travels as an iTunes freeform one."""
    from jamp.tagwriter import _apply_mp4

    tags = {}
    _apply_mp4(tags, {"ARTIST": "Phish", "VENUE": "Den Gra Hal"})
    assert tags["\xa9ART"] == ["Phish"]
    assert bytes(tags["----:com.apple.iTunes:VENUE"][0]) == b"Den Gra Hal"


def test_an_mp4_freeform_tag_reads_back_as_it_was_written(tmp_path):
    """MP4 has no VENUE atom, so it is written as an iTunes freeform one.

    The reader mapped only MP4's eight fixed atoms and skipped the rest, so
    VENUE looked empty on the next pass and was written again every run - 402
    tag edits across 22 Phish folders that could never settle, even though the
    value was already on disk.
    """
    from jamp.audio import _normalize_mp4
    from jamp.tagwriter import _apply_mp4

    tags = {}
    _apply_mp4(tags, {"ARTIST": "Phish", "VENUE": "Armstrong Hall, Colorado College",
                      "SOURCE_CONFIDENCE": "99"})
    back = _normalize_mp4(tags)
    assert back["ARTIST"] == ["Phish"]
    assert back["VENUE"] == ["Armstrong Hall, Colorado College"]
    assert back["SOURCE_CONFIDENCE"] == ["99"]


# --------------------------------------------------------------------------
# a writer must have a reader that agrees with it
# --------------------------------------------------------------------------

@pytest.mark.parametrize("field", list(INTERESTING_TAGS))
def test_every_field_round_trips_through_every_container(field):
    """Write a canonical field, read it back, get the same value.

    This is the invariant two separate bugs broke.  Phase 2 wrote our own
    "[Release]" suffix into ALBUM and phase 1 read it back as part of the
    venue; later the MP4 writer sent VENUE to a freeform atom that the MP4
    reader skipped, so 22 folders were rewritten on every pass and never
    settled.  Both times a writer had been added without a reader that agreed
    with it, and nothing failed loudly.

    The maps live apart on purpose - MP4 atoms and ID3 frames have nothing in
    common - so this test, not a shared table, is what keeps them honest.
    """
    from mutagen import id3
    from jamp.audio import _normalize_id3, _normalize_mp4, _normalize_vorbis
    from jamp.tagwriter import _apply_id3, _apply_mp4

    value = {"TRACKNUMBER": "3", "DISCNUMBER": "2", "DATE": "1997-11-22"}.get(
        field, "probe-value")

    frames = id3.ID3()
    _apply_id3(frames, {field: value})
    assert _normalize_id3(frames).get(field) == [value], "ID3 lost %s" % field

    atoms = {}
    _apply_mp4(atoms, {field: value})
    assert _normalize_mp4(atoms).get(field) == [value], "MP4 lost %s" % field

    assert _normalize_vorbis({field.upper(): [value]}).get(field) == [value], \
        "Vorbis lost %s" % field


def test_until_settled_keeps_going_and_stops_on_a_stalemate(tmp_path, cfg, monkeypatch):
    """Committing changes what the next read sees, so the answer converges over
    a few passes.  Doing that by hand meant commit, then a separate phase 1 to
    see what was left, then commit again - Phish took five rounds of it.

    The loop must also stop when a pass achieves nothing, or a folder the tool
    cannot finish would be rewritten forever.
    """
    from jamp import phase2

    calls = {"n": 0}
    real_build = phase2.build_plans

    def fake_build(*a, **k):
        calls["n"] += 1
        return []                      # nothing to do: an immediate stalemate

    monkeypatch.setattr(phase2, "build_plans", fake_build)
    root = tmp_path / "lib"; root.mkdir()
    stats = phase2.run(root, tmp_path / "out", cfg, commit=True, until_settled=True)
    assert stats["passes"] == 1, "a pass that finds nothing must not loop"
    assert stats["remaining"] == 0
    assert calls["n"] == 2, "one working pass, then one to report what is left"


def test_a_commit_says_whether_anything_is_left(tmp_path, cfg, monkeypatch):
    """The count that used to require running phase 1 again by hand."""
    from jamp import phase2

    monkeypatch.setattr(phase2, "build_plans", lambda *a, **k: [])
    root = tmp_path / "lib"; root.mkdir()
    stats = phase2.run(root, tmp_path / "out", cfg, commit=True)
    assert stats["remaining"] == 0
    assert stats["passes"] == 1


def test_a_dry_run_never_loops_and_reports_no_remaining(tmp_path, cfg, monkeypatch):
    """Nothing was written, so "what is left" would be the whole plan again."""
    from jamp import phase2

    monkeypatch.setattr(phase2, "build_plans", lambda *a, **k: [])
    root = tmp_path / "lib"; root.mkdir()
    stats = phase2.run(root, tmp_path / "out", cfg, commit=False, until_settled=True)
    assert stats["passes"] == 1
    assert "remaining" not in stats


def test_settled_also_says_what_was_left_alone(tmp_path, cfg, monkeypatch):
    """"Nothing left to do" is not "everything got renamed".

    A folder blocked on a date conflict, waiting on a duplicate decision, or
    holding other shows is left alone deliberately - and a run that says only
    SETTLED reads as though those folders were missed.
    """
    from jamp import phase2
    from jamp.phase1 import DUPLICATE, SKIP_BLOCKED

    class FakePlan:
        def __init__(self, status):
            self.status = status
            self.new_folder_name = None
            self.analysis = None

    calls = {"n": 0}

    def build(*a, **k):
        calls["n"] += 1
        # nothing actionable, but three folders deliberately untouched
        return [FakePlan(SKIP_BLOCKED), FakePlan(SKIP_BLOCKED), FakePlan(DUPLICATE)]

    monkeypatch.setattr(phase2, "build_plans", build)
    root = tmp_path / "lib"; root.mkdir()
    stats = phase2.run(root, tmp_path / "out", cfg, commit=True)
    assert stats["remaining"] == 0
    assert stats["held"] == {SKIP_BLOCKED: 2, DUPLICATE: 1}

def test_a_merge_is_refused_when_its_target_name_is_already_taken(tmp_path):
    """Nothing may move until the destination is known to be free.

    A member moves its files into the merge target as its own unit of work, so
    the primary failing to become that target does not stop it.  Trey Anastasio
    2002-05-31: a whole copy of the show already held the target name, and a
    separate split copy of the same date - different audio entirely - was
    merged towards it.  The primary's rename failed with FileExistsError and was
    rolled back, but the member's nine tracks had already been moved into the
    occupant, and the rollback did not bring them home.
    """
    from jamp.phase2 import eligible_plans
    from jamp.phase1 import MERGE

    class _Show:
        def __init__(self, path):
            self.path = path
            self.container = None
            self.filing_parent = None

    class _P:
        def __init__(self, path, target, role):
            self.status = MERGE
            self.show = _Show(path)
            self.analysis = type("A", (), {"show": self.show})()
            self.merge_target = target
            self.merge_role = role
            self.new_folder_name = target.name

    target = tmp_path / "tab2002-05-31.aud.mp3 - Thomas & Mack Center, Las Vegas, NV"
    primary = tmp_path / "2002-05-31 part 1"
    member = tmp_path / "2002-05-31 part 2"
    for d in (primary, member):
        d.mkdir()
    group = [_P(primary, target, "primary"), _P(member, target, "member")]

    # Target free: the merge goes ahead, primary first.
    chosen = eligible_plans(group, include_merges=True)
    assert [p.merge_role for p in chosen] == ["primary", "member"]

    # Target held by a folder that is no part of the merge: nothing is offered,
    # so no file moves and neither half is touched.
    target.mkdir()
    assert eligible_plans(group, include_merges=True) == []

    # A resumed merge, where the primary has already become the target, is not
    # blocked by its own folder existing.
    resumed = [_P(target, target, "primary"), _P(member, target, "member")]
    assert len(eligible_plans(resumed, include_merges=True)) == 2


def test_state_and_backup_are_found_past_the_path_limit(tmp_path):
    """A plain exists() fails past MAX_PATH without long-path support: the state
    file reads as never settled, and a backup that is there reads as absent -
    so the "never overwritten" backup would be replaced."""
    import json

    from jamp import state, tagwriter

    deep = tmp_path
    for part in ("a" * 60, "b" * 60, "c" * 60, "d" * 60):
        deep = deep / part
    fixtures.make_flac(deep / "t01.flac", tags={"TITLE": "Original"})
    state.write_state(deep, {"previous_folder_name": deep.name})
    assert state.is_settled(deep)

    tagwriter.write_backup(deep, [deep / "t01.flac"])
    tagwriter.write_tags(deep / "t01.flac", {"TITLE": "Changed"})
    tagwriter.write_backup(deep, [deep / "t01.flac"])
    from jamp.winpath import opener
    from pathlib import Path
    payload = json.loads(Path(opener(deep / tagwriter.BACKUP_NAME)).read_text(encoding="utf-8"))
    assert "Original" in json.dumps(payload)


@pytest.mark.parametrize("encoding,raw_prefix", [
    ("cp1252", b""), ("utf-8", b""), ("utf-8", b"\xef\xbb\xbf")])
def test_a_rewritten_checksum_file_keeps_its_encoding(tmp_path, cfg, encoding, raw_prefix):
    """It was always written back as UTF-8: a cp1252 file changed the bytes of
    every accented name in it, and a BOM was dropped or added."""
    from jamp.sidecars import plan_sidecar

    folder = tmp_path / "show"
    folder.mkdir()
    old = "Café Wha.flac"
    body = "%s:%s\r\n" % (old, "0" * 32)
    path = folder / "show.ffp"
    path.write_bytes(raw_prefix + body.encode(encoding))
    (folder / old).write_bytes(b"x")

    plan = plan_sidecar(path, {old: "gd1977-05-08d1t01.flac"})
    work = phase2.FolderWork(plan=None, actions=[
        phase2.Action(phase2.SIDECAR, path, None, payload=(plan.new_text, plan.encoding))])
    phase2.execute_folder(work, cfg)
    assert path.read_bytes() == raw_prefix + ("gd1977-05-08d1t01.flac:%s\r\n" % ("0" * 32)).encode(encoding)


def test_a_checksum_file_too_long_to_read_whole_is_left_alone(tmp_path):
    from jamp.sidecars import READ_LIMIT, UNPARSED, plan_sidecar

    path = tmp_path / "huge.md5"
    line = "%s *01 Song.flac\n" % ("0" * 32)
    path.write_text(line * (READ_LIMIT // len(line) + 10), encoding="utf-8")
    plan = plan_sidecar(path, {"01 Song.flac": "x.flac"})
    assert plan.status == UNPARSED and plan.new_text is None


def test_unnest_never_lifts_a_show_out_of_its_act_folder(tmp_path, cfg):
    """An act folder holding one show looks like a container that holds
    nothing else.  Lifting from it put the show loose in ROOT and emptied the
    act folder - how STS9 and TAB had to be moved back by hand."""
    root = tmp_path / "lib"
    act = root / "Umphrey's McGee"
    show = act / "um2001-06-02.sbd.flac16"
    for i in (1, 2):
        fixtures.make_flac(show / ("t%02d.flac" % i), tags={"TITLE": "Song %d" % i})
    plan = next(p for p in build_plans(root, cfg, today=TODAY))
    assert plan.status == PLAN and plan.show.container == act

    phase2.run(root, tmp_path / "out", cfg, commit=True, unnest=True, today=TODAY)
    tracks = [p for p in root.rglob("*.flac") if p.is_file()]
    assert len(tracks) == 2
    assert all(t.parent.parent == act for t in tracks), [str(t) for t in tracks]


def test_a_case_only_rename_that_fails_halfway_is_put_back(tmp_path, monkeypatch):
    import os

    src = tmp_path / "MMJ2006.flac"
    src.write_bytes(b"x")
    real = os.rename

    def second_step_fails(a, b):
        if str(a).endswith(".jamp-tmp") and os.path.basename(str(b)) == "mmj2006.flac":
            raise PermissionError(5, "held open")
        return real(a, b)

    monkeypatch.setattr(phase2.os, "rename", second_step_fails)
    with pytest.raises(PermissionError):
        phase2._safe_rename(src, tmp_path / "mmj2006.flac")
    monkeypatch.setattr(phase2.os, "rename", real)
    assert [p.name for p in tmp_path.iterdir()] == ["MMJ2006.flac"]


def test_settle_freezes_a_date_and_place_only_inside_a_release(tmp_path, cfg):
    """Outside a release, "2023-07-14 Ameris Bank Amphitheatre" is a name
    someone typed, not ours - freezing it would keep it from being renamed."""
    root = tmp_path / "lib"
    loose = root / "My Morning Jacket" / "2023-07-14 Ameris Bank Amphitheatre, Alpharetta, GA"
    fixtures.make_flac(loose / "01 Song.flac", tags={"TITLE": "Song"})
    chosen = phase2.settleable(build_plans(root, cfg, today=TODAY))
    assert loose not in [p.show.path for p in chosen]


def test_the_override_count_includes_path_keys():
    from jamp.overrides import Override, Overrides

    assert len(Overrides([Override("a", {}), Override("x/b", {})])) == 2
