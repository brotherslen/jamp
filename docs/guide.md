# The jamp guide

For collectors of live recordings: how to set the tool up, run it on your
library one act at a time, read what it proposes, and commit it. What each
command and flag does is listed in [reference.md](reference.md); how the tool
decides things is in [rules.md](rules.md).

- [1. What it does to a library](#1-what-it-does-to-a-library)
- [2. Before you start](#2-before-you-start)
- [3. Install and set up](#3-install-and-set-up)
- [4. ZIP files and SHN](#4-zip-files-and-shn)
- [5. Tell it which folder is which act](#5-tell-it-which-folder-is-which-act)
- [6. Take stock](#6-take-stock)
- [7. Plan one act](#7-plan-one-act)
- [8. Commit](#8-commit)
- [9. Answers the files do not contain](#9-answers-the-files-do-not-contain)
- [10. Split shows, wrappers and lossy copies](#10-split-shows-wrappers-and-lossy-copies)
- [11. Filling gaps from the internet](#11-filling-gaps-from-the-internet)
- [12. Is a recording missing songs?](#12-is-a-recording-missing-songs)
- [13. Checking the audio itself (optional)](#13-checking-the-audio-itself-optional)
- [14. Undoing a commit](#14-undoing-a-commit)
- [15. When something goes wrong](#15-when-something-goes-wrong)

---

## 1. What it does to a library

It reads each show folder - the folder name, the track filenames, the tags, the
info file, the checksum files - works out the act, the date, the source, the
taper and the venue, and proposes one consistent name and one consistent set of
tags:

    Grateful Dead 5-8-77 Cornell SBD (Miller)/01 - New Minglewood Blues.flac
    ->
    gd1977-05-08.sbd.miller.flac16 - Barton Hall, Ithaca, NY/gd1977-05-08d1t01.flac

(the venue coming from the folder's info file), with ARTIST, ALBUM, DATE, VENUE, TRACKNUMBER, DISCNUMBER, TITLE and GENRE
written to match, and the folder's `.ffp`, `.md5`, `.st5` and `.cue` files
rewritten to the new filenames.

Official releases keep their product names (`Dave's Picks 16`, `30 Trips Around
The Sun`), and their own tags are not overwritten.

What it will never do:

- **write anything without `--commit`.** Every command is a dry run first.
- **delete anything.** A few opt-in operations move files - into another show
  folder, or aside into `_etree_review/` - but nothing removes them.
- **guess a date, or an act.** A folder it is not sure about is reported and
  left exactly as it is.
- **put a report inside your library.** Reports go to a folder outside it.

## 2. Before you start

- **Back your library up.** The tool backs up every tag it changes and logs
  every rename, but a copy you made yourself is the one to trust.
- **Expect to work one act at a time.** A first run over a whole library
  proposes thousands of changes, and nobody reads thousands of changes.
- **Expect to read.** The dry run is the product. The commit only carries out
  what you have already read and agreed to.
- **Windows is where it has been used day to day.** Its tests run on macOS
  and Linux on every change, but few libraries have been through it there yet.

## 3. Install and set up

Three ways, all giving the same `jamp` command:

- **The standalone download** - one program for Windows, macOS or Linux, with
  nothing else to install. Unzip it, open a terminal in that folder, and run
  `jamp init` (`.\jamp init` on Windows, `./jamp init` elsewhere).
- **With Python** 3.11 or newer, inside a copy of the code: `pip install .`,
  then `jamp init`.
- **With Docker**, for a library on a NAS: see [docker.md](docker.md).

`jamp init` asks two things:

1. **Where your library is** - the folder that holds your act folders.
2. **Where reports should go** - any folder outside the library.

It creates your **user folder** - where your own settings live, apart from the
tool - and remembers both paths, so later commands can leave them out. It does
not read or change anything in the library. At the end it runs `jamp doctor`.

`jamp doctor` checks the setup and changes nothing. Run it whenever something
seems off:

```
jamp doctor
  ok    python             3.11.9
  ok    mutagen            1.48.1
  ok    PyYAML             6.0.3
  ok    your folder        C:\Users\you\AppData\Roaming\jamp
  ok    config             36 acts, 250 venues, with yours laid over
  ok    overrides          0 from ...\jamp\overrides.yaml
  ok    library            E:\Music\Live - 37 top-level folders
  ok    reports            C:\Users\you\jamp-reports - writable
  ok    ffmpeg             ffmpeg version 7.1
Ready.
```

A `warn` line is worth reading but does not stop anything; a `FAIL` does.

**ffmpeg** is needed to convert SHN files (section 4) and for the optional
audio check (section 13), and is not included with the tool (the Docker image
is the exception). Download it from https://ffmpeg.org/download.html - on Windows
`winget install Gyan.FFmpeg`, or put `ffmpeg.exe` in the same folder as the
standalone `jamp`; on macOS `brew install ffmpeg`; on Linux your
package manager.

## 4. ZIP files and SHN

Two things hide shows from everything else, and both are dealt with first.

**Shows still in ZIP files.**

```bash
jamp unpack
```

lists every ZIP in the library and where its contents would go. Everything is
extracted beside the ZIP, laid out by what is inside:

| the ZIP holds | extracted as |
| --- | --- |
| one folder | that folder |
| disc folders (`Disc 1`, `CD2`, `... (Disc 1)`), or tracks loose at the top | one show: a folder named after the ZIP |
| two or more folders of audio that are not discs | separate shows: each folder beside the ZIP; any loose files (artwork, text) in a folder named after the ZIP |
| one folder of audio beside other folders (`Artwork`) | everything in a folder named after the ZIP |

Nothing is overwritten - a ZIP is left alone if anything it would create
already exists, or reported as already extracted when all its files are
already there. A password-protected, damaged or unsafe ZIP is refused and
listed. RAR and 7z archives are listed too, for you to extract with 7-Zip.

When the list looks right:

```bash
jamp unpack --commit
```

Every file is checked against the checksum the ZIP carries, and a ZIP that fails
leaves nothing half-extracted behind. The ZIPs themselves stay where they are.

**Shows in SHN (Shorten).** Most players cannot open SHN and nothing can tag it,
so it is converted to FLAC:

```bash
jamp convert
jamp convert --commit
```

Each `.shn` gets a `.flac` of the same name beside it, and **a FLAC is kept only
if both files decode to exactly the same audio**, checked by decoding each one -
so the conversion is proven lossless, file by file. It takes a few seconds per
file (`--workers` sets how many at once). A folder where every file converted
gets a `.ffp` fingerprint file for the new FLACs. The folder's old `.md5` or
`.st5` files still name the `.shn` files and are left as they are.

`convert_summary.txt` lists every file: `converted`, `already converted` (a FLAC
with identical audio was already there), `MISMATCH` (the FLAC beside it holds
different audio - both left alone), or `FAILED` (the SHN does not decode).

**Removing the originals.** Once you are happy, run either command again with
`--set-aside`:

```bash
jamp convert --set-aside
jamp convert --set-aside --commit
```

This moves every SHN whose FLAC is proven - and, for `unpack`, every ZIP whose
files are all on disk with the right checksums - into `_etree_review/originals/`
at the top of your library, mirroring where each was. An SHN proven by an
earlier run and untouched since is moved straight away; anything else is
decoded and checked again first. **Nothing is deleted:** when you no longer want
the originals, delete that folder yourself.

`--artist` limits either command to one folder, as it does everywhere.

## 5. Tell it which folder is which act

The tool ships knowing a set of acts - the Grateful Dead and the Garcia
family, Phish and Trey, Umphrey's McGee, My Morning Jacket, the Disco Biscuits,
the Allman Brothers, Widespread Panic, King Gizzard and others. Every other act
in your library has to be added before its shows can be named, because the act
abbreviation starts every folder name (`gd1977-05-08...`) and the tool will not
invent one.

```bash
jamp acts
```

lists every top-level folder in your library and what the tool makes of it:

```
  Billy Strings                 13  UNKNOWN
  Grateful Dead                 32  Grateful Dead (gd)
  King Gizz                     19  shows name their act  inside: kglw 19
  Studio                         4  ignored - never scanned
  Various - Live                61  UNKNOWN
```

In a terminal it then goes through the unknown ones and asks, for each:

- **[a]dd** it as an act. You give the act's name and an **abbreviation**. The
  abbreviation is permanent in practice: it starts the name of every show by
  that act. Use what traders already use if there is a convention (`gd`, `ph`,
  `jgb`, `um`); otherwise something short and unmistakable. The tool refuses one
  that another act already uses, or that already means something in a folder
  name (`sbd`, `aud`, `flac`).
- **[i]gnore** it. The folder is never scanned: studio albums, compilations,
  photos, anything that is not live recordings.
- **[s]kip** it for now.

Your answers are saved to `acts.yaml` in your user folder. Nothing in the
library is touched.

Without a terminal (a script, a container), use the flags instead:

```bash
jamp acts --add "Billy Strings" --abbrev bs
jamp acts --ignore "Studio" --ignore "Compilations"
```

**Mixed folders** like `Various - Live` should not be added as an act. Shows
inside a folder like that are placed by their own names and tags; the ones the
tool cannot place are blocked and listed, never filed under the folder's name.

**Nicknames** like `King Gizz` need nothing: when a folder's own name says
nothing but the shows inside name a known act, `acts` says so.

For more than a name and an abbreviation - an act's other spellings, the years
it was active, side projects filed under a parent act - edit `jamp.yaml` in
your user folder; see [reference.md](reference.md#your-jampyaml).

## 6. Take stock

```bash
jamp phase0
```

walks the library and counts what is there: formats, how folders are named,
which acts and dates it can read, which folders are official releases, which
it cannot read at all. It changes nothing. Read `phase0_summary.txt` in your
reports folder.

Two reports are worth knowing about:

- `phase0_bands_stub.yaml` - a draft entry for every act it could not place,
  if you would rather write acts into `jamp.yaml` by hand.
- `phase0_same_audio.csv` - folders that share audio, found from the
  fingerprint every FLAC carries. A pair that shares every track is the same
  recording twice, whatever the names say.

## 7. Plan one act

```bash
jamp phase1 --artist "Grateful Dead"
```

`--artist` is the name of a top-level folder in your library. Leave it out and
the plan covers everything - useful later, overwhelming at first.

This is the **dry run**, and nothing is written to the library. Open
`phase1_summary.txt` in your reports folder. Every show folder gets one outcome:

| outcome | meaning | what to do |
| --- | --- | --- |
| `PLAN` | it will be renamed and retagged as shown | read the proposed name |
| `UNCHANGED` | it is already right | nothing |
| `SKIP_BLOCKED` | something is missing or contradictory; it will be left alone | see the reason below it |
| `DUPLICATE` | two folders are the same show and would get the same name | delete the copy you do not want, and run again |
| `COLLISION` | two different recordings would get the same name | usually two transfers of one night; rename one by hand or add an override |
| `MERGE` | one show split across sibling folders (`... Set 1`, `... Set 2`) | only happens with `--include-merges`; see section 10 |
| `SPLIT_SHOW` | a split show that cannot be merged safely | reported; sort it by hand |

**Read the proposed renames.** Each line says what it is based on:

```
  Grateful Dead\gd77-05-08 sbd miller
      -> gd1977-05-08.sbd.miller.flac16 - Barton Hall, Ithaca, NY   [UNOFFICIAL sbd, date confidence 90]
```

A wrong venue, a taper you know is wrong, a soundboard that is really an
audience tape - these are what the dry run is for. Fix the cause (section 9)
and run the dry run again until the plan is what you want.

**Blocked folders** say why. The common reasons:

| reason | meaning | the way through |
| --- | --- | --- |
| `NO_DATE` | no date anywhere it looked | an override with `date:` |
| `LOW_DATE_CONFIDENCE` | a date, but read one of two ways (`05-06-77`), or contradicted | an override with `date:` |
| `NO_BAND` | no act matches | `jamp acts`, or an override with `band:` |
| `ACT_NOT_CONFIGURED` | the tags name an act the tool does not know; only the folder it sits in says otherwise | add that act with `jamp acts`, or an override with `band:` if the folder is right |
| `ALBUM_NOT_A_SHOW` | it looks like a studio album or compilation | usually leave it; or add the folder to `ignore_folders` |
| `MULTI_DATE_RELEASE` | an official release spanning several nights with no per-show dates | leave it; releases keep their names |
| `CONTAINER_NOT_A_SHOW` | loose audio beside several show folders | file the loose tracks into a folder of their own |
| `UNREADABLE_AUDIO` | a file cannot be read at all | check that file; it may be damaged or not audio |
| `UNKNOWN_ORIGIN` | the evidence says official and unofficial in equal measure | an override with `classification:` |

Every block also names a site to look the show up on. Detail for every folder
is in `phase1_folders.csv`, every tag change in `phase1_tags.csv`, every track
rename in `phase1_tracks.csv`, and every note in `phase1_issues.csv`.

## 8. Commit

When the plan is what you want, run the same command as `phase2` with
`--commit`, **into the same reports folder**:

```bash
jamp phase2 --artist "Grateful Dead" --commit --until-settled
```

The commit is refused unless that reports folder holds a phase 1 plan for the
same library and the same `--artist` - it is how the tool knows there was a
dry run to read.

For each folder it:

1. saves every tag it is about to change to `.etree_backup.json` in the folder
   (never overwritten, so the first backup stays the original);
2. writes the tags, renames the tracks, rewrites the checksum and cue files,
   and renames the folder;
3. records the settled name in `.etree_state.json`, so the next run leaves the
   folder alone;
4. logs every step to `phase2_committed.csv` in the reports folder.

If any step fails, that folder is put back as it was and the run carries on
with the next. Stopping the run with Ctrl-C puts back the folder it was in the
middle of.

`--until-settled` repeats until a pass changes nothing: a commit can reveal
something the next pass acts on (a folder freed from a wrapper, a venue now in
the tags). It ends with **SETTLED: nothing is left to do**, plus what was left
alone on purpose - blocked folders, duplicates.

Run it again at any time: a settled folder is left as it is. To have the tool
reconsider settled folders - after adding an override, say - add `--reclassify`
to both the dry run and the commit.

**Keep the reports folder** until you are happy with the result.
`phase2_committed.csv` is the record of what changed, and the next run into the
same folder replaces it. A new reports folder per act is a good habit:

```bash
jamp phase1 --artist "Grateful Dead" --out-dir "C:\jamp-reports\gd"
jamp phase2 --artist "Grateful Dead" --out-dir "C:\jamp-reports\gd" --commit --until-settled
```

## 9. Answers the files do not contain

Some things cannot be read from a folder: which night "New Year's Eve 2006"
was, the city of a venue nobody wrote down, that a folder is a box set to be
left alone. Those go in `overrides.yaml` in your user folder, and an override
beats every other kind of evidence:

```yaml
folders:

  "My Band - New Years Eve 2006":
    date: 2006-12-31
    note: the show that saw 2006 out, not the one that rang it in.

  "gd1981-03-20 rainbow theatre":
    venue: Rainbow Theatre
    city: London

  "Some Band/Official Albums/Live at the Fillmore":
    skip: true
    note: an official multi-disc release - leave it alone.
```

- **The key is the folder name as it is on disk now**, before any renaming. It
  keeps working after the tool renames the folder.
- **A key with a slash** matches that path inside the library, for a name that
  repeats (`Disc One`, `Set 2`).
- **Fields:** `date`, `band` (an abbreviation), `venue`, `city`, `state`,
  `source` (`sbd`, `aud`, `mtx`...), `provenance` (the taper), `format`,
  `marker` (`early`/`late`), `classification` (`OFFICIAL`/`UNOFFICIAL`),
  `skip`, and `note` for yourself. Outside the US, give the `city` and leave
  `state` out.
- Every override used is reported as `OVERRIDDEN` in the plan, so you can see
  which decisions were yours.

Run the dry run again after editing, with `--reclassify` if the folder was
already settled.

**Before writing an override, ask whether the tool should know it.** A venue
the tool keeps spelling two ways belongs in `venues.yaml` in your user folder,
where it helps every show at that room, not one:

```yaml
venues:
- name: Barton Hall
  city: Ithaca
  state: NY
  aliases: [Barton Hall Cornell University]
```

## 10. Split shows, wrappers and lossy copies

Three phase 2 flags move files. Each is off unless given, and each deserves a
run of its own, dry run first, so that if something looks wrong you know which
did it.

- **`--include-merges`** joins one show split across sibling folders
  (`gd1973-12-10 s1` and `s2`) into one folder, tracks renumbered. It is the
  only thing that moves files between folders.
- **`--unnest`** lifts a show out of a wrapper folder that holds nothing else
  (`Downloads/gd1977-05-08.../`). It never lifts a show to the top of your
  library, and it leaves the emptied wrapper behind.
- **`--quarantine-lossy`** moves an MP3 folder that duplicates a lossless copy
  of the same recording into `_etree_review/` at the top of your library,
  mirroring where it was, for you to review and delete yourself.

Emptied folders are left where they are. On Windows,
`tools/empty_dirs.ps1 -Root "<folder>"` lists the completely empty ones and
`-Commit` removes them; anything holding even one file is left alone.

## 11. Filling gaps from the internet

Once an act is settled, `phase3` asks reference sites what your files never
said - a missing venue, missing song titles:

```bash
jamp phase3 --artist "Grateful Dead"
```

| source | covers | durations |
| --- | --- | --- |
| archive.org | most taped acts | yes |
| phish.in | Phish | yes |
| phish.net | Phish (needs a free API key) | no |
| jerrybase.com | the Dead and every Garcia act | no |
| archive.mymorningjacket.net | My Morning Jacket | no |

It only ever **fills** an empty field: a title or venue you already have is
never replaced. A title is written only when the recording's track lengths line
up with the source's (for sources with durations), or the track counts agree
exactly (for sources without). Every request is cached, so a second run is
quick and `--offline` works without a connection.

It writes a report first, `phase3_summary.txt`, and changes nothing. To write
what it found:

```bash
jamp phase3 --apply --commit
```

`--apply` refuses to run without that report. It backs up and logs like
phase 2. A filled venue reaches the folder name on the next phase 1 and 2 run.

**phish.net** needs a key: request one free at https://phish.net/api/keys/, save
the private key in a text file outside the library, and pass
`--phishnet-key <file>` (or give it to `jamp init`). Without a key, phish.in
covers the same shows.

## 12. Is a recording missing songs?

`jamp complete` compares each recording's track lengths with the setlist and
reports recordings that stop early, miss a set, or have songs cut out. It needs
phase 0's reports and a show database built from phase 3's cache with
`tools/distill_cache.py`, and it never goes online. See
[reference.md](reference.md#jamp-complete).

## 13. Checking the audio itself (optional)

```bash
jamp phase0 --verify-audio
```

decodes every FLAC with ffmpeg and checks it against the fingerprint stored
inside it, which finds files that play but are silently damaged. **It is
optional and slow**: on a large library it runs for hours at full CPU. Nothing
else in the tool needs it. Run it when you want to know, not as part of
setting up. `--workers` sets how many files are decoded at once (default 4).

Results are in `phase0_verify_audio.csv`: `PASS`, `MISMATCH` (it decodes, but to
different audio than it should), `UNREADABLE`, or `NO_MD5` (the file carries no
fingerprint to check against).

## 14. Undoing a commit

```bash
jamp restore "Grateful Dead/gd1977-05-08.sbd.miller.flac16 - Barton Hall, Ithaca, NY"
```

puts a committed folder back the way it was before the tool touched it: the
original tags, the original track names, the checksum and cue files pointing at
those names again, and the original folder name. Give one or more folders, as
paths inside your library.

Like everything else it is a **dry run first**. Read `restore_summary.txt` in
your reports folder, then run the same command with `--commit`. If any step
fails, the folder is left exactly as it was committed.

It works from what the commit left behind - the tag backup and the state file
in the folder. It **refuses** a folder it cannot put back completely rather
than half-restore it, and says why:

- **files it cannot account for** - most often tracks merged in from another
  folder by `--include-merges`. `--partial` restores the rest and leaves those
  files as they are.
- **a name already taken** - another folder already has the original name.
- **a folder the tool never changed.**

If you kept the reports from the commit, pass its log with
`--log <reports>/phase2_committed.csv` (repeat for several). The log follows
every rename, where the backup only knows the first, so it matters for folders
committed more than once and for SHN files, which hold no tags to back up.

What it does not do: move a folder back to where it was before `--unnest`
lifted it (it says where that was), split a merged show back into its parts,
or bring back files set aside by `--quarantine-lossy` (they are in
`_etree_review/`, mirroring where they came from).

**A restored folder is no longer settled**, so the next dry run proposes the
same rename again. If you want the folder left as it is for good, add it to
`overrides.yaml` with `skip: true`.

## 15. When something goes wrong

**"--commit refused: no phase 1 dry run..."** - run the `phase1` command it
prints, read the plan, then commit. The dry run and the commit need the same
reports folder and the same `--artist` flags.

**"Access is denied" and a folder rolled back (Windows).** Antivirus and Windows
Search open files the moment they change. The tool retries briefly; if it keeps
happening, exclude the library folder from Windows Defender scanning, and run
again - the rolled-back folders are picked up.

**A folder fails every time with "access is denied".** The files are read-only.
In the folder: `attrib -R *.* /S`.

**A folder is renamed again on every run.** Something it writes does not read
back the same way. Run the dry run with `--reclassify` and look at that
folder's reasons; an override usually settles it.

**"No show folders found".** Check the library path (`jamp doctor`), the
spelling of `--artist`, and that the folder is not in `ignore_folders`. A show
folder is one that holds audio: FLAC, MP3, M4A, WMA, OGG, WAV, AIFF, APE, WavPack
or SHN.

**Long paths (Windows).** The tool handles paths past 260 characters itself, but
other programs may not. It drops the venue from a folder name rather than
produce a track path that is too long. `jamp doctor` says whether Windows long
paths are enabled.

**"written by a newer version of jamp".** Someone, or another computer, ran a
newer jamp on that folder. Update jamp; this version leaves the folder alone
rather than misread it.

**Phase 3 "stopped asking" a site.** The site kept failing - down, or asking for
fewer requests. Phase 3 already waits and retries when a site asks it to; after
several failures in a row it leaves that site alone for the rest of the run.
Run phase 3 again later: everything already fetched is cached.

**The reports from an earlier run disappeared.** Each run replaces the reports
in its folder. Use a new `--out-dir` for anything you want to keep.
