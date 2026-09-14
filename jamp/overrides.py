"""Answers you have given, that the files themselves do not contain.

Some things cannot be worked out from a folder: which night "New Year's Eve
2006" was, the venue behind an abbreviation, the band on an untagged disc.  When
you settle one, it goes here rather than into the code, so the pipeline keeps
one way of knowing things and the reasoning stays inspectable.

An override is matched on the folder name, and it wins over everything the
files say - that is the point of it - so each one is reported wherever it is
used.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

OVERRIDES_NAME = "overrides.yaml"

FIELDS = ("date", "band", "venue", "city", "state", "source", "provenance",
          "format", "marker", "classification", "skip", "note")


@dataclass
class Override:
    match: str
    values: dict = field(default_factory=dict)

    @property
    def is_skip(self) -> bool:
        return bool(self.values.get("skip"))


def _norm_key(text: str) -> str:
    """Fold case and both slash styles, so keys are written either way."""
    return str(text).strip().strip("/\\").replace("\\", "/").lower()


class Overrides:
    """The whole table, looked up by folder name."""

    def __init__(self, entries: list[Override] | None = None, path: Path | None = None):
        self._by_name: dict[str, Override] = {}
        self._by_path: dict[str, Override] = {}
        for entry in entries or []:
            key = _norm_key(entry.match)
            # A key with a separator in it addresses a folder by where it sits,
            # not just by what it is called.  Generic names like "Disc One"
            # repeat all over a library, so keying those by name alone would
            # silently apply one show's answer to every other.
            if "/" in key:
                self._by_path[key] = entry
            else:
                self._by_name[key] = entry
        self.path = path

    def __len__(self) -> int:
        # Both kinds.  Counting only the names reported 49 folders from a file
        # that held 57 entries.
        return len(self._by_name) + len(self._by_path)

    def for_folder(self, folder_name: str,
                   relative_path: str | None = None) -> Override | None:
        """The entry for this folder: a path key wins over a bare name key."""
        if relative_path:
            rel = _norm_key(relative_path)
            for key, entry in self._by_path.items():
                if rel == key or rel.endswith("/" + key):
                    return entry
        return self._by_name.get(_norm_key(folder_name))

    @classmethod
    def load(cls, path: Path | str | None) -> "Overrides":
        if path is None:
            return cls()
        path = Path(path)
        if not path.exists():
            return cls(path=path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries = []
        for match, values in (data.get("folders") or {}).items():
            values = values or {}
            unknown = set(values) - set(FIELDS)
            if unknown:
                raise ValueError(
                    "%s: %r sets unknown field(s) %s; known fields are %s"
                    % (path.name, match, ", ".join(sorted(unknown)), ", ".join(FIELDS))
                )
            entries.append(Override(match=str(match), values=values))
        return cls(entries, path=path)
