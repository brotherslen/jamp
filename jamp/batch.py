"""What `jamp unpack` and `jamp convert` share.

Both work on files rather than shows, both are a dry run until `--commit`, and
both can set their originals aside once the new copy is proven.  Setting aside
is a move, never a delete: the original goes to `_etree_review/originals/`,
mirroring where it was, and emptying that folder is left to you.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .report import ensure_out_dir
from .scan import walk_dirs
from .winpath import opener

ORIGINALS = "originals"


class Refused(Exception):
    """A reason to stop before anything is touched, for the user to read."""


def resolve(args, what: str):
    """ROOT, the reports folder and the config, from the arguments or memory."""
    from .cli import load_settings
    from .config import load_config

    remembered = load_settings()
    root = args.root or (Path(remembered["root"]) if remembered.get("root") else None)
    out_dir = args.out_dir or (Path(remembered["out_dir"])
                               if remembered.get("out_dir") else None)
    if root is None or out_dir is None:
        raise Refused("%s: the library and reports folders are needed - run jamp "
                      "init, or give ROOT and --out-dir" % what)
    root = Path(root).resolve()
    if not root.is_dir():
        raise Refused("%s: the library is not a folder: %s" % (what, root))
    try:
        out_dir = ensure_out_dir(out_dir, root)
    except ValueError as exc:
        raise Refused(str(exc)) from exc
    return root, out_dir, load_config()


def files_in_scope(root: Path, cfg, artists, suffixes: set[str],
                   errors: list | None = None) -> list[Path]:
    """Every file under ROOT (or the --artist folders) with one of `suffixes`.

    The same walk the phases use, so ignore_folders, the review folder and
    unreadable folders are treated the same way.
    """
    dirs = walk_dirs(root, skip_names={cfg.settings.review_folder},
                     include_top=set(artists) if artists else None,
                     ignore=cfg.settings.ignore_folders, errors=errors)
    out = []
    for info in dirs.values():
        for group in (info.audio, info.others, info.texts, info.images):
            out.extend(p for p in group if p.suffix.lower() in suffixes)
    return sorted(set(out), key=lambda p: str(p).lower())


def scope_key(root: Path, artists, **flags) -> dict:
    return {"root": str(root), "scope": sorted(artists) if artists else None,
            **flags}


def plan_check(plan: Path, wanted: dict, command: str) -> str | None:
    """Why a commit should wait for its dry run, or None."""
    if not plan.exists():
        return "no %s dry run in %s" % (command, plan.parent)
    try:
        data = json.loads(plan.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "the %s plan cannot be read: %s" % (command, exc)
    for key, value in wanted.items():
        if data.get(key) != value:
            return ("the %s dry run in %s was for a different %s"
                    % (command, plan.parent, key.replace("_", " ")))
    return None


def refuse_commit(why: str, command: str) -> int:
    print("--commit refused: %s.\nRun the same %s without --commit first, into "
          "the same reports folder, and read %s_summary.txt."
          % (why, command, command), file=sys.stderr)
    return 2


def set_aside_target(path: Path, root: Path, cfg) -> Path:
    return root / cfg.settings.review_folder / ORIGINALS / path.relative_to(root)


def set_aside(path: Path, root: Path, cfg) -> Path:
    """Move an original into the review folder, mirroring where it was."""
    from .phase2 import _safe_rename

    target = set_aside_target(path, root, cfg)
    _safe_rename(path, target, attempts=cfg.settings.rename_attempts,
                 first_delay=cfg.settings.rename_retry_delay, make_parents=True)
    return target


def stat_key(path: Path) -> tuple[int, int] | None:
    """Size and modification time: enough to tell a file was not touched since."""
    try:
        st = os.stat(opener(path))
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns
