"""Boundary-aware token matching over messy folder names.

Folder names separate things with any of ``- _ . space``, so a naive
``"sbd" in name`` matches inside words and a naive word-boundary regex misses
``4011s`` and ``CA-11``.  Everything that needs "does this name contain this
token" goes through here.
"""
from __future__ import annotations

import re

SEPARATORS = "-_. /[](){}+,"
_SEP_CLASS = r"[^a-z0-9]"


def normalize(text: str) -> str:
    return text.lower().strip()


def _token_body(token: str) -> str:
    """Letter/digit runs joined by an optional separator.

    ``akg414`` then matches "akg414", "akg 414" and "akg-414"; ``ca-11`` matches
    "ca-11", "ca11" and "ca 11".  Real names spell these every possible way.
    """
    runs = re.findall(r"[a-z]+|[0-9]+", token.lower())
    if not runs:
        return re.escape(token.lower())
    return r"[-_. ]?".join(re.escape(r) for r in runs)


def _token_pattern(token: str) -> "re.Pattern[str]":
    """A token match must be preceded and followed by a separator or an edge.

    A trailing plural ``s`` is allowed so ``4011s`` matches the mic ``4011``.
    """
    body = _token_body(token)
    return re.compile(r"(?:(?<=^)|(?<=" + _SEP_CLASS + r"))" + body + r"s?(?=" + _SEP_CLASS + r"|$)")


_CACHE: dict[str, "re.Pattern[str]"] = {}


def has_token(text: str, token: str) -> bool:
    pat = _CACHE.get(token)
    if pat is None:
        pat = _CACHE[token] = _token_pattern(token)
    return bool(pat.search(normalize(text)))


def find_tokens(text: str, tokens) -> list[str]:
    """Every token from `tokens` present in `text`, in config order."""
    low = normalize(text)
    return [t for t in tokens if has_token(low, t)]


def has_phrase(text: str, phrase: str) -> bool:
    """Substring match for multi-word phrases like ``dave's picks``.

    Separators in the haystack are flattened so ``Daves.Picks.16`` still hits.
    """
    flat = re.sub(r"[^a-z0-9]+", " ", normalize(text))
    needle = re.sub(r"[^a-z0-9]+", " ", normalize(phrase)).strip()
    return bool(needle) and needle in flat


def mask_spans(text: str, spans) -> str:
    """Blank out character ranges (used to hide date digits from token scans)."""
    chars = list(text)
    for start, end in spans:
        for i in range(max(0, start), min(len(chars), end)):
            chars[i] = " "
    return "".join(chars)


def split_tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^A-Za-z0-9&']+", text) if t]


def strip_format_suffixes(name: str, suffixes) -> str:
    """Drop trailing ``.flac16`` / ``.shnf`` style descriptors from a folder name."""
    out = name
    changed = True
    while changed:
        changed = False
        for suffix in sorted(suffixes, key=len, reverse=True):
            for sep in (".", " ", "_", "-"):
                tail = sep + suffix
                if out.lower().endswith(tail):
                    out = out[: -len(tail)]
                    changed = True
                    break
            if changed:
                break
    return out.strip(" .-_")
