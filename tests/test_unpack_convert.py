"""`jamp unpack` and `jamp convert`."""
import csv
import subprocess
import zipfile
from pathlib import Path

import pytest

from jamp import cli, convert, unpack, userdir
from jamp.integrity import find_ffmpeg


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("JAMP_HOME", str(h))
    monkeypatch.setattr(userdir, "LEGACY_OVERRIDES", tmp_path / "none.yaml")
    monkeypatch.setattr(userdir, "LEGACY_PATHS", tmp_path / "none.json")
    return h


def _run(command, root, out, *extra):
    return cli.main([command, str(root), "--out-dir", str(out), *extra])


def _zip(path: Path, members: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


# -- unpack -----------------------------------------------------------------

def test_a_zip_holding_one_folder_is_extracted_as_that_folder(tmp_path, home):
    root = tmp_path / "lib"
    z = _zip(root / "Phish" / "download.zip", {
        "ph1997-11-22/ph1997-11-22d1t01.flac": b"one",
        "ph1997-11-22/info.txt": b"Hampton"})
    out = tmp_path / "r"
    assert _run("unpack", root, out) == 0
    assert not (root / "Phish" / "ph1997-11-22").exists()          # dry run
    assert _run("unpack", root, out, "--commit") == 0
    show = root / "Phish" / "ph1997-11-22"
    assert (show / "ph1997-11-22d1t01.flac").read_bytes() == b"one"
    assert z.exists()                                              # kept
    assert not list((root / "Phish").glob(".jamp-unpacking-*"))


def test_loose_files_get_a_folder_named_after_the_zip(tmp_path, home):
    root = tmp_path / "lib"
    _zip(root / "Phish" / "ph1998-04-02.zip", {"d1t01.flac": b"a", "d1t02.flac": b"b"})
    zp = unpack.plan_zip(root / "Phish" / "ph1998-04-02.zip")
    assert zp.status == unpack.EXTRACT
    assert [t.name for t in zp.targets] == ["ph1998-04-02"]


def test_an_existing_matching_folder_is_already_extracted(tmp_path, home):
    root = tmp_path / "lib"
    z = _zip(root / "A" / "show.zip", {"show/t1.flac": b"abc"})
    (root / "A" / "show").mkdir()
    (root / "A" / "show" / "t1.flac").write_bytes(b"abc")
    assert unpack.plan_zip(z).status == unpack.ALREADY
    (root / "A" / "show" / "t1.flac").write_bytes(b"different")
    assert unpack.plan_zip(z).status == unpack.REFUSED


def test_a_zip_that_writes_outside_its_folder_is_refused(tmp_path):
    z = _zip(tmp_path / "evil.zip", {"../../escape.txt": b"x"})
    zp = unpack.plan_zip(z)
    assert zp.status == unpack.REFUSED and "outside" in zp.reason


def test_a_damaged_zip_leaves_nothing_behind(tmp_path):
    z = _zip(tmp_path / "lib" / "bad.zip", {"bad/t1.flac": b"x" * 5000})
    zp = unpack.plan_zip(z)
    zp.members[0].crc ^= 1                     # the ZIP now disagrees with itself
    with pytest.raises(zipfile.BadZipFile):
        unpack.extract(zp)
    assert sorted(p.name for p in (tmp_path / "lib").iterdir()) == ["bad.zip"]


def test_rar_and_7z_are_reported(tmp_path):
    p = tmp_path / "show.rar"
    p.write_bytes(b"Rar!")
    assert unpack.plan_zip(p).status == unpack.UNSUPPORTED


def test_set_aside_moves_only_zips_proven_on_disk(tmp_path, home):
    root = tmp_path / "lib"
    good = _zip(root / "A" / "good.zip", {"good/t1.flac": b"abc"})
    _zip(root / "B" / "tampered.zip", {"tampered/t1.flac": b"abc"})
    out = tmp_path / "r"
    _run("unpack", root, out)
    _run("unpack", root, out, "--commit")
    (root / "B" / "tampered" / "t1.flac").write_bytes(b"xyz")        # same size
    _run("unpack", root, out, "--set-aside")
    _run("unpack", root, out, "--set-aside", "--commit")
    review = root / "_etree_review" / "originals"
    assert (review / "A" / "good.zip").exists() and not good.exists()
    assert (root / "B" / "tampered.zip").exists()
    assert not (review / "B").exists()


def test_unpack_commit_needs_its_dry_run(tmp_path, home, capsys):
    root = tmp_path / "lib"
    _zip(root / "A" / "x.zip", {"x/t.flac": b"1"})
    assert _run("unpack", root, tmp_path / "r", "--commit") == 2
    assert "dry run" in capsys.readouterr().err


# -- convert ----------------------------------------------------------------

def _ffmpeg():
    ff = find_ffmpeg()
    try:
        subprocess.run([ff, "-version"], capture_output=True, timeout=20, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return ff


FFMPEG = _ffmpeg()
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")


def _fake_shn(path: Path, freq: int = 440) -> Path:
    """Real audio for ffmpeg to decode. ffmpeg cannot write Shorten, but it
    reads a file by what is in it, so a WAV named .shn decodes like one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=%d:duration=1" % freq, "-c:a", "pcm_s16le",
                    "-f", "wav", str(path)], check=True)
    return path


@needs_ffmpeg
def test_shn_is_converted_and_proven(tmp_path, home):
    root = tmp_path / "lib"
    show = root / "Dead" / "gd1977-05-08.sbd.shnf"
    for n in (1, 2):
        _fake_shn(show / ("gd77-05-08d1t0%d.shn" % n), 440 * n)
    (show / "gd77-05-08.md5").write_text("x *gd77-05-08d1t01.shn\n")
    out = tmp_path / "r"
    assert _run("convert", root, out) == 0
    assert not list(show.glob("*.flac"))                          # dry run
    assert _run("convert", root, out, "--commit") == 0
    for n in (1, 2):
        assert (show / ("gd77-05-08d1t0%d.flac" % n)).exists()
        assert (show / ("gd77-05-08d1t0%d.shn" % n)).exists()     # kept
    assert not list(show.glob("*.jamp-part.flac"))
    ffp = (show / (show.name + ".ffp")).read_text().splitlines()
    assert [l.split(":")[0] for l in ffp] == ["gd77-05-08d1t01.flac", "gd77-05-08d1t02.flac"]
    rows = list(csv.DictReader(open(out / "convert_committed.csv", encoding="utf-8-sig")))
    assert {r["result"] for r in rows} == {"converted"}
    assert all(r["shn_md5"] == r["flac_md5"] for r in rows)
    assert "still name the .shn files" in (out / "convert_summary.txt").read_text()


@needs_ffmpeg
def test_a_flac_that_differs_is_a_mismatch_and_kept(tmp_path, home):
    root = tmp_path / "lib"
    shn = _fake_shn(root / "A" / "s" / "t1.shn", 440)
    other = _fake_shn(root / "A" / "s" / "other.shn", 880)
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(other), "-f", "flac",
                    str(shn.with_suffix(".flac"))], check=True)
    other.unlink()
    sp = convert.plan_files([shn])[0]
    convert.convert_one(FFMPEG, sp)
    assert sp.result == convert.MISMATCH and shn.with_suffix(".flac").exists()


@needs_ffmpeg
def test_a_conversion_that_does_not_match_is_not_kept(tmp_path, monkeypatch):
    shn = _fake_shn(tmp_path / "s" / "t1.shn")
    real = convert.audio_md5
    calls = []

    def lying(ffmpeg, path, timeout=3600):
        calls.append(path)
        md5, why = real(ffmpeg, path, timeout)
        return (("0" * 32 if path.name.endswith(convert._PART) else md5), why)

    monkeypatch.setattr(convert, "audio_md5", lying)
    sp = convert.plan_files([shn])[0]
    convert.convert_one(FFMPEG, sp)
    assert sp.result == convert.MISMATCH
    assert sorted(p.name for p in shn.parent.iterdir()) == ["t1.shn"]


@needs_ffmpeg
def test_set_aside_after_a_proven_run_moves_the_shn(tmp_path, home, monkeypatch):
    root = tmp_path / "lib"
    shn = _fake_shn(root / "A" / "s" / "t1.shn")
    out = tmp_path / "r"
    _run("convert", root, out)
    _run("convert", root, out, "--commit")

    # Proven by the log and unchanged: set aside without decoding again.
    monkeypatch.setattr(convert, "audio_md5",
                        lambda *a, **k: pytest.fail("decoded again"))
    _run("convert", root, out, "--set-aside")
    assert _run("convert", root, out, "--set-aside", "--commit") == 0
    assert not shn.exists() and shn.with_suffix(".flac").exists()
    assert (root / "_etree_review" / "originals" / "A" / "s" / "t1.shn").exists()


@needs_ffmpeg
def test_set_aside_decodes_again_when_the_flac_changed(tmp_path, home):
    root = tmp_path / "lib"
    shn = _fake_shn(root / "A" / "s" / "t1.shn", 440)
    out = tmp_path / "r"
    _run("convert", root, out)
    _run("convert", root, out, "--commit")
    other = _fake_shn(tmp_path / "other.shn", 880)
    flac = shn.with_suffix(".flac")
    flac.unlink()
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(other), "-f", "flac",
                    str(flac)], check=True)
    _run("convert", root, out, "--set-aside")
    assert _run("convert", root, out, "--set-aside", "--commit") == 1
    assert shn.exists()                                   # not proven, so kept


def test_convert_commit_needs_its_dry_run(tmp_path, home, capsys):
    root = tmp_path / "lib"
    (root / "A").mkdir(parents=True)
    assert _run("convert", root, tmp_path / "r", "--commit") == 2
    assert "dry run" in capsys.readouterr().err


def test_two_disc_folders_become_one_show_folder_named_for_the_zip(tmp_path, home):
    root = tmp_path / "lib"
    _zip(root / "Dead" / "gd1977-05-08.zip", {"Disc 1/01.flac": b"a",
                                                "Disc 2/01.flac": b"b"})
    out = tmp_path / "r"
    _run("unpack", root, out)
    _run("unpack", root, out, "--commit")
    show = root / "Dead" / "gd1977-05-08"
    assert (show / "Disc 1" / "01.flac").read_bytes() == b"a"
    assert (show / "Disc 2" / "01.flac").read_bytes() == b"b"


def test_several_shows_each_land_beside_the_zip_and_extras_in_its_folder(tmp_path, home):
    root = tmp_path / "lib"
    members = {"ph1997-07-0%d/t1.flac" % n: b"x" for n in (1, 2, 3, 5, 6)}
    members.update({"info.txt": b"i", "cover.jpg": b"c"})
    _zip(root / "Phish" / "Summer Tour 1997.zip", members)
    out = tmp_path / "r"
    _run("unpack", root, out)
    assert "5 separate folders" in (out / "unpack_summary.txt").read_text()
    _run("unpack", root, out, "--commit")
    assert sorted(p.name for p in (root / "Phish").iterdir()) == [
        "Summer Tour 1997", "Summer Tour 1997.zip", "ph1997-07-01", "ph1997-07-02",
        "ph1997-07-03", "ph1997-07-05", "ph1997-07-06"]
    assert sorted(p.name for p in (root / "Phish" / "Summer Tour 1997").iterdir()) == [
        "cover.jpg", "info.txt"]
    assert (root / "Phish" / "ph1997-07-05" / "t1.flac").read_bytes() == b"x"


def test_two_shows_without_extras_need_no_folder_for_the_zip(tmp_path):
    z = _zip(tmp_path / "Dead" / "two nights.zip",
             {"gd1977-05-07/t1.flac": b"a", "gd1977-05-08/t1.flac": b"b"})
    assert [t.name for t in unpack.plan_zip(z).targets] == ["gd1977-05-07", "gd1977-05-08"]


@pytest.mark.parametrize("members", [
    {"Grateful Dead 1977-05-08 (Disc 1)/t1.flac": b"a",
     "Grateful Dead 1977-05-08 (Disc 2)/t1.flac": b"b"},
    {"CD1/t1.flac": b"a", "CD2/t1.flac": b"b", "artwork/front.jpg": b"c"},
    {"show/t1.flac": b"a", "Artwork/front.jpg": b"c"},       # one show with extras
    {"t1.flac": b"a", "t2.flac": b"b", "info.txt": b"i"},     # loose files
])
def test_one_show_goes_in_one_folder_named_after_the_zip(tmp_path, members):
    z = _zip(tmp_path / "Dead" / "gd1977-05-08.zip", members)
    assert [t.name for t in unpack.plan_zip(z).targets] == ["gd1977-05-08"]


def test_a_zip_is_refused_when_only_some_of_its_folders_exist(tmp_path):
    z = _zip(tmp_path / "Dead" / "two.zip",
             {"gd1977-05-07/t1.flac": b"a", "gd1977-05-08/t1.flac": b"b"})
    (tmp_path / "Dead" / "gd1977-05-08").mkdir()
    zp = unpack.plan_zip(z)
    assert zp.status == unpack.REFUSED and "gd1977-05-08" in zp.reason


def test_a_failed_move_puts_every_extracted_folder_back(tmp_path, monkeypatch):
    z = _zip(tmp_path / "Dead" / "two.zip",
             {"gd1977-05-07/t1.flac": b"a", "gd1977-05-08/t1.flac": b"b"})
    zp = unpack.plan_zip(z)
    real = unpack.os.rename

    def fail_second(src, dst):
        if str(dst).endswith("gd1977-05-08"):
            raise PermissionError("locked")
        real(src, dst)

    monkeypatch.setattr(unpack.os, "rename", fail_second)
    with pytest.raises(PermissionError):
        unpack.extract(zp)
    assert sorted(p.name for p in (tmp_path / "Dead").iterdir()) == ["two.zip"]
