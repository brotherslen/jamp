# Third-party software

JAMP itself is licensed under the GNU General Public License v3.0 or later
(`LICENSE`). It depends on, and the standalone downloads include, the following.

| component | licence | source |
| --- | --- | --- |
| Python | Python Software Foundation License | https://www.python.org/ |
| mutagen | GNU GPL v2.0 or later | https://github.com/quodlibet/mutagen |
| PyYAML | MIT | https://github.com/yaml/pyyaml |
| PyInstaller bootloader (standalone downloads only) | GPL v2.0 with the bootloader exception | https://github.com/pyinstaller/pyinstaller |

The Docker image is built on the official `python:3.11-slim` image (Debian) and
installs Debian's `ffmpeg` package, each under its own licences.

**ffmpeg is not included** in the standalone downloads or the Python package.
JAMP runs an ffmpeg you install yourself (https://ffmpeg.org/download.html) as a
separate program.

The complete source code for every JAMP release is in this repository, under
the tag of that release.
