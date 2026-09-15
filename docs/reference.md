# jamp reference

Every command, flag, file and code. For how to use them together, read
[guide.md](guide.md); for why the tool decides as it does, [rules.md](rules.md).

- [Commands](#commands)
- [Flags](#flags)
- [Environment variables](#environment-variables)
- [The user folder](#the-user-folder)
- [Your jamp.yaml](#your-jampyaml)
- [overrides.yaml](#overridesyaml)
- [venues.yaml](#venuesyaml)
- [What the tool writes inside the library](#what-the-tool-writes-inside-the-library)
- [Reports](#reports)
- [Plan outcomes](#plan-outcomes)
- [Issue codes](#issue-codes)
- [The naming scheme](#the-naming-scheme)
- [jamp check](#jamp-check)
- [Tools](#tools)

---

## Commands

| command | what it does | writes to the library |
| --- | --- | --- |
| `jamp init` | sets up the user folder and remembers the library and reports folders, then runs `doctor` | never |
| `jamp doctor` | checks Python, libraries, config, overrides, library, reports folder, ffmpeg and long paths; exits 1 if something must be fixed | never |
| `jamp acts` | lists the top-level folders and the act each is; adds acts and ignored folders to `acts.yaml` | never |
| `jamp unpack` | extracts ZIP files beside themselves, each file checked against the ZIP's CRC | only with `--commit` |
| `jamp convert` | converts SHN to FLAC with ffmpeg, each file proven to decode to identical audio | only with `--commit` |
| `jamp scan` | inventory: formats, naming patterns, acts, dates, origin, sidecars, audio fingerprints | never |
| `jamp plan` | the plan: every folder's proposed name, track names, tags and sidecar rewrites, and every folder left alone with the reason | never |
| `jamp apply` | carries out the plan | only with `--commit` |
| `jamp lookup` | asks reference sites for missing venues and titles; a report | only with `--apply --commit` |
| `jamp check` | compares recordings against setlists to find missing songs | never |
| `jamp restore` | puts committed folders back as they were: tags, track names, checksum files, folder name | only with `--commit` |
| `jamp tidy` | removes folders with nothing at all in them; lists folders with files but no audio | only with `--commit`, and only empty folders |

**Older names.** Five commands were called by the step they are in, and still
answer to it: `phase0` is `scan`, `phase1` is `plan`, `phase2` is `apply`,
`phase3` is `lookup` and `complete` is `check`. A dry run made with one name
can be committed with the other. Their reports keep the old names
(`phase1_plan.json`, `phase2_committed.csv`), so a reports folder from before
the change still works.

`jamp --version` prints the version. `py -m jamp ...` (or `python -m jamp`)
runs the same commands from a copy of the code.

## Flags

**`scan`, `plan`, `apply`, `lookup` and `check`**, whichever name is typed:

| flag | |
| --- | --- |
| `ROOT` | the library. Optional once `jamp init` or `--remember` has saved one |
| `--out-dir DIR` | where reports go. Must be outside ROOT. Optional once saved |
| `--artist FOLDER` | only this top-level folder (or a path inside the library, `"Container/Act"`). Repeat for several |
| `--remember` | save ROOT, `--out-dir`, `--cache`, `--shows` and `--phishnet-key` as defaults |
| `--config FILE` | a different shipped `jamp.yaml` to start from; your own is still laid over it |
| `--overrides FILE` | a different `overrides.yaml` |
| `--today YYYY-MM-DD` | pretend today is this date (tests) |

A path given on the command line always beats a remembered one.

**`scan`**

| flag | |
| --- | --- |
| `--verify-audio` | decode every FLAC and check it against its stored MD5. Optional; hours on a large library. Resumes from `verify_audio_ledger.csv` in `--out-dir` |
| `--workers N` | parallel decodes for `--verify-audio` (default 4) |
| `--tag-sample N` | read tags from at most N files per folder (0 = all) |

**`plan` and `apply`**

| flag | |
| --- | --- |
| `--reclassify` | reconsider folders already settled, ignoring `.etree_state.json`. A commit needs it exactly when its dry run had it |
| `--unnest` | lift shows out of wrapper folders; in `plan` it lists every lift. A commit needs it exactly when its dry run had it |

**`apply`**

| flag | |
| --- | --- |
| `--commit` | actually write, pass after pass until nothing changes. Refused without a plan in `--out-dir` for the same library, `--artist`, `--reclassify` and `--unnest`, made with the same config, acts, venues and overrides files. On the first pass a folder is left alone if its plan is no longer the one in `phase1_plan.json` or its files changed after the dry run; folders committed by a later pass are listed in `phase2_summary.txt` |
| `--one-pass` | commit a single pass only |
| `--until-settled` | the default; accepted for older commands |
| `--skip-plan-check` | commit without a matching plan, and without holding each folder to it |
| `--include-merges` | merge shows split across sibling folders; moves files between folders |
| `--quarantine-lossy` | move an MP3 copy that duplicates a lossless one into the review folder |
| `--settle` | rename nothing; record folders already named in this scheme as settled |

**`lookup`**

| flag | |
| --- | --- |
| `--apply` | write the tags in `phase3_proposals.json`; refused if that report is not in `--out-dir` |
| `--commit` | with `--apply`: actually write (without it, `phase3_dry_run.csv`) |
| `--offline` | answer only from the cache; never connect |
| `--seed` | fetch what every source knows about every settled folder, write `shows.sqlite` beside the cache, and stop |
| `--cache FILE` | the cache of everything fetched (default: `archive.sqlite` in your cache folder, or one already in `<out-dir>/cache/`) |
| `--shows FILE` | a distilled show database, read before the network |
| `--phishnet-key FILE` | a file holding a phish.net API key |

**`acts`**

| flag | |
| --- | --- |
| `ROOT` | the library (default: remembered) |
| `--add FOLDER` | add this top-level folder as an act |
| `--abbrev X` | with `--add`: the abbreviation (default: a guess) |
| `--name "Act"` | with `--add`: the name (default: from the folder) |
| `--ignore FOLDER` | never scan this folder; repeat for several |

**`unpack` and `convert`**

| flag | |
| --- | --- |
| `ROOT`, `--out-dir DIR` | default: remembered |
| `--artist FOLDER` | only inside this folder; repeat for several |
| `--commit` | actually do it. Refused without a dry run of the same scope (and the same `--set-aside`) in `--out-dir` |
| `--skip-plan-check` | commit without that dry run |
| `--set-aside` | move originals whose new copy is proven into `_etree_review/originals/`, mirroring where they were |
| `--workers N` | `convert` only: files converted at once (default 4) |
| `--ffmpeg PATH` | `convert` only: where ffmpeg is |

`unpack` outcomes: `extract`, `already extracted` (the folder holds every file
at the same size), `refused` (unreadable, empty, password-protected, an unsafe
path, an unsupported compression method, a folder of that name that does not
match, not enough disk space), `unsupported` (RAR, 7z).

`convert` results: `converted`, `already converted` (a FLAC with identical audio
was beside it), `MISMATCH` (the FLAC beside it, or the new one, holds different
audio - an existing FLAC is left alone, a new one is not kept), `FAILED` (the SHN
does not decode, or ffmpeg failed). Exit code 1 when anything is `MISMATCH` or
`FAILED`.

**`restore`**

| flag | |
| --- | --- |
| `FOLDER ...` | committed folders, as paths inside the library (or absolute) |
| `--root DIR`, `--out-dir DIR` | default: remembered |
| `--commit` | actually restore. Refused without a dry run of the same folders in `--out-dir` |
| `--skip-plan-check` | commit without that dry run |
| `--log FILE` | a `phase2_committed.csv` to follow renames through; repeat for several |
| `--partial` | restore a folder even when some of its files cannot be matched; those are left as they are |

**`tidy`**

| flag | |
| --- | --- |
| `ROOT`, `--out-dir DIR` | default: remembered |
| `--artist FOLDER` | only inside this folder (never removed itself); repeat for several |
| `--commit` | actually remove. Refused without a dry run of the same scope in `--out-dir` |
| `--skip-plan-check` | commit without that dry run |

**`init`**

| flag | |
| --- | --- |
| `--root DIR`, `--out-dir DIR` | the library and reports folders; asked for in a terminal |
| `--phishnet-key FILE` | remember a phish.net key file |
| `--ffmpeg PATH` | where ffmpeg is, if it is not found |
| `--ask` | ask for the paths again even when remembered |

**`doctor`**: `--ffmpeg PATH`.

## Environment variables

| variable | |
| --- | --- |
| `JAMP_HOME` | the user folder, instead of the platform default, with the cache in `cache/` inside it. The way to point a container at `/config` |
| `JAMP_FFMPEG` | the ffmpeg to use. Otherwise: the one `init` remembered, one beside the standalone executable, PATH, and on Windows winget's folder |

## The user folder

| OS | default |
| --- | --- |
| Windows | `%APPDATA%\jamp` |
| macOS | `~/Library/Application Support/jamp` |
| Linux | `$XDG_CONFIG_HOME/jamp`, else `~/.config/jamp` |

| file | written by | holds |
| --- | --- | --- |
| `jamp.yaml` | you (`init` starts it) | settings and acts, laid over the shipped config |
| `acts.yaml` | `jamp acts` | acts added and folders ignored there; laid under your `jamp.yaml` |
| `overrides.yaml` | you (`init` starts it) | answers about specific folders |
| `venues.yaml` | you | rooms added to the shipped gazetteer |
| `paths.json` | `init`, `--remember` | remembered paths |

**The cache folder** holds what `lookup` has downloaded, and can be deleted at
any time: `%LOCALAPPDATA%\jamp\cache` on Windows, `~/Library/Caches/jamp` on
macOS, `$XDG_CACHE_HOME/jamp` or `~/.cache/jamp` on Linux.

**File formats.** `.etree_state.json` and `.etree_backup.json` carry an `etree_format`
number (a file without one is format 1). A version of jamp never reads or
rewrites a file with a higher number than it knows: the folder is blocked
(`WRITTEN_BY_NEWER_VERSION`), `lookup` skips it, and `restore` refuses it.

The config a run uses is: the shipped `jamp.yaml` → your `acts.yaml` → your
`jamp.yaml`. Every run prints the files it read.

## Your jamp.yaml

Anything in it is laid over the shipped config:

- **settings** are replaced one at a time;
- **lists** (`ignore_folders`, `microphones`, token lists) gain what you add;
- **a band or side project** with a shipped abbreviation replaces that entry
  whole; a new abbreviation adds an act. An act moved between `bands` and
  `side_projects` is moved, not duplicated;
- **an official series** with a shipped name replaces it.

**Settings**

| setting | default | |
| --- | --- | --- |
| `ignore_folders` | `[]` | folders never scanned. A bare name matches any folder so called; a path (`"Act/Studio"`) only that one |
| `genre` | `Live` | GENRE written to shows |
| `preserve_official_genre` | `true` | leave an official release's own genre alone |
| `folder_location` | `venue_only` | add the place to folder names: `venue_only`, `any_known` (a city is enough) or `off` |
| `folder_location_max` | `60` | longest place text in a folder name |
| `review_folder` | `_etree_review` | where `--quarantine-lossy` moves files, at the top of the library |
| `min_date_confidence_commit` | `70` | below this a folder is never committed |
| `min_date_confidence_plan` | `25` | below this a folder is not even planned |
| `earliest_show_date` | `1960-01-01` | an earlier date is not believed |
| `max_path_length` | `260` | path length flagged as too long |
| `phase0_tag_sample` | `4` | files per folder whose tags `scan` reads |
| `rename_attempts`, `rename_retry_delay` | `5`, `0.15` | retries when a rename is briefly locked (antivirus, indexing) |
| `unknown_defaults_to_unofficial` | `true` | treat a folder that names no series and no store as unofficial |
| `classify_min_score`, `classify_min_margin` | `30`, `15` | how decisive official/unofficial evidence must be |

**An act**

```yaml
bands:
  - abbrev: bs                 # starts every folder name: bs2021-10-31...
    name: Billy Strings        # written to ARTIST
    prefixes: [bs]             # abbreviations found at the start of folder names
    aliases: ["Billy Strings", "Billy Strings Band"]   # names in folders and tags
    active_years: [2012, 2035] # dates outside are doubted
```

Also available: `family_prefix: true` (the prefix files a family of acts, so a
more specific act named later wins), and `defaults_to`, `defaults_from_year`,
`defaults_unless_named` (from a year on, a bare family name means one act,
unless the name contains one of those words).

**A side project** is the same, under `side_projects:`, with `parent:` naming
the act whose folder it lives in. The parent folder is then never taken as
evidence of who played.

Other sections of the shipped `jamp.yaml` - `microphones`, `source_tokens`,
`tapers`, `official_series`, `provenance`, `broadcast`, `classifier_weights`,
`date_confidence`, `references` - can be extended the same way. Read the
shipped file (`jamp/data/jamp.yaml`) for their shape.

## overrides.yaml

```yaml
folders:
  "<folder name, or path/inside/the/library>":
    date: 2006-12-31
    band: gd
    venue: Barton Hall
    city: Ithaca
    state: NY
    source: sbd
    provenance: miller
    format: flac16
    marker: late
    classification: UNOFFICIAL
    skip: true
    note: why
```

| field | |
| --- | --- |
| `date` | the show date |
| `band` | an act abbreviation; one the config does not have is an error |
| `venue`, `city`, `state` | the place. Outside the US, leave `state` out |
| `source` | `sbd`, `aud`, `mtx`, ... |
| `provenance` | the taper or transfer, as a short slug |
| `format` | the format token |
| `marker` | `early` or `late` |
| `classification` | `OFFICIAL` or `UNOFFICIAL` |
| `skip` | `true`: leave the folder exactly as it is |
| `note` | for you; never read |

An unknown field is an error. The key matches the folder's name on disk, and
keeps matching after the tool renames the folder; a key with a `/` matches a
path inside the library.

## venues.yaml

```yaml
venues:
- name: Fox Theatre
  city: Oakland
  state: CA
  aliases: [Fox Oakland]
  years: [2009, 2035]   # optional: for a name that moved
  generic: false        # true: a name too common to use without a city
```

A name shared by two rooms is never guessed between: the show needs a city.

## What the tool writes inside the library

Only `--commit` runs of `apply`, `lookup --apply`, `unpack`, `convert` and `restore` write anything:

| file | |
| --- | --- |
| renamed folders and tracks | the new names |
| tags | per the plan |
| `.ffp`, `.md5`, `.st5`, `.sfv`, `.cue` | rewritten to the new filenames |
| `.etree_backup.json` | the tags before they changed; written once and never overwritten |
| `.etree_state.json` | the settled name, the original name and path, what `lookup` wrote; after `restore`, when and from what |
| `_etree_review/` | at the top of the library: lossy copies (`--quarantine-lossy`), and originals set aside by `unpack` and `convert` (`originals/`) |
| extracted folders, `.flac` files, `<folder>.ffp` | from `unpack --commit` and `convert --commit` |

## Reports

All in `--out-dir`. The newest run's reports are always at the top of the
folder, under the names below. **Nothing is lost to the next run:** before a
run replaces a report, it moves the earlier one into
`history/<date>T<time> <command>/` in the same folder (the command by its old
name - `phase1`, `phase2`). `phase1_reads.json.gz`,
`verify_audio_ledger.csv` and `convert_committed.csv` stay where they are.
History is never cleared by the tool; delete what you no longer want. A
`.jamp_run.lock` stops two runs using one folder at once.

| command | files |
| --- | --- |
| `scan` | `verify_audio_ledger.csv` (with `--verify-audio`, kept between runs), `phase0_summary.txt`, `phase0_inventory.json`, `phase0_folders.csv`, `phase0_unparsed.csv`, `phase0_audio_identity.csv`, `phase0_same_audio.csv`, `phase0_verify_audio.csv` (with `--verify-audio`) |
| `plan` | `phase1_summary.txt`, `phase1_folders.csv`, `phase1_tracks.csv`, `phase1_tags.csv`, `phase1_sidecars.csv`, `phase1_issues.csv`, `phase1_damaged.csv`, `phase1_plan.json`, `phase1_reads.json.gz` (what each audio file said, reused by the commit for files unchanged since), `phase1_audio_identity.csv` and `phase1_same_audio.csv` (the same reports as `scan`'s, for the folders in the plan) |
| `apply` | `phase2_summary.txt`, `phase2_committed.csv` (every action, written as each folder finishes), `phase2_journal.json` |
| `lookup` | `phase3_summary.txt`, `phase3_proposals.json`, `phase3_committed.csv` (apply), `phase3_dry_run.csv` (apply without `--commit`) |
| `check` | `completeness_summary.txt`, `completeness.csv` |
| `tidy` | `tidy_summary.txt`, `tidy_plan.csv` and `tidy_plan.json` (dry run), `tidy_committed.csv` (commit) |
| `unpack` | `unpack_summary.txt`, `unpack_plan.csv` and `unpack_plan.json` (dry run), `unpack_committed.csv` (commit) |
| `convert` | `convert_summary.txt`, `convert_plan.csv` and `convert_plan.json` (dry run), `convert_committed.csv` (commit; appended to, never replaced, since it is the proof `--set-aside` relies on) |
| `restore` | `restore_summary.txt`, `restore_steps.csv` and `restore_plan.json` (dry run), `restore_committed.csv` (commit) |

## Plan outcomes

| outcome | |
| --- | --- |
| `PLAN` | will be renamed and retagged |
| `UNCHANGED` | already right |
| `SKIP_BLOCKED` | left alone; at least one `block` issue says why |
| `DUPLICATE` | same act, date, source and format as another folder: the same show twice. Neither is renamed |
| `COLLISION` | a different recording would get the same name. Neither is renamed |
| `MERGE` | part of a split show that `--include-merges` will join |
| `SPLIT_SHOW` | a split show that cannot be merged safely; reported |

## Issue codes

In `phase1_issues.csv`. **block** stops a folder being renamed; **warn** is
renamed but worth reading; **info** records how something was decided.

| code | severity | |
| --- | --- | --- |
| `ACT_NOT_CONFIGURED` | block | ARTIST names an act the config lacks; only the parent folder says otherwise |
| `ALBUM_NOT_A_SHOW` | block | looks like a studio album or compilation |
| `CONTAINER_NOT_A_SHOW` | block | loose audio beside several show folders |
| `LOW_DATE_CONFIDENCE` | block | a date, but not certain enough to commit |
| `MULTI_DATE_RELEASE` | block | an official release spanning several dates |
| `NO_AUDIO` | block | no audio the tool can read |
| `NO_BAND` | block | no act matches |
| `NO_DATE` | block | no date found |
| `SKIPPED_BY_HAND` | block | `skip: true` in overrides |
| `WRITTEN_BY_NEWER_VERSION` | block | the folder's `.etree_state.json` is from a newer version of jamp; update before working on it |
| `UNKNOWN_ORIGIN` | block | official and unofficial evidence too close to call |
| `UNREADABLE_AUDIO` | block | a file cannot be read |
| `AMBIGUOUS_DATE` | warn | a date readable two ways |
| `BAND_FROM_PARENT` | warn | the act came only from the folder it sits in |
| `DATE_CONFLICT` | block or warn | sources disagree about the date; only a warning when the track filenames settle it |
| `FORMAT` | warn | a format or encoding problem, e.g. mixed formats, which get no format token |
| `INCOMPLETE_SHOW` | warn | gaps in the track numbering; kept, so the gaps stay visible |
| `LONG_PATH` | warn | a path is past the length limit |
| `NESTED_CONTAINER` | warn | the show sits inside a folder with no audio of its own, which keeps its name |
| `PROVENANCE_DISAGREES` | warn | the tags and the folder name name different tapers; the tags are used |
| `SAME_SHOW_IN_ANOTHER_FOLDER` | warn | the same name is planned in a different folder |
| `SHN_NO_TAGS` | warn | SHN cannot hold tags; renamed only |
| `SOURCE_CONFLICT` | warn | evidence for both soundboard and audience |
| `DATE_FROM_FILENAMES` | info | the track filenames agreed on a date |
| `FAMILY_DEFAULT_APPLIED`, `FAMILY_DEFAULT_NOT_APPLIED` | info | a family act's default was, or was not, used |
| `INFO_FILE_CHOICE` | info | which of several info files was read |
| `IN_RELEASE_FOLDER` | info | a show inside an official release, named as part of it |
| `NO_INFO_FILE` | info | no info file |
| `OVERRIDDEN` | info | an override was applied |
| `PLACE_FROM_CANONICAL_NAME` | info | the place was read back from a folder name this tool wrote |
| `PROVENANCE_CHOICE`, `PROVENANCE_FROM_NAME` | info | how the taper was chosen |
| `SIDE_PROJECT` | info | a side project under its parent's folder |
| `SINGLE_DATE_OF_A_SERIES` | info | one date found in a usually multi-night series |
| `SOURCE_INFERRED` | info | the source was inferred, not stated |
| `STORE_IS_LINEAGE` | info | a store named beside audience evidence: a fan matrix, not a release |
| `TAG_DATE_REFUSED` | info | a date in the tags was not believed |
| `TAPER_LINE_IS_LINEAGE` | info | a "Taper:" line was equipment, not a person |
| `TAPER_NOT_IN_CONFIG` | info | a taper not in the `tapers` table |
| `TORRENT_MARKER` | info | a "Torrent downloaded from" file |
| `VENUE_AMBIGUOUS` | info | a venue name shared by several rooms |
| `VENUE_FROM_GAZETTEER`, `VENUE_FROM_LIBRARY`, `VENUE_FROM_NAME` | info | where the venue came from |

## The naming scheme

    <act><date>[.<marker>][.<source>][.<provenance>].<format>[ - <venue, city, ST>]
    gd1977-05-08.sbd.miller.flac16 - Barton Hall, Ithaca, NY
    jgb1980-03-01.late.aud.flac16 - Capitol Theatre, Passaic, NJ

Tracks: `<act><date>d<disc>t<track>.<ext>` (`gd1977-05-08d1t01.flac`), or
`s<set>` when the set is known. Track zero keeps a letter: `d1t0a`.

- `format`: `flac16`, `flac24` (read from the FLAC stream), `mp3`, `m4a`,
  `wma`, `shn`.
- The place is added only after ` - `; everything the tool compares uses the
  part before it.
- Official releases keep their product name, and a show inside one is named
  `YYYY-MM-DD Venue, City, ST`, a disc `Disc N`.

## jamp check

Needs the durations `plan` or `scan` measured, in the same reports folder, and a
show database:

```bash
jamp plan                  # durations (or jamp scan)
jamp lookup --seed         # fetches setlists; writes shows.sqlite beside the cache
jamp check
```

`--shows` defaults to `shows.sqlite` beside `lookup`'s cache (`--cache` to name
another cache), built or rebuilt from the cache when it is missing or older.
`--identity` and `--folders` default to the newer pair in `--out-dir`:
`phase1_audio_identity.csv` with `phase1_folders.csv`, or
`phase0_audio_identity.csv` with `phase0_folders.csv`. It never reads the
library or the network.

| verdict | |
| --- | --- |
| `COMPLETE` | every song in the setlist is accounted for |
| `TRUNCATED` | songs are missing - at the end, in a block, or throughout |
| `SHORT_BY_COUNT` | fewer tracks than songs, with no durations to say more |
| `STATED_PARTIAL` | the recording says it is partial |
| `NOT_COMPARABLE` | the counts cannot be compared (segues unknown) |
| `REFERENCE_SHORTER` | the setlist is shorter than the recording, which proves nothing |
| `NO_REFERENCE` | no setlist for this show |

## Tools

Scripts in `tools/`, run from a copy of the code (`python tools/<name>`). Each
is a dry run unless given its commit flag.

| tool | |
| --- | --- |
| `verify_audio.py` | the `scan --verify-audio` check on its own, with a ledger of your choosing |
| `distill_cache.py` | a show database from any cache, to any destination - a copy to share |
| `export_cache.py` | a shareable copy of the cache, phish.net rows left out |
| `build_gazetteer.py` | seed a `venues.yaml` from a library and its overrides |

