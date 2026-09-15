# Working on jamp

For anyone changing the code. Users want [docs/guide.md](docs/guide.md).

## Setup

```bash
pip install -e .[dev]
python -m pytest
```

Tests set `JAMP_HOME` to an empty folder (`tests/conftest.py`), so they read
the shipped config only and never the user folder of whoever runs them. They
build miniature libraries from real folder names - real FLAC headers, real ID3
frames, a real M4A - and ffmpeg-dependent tests skip when ffmpeg is absent.

## The rules of the codebase

These are the invariants the design depends on. Each one was learned from a
bug that broke it.

1. **Nothing writes without `--commit`.** Phases 0 and 1, `acts`, `init`,
   `doctor`, `check` and a dry run of `restore`, `unpack`, `convert` or `tidy` have
   no code path that renames, moves, deletes or tags anything in the library.
   Keep it that way structurally, not by checking a flag deep inside.
2. **One reader per container and field.** A second reader for the same thing
   always ends up disagreeing with the first. `lookup` (phase 3) once decided whether
   TITLE was empty with its own reader and saw no title on WAV; a later FLAC
   header reader forgot that an ID3 tag may precede `fLaC` and called intact
   files damaged. Use `audio.py`'s readers.
3. **Every writer has a reader that agrees with it**, enforced by round-trip
   tests: every canonical field through every container, every configured band
   through the name builders and parsers. A writer without one produces a
   folder that is rewritten on every run.
4. **The tool's output is its next input.** Folder names and tags written by
   `apply` (phase 2) are read by `plan` (phase 1) next time. Any change to naming or tagging must
   be checked for what a second pass makes of it: run `--reclassify` on a
   settled library and expect zero `PLAN`.
5. **A silent skip is worse than an error.** Anything refused, unreadable or
   ignored says so in a report.
6. **Never guess.** Below confidence, report and leave the folder alone.
   Overrides exist so that a guard need not be loosened for one folder.
7. **Phases 0-2 never use the network.**
8. **Anything opening a path in Python goes through `winpath.opener`** (long
   Windows paths); anything handed to ffmpeg must not.
9. **Files left in a library are versioned.** `.etree_state.json` and
   `.etree_backup.json` carry `etree_format` (`state.FORMAT`, `tagwriter.BACKUP_FORMAT`).
   Add fields freely; raise the number only when an older version would
   misread the file, and never read or rewrite a file with a higher number.

## Making a change

- **A fix comes with a test that fails without it.**
- **Measure the blast radius.** For anything that changes analysis, naming or
  tagging, run a whole-library `plan --reclassify` into a reports folder
  before the change and another after, and diff every report. Explain every
  line that differs. Several changes have been reverted on exactly this
  evidence.
- **Prefer an override to a looser rule** when one folder is the problem.
- Match the surrounding style: comments say *why*, with the case that forced
  the rule.

## Where things are

| path | |
| --- | --- |
| `jamp/` | the package |
| `jamp/data/` | shipped config: `jamp.yaml`, `venues.yaml`, `templates/` (what `init` writes), `examples/` |
| `tests/` | pytest; `fixtures.py` builds test libraries |
| `tools/` | maintenance scripts, run from a checkout; not installed. Nothing a user needs may live only here |
| `docs/` | user documentation |
| `packaging/` | the standalone build's entry point and readme |
| `Dockerfile` | the container image |
| `.github/workflows/` | `test.yml` runs the tests on Windows, macOS and Linux and builds the image; `release.yml` builds the standalone programs on a `v*` tag |

| module | owns |
| --- | --- |
| `cli.py` | argument parsing, the `--commit` refusals and plan check, the out-dir guard, remembered paths |
| `install.py` | `jamp init` and `jamp doctor` |
| `acts.py` | `jamp acts` |
| `restore.py` | `jamp restore` |
| `tidy.py` | `jamp tidy`: removing only completely empty folders |
| `distill.py` | the show database `check` and `--shows` read, from `lookup`'s cache |
| `verify.py` | the resumable audio check behind `scan --verify-audio` |
| `unpack.py`, `convert.py`, `batch.py` | `jamp unpack`, `jamp convert`, and what they share: scope, plan check, setting originals aside |
| `userdir.py` | where the user folder is |
| `config.py` | loading the config and laying the user's layers over the shipped one |
| `overrides.py` | `overrides.yaml`, matched by folder name or path |
| `venues.py` | the gazetteer |
| `scan.py` | walking the library, folding disc folders, finding containers and releases |
| `audio.py` | formats, tag reading, track filename parsing, structural damage checks |
| `infofile.py` | choosing and parsing the info file: fields, place, setlist |
| `tokens.py` | boundary-aware token matching over messy names |
| `dates.py` | date extraction, ambiguity, confidence |
| `bands.py` | act attribution, family prefixes, side projects |
| `classify.py` | OFFICIAL / UNOFFICIAL / UNKNOWN, store download vs disc rip |
| `sources.py` | sbd / aud / mtx inference, provenance and tapers |
| `analyze.py` | one pass per folder, producing everything the phases need, and the issue codes |
| `naming.py` | building and parsing names, place text, filename legality |
| `sidecars.py` | rewriting `.ffp` / `.md5` / `.st5` / `.sfv` / `.cue` |
| `dupes.py` | lossy copies beside lossless ones |
| `integrity.py`, `identity.py` | FLAC STREAMINFO, decoding against the MD5, finding ffmpeg, which folders share audio |
| `phase0.py`, `phase1.py`, `phase2.py` | `scan`, `plan` and `apply`: the inventory, the plan, the writer. The modules keep the phase names |
| `reads.py` | each audio file's read saved with its size and modification time, reused while both match; the per-folder fingerprint a commit is checked against |
| `tagwriter.py` | writing tags, and backing up and restoring them per container |
| `state.py` | `.etree_state.json` |
| `report.py` | CSV/JSON/text reports, the run lock, and moving a replaced report into `history/` |
| `winpath.py`, `textio.py` | long Windows paths; encoding-tolerant text reading |
| `confirm.py` | `lookup` (phase 3) |
| `archiveorg.py`, `phishin.py`, `phishnet.py`, `jerrybase.py`, `mmjarchive.py` | one reference source each |
| `httpcache.py`, `showstore.py` | the HTTP cache and the distilled show database |
| `setlist.py`, `complete.py` | duration alignment and the completeness check |

## Adding an act to the shipped list

Add it to `jamp/data/jamp.yaml` under `bands` (or `side_projects` with a
`parent`), with the abbreviation traders already use, every spelling found in
folder names and tags as an `aliases` entry, and `active_years`. The round-trip
test covers every configured band through the name builders and parsers; run
it. A shipped act should be one many collectors have - personal ones belong in
a user's own `jamp.yaml`.

## Adding a reference source

Think twice. Five sources are past diminishing returns: the last three closed a
handful of gaps between them, and the remaining gaps are shows no source
publishes durations for. A new source must be proven alone, fill only, and put
its answers through the same alignment rules as the rest.
