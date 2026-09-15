"""Where one user's own answers live, apart from what the tool ships.

The repository ships knowledge anyone's library can use: microphones, source
tokens, official series, venues and a starter list of bands.  What belongs to
one collection - the folders to ignore, the acts only it holds, the answers in
overrides.yaml, the remembered library path - lives in a user folder instead,
so updating the tool never touches it and sharing the tool never shares it.

The folder is, in order:

* ``JAMP_HOME``, when set - the way to point a container or a test at one;
* ``%APPDATA%\\jamp`` on Windows;
* ``~/Library/Application Support/jamp`` on macOS;
* ``$XDG_CONFIG_HOME/jamp``, or ``~/.config/jamp``, elsewhere.

The tool was called etree-pipeline before it was jamp.  Where no ``jamp``
folder exists but an ``etree`` one does, that one is used, and ``ETREE_HOME``
is honoured when ``JAMP_HOME`` is not set.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_VAR = "JAMP_HOME"
LEGACY_ENV_VAR = "ETREE_HOME"
NAME = "jamp"
LEGACY_NAME = "etree"

USER_CONFIG_NAME = "jamp.yaml"
LEGACY_CONFIG_NAME = "etree.yaml"
USER_OVERRIDES_NAME = "overrides.yaml"
USER_VENUES_NAME = "venues.yaml"
USER_PATHS_NAME = "paths.json"
# Written by `jamp acts`: the acts and ignored folders you chose there.
ACTS_NAME = "acts.yaml"

# What ships, inside the package so an installed copy carries it.
SHIPPED_DIR = Path(__file__).resolve().parent / "data"
# Where a checkout kept config before it moved into the package.
_CHECKOUT_CONFIG = Path(__file__).resolve().parent.parent / "config"


def _home_env() -> str | None:
    return os.environ.get(ENV_VAR) or os.environ.get(LEGACY_ENV_VAR)


def _config_base() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return Path(base) if base else Path.home() / "AppData" / "Roaming"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base) if base else Path.home() / ".config"


def _named(base: Path) -> Path:
    """base/jamp - or base/etree when only the folder of the old name exists."""
    if not (base / NAME).exists() and (base / LEGACY_NAME).exists():
        return base / LEGACY_NAME
    return base / NAME


def user_dir() -> Path:
    env = _home_env()
    if env:
        return Path(env).expanduser()
    return _named(_config_base())


def user_cache_dir() -> Path:
    """Where downloaded answers are kept: rebuildable, so not beside the config.

    Under JAMP_HOME when that is set, so a container keeps its cache on the
    same volume as its settings.
    """
    env = _home_env()
    if env:
        return Path(env).expanduser() / "cache"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return _named(Path(base) if base else Path.home() / "AppData" / "Local") / "cache"
    if sys.platform == "darwin":
        return _named(Path.home() / "Library" / "Caches")
    base = os.environ.get("XDG_CACHE_HOME")
    return _named(Path(base) if base else Path.home() / ".cache")


CACHE_NAME = "archive.sqlite"


def cache_path(explicit: Path | None, out_dir: Path) -> tuple[Path, str]:
    """The HTTP cache to use, and why that one.

    It used to live in each reports folder, so a new reports folder - one per
    act is the advice - started with an empty cache and asked every site
    everything again.  A cache already in the reports folder is still used.
    """
    if explicit:
        return Path(explicit), "given"
    legacy = Path(out_dir) / "cache" / CACHE_NAME
    if legacy.exists():
        return legacy, "the one already in the reports folder"
    return user_cache_dir() / CACHE_NAME, "your cache folder"


def user_config_path() -> Path:
    """jamp.yaml in the user folder - or etree.yaml, its old name, if that is all
    there is."""
    folder = user_dir()
    if not (folder / USER_CONFIG_NAME).exists() and (folder / LEGACY_CONFIG_NAME).exists():
        return folder / LEGACY_CONFIG_NAME
    return folder / USER_CONFIG_NAME


def unseen_settings(config_found: bool) -> str | None:
    """Why a run should not go ahead with the settings it can see, or None.

    `jamp init` always leaves a config file in the user folder.  A folder that
    exists with none in it is not a new user; it is settings this process cannot
    see - a dry run once ran in such a window, with no ignore_folders and no
    overrides, and planned fifteen renames nobody wanted.
    """
    home = user_dir()
    if config_found or not home.is_dir():
        return None
    try:
        names = {p.name for p in home.iterdir()}
    except OSError:
        names = set()
    # The cache alone is not settings: with JAMP_HOME set it lives in that
    # folder, and a container may have made it before anyone ran init.  An
    # empty folder is suspect, though - it is what a window that cannot see
    # the files in it shows.
    if names == {"cache"}:
        return None
    return ("your settings folder %s exists, but no %s or %s can be seen in it, "
            "so ignore_folders and your overrides would not be used.\n"
            "If your settings are in that folder, this program cannot see them. "
            "On Windows a packaged app - the Claude desktop app is one - keeps "
            "its own private copy of AppData, so files written from inside it "
            "exist only there (under %%LOCALAPPDATA%%\\Packages\\<app>\\LocalCache). "
            "Keep settings outside AppData and point JAMP_HOME at them.  If the "
            "folder really is empty, run jamp init to set it up."
            % (home, USER_CONFIG_NAME, LEGACY_CONFIG_NAME))


def user_overrides_path() -> Path:
    return user_dir() / USER_OVERRIDES_NAME


def user_venues_path() -> Path:
    return user_dir() / USER_VENUES_NAME


def user_paths_path() -> Path:
    return user_dir() / USER_PATHS_NAME


# Where these files lived before the user folder existed.  Still read when the
# user folder has no copy, so an older checkout keeps its answers - but named
# on every run, because a file inside the repository is one `git pull` or one
# shared zip away from being lost or handed to someone else.
LEGACY_OVERRIDES = _CHECKOUT_CONFIG / "overrides.yaml"
LEGACY_PATHS = _CHECKOUT_CONFIG / "etree.paths.json"


def resolve(user_path: Path, legacy: Path) -> tuple[Path, bool]:
    """The file to read, and whether it is the legacy copy."""
    if not user_path.exists() and legacy.exists():
        return legacy, True
    return user_path, False
