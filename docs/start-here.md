# Start here: JAMP, step by step

This page is for anyone who has not used a command-line program before. It
walks through one act from download to finished, with nothing skipped. If you
are comfortable in a terminal, [the guide](guide.md) covers the same ground
faster and in more depth.

**What JAMP does:** it tidies a collection of live recordings - folder names,
track names and tags - so that this

    Grateful Dead 5-8-77 Cornell SBD (Miller)/01 - New Minglewood Blues.flac

becomes this:

    gd1977-05-08.sbd.miller.flac16 - Barton Hall, Ithaca, NY/gd1977-05-08d1t01.flac

**Using ChatGPT or another AI assistant to help?** Upload
[ai-assistant.md](ai-assistant.md) into the chat first (or paste all of it).
JAMP is new, so assistants do not know it and will otherwise guess.

**The one thing to remember:** JAMP never changes anything until you type
`--commit` at the end of a command. Everything before that is a preview.

---

## Step 1. Make a copy of your music

JAMP keeps a backup of everything it changes and can undo its work, but a copy
you made yourself is the one to trust. Copy at least the act folder you are
going to try it on - for example `Grateful Dead` - to another drive.

## Step 2. Download JAMP

1. Go to **https://github.com/brotherslen/jamp/releases** and open the newest
   release.
2. Under **Assets**, download the one for your computer:
   - **Windows:** `jamp-windows-x64.zip`
   - **Mac with an Apple chip (M1, M2, M3, M4):** `jamp-macos-arm64.zip`
   - **Mac with an Intel chip:** `jamp-macos-intel.zip`
     (Not sure? Apple menu > About This Mac. "Chip: Apple ..." means Apple;
     "Processor: Intel ..." means Intel.)
   - **Linux:** `jamp-linux-x64.zip`
3. Unzip it (Windows: right-click > **Extract All**; Mac: double-click it).
   Put the unzipped folder somewhere easy to find, such as your Desktop or
   Documents. It is **not** installed anywhere else - that folder is the whole
   program.

## Step 3. Open a terminal in that folder

A terminal is a window where you type commands instead of clicking. You only
need to type (or paste) the commands on this page.

**Windows 11**

1. Open the unzipped `jamp-windows-x64` folder in File Explorer.
2. Right-click an empty space inside the folder and choose **Open in Terminal**.

(On Windows 10: click the address bar at the top of the folder window, type
`powershell` and press Enter.)

**Mac**

1. Open **Terminal** (press Cmd+Space, type `Terminal`, press Enter).
2. Type `cd ` - the letters c and d, then a space - but do **not** press Enter
   yet.
3. Drag the unzipped `jamp-macos-...` folder from Finder into the Terminal
   window, then press Enter.
4. The first time only, macOS blocks programs downloaded from the internet.
   Paste this and press Enter:

       xattr -d com.apple.quarantine jamp

**Linux:** open your terminal in the unzipped folder (most file managers have
**Open in Terminal** on the right-click menu).

**How to type a command:** type or paste it, then press **Enter**. To paste
into a terminal, use Ctrl+V on Windows or Cmd+V on Mac (or right-click).

**On this page every command starts with `jamp`. You must type the start
differently for your computer:**

| your computer | type this | example |
| --- | --- | --- |
| Windows | `.\jamp` | `.\jamp doctor` |
| Mac or Linux | `./jamp` | `./jamp doctor` |

If you see *"not recognized"* or *"command not found"*, the start was typed
without the `.\` or `./`, or the terminal is not open in the jamp folder.

If Windows shows a blue **"Windows protected your PC"** box, click **More info**
and then **Run anyway**: the program is not signed by a company, which is what
that warning is about.

## Step 4. Set it up

    jamp init

It asks two questions. Answer each one and press Enter.

1. **"Where is your live music library?"** - the folder that holds your act
   folders (the folder that *contains* `Grateful Dead`, `Phish` and so on, not
   one of those).
   - Windows: in File Explorer, hold **Shift**, right-click the folder, choose
     **Copy as path**, then paste it into the terminal.
   - Mac: in Finder, right-click the folder, hold **Option**, choose **Copy
     "..." as Pathname**, then paste it into the terminal.
2. **"Where should reports go?"** - JAMP writes what it plans to do into this
   folder, for you to read. It suggests one in your home folder; just press
   Enter to accept it. (It must not be inside your music library.)

It ends by checking everything. Lines starting `ok` are good; a `warn` is worth
reading but does not stop you; a `FAIL` needs fixing first - see
[When something goes wrong](#when-something-goes-wrong).

You only do this once. JAMP remembers both folders, so later commands do not
ask again.

## Step 5. Tell it who your acts are

    jamp acts

It lists each folder in your library and what it thinks it is:

```
  Billy Strings                 13  UNKNOWN
  Grateful Dead                 32  Grateful Dead (gd)
  Studio                         4  ignored - never scanned
```

JAMP ships knowing about fifteen bands. For each folder it does **not** know
(`UNKNOWN`), it asks you to type one letter and press Enter:

- **a** - add it as an act. It then asks for:
  - the act's **name** (press Enter to accept its suggestion if it is right);
  - an **abbreviation** - a few letters that will start every folder
    name for that act, like `gd` for Grateful Dead or `bs` for Billy Strings.
    **Choose carefully; you will not want to change it later.** If traders
    already use one for that band, use theirs.
- **i** - ignore it: for folders that are not live shows (studio albums,
  compilations, photos).
- **s** - skip it for now.
- **q** - stop here and save your answers.

This changes nothing in your music. You can run it again any time.

## Step 6. Preview one act

Pick **one** act to start - a small one is best. Put its folder name in quotes,
spelled exactly as it is in your library:

    jamp plan --artist "Grateful Dead"

This may take a few minutes for a big act. **Nothing in your music is changed.**

When it finishes, open your reports folder (the one from step 4) and open
**`phase1_summary.txt`** - double-click it; it opens in Notepad or TextEdit.

At the top, **Outcome per folder** counts your show folders by what will happen
to each:

| word | what it means |
| --- | --- |
| **PLAN** | it will be renamed |
| **UNCHANGED** | already correct; left alone |
| **SKIP_BLOCKED** | JAMP was not sure, so it will leave this folder alone |
| **DUPLICATE** | two folders are the same show |

Below that, the sections to read:

- **Proposed renames** - every folder that will change. Read these.
- **Duplicates - same show twice, delete one** - keep the copy you want and
  remove the other yourself; JAMP never deletes anything.
- **Skipped - reported, not touched** - each folder it left alone, with the
  reason underneath.

A rename looks like this - the old name, then `->` and the new one:

```
  Grateful Dead\gd77-05-08 sbd miller
      -> gd1977-05-08.sbd.miller.flac16 - Barton Hall, Ithaca, NY   [UNOFFICIAL sbd, date confidence 90]
```

**Read through the PLAN list.** A wrong date, venue or source is exactly what
this step is for. Blocked folders are not failures: JAMP will not guess, and
leaves anything uncertain exactly as it is.

## Step 7. Make the changes

When the preview looks right, run the same act again with `apply` instead of
`plan`, and `--commit` on the end:

    jamp apply --artist "Grateful Dead" --commit

- It only does what the preview showed. If a folder changed after the preview,
  it leaves that folder alone and tells you.
- If you get **"--commit refused"**, run step 6 again first, exactly the same
  way, then this step.
- When it finishes you should see **"SETTLED: nothing is left to do"**.

Your folders now have their new names. Open a few and check them in your music
player.

Then do the next act: steps 6 and 7 again with a different folder name.

## Step 8. Fill in missing venues and song titles (optional)

    jamp lookup --artist "Grateful Dead"

This asks archive.org and similar sites about shows that are missing a venue or
song titles, and writes what it found to **`phase3_summary.txt`** in your
reports folder. It changes nothing. If the suggestions look right:

    jamp lookup --artist "Grateful Dead" --apply --commit

Then run steps 6 and 7 again for that act, so the new venues reach the folder
names.

## Undoing a change

To put a folder back exactly as it was - its name, track names and tags - give
its path inside your library. First the preview:

    jamp restore "Grateful Dead\gd1977-05-08.sbd.miller.flac16 - Barton Hall, Ithaca, NY"

Then, if it says it can put everything back, the same again with `--commit`:

    jamp restore "Grateful Dead\gd1977-05-08.sbd.miller.flac16 - Barton Hall, Ithaca, NY" --commit

(On a Mac, use `/` instead of `\` between folder names.)

A restored folder will be offered for renaming again the next time you run
step 6, so skip over it there if you want to keep it as it was.

## When something goes wrong

**First, run the check:**

    jamp doctor

It changes nothing and says what is wrong in plain words.

| you see | what to do |
| --- | --- |
| "not recognized" / "command not found" | type `.\jamp` (Windows) or `./jamp` (Mac), and make sure the terminal is open in the jamp folder - step 3 |
| "that is not a folder that exists - try again" | the path was mistyped; copy it again as in step 4 |
| "no such folder under ROOT" | the name after `--artist` does not match a folder in your library exactly - check spelling, spaces and capitals |
| "--commit refused" | run the `plan` command from step 6 first, the same way, then try again |
| "Access is denied" and a folder "rolled back" (Windows) | something else had a file open - close your music player, wait a minute, and run the same command again. Nothing was lost; that folder was put back |
| "is already being written by ..." | another JAMP window is still running. Wait for it to finish. If none is running, the message names a file to delete |
| "your settings folder ... exists, but no jamp.yaml ... can be seen" | run `jamp init` again |

**Still stuck, or something looks wrong?** Please tell us:
**https://github.com/brotherslen/jamp/issues/new/choose**. Choose "Something went
wrong", and paste what `jamp doctor` says plus the error. Reports include your
folder names; remove anything you would rather not share.

---

**Where to next:** once you are comfortable, [the guide](guide.md) explains
ZIP files, SHN conversion, fixing a wrong date or venue by hand, split shows,
and everything else.
