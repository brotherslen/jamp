"""`jamp init` and `jamp doctor`: setting up, and checking the setup.

Neither touches the library.  `init` writes only into the user folder and
creates the reports folder; `doctor` writes nothing but a probe file in the
reports folder, which it removes, to prove the folder can be written.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__, userdir

TEMPLATES = userdir.SHIPPED_DIR / "templates"

OK, WARN, FAIL = "ok", "warn", "FAIL"


# -- init -----------------------------------------------------------------

def _ask(prompt: str, default: str | None = None) -> str:
    suffix = " [%s]" % default if default else ""
    try:
        got = input("%s%s: " % (prompt, suffix)).strip().strip('"')
    except EOFError:
        got = ""
    return got or (default or "")


def _interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def _default_reports(root: Path) -> Path:
    """Beside the home folder, never inside the library."""
    candidate = Path.home() / "jamp-reports"
    if candidate == root or root in candidate.parents:
        candidate = root.parent / (root.name + "-jamp-reports")
    return candidate


def run_init(args) -> int:
    home = userdir.user_dir()
    print("jamp %s - setting up" % __version__)
    print("  your folder: %s" % home)
    print("  nothing in your music library is read or changed by this step")
    print()

    remembered = {}
    paths_file = userdir.user_paths_path()
    if paths_file.exists():
        try:
            remembered = json.loads(paths_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            remembered = {}

    # The library.
    root = args.root or (Path(remembered["root"]) if remembered.get("root") else None)
    if root is None or args.ask:
        if not _interactive():
            print("init: give the library folder with --root when not run in "
                  "a terminal", file=sys.stderr)
            return 2
        while True:
            answer = _ask("Where is your live music library?",
                          str(root) if root else None)
            if answer and Path(answer).expanduser().is_dir():
                root = Path(answer).expanduser()
                break
            print("  that is not a folder that exists - try again")
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        print("init: the library folder does not exist: %s" % root, file=sys.stderr)
        return 2

    # The reports.
    out_dir = args.out_dir or (Path(remembered["out_dir"])
                               if remembered.get("out_dir") else None)
    if out_dir is None or args.ask:
        default = out_dir or _default_reports(root)
        if _interactive():
            out_dir = Path(_ask("Where should reports go? (outside the library)",
                                str(default))).expanduser()
        else:
            out_dir = default
    out_dir = Path(out_dir).expanduser().resolve()
    if out_dir == root or root in out_dir.parents:
        print("init: reports must go outside the library, and %s is inside %s"
              % (out_dir, root), file=sys.stderr)
        return 2

    # Write only what is missing: a second init must never lose your answers.
    home.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in (userdir.USER_CONFIG_NAME, userdir.USER_OVERRIDES_NAME):
        target = home / name
        if name == userdir.USER_CONFIG_NAME and userdir.user_config_path().exists():
            # Including etree.yaml, the old name: a jamp.yaml beside it would
            # take its place and every answer in it would stop counting.
            target = userdir.user_config_path()
        if target.exists():
            print("  kept     %s (already there)" % target)
        else:
            legacy = userdir.LEGACY_OVERRIDES if name == userdir.USER_OVERRIDES_NAME else None
            if legacy is not None and legacy.exists():
                shutil.copy2(legacy, target)
                print("  copied   %s from %s" % (target, legacy))
            else:
                shutil.copy2(TEMPLATES / name, target)
                print("  created  %s" % target)

    values = dict(remembered)
    values["root"] = str(root)
    values["out_dir"] = str(out_dir)
    if args.phishnet_key:
        values["phishnet_key"] = str(Path(args.phishnet_key).expanduser().resolve())
    from .integrity import locate_ffmpeg

    ffmpeg, how = locate_ffmpeg(args.ffmpeg)
    if ffmpeg and (args.ffmpeg or how == "winget"):
        # Remembered only when it would not otherwise be found next time.
        values["ffmpeg"] = str(ffmpeg)
    paths_file.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
    print("  saved    %s" % paths_file)
    print()

    code = run_doctor(args)
    print()
    print("Next - none of these change anything in the library:")
    print("  jamp unpack          ZIP files that still need extracting")
    print("  jamp convert         SHN files to convert to FLAC")
    print("  jamp acts            which folders are which act; add the unknown ones")
    print("  jamp phase0          what is in the library")
    print('  jamp phase1 --artist "<a folder in your library>"')
    print("                        what would be renamed and retagged, for one act")
    print("Read the reports in %s before ever adding --commit." % out_dir)
    return code


# -- doctor ---------------------------------------------------------------

class _Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, what: str, detail: str = "") -> None:
        self.rows.append((status, what, detail))
        print("  %-4s  %-18s %s" % (status, what, detail))

    @property
    def failed(self) -> bool:
        return any(s == FAIL for s, _, _ in self.rows)


def _long_paths_enabled() -> bool | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            return bool(winreg.QueryValueEx(key, "LongPathsEnabled")[0])
    except OSError:
        return False


def _ffmpeg_checks(report: _Report, explicit: str | None) -> None:
    from .integrity import locate_ffmpeg

    ffmpeg, how = locate_ffmpeg(explicit)
    if not ffmpeg:
        report.add(WARN, "ffmpeg", "%s. Needed for jamp convert (SHN to FLAC) "
                   "and the optional phase0 --verify-audio. Download: "
                   "https://ffmpeg.org/download.html" % how)
        return
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-version"],
                           capture_output=True, text=True, timeout=30)
        first = (r.stdout or "").splitlines()[0] if r.stdout else "no version line"
        first = first.split(" Copyright")[0]
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.add(WARN, "ffmpeg", "found at %s (%s) but it did not run: %s"
                   % (ffmpeg, how, exc))
        return
    report.add(OK, "ffmpeg", "%s - %s (%s)" % (first, ffmpeg, how))
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-decoders"],
                           capture_output=True, text=True, timeout=30)
        names = {line.split()[1] for line in (r.stdout or "").splitlines()
                 if len(line.split()) > 1}
    except (OSError, subprocess.TimeoutExpired):
        names = set()
    missing = [d for d in ("flac", "shorten") if d not in names]
    if missing:
        report.add(WARN, "ffmpeg decoders", "missing: %s%s" % (
            ", ".join(missing),
            " - jamp convert cannot read SHN with this ffmpeg" if "shorten" in missing
            else ""))


def run_doctor(args) -> int:
    print("jamp doctor")
    report = _Report()

    v = sys.version_info
    report.add(OK if v >= (3, 11) else FAIL, "python",
               "%d.%d.%d at %s" % (v.major, v.minor, v.micro, sys.executable))
    for module, name in (("mutagen", "mutagen"), ("yaml", "PyYAML")):
        try:
            mod = __import__(module)
            report.add(OK, name, getattr(mod, "version_string", None)
                       or getattr(mod, "__version__", "installed"))
        except ImportError:
            report.add(FAIL, name, "not installed - pip install %s" % name)
    report.add(OK, "jamp", "%s at %s" % (__version__, Path(__file__).parent))

    home = userdir.user_dir()
    report.add(OK if home.is_dir() else WARN, "your folder",
               str(home) if home.is_dir() else "%s does not exist yet - run jamp init" % home)

    from .config import ConfigError, load_config

    try:
        cfg = load_config()
        report.add(OK, "config", "%d acts, %d venues%s" % (
            len(cfg.bands), len(cfg.venues or ()),
            ", with yours laid over" if cfg.user_path else ", shipped only"))
    except (ConfigError, OSError, ValueError) as exc:
        report.add(FAIL, "config", str(exc))

    from .overrides import Overrides

    ov_path, legacy = userdir.resolve(userdir.user_overrides_path(),
                                      userdir.LEGACY_OVERRIDES)
    try:
        ov = Overrides.load(ov_path)
        report.add(WARN if legacy else OK, "overrides",
                   "%d from %s%s" % (len(ov), ov_path,
                                     " - move it to %s" % userdir.user_overrides_path()
                                     if legacy else ""))
    except (OSError, ValueError) as exc:
        report.add(FAIL, "overrides", str(exc))

    remembered = {}
    paths_file = userdir.resolve(userdir.user_paths_path(), userdir.LEGACY_PATHS)[0]
    if paths_file.exists():
        try:
            remembered = json.loads(paths_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            report.add(FAIL, "remembered paths", "%s: %s" % (paths_file, exc))

    root = Path(remembered["root"]) if remembered.get("root") else None
    if root is None:
        report.add(WARN, "library", "none remembered - run jamp init")
    elif not root.is_dir():
        report.add(FAIL, "library", "%s is not reachable (drive unplugged?)" % root)
    else:
        try:
            count = sum(1 for e in os.scandir(root) if e.is_dir())
            report.add(OK, "library", "%s - %d top-level folders" % (root, count))
        except OSError as exc:
            report.add(FAIL, "library", "%s cannot be listed: %s" % (root, exc))

    out_dir = Path(remembered["out_dir"]) if remembered.get("out_dir") else None
    if out_dir is None:
        report.add(WARN, "reports", "none remembered - run jamp init")
    elif root is not None and (out_dir.resolve() == root.resolve()
                               or root.resolve() in out_dir.resolve().parents):
        report.add(FAIL, "reports", "%s is inside the library" % out_dir)
    else:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            fd, probe = tempfile.mkstemp(dir=out_dir, prefix=".jamp-doctor-")
            os.close(fd)
            os.remove(probe)
            report.add(OK, "reports", "%s - writable" % out_dir)
        except OSError as exc:
            report.add(FAIL, "reports", "%s cannot be written: %s" % (out_dir, exc))

    _ffmpeg_checks(report, getattr(args, "ffmpeg", None))

    longp = _long_paths_enabled()
    if longp is False:
        report.add(WARN, "long paths", "Windows long paths are off: the tool "
                   "copes, but other programs may not open files past 260 "
                   "characters")
    elif longp:
        report.add(OK, "long paths", "enabled")

    key = remembered.get("phishnet_key")
    if key:
        kp = Path(key)
        good = kp.exists() and kp.read_text(encoding="utf-8").strip()
        report.add(OK if good else WARN, "phish.net key",
                   "present" if good else "%s is missing or empty" % kp)

    if report.failed:
        print("Something above needs fixing before a run.")
        return 1
    print("Ready.")
    return 0
