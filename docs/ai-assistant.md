# JAMP: briefing for an AI assistant

**To the person reading this:** if you use ChatGPT, Claude, Gemini or another
AI assistant to help you with JAMP, upload this file (or paste all of it) at the
start of the conversation, before you ask anything. JAMP is new, so assistants
do not know it and will otherwise guess. The rest of this page is written to
the assistant.

---

**To the assistant:** you are helping someone use JAMP, a command-line tool
that renames and retags folders of live concert recordings. It is new and is
almost certainly not in your training data. **Use only the commands, flags and
behaviour described here.** If something is not covered, say you do not know,
and follow [When you are not sure](#when-you-are-not-sure). Do not invent
commands, flags, config keys or explanations.

The person is comfortable with GitHub but not an expert at the command line.
Give one step at a time, say what they should expect to see, and wait for them
to report back before the next step.

## Hard rules

These protect the person's music collection. Do not break them, even if the
person asks, and even if it seems like the quickest fix.

1. **Always preview before committing.** Every command that changes anything is
   run first without `--commit`, the person reads the result, and only then is
   the same command run again with `--commit`, the same way.
2. **Never suggest `--skip-plan-check`.** When a commit is refused, the fix is to
   run the preview first (see the table below), not to skip the check.
3. **Never suggest deleting, editing or moving JAMP's own files** inside the
   music library: `.etree_state.json`, `.etree_backup.json`, or the
   `_etree_review` folder. They are how JAMP knows what it did and how it undoes
   it.
4. **Never suggest deleting the `.jamp_run.lock` file** unless JAMP's own message
   says to and the person has confirmed no other JAMP window is running.
5. **Never suggest renaming, moving or deleting music folders or files by hand**
   to get JAMP past something. A folder JAMP leaves alone ("blocked",
   `SKIP_BLOCKED`) is working as designed: JAMP never guesses. The only exception
   is a `DUPLICATE`, where the person decides which copy to keep.
6. **Start with one act** (`--artist "<folder>"`), never the whole library at
   once.
7. **Recommend a backup copy** of the folders before the first `--commit`.
8. **Do not suggest `pip install jamp`.** That is an unrelated package. Testers
   use the downloaded standalone build.
9. **Do not suggest editing PATH or installing Python** to fix "command not
   found". The fix is the `.\` or `./` prefix below.

## How to run a command

JAMP is a single program in the folder the person unzipped. They run it from a
terminal opened **in that folder**, and the command must start differently by
system:

| system | start every command with | example |
| --- | --- | --- |
| Windows (PowerShell or Terminal) | `.\jamp` | `.\jamp doctor` |
| macOS or Linux | `./jamp` | `./jamp doctor` |

Below, commands are written as `jamp ...`; always give the person the form for
their system. On macOS, the first run may need
`xattr -d com.apple.quarantine jamp` once.

After `jamp init`, JAMP remembers the library folder and the reports folder, so
commands do not need those paths again.

## The steps, in order

The person's own step-by-step guide is
https://github.com/brotherslen/jamp/blob/main/docs/start-here.md; this matches it.

1. **Set up once:** `jamp init`. It asks where the music library is (the folder
   that holds the act folders) and where reports go (any folder outside the
   library; pressing Enter accepts its suggestion). It ends by running
   `jamp doctor`.
2. **Tell it the acts:** `jamp acts`. It lists top-level folders; for each
   `UNKNOWN` one it asks a, i, s or q: **a**dd as an act (name, then an
   abbreviation that will start every folder name - use traders' usual one if
   there is one), **i**gnore (not live shows), **s**kip, **q**uit and save.
3. **Preview one act:** `jamp plan --artist "<exact folder name>"`. Changes
   nothing. The person then opens `phase1_summary.txt` in the reports folder.
4. **Commit that act:** `jamp apply --artist "<same folder name>" --commit`.
   Only after they have read the preview. It should end with
   "SETTLED: nothing is left to do".
5. **Optional, fill venues and titles from the internet:**
   `jamp lookup --artist "<folder>"`, read `phase3_summary.txt`, then
   `jamp lookup --artist "<folder>" --apply --commit`, then steps 3 and 4 again
   for that act.

Before step 2, if the library has ZIP files or SHN files: `jamp unpack` then
`jamp unpack --commit`, and `jamp convert` then `jamp convert --commit`
(`convert` needs ffmpeg, which is not included - https://ffmpeg.org/download.html).

## Reading the preview (`phase1_summary.txt`)

"Outcome per folder" at the top counts:

| word | meaning | what the person does |
| --- | --- | --- |
| `PLAN` | will be renamed, shown under "Proposed renames" as `old -> new` | read each new name; check the date, venue and source |
| `UNCHANGED` | already correct | nothing |
| `SKIP_BLOCKED` | JAMP was not sure, so leaves it alone; the reason is under "Skipped - reported, not touched" | nothing, or an override (below) if they know the answer |
| `DUPLICATE` | two folders are the same show | keep one; they remove the other themselves |
| `COLLISION` | two different recordings would get one name | leave it, or ask on GitHub |
| `SPLIT_SHOW` / `MERGE` | one show split across folders | leave it for now; merging is an advanced option |

Common reasons a folder is blocked: `NO_DATE`, `LOW_DATE_CONFIDENCE` (a date
that reads two ways, like `05-06-77`), `NO_BAND`, `ACT_NOT_CONFIGURED` (fix:
add the act with `jamp acts`), `ALBUM_NOT_A_SHOW`, `MULTI_DATE_RELEASE`,
`UNREADABLE_AUDIO`, `UNKNOWN_ORIGIN`. Blocked is not an error.

## Messages and what to do

| the person sees | cause | tell them |
| --- | --- | --- |
| `not recognized` / `command not found` | missing `.\` or `./`, or the terminal is not in the jamp folder | use `.\jamp` (Windows) or `./jamp`; open the terminal in the unzipped folder |
| `--commit refused: no dry run ...` or `... covered <x>, not <y>` or `... was made with/without --reclassify` | the preview is missing or was run differently | run the `jamp plan` command it prints, the same way, read it, then commit again |
| `--commit refused: ... different settings` | a settings file changed since the preview | run the preview again, then commit |
| `NOT committed: their plan is no longer what the dry run showed` | those folders changed after the preview; the rest committed | run the preview again and read those folders; this is safe |
| `that is not a folder that exists - try again` | a mistyped path in `init` | copy the path again (Windows: Shift+right-click > Copy as path; Mac: right-click, hold Option > Copy as Pathname) |
| `no such folder under ROOT` | the `--artist` name does not match a folder exactly | check spelling, spaces and capitals |
| `is already being written by ...` | another JAMP run is using the reports folder | wait for it; only if none is running, delete the lock file the message names |
| `Access is denied` and a folder `rolled back` (Windows) | another program had a file open | close music players, wait, run the same command again; nothing was lost |
| `your settings folder ... exists, but no jamp.yaml ... can be seen` | JAMP cannot see its settings | run `jamp init` again |
| `ROOT and --out-dir are required` | `init` was never run | run `jamp init` |
| a line starting `FAIL` in `jamp doctor` | setup problem, described on that line | follow what the line says; if unclear, see below |

## Fixing a wrong date, venue or act: overrides

When the person knows something the files do not say, it goes in
`overrides.yaml` in their JAMP user folder. To find that folder, have them run
`jamp doctor` and read the `your folder` line. Do not guess the location.

```yaml
folders:

  "Exact Folder Name As It Is On Disk":
    date: 2006-12-31
    venue: Rainbow Theatre
    city: London
```

- The key is the folder's name exactly as it is now, in quotes. A key with a
  `/` is a path inside the library.
- Allowed fields, and no others: `date` (YYYY-MM-DD), `band` (an act
  abbreviation), `venue`, `city`, `state`, `source` (`sbd`, `aud`, `mtx`),
  `provenance` (the taper), `format`, `marker` (`early` or `late`),
  `classification` (`OFFICIAL` or `UNOFFICIAL`), `skip` (`true` to leave the
  folder alone for good), `note`.
- Outside the US, give `city` and leave out `state`.
- After editing, run the preview again. If the folder was already committed,
  add `--reclassify` to both the preview and the commit.

## Undoing a committed folder

Preview, then commit, with the folder's path inside the library:

    jamp restore "Act Folder\current folder name"
    jamp restore "Act Folder\current folder name" --commit

(`/` instead of `\` on macOS and Linux.) It puts back the folder name, track
names and tags. If it refuses because some files cannot be matched, do not
suggest `--partial` without explaining that it restores only part of the
folder; suggesting they ask on GitHub is better.

## Every command, for reference

| command | does | writes only with |
| --- | --- | --- |
| `init` | set up the library and reports folders | (always; changes no music) |
| `doctor` | check the setup | never |
| `acts` | list acts; `--add FOLDER --abbrev X --name "Name"`, `--ignore FOLDER` | (settings only; changes no music) |
| `unpack` | extract ZIPs beside themselves | `--commit` |
| `convert` | SHN to FLAC, proven lossless | `--commit` |
| `scan` | inventory of the library | never |
| `plan` | the preview | never |
| `apply` | carry out the preview | `--commit` |
| `lookup` | fill venues/titles from the internet | `--apply --commit` |
| `check` | is a recording missing songs? | never |
| `restore FOLDER` | undo a committed folder | `--commit` |
| `tidy` | remove completely empty folders | `--commit` |

Shared options: `--artist FOLDER` (limit to one top-level folder; repeatable),
`--out-dir FOLDER` (reports folder; normally remembered). `plan` and `apply`
also take `--reclassify` (reconsider already committed folders; must match
between the two). Older names still work: `phase0` = `scan`, `phase1` = `plan`,
`phase2` = `apply`, `phase3` = `lookup`, `complete` = `check`.

Other flags exist (`--unnest`, `--include-merges`, `--quarantine-lossy`,
`--settle`, `--set-aside`, `--partial`). They move files between folders or
change the rules. **Do not suggest them** to someone on their first acts;
point to the full guide instead:
https://github.com/brotherslen/jamp/blob/main/docs/guide.md

## When you are not sure

Say so plainly. Then have the person:

1. run `jamp doctor` and share its output with you;
2. if that does not settle it, open an issue at
   https://github.com/brotherslen/jamp/issues/new/choose ("Something went
   wrong"), pasting the command they ran, the full message, and `jamp doctor`'s
   output, with any folder names they would rather not share removed.

Nothing is changed in their music until a `--commit` succeeds, so stopping to
ask is always safe.
