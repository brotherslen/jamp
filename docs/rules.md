# How jamp decides

The rules the tool runs by, and why. Read this when a plan surprises you: most
surprises are one of these rules doing its job. How to run the tool is in
[guide.md](guide.md); every flag and file is in [reference.md](reference.md).

- [1. Safety](#1-safety)
- [2. Acts](#2-acts)
- [3. Dates](#3-dates)
- [4. Official or not](#4-official-or-not)
- [5. Source and taper](#5-source-and-taper)
- [6. Names](#6-names)
- [7. Titles and track numbers](#7-titles-and-track-numbers)
- [8. Venues](#8-venues)
- [9. Official releases and box sets](#9-official-releases-and-box-sets)
- [10. Split shows, duplicates and collisions](#10-split-shows-duplicates-and-collisions)
- [11. Tags and formats](#11-tags-and-formats)
- [12. Your answers](#12-your-answers)
- [13. The internet](#13-the-internet)
- [14. What it cannot see](#14-what-it-cannot-see)

---

## 1. Safety

These override everything else.

1. **Dry run by default.** Only the `--commit` runs of `phase2`, `phase3
   --apply`, `unpack`, `convert` and `restore` write. Nothing else has any way
   to rename, move, delete or tag.
2. **A commit follows its dry run.** `phase2 --commit` needs a phase 1 plan for
   the same library and scope in its reports folder.
3. **Nothing is deleted.** A few opt-in operations move files: merging a split
   show, setting a lossy duplicate aside, and setting aside a ZIP or SHN whose
   replacement is proven - both into `_etree_review/`, for you to empty. The
   only files ever removed are the tool's own unfinished output: a
   half-extracted ZIP's temporary folder, a FLAC that failed its check.
4. **A date or an act is never guessed.** Below the confidence threshold a
   folder is reported and left alone, even under `--commit`.
5. **Tags are backed up before they change**, to `.etree_backup.json`, which is
   never overwritten: the first backup is the original. The backup records what
   each file is about to be renamed to, so it still finds the file afterwards,
   and it restores each tag in the file's own format.
6. **A failed folder is put back whole** - tags, names, rewritten checksum
   files - and Ctrl-C puts back the folder in progress. Every committed action
   is logged as each folder finishes.
7. **A commit can be undone.** `jamp restore` reads the backup and the state
   file and puts the tags, track names, checksum entries and folder name back,
   under the same dry-run-first and all-or-nothing rules. It refuses a folder
   it cannot put back completely rather than restoring part of it.
8. **Running twice changes nothing.** `.etree_state.json` records a folder's
   settled name; `--reclassify` asks the tool to think again.
9. **Two shows that would get one name are both reported**; neither is renamed.
10. **Reports never go inside the library.** Enforced, not advised.
11. **Nothing unreadable is passed over in silence.** A folder that cannot be
    listed is named; a file whose tags cannot be read is never treated as a
    file with no tags.
12. **A file from a newer jamp is never touched.** The state and backup files
    carry a format number; a version that finds a higher one than it knows
    blocks the folder rather than misreading or rewriting it.
13. **One run per reports folder.** A lock stops two runs writing the same
    reports, which would leave files that look fine and are quietly wrong.

## 2. Acts

The act is taken from, in order: a name at the start of the folder name, an
abbreviation before the date (`gd77-05-08`), the ARTIST tag, a name anywhere in
the folder name, the info file, and last the folder the show sits in.

**The folder a show sits in is the weakest evidence.** Side projects live under
their parent act's folder, compilations under whoever collected them, so the
parent folder is always marked as not authoritative. And when it is the only
evidence while the ARTIST tag names a performer the tool does not know, the
show is **blocked** (`ACT_NOT_CONFIGURED`): filing a Dickey Betts & Great
Southern show under the Allman Brothers folder would rewrite who played. A tag
naming nobody ("Unknown Artist", "Various") does not block.

**Acts the tool does not know are added, never guessed.** The abbreviation
starts every folder name for that act, so it is chosen by you (`jamp acts`).

**A family prefix is not an answer on its own.** `jg` files the whole Jerry
Garcia family. Before settling on it the tool looks further: track filenames
that all say `jgb...`, then the info file and the ARTIST tag (only another act
in the same family may win this way), then the date - from 1987 a folder
saying only "Jerry Garcia" is the Jerry Garcia Band, unless its name says
`acoustic`, `grisman`, `saunders` and similar. Trey Anastasio is configured the
same way, and any act can be (`defaults_to`, `defaults_from_year`,
`defaults_unless_named`).

## 3. Dates

- **US month first.** `05-06-77` is 5 May 1977. Where the other reading is also
  a real date, the folder loses confidence unless something else agrees.
- **Six digits are `yymmdd`** (`u111105` is 2011-11-05). An act's active years
  prefer the reading that falls inside them.
- **Two-digit years pivot on today**, and no date after today is accepted.
- **A bare year is not a date.** It is reported for you to fill in, never
  turned into 1 January.
- **Confidence runs 0-99, with the reasons written down.** Committing needs 70.
- **Track filenames outrank the folder name** when at least three of them, and
  60% or more, agree on a date.

Things that look like dates and are not:

- **etree catalogue numbers.** In `jg83-06-01.010962.jgb...`, `010962` is a
  shnid, not 9 January 1962.
- **Transfer dates.** A date years later than the one in the folder name,
  found only in an info file, log or tag, is about the copy, not the show.
- **Filler.** `Filler: 2000/10/01 Desert Sky` is another night's recording.
- **A track number and a song title.** `2-06 2001.flac` is track 6, the song
  *2001*, not 6 February 2001.
- **Timestamps and version strings.**

## 4. Official or not

Every folder is OFFICIAL (a release by the act or a label), UNOFFICIAL (fan
recordings, however they travelled) or, when the evidence is too close to
call, blocked as `UNKNOWN_ORIGIN`. Signals are scored, never chained, and every
weight is in the config.

- **How a copy travelled says nothing about who made it.** A torrent file or a
  lineage line counts, but only as one weighted signal.
- **An etree catalogue number is decisive.** etree hosts no official releases.
- **A store named beside audience evidence is lineage.** There are no official
  audience releases, so a folder naming LivePhish *and* a microphone is a fan
  matrix built on the board feed.
- **Tidy tags are weak evidence.** Plenty of tapers tag meticulously.
- **Naming the source in the folder name is taper vocabulary.** No label ships
  a product called `4-17-82 Grateful Dead sbd flac`.
- **When nothing settles it**, a folder that names no official series and no
  store is read as unofficial: an official release announces itself.
- **Clues are looked for everywhere in the tags** - COMMENT, PUBLISHER,
  COPYRIGHT, ENCODEDBY, URL, ALBUM - so a store found in one field is found in
  all of them.

## 5. Source and taper

The source is decided in a fixed order: a matrix keyword, a microphone, a board
or FM keyword, an audience keyword, an official store, nothing. A microphone
*and* a board keyword without a matrix keyword is a conflict: no source is
written and the folder is reported.

- **Where the mics stood is not where the signal came from.** "Behind the
  SBD" or "at the board" describes a taper's position; reading it as a
  soundboard would turn an audience tape into a board, the one thing this
  field must never get wrong.
- **Microphone models match their variants** (`C414xls` is a C414).
- **The taper is one short slug** from the config's `tapers` table where known.
- **Equipment is never a person.** `Taper: MAC > Nakamichi DR-2 > Edirol` is a
  signal chain.
- **etree names are read by position.** The taper can sit either side of the
  microphone (`jgb.nak700.dyche`); co-tapers joined by `-` or `+` are a taper
  field; a taper is never the act, a show marker, a taping position (`fob`,
  `motb`), a flag (`sbeok`) or `unknown`.
- **A microphone may stand in for the taper** on audience and matrix recordings
  when no taper is known - often the only thing telling two copies of one
  night apart. Never on soundboards.
- **A radio broadcast keeps its station's call sign** (`wxpn`), never just `fm`.

## 6. Names

    <act><date>[.<marker>][.<source>][.<provenance>].<format>[ - <venue, city, ST>]
    gd1977-05-08.sbd.miller.flac16 - Barton Hall, Ithaca, NY
    gd1977-05-08d1t01.flac

- **Track filenames stay mechanical.** The venue is for reading in a file
  browser, not for two thousand files to repeat.
- **Everything the tool compares uses the part before ` - `**, so correcting a
  venue can never turn one show into two, or hide a duplicate.
- **`early` and `late` go straight after the date.**
- **`flac16` and `flac24` come from the audio stream**, not from what the
  folder name claims. Mixed formats get no format token, and a warning.
- **Dates come out of place text before illegal characters do**, so an
  `8/16/1996` in a tag never becomes `8161996` in a folder name.
- **The venue is dropped from a folder name, but kept in the tags,** when it
  would push a track path past the length limit.

## 7. Titles and track numbers

**A title comes from**, in order: an official release's own tag (never
replaced); the folder's setlist file matched by disc or set and track; the
existing tag; a setlist matched by track number alone (only in a folder with a
single disc or set, and only where there is no tag); the track filename.
Nothing is cleared and nothing is invented.

- **The folder's own setlist is the authority on wording**, when it can say
  which disc or set a track is on. `Disc One`, `CD II` and `Set 2` are all
  headers. Lines are cleaned as they are read: times before or after a title,
  footnote markers, stray slashes and trailing commas come off; `(v1)`, `AC/DC
  Bag` and `w/` stay. What the setlist itself gets wrong is taken as written.
- **A rip log is not a setlist.** Its table-of-contents rows look like numbered
  lines, and are not songs.
- **A bare number is still a title.** "2001" is a song.

**Track numbers come from**, best first: official tags, `s1t01` filenames,
`d1t01` filenames, the setlist file, a track number in the filename, sorted
order. Only the last is a guess, and it is flagged.

- `01.01 - Promised Land` is disc 1, track 1.
- Roman numerals before a number are sets (`II 01 Tweezer`).
- **Track zero is material before the show** - a soundcheck, a tuning - and
  keeps a letter (`d1t0a`) rather than shifting the running order.
- **Gaps in the numbering are kept**, so a recording with tracks 3, 4, 6 still
  shows that 5 is missing.
- **Disc folders fold into the show**, and the disc number comes from the
  folder: in `Disc 2`, `01 Ramble On Rose` is disc 2, track 1.
- **Tags that put two files in one slot are not a numbering**; the filenames are
  used instead.

## 8. Venues

Venue text is cleaned the same way from any source: a city with no venue is
dropped, a repeated part is not written twice, a leading act prefix is removed
only when it is a known one, product words (`Box Set`, `CD REL`) come off, and a
state gets its comma.

**A folder name gives a venue only with a `City, ST` after it.** That guard is
what keeps loose words like "opera house" out of venues. A show that loses its
place to it is answered with an override, not by loosening the rule.

**The gazetteer (`venues.yaml`) knows rooms**, and does two things: gives a room
named without a city its city, and settles one room written two ways. Three
restrictions:

- **A venue name is not unique.** There are several Fox Theatres. A name shared
  by rooms in the table is never guessed between; the show needs a city.
- **It can only see the rooms it has.** A name too common to stand alone is
  marked `generic` and is used only once the city is known.
- **It never contradicts the folder.** Where they disagree it withdraws.

**The library can teach a room its city**, when every folder naming that room
agrees on one. A blocked folder can learn this way but cannot teach.

A festival is a venue by another name. A name that moved between sites carries
the `years` it was in each.

## 9. Official releases and box sets

- **A release folder keeps its product name.** `Dave's Picks 16`, `30 Trips
  Around The Sun`.
- **Being a release is stated, not guessed from shape.** A box set and a taper's
  multi-night bundle look the same on disk; the config's `official_series`
  decides. Everything else is filing, and its shows are named normally.
- **Each show inside a release is named `YYYY-MM-DD Venue, City, ST`**, each disc
  `Disc N`: no act prefix and no release name, which the parent already carries.
- **A release spanning several nights with no per-show dates is left alone**
  (`MULTI_DATE_RELEASE`).
- **Studio albums and compilations are recognised as albums**, not as shows
  missing a date, and point to Discogs rather than a setlist site.

## 10. Split shows, duplicates and collisions

- **Split shows merge, if you ask.** Sibling folders with the same act and date
  that differ only by a set or disc marker become one folder with
  `--include-merges`. It is the only thing that moves files between folders, so
  it is opt-in.
- **The same show in two formats is not a split show.** A split needs different
  markers, not the same one twice.
- **A merge whose target name is taken does not happen at all**, and nothing
  moves.
- **Duplicates are reported, never resolved.** Two folders that are the same
  show are both flagged, and you delete the copy you do not want. An MP3 copy
  beside a lossless one can be set aside with `--quarantine-lossy`.
- **A different recording that would get the same name is a collision**, and
  neither is renamed.
- **A folder holding show folders is filing, not a concert.** Loose audio beside
  them is reported, never used to name the container.
- **`--unnest` never lifts a show to the top of the library.** An act folder
  holding one show looks exactly like a wrapper.
- **A show nested inside another show is renamed first**, deepest first, so
  renaming the parent never moves the child out from under its own plan.
- **Deleting is not part of the tool.** Emptied folders are left behind, and
  clearing them is a separate step.

## 11. Tags and formats

**ZIPs and SHN are dealt with before anything else**, since both hide shows:

- **A ZIP is extracted beside itself** and never over anything; each file is
  checked against the CRC the ZIP records, and a failure leaves nothing
  half-extracted. A member path that would land outside the folder refuses the
  whole ZIP.
- **A ZIP's contents keep the shape of what they are.** One folder stays one
  folder. Disc folders are one show, so they go into one folder named after
  the ZIP. Several folders of audio that are not discs are several shows, so
  each lands beside the ZIP, like every other show in that act's folder, and
  loose extras go into a folder named after the ZIP rather than scattering.
- **An SHN is converted to FLAC only when both decode to identical audio**,
  hashed from what ffmpeg decodes out of each, never from the encoder's say-so.
  An existing FLAC beside it is checked, never replaced.
- **An original is set aside only once its replacement is proven**: an SHN by
  its decode check (from the log, if neither file has changed since; decoded
  again otherwise), a ZIP by every extracted file's CRC.

FLAC, MP3, M4A (ALAC and AAC), WMA, OGG, WAV, AIFF, APE, WavPack and SHN are
read. Everything but SHN and WMA can be written: SHN has nowhere to put a tag,
and WMA's tags cannot yet be written so that they read back. Audio in any other
format is invisible to the tool.

**Every writer has a reader that agrees with it.** Every field, through every
format, must come back out exactly as it went in, and a test enforces it. Each
bug of this kind makes a folder that is renamed on every run and never settles.

- MP4 keeps VENUE and other fields it has no atom for as freeform atoms.
- Track and disc totals already in a file are kept.
- Artwork is never written, and left where it is.

**Checksums after tagging.** `.ffp` and `.st5` fingerprint the decoded audio, so
they stay valid through tagging and renaming, and remain the real record of
the recording. A plain `.md5` hashes the whole file, tags included, so writing
tags invalidates it whatever happens to the names.

## 12. Your answers

`overrides.yaml` holds answers the files do not contain, and **an override beats
every other kind of evidence**. It is reported wherever it is used.

- **An override survives the rename it causes.** The tool remembers a folder's
  original name and path, so the next run still finds the answer - otherwise
  the evidence it overruled would win again and undo it.
- **Every field offered is applied**; an unknown field, or a `band` naming no
  act, is an error rather than silently ignored.
- **A key with a path** is for names that repeat (`Without a Net/Disc One`), so
  one answer never applies to every `Disc One` in the library.
- **Most things are not overrides.** A room's city belongs in `venues.yaml`,
  where it helps every show there; a release's shape belongs in
  `official_series`. An override is for a judgement about one folder.

## 13. The internet

**Phases 0, 1 and 2 never touch the network**, so a plan is reproducible. Only
phase 3 goes online, and it caches everything it asks.

- **Fill only.** A field with a value is never replaced - unless the value is
  one phase 3 itself wrote, which it records in `.etree_state.json`.
- **Report first**, structurally: `--apply` refuses to run without the report.
- **Durations decide which recording a folder holds**, not the words in its
  name. Where a source has track lengths, a title is written only when the
  recording lines up with it - 85% alignment and total length within 5%, both.
  Where it has none, the track counts must agree exactly, and the report says
  that is all it rests on.
- **Several copies of a night vote**; a title needs a clear majority.
- **A near miss is only near when both numbers are close.** A one-track
  soundcheck scoring 100% against a four-hour show is not close.
- **Sites are asked politely.** Requests are spaced out and identify the tool;
  a site asking for fewer ("too many requests", "unavailable") is waited for
  as long as it asks, up to two minutes, twice; a site that keeps failing is
  left alone for the rest of the run. The cache lives in a per-user cache
  folder, so a second reports folder does not mean asking everything again.
- **Old answers stay good for longer.** This year's shows are refreshed after a
  week, earlier ones after a month.

`jamp complete` reads the recordings from the other side: songs in the
setlist that nothing in the recording answers to. A shortfall counts only when
the recording is a subset of the setlist; a reference shorter than the
recording proves nothing; and without segue information a low track count is
not comparable, since three songs joined by segues and three songs missing look
the same.

## 14. What it cannot see

- **Sample rate and catalogue number are not in the name.** Two different
  transfers of one night can resolve to the same name, and are reported as
  duplicates or collisions.
- **A checksum file's own filename is not read as evidence.**
- **The folder name and tags are both the tool's output and its input.** Much
  of what is above - the state file, remembering original names, recognising
  its own names - exists so that a second run does not read the first run's
  output as fresh evidence and reach a worse answer.
- **Damaged audio is found only by decoding**, which is optional
  (`--verify-audio`). A file that decodes to the wrong audio reads, tags and
  plays like a healthy one. A tag before the FLAC marker is legal and is not
  damage.
