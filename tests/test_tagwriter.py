"""The backup is the undo for the one change that cannot be reversed from a log.

Every container has to come back out of a restore exactly as it went in, and
the container itself has to survive the trip.
"""
import json

import mutagen
import pytest
from mutagen import id3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

import fixtures
from jamp.tagwriter import (BACKUP_NAME, TagWriteError, restore_from_backup,
                             write_backup, write_tags)

CHANGES = {"TITLE": "Changed", "VENUE": "Somewhere Else", "TRACKNUMBER": "7",
           "ALBUM": "2001-01-01"}


def snapshot(path):
    return {str(k): repr(v) for k, v in mutagen.File(str(path)).tags.items()}


def _rich_mp3(path):
    fixtures.make_mp3(path)
    t = id3.ID3()
    t.add(id3.TIT2(encoding=3, text=["Bertha"]))
    t.add(id3.TXXX(encoding=3, desc="SOURCE", text=["sbd"]))
    t.add(id3.TXXX(encoding=3, desc="TAPER", text=["miller"]))
    t.add(id3.COMM(encoding=3, lang="deu", desc="notes", text=["setlist"]))
    t.add(id3.APIC(encoding=1, mime="image/jpeg", type=3, desc="", data=b"\xff\xd8art"))
    t.save(str(path), v2_version=3)
    return path


def _rich_m4a(path):
    fixtures.make_m4a(path)
    m = MP4(str(path))
    m["\xa9nam"] = ["Bertha"]
    m["trkn"] = [(3, 18)]
    m["cpil"] = True
    m["tmpo"] = [120]
    m["covr"] = [MP4Cover(b"\xff\xd8art")]
    m["----:com.apple.iTunes:VENUE"] = [MP4FreeForm(b"Barton Hall")]
    m.save()
    return path


def _tagged_wav(path):
    fixtures.make_wav(path)
    w = mutagen.File(str(path))
    w.add_tags()
    w.tags.add(id3.TIT2(encoding=3, text=["Bertha"]))
    w.tags.add(id3.TXXX(encoding=3, desc="SOURCE", text=["aud"]))
    w.save()
    return path


@pytest.mark.parametrize("make", [_rich_mp3, _rich_m4a, _tagged_wav],
                         ids=["mp3", "m4a", "wav"])
def test_a_restore_puts_every_container_back_exactly(tmp_path, make):
    path = make(tmp_path / ("01" + {"_rich_mp3": ".mp3", "_rich_m4a": ".m4a",
                                    "_tagged_wav": ".wav"}[make.__name__]))
    before = snapshot(path)
    write_backup(tmp_path, [path])
    write_tags(path, CHANGES)
    assert snapshot(path) != before

    assert restore_from_backup(tmp_path) == 1
    assert snapshot(path) == before


def test_a_wav_is_still_a_wav_after_a_restore(tmp_path):
    """ID3.save on the path wrote a bare ID3 header over the RIFF container."""
    path = _tagged_wav(tmp_path / "01.wav")
    write_backup(tmp_path, [path])
    write_tags(path, CHANGES)
    restore_from_backup(tmp_path)
    assert path.read_bytes()[:4] == b"RIFF"
    assert mutagen.File(str(path)).tags["TIT2"].text == ["Bertha"]


def test_artwork_survives_a_restore_and_stays_out_of_the_backup(tmp_path):
    path = _rich_mp3(tmp_path / "01.mp3")
    write_backup(tmp_path, [path])
    write_tags(path, CHANGES)
    restore_from_backup(tmp_path)
    assert [f.data for f in id3.ID3(str(path)).getall("APIC")] == [b"\xff\xd8art"]
    assert b"\xff\xd8art" not in (tmp_path / BACKUP_NAME).read_bytes()


def test_a_backup_from_before_the_format_change_still_restores(tmp_path):
    """824 folders carry backups written as text.  They must keep working, and
    now recover what that text form mangled: TXXX names and COMM languages."""
    mp3 = _rich_mp3(tmp_path / "01.mp3")
    m4a = _rich_m4a(tmp_path / "02.m4a")
    before = {p.name: snapshot(p) for p in (mp3, m4a)}
    legacy = {
        "01.mp3": {"__kind__": "id3", "frames": {
            "TIT2": ["Bertha"], "TXXX:SOURCE": ["sbd"], "TXXX:TAPER": ["miller"],
            "COMM:notes:deu": ["setlist"],
            "APIC:": ["APIC(encoding=<Encoding.UTF16: 1>, mime='image/jpeg', ...)"]}},
        "02.m4a": {"__kind__": "vorbis", "fields": {
            "\xa9nam": ["Bertha"], "trkn": ["(3, 18)"], "cpil": ["True"],
            "tmpo": ["120"], "covr": ["b'\\xff\\xd8art'"],
            "----:com.apple.iTunes:VENUE": ["b'Barton Hall'"]}},
    }
    (tmp_path / BACKUP_NAME).write_text(
        json.dumps({"folder": str(tmp_path), "files": legacy}), encoding="utf-8")
    for p in (mp3, m4a):
        write_tags(p, CHANGES)

    assert restore_from_backup(tmp_path) == 2
    after_mp3 = id3.ID3(str(mp3))
    assert after_mp3.getall("TXXX:SOURCE")[0].text == ["sbd"]
    assert after_mp3.getall("TXXX:TAPER")[0].text == ["miller"]
    assert not after_mp3.getall("TXXX:VENUE")
    assert after_mp3.getall("COMM")[0].lang == "deu"
    assert [f.data for f in after_mp3.getall("APIC")] == [b"\xff\xd8art"]
    assert snapshot(m4a) == before["02.m4a"]


def test_a_backup_that_cannot_be_read_stops_before_any_file_is_touched(tmp_path):
    """Half a folder restored is worse than none: the caller cannot tell which."""
    good = _rich_mp3(tmp_path / "01.mp3")
    odd = _rich_mp3(tmp_path / "02.mp3")
    write_backup(tmp_path, [good, odd])
    payload = json.loads((tmp_path / BACKUP_NAME).read_text(encoding="utf-8"))
    payload["files"]["02.mp3"]["__kind__"] = "mp4"
    (tmp_path / BACKUP_NAME).write_text(json.dumps(payload), encoding="utf-8")
    write_tags(good, CHANGES)
    changed = snapshot(good)

    with pytest.raises(TagWriteError):
        restore_from_backup(tmp_path)
    assert snapshot(good) == changed
