"""Remembering what was decided about a folder.

Phase 2 changes the very evidence phase 1 classifies on: once an unofficial folder
has been given complete, consistent tags it starts to look like a store
download, and the next run can reach a different conclusion and want to rename
it again.  That breaks the rule that running twice changes nothing.

So a committed folder gets a small `.etree_state.json` recording what was
decided and the name it was given.  A later run sees the folder is settled and
leaves it alone.  Delete the file, or pass --reclassify, to make the pipeline
think again.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from .winpath import opener

STATE_NAME = ".etree_state.json"

# The layout of the files this tool leaves inside a library - this state file
# and the tag backup.  Libraries outlive versions of the tool, and several
# collectors on several versions may work on one shared library, so every such
# file says which layout it is in.  A file with no number is format 1, which is
# what every file written before the number existed is.  Raise it only when an
# older version would misread the new layout.
FORMAT = 1
# Not "format": the state file has always carried the audio format under that
# name ("flac16"), and a check reading it as a version blocked 1,061 folders
# in the dry run that caught it.
FORMAT_KEY = "etree_format"
NEWER = "__written_by_a_newer_version__"


class NewerFormat(Exception):
    """A file written by a newer jamp, which this version must not touch."""

    def __init__(self, where, data):
        super().__init__("%s was written by a newer version of jamp (format %s; "
                         "this version reads up to %d) - update jamp before "
                         "working on this folder"
                         % (where, (data or {}).get(FORMAT_KEY), FORMAT))


def newer_format(data) -> bool:
    try:
        return isinstance(data, dict) and int(data.get(FORMAT_KEY) or 1) > FORMAT
    except (TypeError, ValueError):
        return True             # a format this version cannot even parse

# What other steps record in the state file, which a phase 2 decision must carry
# forward rather than replace.  write_state used to build the file from its
# payload alone, so every rename after phase 3 had filled a folder dropped
# phase3_written - the record that lets phase 3 correct its own answer and
# nobody else's.  Of the folders phase 3 filled, 16 still had it.
HISTORY_KEYS = ("phase3_written", "phase3_filled", "phase3_filled_at",
                "titles_restored", "titles_restored_at")


def write_state(folder: Path, payload: dict) -> Path:
    target = Path(folder) / STATE_NAME
    body = dict(payload)
    body["settled_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    body["folder_name"] = Path(folder).name
    # The name this folder had before the pipeline ever touched it, recorded
    # once and never rewritten.  An override is keyed on the name as it was on
    # disk, so the first successful rename used to put the folder beyond its own
    # override's reach: the evidence the override existed to beat then won the
    # next pass and undid the correction.  previous_folder_name cannot serve,
    # since it only remembers one rename back.
    _prior = read_state(folder) or {}
    body["original_folder_name"] = (
        _prior.get("original_folder_name")
        or payload.get("previous_folder_name")
        or _prior.get("folder_name")
        or Path(folder).name)
    # The same, for where it sat.  A name is not enough for an override keyed
    # by path once the folder has moved as well as been renamed: --unnest lifted
    # "MMW 04-07-14 Lugano, Switzerland/04-07-14 Lugano, Switzerland" a level
    # and renamed it, and no rebuilt path could reach the key after that.
    # Only a first write knows it: a folder settled before this was recorded
    # has already moved, and its current path would be a false "original".
    original_rel = (_prior.get("original_relative_path")
                    or (payload.get("previous_relative_path") if not _prior else None))
    if original_rel:
        body["original_relative_path"] = original_rel
    body.pop("previous_relative_path", None)
    for key in HISTORY_KEYS:
        if key in _prior and key not in body:
            body[key] = _prior[key]
    if NEWER in _prior:
        raise NewerFormat(folder, {FORMAT_KEY: _prior[NEWER]})
    body = {FORMAT_KEY: FORMAT, **{k: v for k, v in body.items() if k != FORMAT_KEY}}
    Path(opener(target)).write_text(json.dumps(body, indent=2, ensure_ascii=False),
                                    encoding="utf-8")
    return target


def read_state(folder: Path) -> dict | None:
    """The folder's state, or None.

    A state file written by a newer jamp comes back as just its format
    number, under NEWER: nothing in it is read, so nothing is misread, and
    the folder is not settled.  analyze blocks such a folder, so it is not
    rewritten either.
    """
    # Extended past MAX_PATH like every other file this pipeline opens: an
    # unreachable state file reads as "never settled", and the folder is
    # planned again from the tags our own commit wrote.
    path = Path(opener(Path(folder) / STATE_NAME))
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if newer_format(data):
        return {NEWER: data.get(FORMAT_KEY)}
    return data


def is_settled(folder: Path) -> bool:
    """True when this exact folder name was the one we committed."""
    state = read_state(folder)
    return bool(state) and state.get("folder_name") == Path(folder).name
