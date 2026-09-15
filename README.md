# JAMP

An asset management pipeline for the live music archive.

JAMP puts a collection of live recordings in order - folder names, track
names, tags and checksum files - without destroying anything.

    Grateful Dead 5-8-77 Cornell SBD (Miller)/01 - New Minglewood Blues.flac
    ->
    gd1977-05-08.sbd.miller.flac16 - Barton Hall, Ithaca, NY/gd1977-05-08d1t01.flac

It reads what each folder already says - the name, the track filenames, the
tags, the info file (where the venue above came from) - and works out the act,
the date, the source, the taper and the venue. It knows fan recordings from
official releases, leaves releases their own names, and can fill missing venues
and song titles from archive.org, phish.in, phish.net, jerrybase and the My
Morning Jacket archive. It also extracts shows still in ZIP files and converts
SHN to FLAC, proving each conversion lossless.

## What it promises

- **Nothing is written without `--commit`**, and a commit is refused until
  there is a dry run of the same thing to read. It then does what that dry run
  showed: a folder that changed since is left alone and listed.
- **No file is ever deleted.** ZIPs and SHN files are replaced only by copies
  proven identical, and the originals are set aside, not removed. `jamp tidy`
  removes folders with nothing at all in them, and only those.
- **A date or an act is never guessed.** A folder it is not sure about is
  listed with the reason and left exactly as it is.
- **Every tag is backed up before it changes**, every change is logged, and
  `jamp restore` puts a committed folder back. No run's reports are overwritten
  by the next.
- **Running it twice changes nothing.**

## Quick start

**New to the command line?** Follow **[Start here](docs/start-here.md)** instead:
the same steps, one at a time, with nothing assumed.

Download the standalone build for Windows, macOS (Apple Silicon or Intel) or
Linux from [Releases](https://github.com/brotherslen/jamp/releases), or with
Python 3.11 or newer run `pip install .` in a copy of this repository, or use
[Docker](docs/docker.md). Converting SHN needs [ffmpeg](https://ffmpeg.org/download.html),
which is not included. Then:

```bash
jamp init                        # where your library is, where reports go
jamp unpack                      # ZIPs to extract (add --commit to do it)
jamp convert                     # SHN to convert to FLAC (add --commit)
jamp acts                        # which folder is which act
jamp plan --artist "Grateful Dead"
```

Read `phase1_summary.txt` in your reports folder. When the plan is right:

```bash
jamp apply --artist "Grateful Dead" --commit
```

## Documentation

- **[Start here](docs/start-here.md)** - step by step for anyone who has not
  used a command-line program: download, set up, preview, commit, undo.
- **[The guide](docs/guide.md)** - setting up, reading a plan, committing,
  overrides, filling gaps from the internet, undoing with `jamp restore`,
  troubleshooting.
- **[Reference](docs/reference.md)** - every command, flag, config setting,
  report and issue code.
- **[How it decides](docs/rules.md)** - the rules behind dates, acts, sources,
  names and titles, and why.
- **[Docker](docs/docker.md)** - running it on a NAS.
- **[Contributing](CONTRIBUTING.md)** - working on the code.

## Status

**Beta** (0.2.0b4). Built on and used for one large collection on Windows, where it has renamed and
retagged over 800 shows across thirteen acts. Its tests run on Windows, macOS
and Linux on every change, but day-to-day use so far has been on Windows. Back
up a library before its first commit.

## Licence

JAMP is free software under the [GNU General Public License v3.0 or later](LICENSE).
The standalone downloads also contain third-party software under its own
licences; see [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

JAMP is not affiliated with etree.org. It follows the naming conventions the
etree trading community established, and asks the reference sites it uses
only for what a collector would look up by hand.
