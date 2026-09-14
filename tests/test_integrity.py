"""Tests for the decode-against-the-stored-fingerprint check.

The interesting case is not a broken file - it is a file that decodes perfectly
well and is still not the audio it claims to be.  That is the class a header
check calls healthy, and it is what this tool exists to catch, so it is tested
against a real encoder rather than a hand-built stub wherever one is available.
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jamp import integrity  # noqa: E402


def _ffmpeg():
    ff = integrity.find_ffmpeg()
    try:
        subprocess.run([ff, "-version"], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return ff


FFMPEG = _ffmpeg()
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")


@pytest.fixture(scope="module")
def a_flac(tmp_path_factory):
    """Two seconds of a tone, encoded by a real encoder."""
    d = tmp_path_factory.mktemp("audio")
    out = d / "tone.flac"
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=2", "-c:a", "flac", str(out)],
        check=True, capture_output=True)
    return out


# --- reading the fingerprint ------------------------------------------------

@needs_ffmpeg
def test_streaminfo_is_read_without_mutagen(a_flac):
    si = integrity.flac_streaminfo(a_flac)
    assert si is not None
    assert si["rate"] == 44100
    assert 1.9 < si["seconds"] < 2.1
    assert si["md5"] and si["md5"] != integrity.ZERO_MD5


def test_a_file_that_is_not_flac_reads_as_nothing(tmp_path):
    p = tmp_path / "notflac.flac"
    p.write_bytes(b"ID3\x04\x00" + bytes(200))
    assert integrity.flac_streaminfo(p) is None


def test_streaminfo_survives_a_broken_later_block(tmp_path, a_flac):
    """The whole point of reading it by hand.

    Mutagen refuses a file whose block chain degenerates - which is the state of
    every file this was written for - so STREAMINFO has to be read at its fixed
    offset rather than through a parser that walks the chain.
    """
    if FFMPEG is None:
        pytest.skip("ffmpeg is not installed")
    good = a_flac.read_bytes()
    # keep fLaC + STREAMINFO, then rubble: zero-length headers that never end
    broken = tmp_path / "broken.flac"
    broken.write_bytes(good[:42] + bytes(4096))
    si = integrity.flac_streaminfo(broken)
    assert si is not None
    assert si["md5"] == integrity.flac_streaminfo(a_flac)["md5"]


# --- the four verdicts ------------------------------------------------------

@needs_ffmpeg
def test_an_intact_file_passes(a_flac, tmp_path):
    v = integrity.verify(FFMPEG, a_flac)
    assert v["status"] == integrity.PASS
    assert v["stored_md5"] == v["decoded_md5"]


@needs_ffmpeg
def test_silent_corruption_is_a_mismatch_not_a_pass(a_flac, tmp_path):
    """A file that decodes cleanly to the wrong audio.

    This is the Phish 2018-12-28 case: valid header, readable tags, plays in any
    player, and 60% of the audio replaced by silence.  It must not come back
    PASS, and it must not come back UNREADABLE either - saying "unreadable"
    about a file that reads perfectly well sends somebody looking for the wrong
    problem.
    """
    good = a_flac.read_bytes()
    si = integrity.flac_streaminfo(a_flac)
    # Re-encode different audio, then graft the original STREAMINFO onto it so
    # the file promises audio it does not contain.
    other = tmp_path / "other.flac"
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=880:duration=2", "-c:a", "flac", str(other)],
        check=True, capture_output=True)
    forged = tmp_path / "forged.flac"
    ob = other.read_bytes()
    forged.write_bytes(ob[:8] + good[8:8 + 34] + ob[8 + 34:])

    v = integrity.verify(FFMPEG, forged)
    assert v["status"] == integrity.MISMATCH
    assert v["stored_md5"] == si["md5"]
    assert v["decoded_md5"] and v["decoded_md5"] != v["stored_md5"]


@needs_ffmpeg
def test_a_file_no_decoder_can_open_is_unreadable(tmp_path, a_flac):
    good = a_flac.read_bytes()
    p = tmp_path / "rubble.flac"
    p.write_bytes(good[:42] + bytes(200000))
    assert integrity.verify(FFMPEG, p)["status"] == integrity.UNREADABLE


@needs_ffmpeg
def test_an_unset_md5_is_reported_as_uncheckable_not_as_a_failure(tmp_path, a_flac):
    """Some encoders leave the field zeroed.

    "We cannot check this" and "this failed" are different statements, and
    collapsing them would put clean files on a damage report.
    """
    b = bytearray(a_flac.read_bytes())
    b[8 + 18:8 + 34] = bytes(16)
    p = tmp_path / "nomd5.flac"
    p.write_bytes(bytes(b))
    assert integrity.verify(FFMPEG, p)["status"] == integrity.NO_MD5


@needs_ffmpeg
def test_embedded_cover_art_does_not_fail_the_audio(tmp_path, a_flac):
    """A damaged picture block is not a damaged recording.

    Without `-map 0:a` ffmpeg decodes the artwork too, and one of the files this
    was written for failed its whole decode on a malformed PNG while its audio
    was fine.
    """
    png = tmp_path / "art.png"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi",
                    "-i", "color=c=red:s=16x16:d=1", "-frames:v", "1", str(png)],
                   check=True, capture_output=True)
    withart = tmp_path / "withart.flac"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(a_flac), "-i", str(png),
                    "-map", "0:a", "-map", "1:v", "-c", "copy",
                    "-disposition:v", "attached_pic", str(withart)],
                   check=True, capture_output=True)
    assert integrity.verify(FFMPEG, withart)["status"] == integrity.PASS


@needs_ffmpeg
@pytest.mark.parametrize("depth,fmt", [(16, "s16"), (24, "s32")])
def test_every_bit_depth_passes_a_file_that_is_fine(tmp_path, depth, fmt):
    """The bug this suite was written blind to.

    FLAC hashes its samples packed - three bytes each at 24 bits - while ffmpeg
    decodes anything above 16 bits to s32, four bytes each.  Comparing the two
    fails every time, so a whole healthy 24-bit folder reported 23/23 MISMATCH
    and looked exactly like catastrophic damage.

    It survived the first round of tests because every control happened to be
    16-bit, which is the only depth where the two layouts agree.  Parametrising
    on depth is the fix: a check that cannot tell a good file from a bad one at
    some depth is worse than no check, because it is believed.
    """
    p = tmp_path / ("tone%d.flac" % depth)
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", "-sample_fmt", fmt,
         "-c:a", "flac", str(p)],
        check=True, capture_output=True)
    si = integrity.flac_streaminfo(p)
    assert si["depth"] == depth, "fixture did not produce %d-bit audio" % depth
    assert integrity.verify(FFMPEG, p)["status"] == integrity.PASS


@needs_ffmpeg
def test_an_accented_path_is_checked_not_crashed_on(tmp_path, a_flac):
    """"Barceló Maya Beach" killed the run before a single file was checked.

    A Windows console defaults to cp1252, which cannot encode the combining
    acute in that folder's name, so printing the path raised.  The library has
    such names, so this is an ordinary case rather than an edge one.
    """
    d = tmp_path / "Barceló Maya Beach"
    d.mkdir()
    p = d / "Tweezer é.flac"
    p.write_bytes(a_flac.read_bytes())
    assert integrity.verify(FFMPEG, p)["status"] == integrity.PASS


@needs_ffmpeg
def test_an_id3_tag_in_front_of_the_flac_is_skipped(tmp_path, a_flac):
    """Legal, common, and it called two intact shows unreadable.

    Plenty of taggers write an ID3v2 tag before the FLAC marker. audio.py's
    structural check has always known to look past one; this did not, and a
    full verification pass reported 45 files of two LivePhish Hampton shows as
    having no FLAC marker at all when every one of them was fine.
    """
    good = a_flac.read_bytes()
    # ID3v2.3, no flags, four syncsafe length bytes, then the real file.
    payload = b"\x00" * 100
    size = len(payload)
    header = (b"ID3\x03\x00\x00"
              + bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F,
                       (size >> 7) & 0x7F, size & 0x7F]))
    p = tmp_path / "tagged.flac"
    p.write_bytes(header + payload + good)

    si = integrity.flac_streaminfo(p)
    assert si is not None, "the marker is past the tag, not missing"
    assert si["md5"] == integrity.flac_streaminfo(a_flac)["md5"]
    assert integrity.verify(FFMPEG, p)["status"] == integrity.PASS


def test_a_file_that_really_has_no_marker_is_still_unreadable(tmp_path):
    """The guard must not start finding markers that are not there."""
    p = tmp_path / "rubbish.flac"
    p.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x0a" + b"\x00" * 10 + b"nonsense")
    assert integrity.flac_streaminfo(p) is None


def test_ffmpeg_is_given_the_plain_path_even_past_the_limit(tmp_path, monkeypatch):
    """ffmpeg refuses the \\?\ form ("Error opening input: Invalid argument"),
    so a long path would have reported a healthy file UNREADABLE."""
    import subprocess
    from types import SimpleNamespace

    from jamp import integrity

    deep = tmp_path
    for part in ("a" * 60, "b" * 60, "c" * 60, "d" * 60):
        deep = deep / part
    path = deep / "t01.flac"
    seen = {}

    def fake_run(cmd, **kw):
        seen["input"] = cmd[cmd.index("-i") + 1]
        return SimpleNamespace(stdout="MD5=" + "0" * 32, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    integrity.decode_md5("ffmpeg", path, 16)
    assert seen["input"] == str(path)
    assert not seen["input"].startswith("\\?\\")
