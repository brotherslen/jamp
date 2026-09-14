"""Builds a miniature copy of the real library for tests and demo runs.

The audio files are real enough for mutagen: a valid fLaC STREAMINFO block with
no audio frames, and a single silent MPEG-1 Layer III frame.  SHN files are
opaque bytes, which is exactly how the pipeline has to treat them anyway.
"""
from __future__ import annotations

import os
from pathlib import Path

from jamp.winpath import opener


def _mkdirs(folder: Path) -> None:
    """mkdir that also works past the Windows path limit."""
    os.makedirs(opener(folder), exist_ok=True)


def _streaminfo(rate: int, channels: int, bits: int, samples: int = 0) -> bytes:
    bit_string = (
        format(4096, "016b")          # min blocksize
        + format(4096, "016b")        # max blocksize
        + format(0, "024b")           # min framesize (unknown)
        + format(0, "024b")           # max framesize (unknown)
        + format(rate, "020b")
        + format(channels - 1, "03b")
        + format(bits - 1, "05b")
        + format(samples, "036b")
    )
    body = int(bit_string, 2).to_bytes(len(bit_string) // 8, "big")
    return body + b"\x00" * 16       # md5 of the unencoded audio


def make_flac(path: Path, bits: int = 16, rate: int = 44100, tags: dict | None = None) -> Path:
    _mkdirs(path.parent)
    info = _streaminfo(rate, 2, bits)
    header = b"fLaC" + bytes([0x80]) + len(info).to_bytes(3, "big") + info
    open(opener(path), 'wb').write(header)
    if tags:
        from mutagen.flac import FLAC

        audio = FLAC(opener(path))
        for key, value in tags.items():
            audio[key] = [str(value)]
        audio.save()
    return path


_ID3_FRAMES = {
    "ARTIST": "TPE1", "ALBUMARTIST": "TPE2", "ALBUM": "TALB", "TITLE": "TIT2",
    "TRACKNUMBER": "TRCK", "DISCNUMBER": "TPOS", "DATE": "TDRC", "GENRE": "TCON",
    "COPYRIGHT": "TCOP", "PUBLISHER": "TPUB", "ENCODEDBY": "TENC",
}


def make_mp3(path: Path, tags: dict | None = None) -> Path:
    _mkdirs(path.parent)
    # MPEG-1 Layer III, 128 kbps, 44.1 kHz, stereo -> 417 byte frames.
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    open(opener(path), 'wb').write(frame * 8)
    if tags:
        from mutagen import id3

        frames = id3.ID3()
        for key, value in tags.items():
            key = key.upper()
            if key == "COMMENT":
                frames.add(id3.COMM(encoding=3, lang="eng", desc="", text=[str(value)]))
                continue
            frame_id = _ID3_FRAMES.get(key)
            if frame_id is None:
                frames.add(id3.TXXX(encoding=3, desc=key, text=[str(value)]))
                continue
            frames.add(getattr(id3, frame_id)(encoding=3, text=[str(value)]))
        frames.save(opener(path), v2_version=3)
    return path


# 0.1 s of silent AAC in an MP4 container, made once with ffmpeg so the tests do
# not need it.  mutagen needs a real moov atom to open the file at all.
_TINY_M4A = (
    "AAAAHGZ0eXBNNEEgAAACAE00QSBpc29taXNvMgAAAAhmcmVlAAAAH21kYXTcAExhdmM2My4xLjEw"
    "MQACMEAOARggBwAAAt5tb292AAAAbG12aGQAAAAAAAAAAAAAAAAAAB9AAAADIAABAAABAAAAAAAA"
    "AAAAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAACAAACLXRyYWsAAABcdGtoZAAAAAMAAAAAAAAAAAAAAAEAAAAAAAADIAAAAAAA"
    "AAAAAAAAAQEAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAA"
    "ACRlZHRzAAAAHGVsc3QAAAAAAAAAAQAAAyAAAAQAAAEAAAAAAaVtZGlhAAAAIG1kaGQAAAAAAAAA"
    "AAAAAAAAAB9AAAAHIFXEAAAAAAAtaGRscgAAAAAAAAAAc291bgAAAAAAAAAAAAAAAFNvdW5kSGFu"
    "ZGxlcgAAAAFQbWluZgAAABBzbWhkAAAAAAAAAAAAAAAkZGluZgAAABxkcmVmAAAAAAAAAAEAAAAM"
    "dXJsIAAAAAEAAAEUc3RibAAAAGpzdHNkAAAAAAAAAAEAAABabXA0YQAAAAAAAAABAAAAAAAAAAAA"
    "AQAQAAAAAB9AAAAAAAA2ZXNkcwAAAAADgICAJQABAASAgIAXQBUAAAAAAD6AAAADJwWAgIAFFYhW"
    "5QAGgICAAQIAAAAgc3R0cwAAAAAAAAACAAAAAQAABAAAAAABAAADIAAAABxzdHNjAAAAAAAAAAEA"
    "AAABAAAAAgAAAAEAAAAcc3RzegAAAAAAAAAAAAAAAgAAABMAAAAEAAAAFHN0Y28AAAAAAAAAAQAA"
    "ACwAAAAac2dwZAEAAAByb2xsAAAAAgAAAAH//wAAABxzYmdwAAAAAHJvbGwAAAABAAAAAgAAAAEA"
    "AAA9dWR0YQAAADVtZXRhAAAAAAAAACFoZGxyAAAAAAAAAABtZGlyYXBwbAAAAAAAAAAAAAAAAAhp"
    "bHN0"
)


def make_m4a(path: Path) -> Path:
    import base64

    _mkdirs(path.parent)
    open(opener(path), "wb").write(base64.b64decode(_TINY_M4A))
    return path


def make_wav(path: Path) -> Path:
    import wave

    _mkdirs(path.parent)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)
    return path


def make_shn(path: Path) -> Path:
    _mkdirs(path.parent)
    open(opener(path), "wb").write(b"ajkg\x02" + b"\x00" * 64)
    return path


def write(path: Path, text: str) -> Path:
    _mkdirs(path.parent)
    open(opener(path), "w", encoding="utf-8").write(text)
    return path


INFO_TORRENT = """My Morning Jacket
Bonnaroo Music Festival
Manchester, TN
June 16, 2006

Source: DPA 4011s > Lunatec V2 > D8
Taper: Charlie Miller
Lineage: DAT(44.1) > CDR > EAC > SHN
Transferred by: Charlie Miller

Set 1:
01. Wordless Chorus
02. It Beats 4 U
03. Gideon
04. One Big Holiday
"""

UMLIVE_TRACKS = [
    (1, "Catshot >"),
    (2, "All In Time"),
    (3, "Preamble > Mantis >"),
    (4, "Bridgeless"),
]

NEWS_CLIPPING = """Local band packs the house

By a staff reporter.  The show was reviewed in the paper on 10-31-91 and
the crowd was said to be enormous.  Nothing here describes a lineage.
"""

TORRENT_STUB = "Torrent downloaded from jamp.org\n"

# A real XLD rip log: it names the show, but its own version string and
# extraction timestamp are date-shaped and are not the show.
XLD_LOG = """X Lossless Decoder version 20141129 (148.1)

XLD extraction logfile from 2015-11-01 12:41:08 -0500

Grateful Dead / Dave's Picks Volume 16 - 1973-03-28 Springfield Civic Center, Springfield, MA

Used drive : MATSHITA DVD-R   UJ-8A8 (revision HB14)
Media type : Pressed CD

Ripper mode             : XLD Secure Ripper
Read offset correction  : 102

TOC of the extracted CD
     Track |   Start  |  Length  | Start sector | End sector
    ---------------------------------------------------------
        1  | 00:00:00 | 06:07:48 |         0    |    27572
        2  | 06:07:48 | 08:46:54 |     27573    |    67076
"""


def build_library(root: Path) -> Path:
    """A miniature library built from the verbatim folder names in the brief."""
    root = Path(root)

    # --- My Morning Jacket ------------------------------------------------
    mmj = root / "My Morning Jacket"

    d = mmj / "mmj2003-09-26.shnf"
    for i in (1, 2):
        make_shn(d / ("mmj2003-09-26d1t%02d.shn" % i))
    write(d / "mmj2003-09-26.txt",
          "My Morning Jacket\nMurat Egyptian Room\nIndianapolis, IN\n2003-09-26\n\n"
          "Source: SBD > DAT\nTaper: Charlie Miller\nLineage: DAT > CDR > SHN\n")
    write(d / "mmj2003-09-26.md5", "")

    d = mmj / "mmj2005-06-04.ak40.flac16"
    for i in (1, 2, 3):
        make_flac(d / ("mmj2005-06-04d1t%02d.flac" % i), bits=16)

    d = mmj / "MMJ2006-06-16..4011s bonaroo"
    for i in (1, 2, 3, 4):
        make_flac(d / ("mmj2006-06-16d1t%02d.flac" % i), bits=16)
    write(d / "info.txt", INFO_TORRENT)
    write(d / "band comments.txt", NEWS_CLIPPING)
    write(d / "Torrent downloaded from jamp.org.txt", TORRENT_STUB)
    write(
        d / "fingerprint.ffp.txt",
        "".join("mmj2006-06-16d1t%02d.flac:%s\n" % (i, "0" * 32) for i in (1, 2, 3, 4)),
    )
    write(
        d / "mmj2006-06-16d1.md5",
        "".join("%s *mmj2006-06-16d1t%02d.flac\n" % ("0" * 32, i) for i in (1, 2, 3, 4)),
    )

    d = mmj / "mmj2006-12-01.Electric_Factory_WXPN_FM_SBD"
    for i in (1, 2):
        make_flac(d / ("mmj2006-12-01d1t%02d.flac" % i), bits=16)
    write(d / "info.txt", "Source: WXPN FM broadcast\nLineage: FM > DAT > CDR\n")

    d = mmj / "MMJ2012-09-12.MMJ-Wiltern-9-12-12"
    for i in (1, 2):
        make_flac(d / ("mmj120912d1t%02d.flac" % i), bits=24, rate=48000)

    d = mmj / "My Morning Jacket 2023-11-03 Fox Theatre, Atlanta, GA [FLAC24]"
    titles = ["Mahgeetah", "Off The Record", "Golden", "Steam Engine"]
    for i, title in enumerate(titles, start=1):
        make_flac(
            d / ("%02d %s.flac" % (i, title)), bits=24, rate=96000,
            tags={
                "ARTIST": "My Morning Jacket",
                "ALBUMARTIST": "My Morning Jacket",
                "ALBUM": "2023-11-03 Fox Theatre, Atlanta, GA",
                "TITLE": title,
                "TRACKNUMBER": str(i),
                "DISCNUMBER": "1",
                "DATE": "2023-11-03",
                "COMMENT": "Purchased from nugs.net",
            },
        )
    write(d / "folder.jpg", "")

    d = mmj / "My Morning Jacket- iTunes Session(Christmas)"
    for i, title in enumerate(["Xmas Curtain", "Christmas Time Is Here"], start=1):
        make_flac(d / ("%02d %s.flac" % (i, title)), bits=16,
                  tags={"ARTIST": "My Morning Jacket", "ALBUM": "iTunes Session",
                        "TITLE": title, "TRACKNUMBER": str(i), "DATE": "2011"})

    # --- Umphrey's McGee --------------------------------------------------
    um = root / "Umphrey's McGee"

    d = um / "um2001-06-02.shnf"
    for i in (1, 2):
        make_shn(d / ("um2001-06-02d1t%02d.shn" % i))
    write(d / "um2001-06-02d1.md5", "%s *um2001-06-02d1t01.shn\n" % ("0" * 32))

    d = um / "um2004-06-11.mk4minime.flac16"
    for i in (1, 2):
        make_flac(d / ("um2004-06-11d1t%02d.flac" % i), bits=16)

    # Nested: the container has no audio, the show is one level down.  Modelled
    # on the real files - an official UMLive download whose DATE tag is the bare
    # year 2011 while ALBUM carries the actual show date.
    d = um / "u111105" / "2011_11_05 Eagles Ballroom - Milwaukee, WI"
    for num, title in UMLIVE_TRACKS:
        make_mp3(
            d / ("um111105d1_%02d_%s.mp3" % (num, title.split(" >")[0].replace(" ", "_"))),
            tags={
                "ARTIST": "Umphrey's McGee",
                "ALBUMARTIST": "Umphrey's McGee",
                "ALBUM": "2011/11/05 Eagles Ballroom - Milwaukee, WI",
                "TITLE": title,
                "TRACKNUMBER": str(num),
                "DISCNUMBER": "1",
                "DATE": "2011",
                "GENRE": "Jam",
                "COPYRIGHT": "2011, Umphrey's McGee",
                "COMMENT": "UMLive",
            },
        )

    d = um / "UM Summer Camp 5-28-11"
    for i in (1, 2):
        make_flac(d / ("um2011-05-28d1t%02d.flac" % i), bits=16)

    d = um / "Huey Lewis and the rUMors Summer Camp 5-29-11"
    for i in (1, 2):
        make_flac(d / ("hlr2011-05-29d1t%02d.flac" % i), bits=16)

    d = um / "om2011-07-30.CA-11.flac16"
    for i in (1, 2):
        make_flac(d / ("om2011-07-30d1t%02d.flac" % i), bits=16)

    d = um / "OHMphrey - Posthaste"
    for i, title in enumerate(["Posthaste", "Reprise"], start=1):
        make_flac(d / ("%02d %s.flac" % (i, title)), bits=16,
                  tags={"ARTIST": "OHMphrey", "ALBUM": "Posthaste", "TITLE": title,
                        "TRACKNUMBER": str(i), "DATE": "2011"})

    d = um / "UM - Hauntlanta"
    for i in (1, 2):
        make_flac(d / ("track%02d.flac" % i), bits=16)

    d = um / "Umphreys McGee Bonnaroo 2008"
    for i in (1, 2):
        make_flac(d / ("d1t%02d.flac" % i), bits=16)

    # --- Grateful Dead ----------------------------------------------------
    gd = root / "grateful dead"

    for setno in (1, 2):
        d = gd / ("gd1973-12-10 s%d" % setno)
        for i in (1, 2):
            make_flac(d / ("gd1973-12-10s%dt%02d.flac" % (setno, i)), bits=16)
        write(d / "gd1973-12-10.txt",
              "Grateful Dead\nCharlotte Coliseum\nCharlotte, NC\n12-10-73\n\n"
              "Source: SBD > Reel > DAT\nTaper: Charlie Miller\n")

    d = gd / "Grateful Dead 10-31-91"
    for i in (1, 2):
        make_flac(d / ("gd1991-10-31d1t%02d.flac" % i), bits=16)
    write(d / "news clipping.txt", NEWS_CLIPPING)

    # A single-show volume of a series that often spans several nights, ripped
    # by XLD - whose log names the show but is also full of its own dates.
    d = gd / "Dave's Picks 16 [FLAC]"
    box = [("101 Cumberland Blues.flac", 1, 1, "Cumberland Blues"),
           ("102 Bertha.flac", 1, 2, "Bertha"),
           ("201 Playing In The Band.flac", 2, 1, "Playing In The Band")]
    for fname, disc, track, title in box:
        make_flac(d / fname, bits=16,
                  tags={"ARTIST": "Grateful Dead", "ALBUMARTIST": "Grateful Dead",
                        "ALBUM": "Dave's Picks Volume 16", "TITLE": title,
                        "TRACKNUMBER": str(track), "DISCNUMBER": str(disc),
                        "DATE": "2015"})
    write(d / "Dave's Picks Volume 16 Disc 1.log", XLD_LOG)

    # --- Phish ------------------------------------------------------------
    ph = root / "phish"

    d = ph / "ph2018-12-28.New.York.NY.padelimike.akg414.flac2496"
    for i in (1, 2, 3):
        make_flac(d / ("ph2018-12-28d1t%02d.flac" % i), bits=24, rate=96000)
    write(d / "info.txt",
          "Phish\nMadison Square Garden\nNew York, NY\n2018-12-28\n\n"
          "Source: AKG 414 > SD744\nTaper: padelimike\nLineage: 24/96 > FLAC\n")
    write(d / "ph2018-12-28.ffp",
          "".join("ph2018-12-28d1t%02d.flac:%s\n" % (i, "0" * 32) for i in (1, 2, 3)))

    d = ph / "ph2018-12-28 Madison Square Garden, New York, NY [FLAC]"
    for i, title in enumerate(["Blaze On", "Everything's Right", "Simple"], start=1):
        make_flac(d / ("%02d %s.flac" % (i, title)), bits=16,
                  tags={"ARTIST": "Phish", "ALBUMARTIST": "Phish",
                        "ALBUM": "2018-12-28 Madison Square Garden, New York, NY",
                        "TITLE": title, "TRACKNUMBER": str(i), "DISCNUMBER": "1",
                        "DATE": "2018-12-28", "COMMENT": "LivePhish.com download"})

    d = ph / "Phish 12-29-18 MTX"
    for i in (1, 2):
        make_flac(d / ("ph2018-12-29_mtx_%02d.flac" % i), bits=24, rate=48000)
    write(d / "info.txt",
          "Phish 12-29-18\nMadison Square Garden, New York, NY\n\n"
          "Source: Matrix of AKG414 audience and soundboard\nTaper: padelimike\n")

    d = ph / "Phish - 2012-06-28 Noblesville, IN (v0)"
    for i, title in enumerate(["Kill Devil Falls", "Roggae"], start=1):
        make_mp3(d / ("%d-%02d %s.mp3" % (1, i, title)),
                 tags={"ARTIST": "Phish", "ALBUM": "2012-06-28 Klipsch, Noblesville, IN",
                       "TITLE": title, "TRACKNUMBER": str(i), "DATE": "2012-06-28"})

    return root
