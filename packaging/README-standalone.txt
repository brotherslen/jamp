jamp - standalone build
=================================

This folder holds the whole tool as one program: nothing else to install
except, for converting SHN files, ffmpeg.

1. Open a terminal in this folder (Windows: Terminal or PowerShell; macOS and
   Linux: Terminal). On macOS the first run may need: xattr -d com.apple.quarantine jamp

2. Set up, once:

       jamp init        (Windows: .\jamp init   macOS/Linux: ./jamp init)

   It asks where your live music library is and where reports should go. It
   does not change anything in the library.

3. Follow what it prints next: jamp unpack, jamp convert, jamp acts,
   jamp plan --artist "<a folder>".  Write every one of them the same way as
   in step 2 - .\jamp acts on Windows, ./jamp acts on macOS and Linux.

Every command is a dry run until you add --commit.

ffmpeg: needed only for `jamp convert` (SHN to FLAC) and the optional audio
check, and not included. Get it from https://ffmpeg.org/download.html
  Windows: winget install Gyan.FFmpeg  (or https://www.gyan.dev/ffmpeg/builds/)
  macOS:   brew install ffmpeg         (or https://evermeet.cx/ffmpeg/)
  Linux:   your package manager, e.g. sudo apt install ffmpeg
or put an ffmpeg program in this same folder. `jamp doctor` says whether it is
found.

Never used a terminal? Step by step, with nothing assumed:
  https://github.com/brotherslen/jamp/blob/main/docs/start-here.md

The full guide: https://github.com/brotherslen/jamp/blob/main/docs/guide.md
