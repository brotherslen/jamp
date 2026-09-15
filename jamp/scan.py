"""Walking the library and deciding what counts as one show folder.

Show folders sit at varying depth, so we do not assume one.  We find every
directory that directly contains audio, then fold disc subdirectories back into
their parent and record single-child containers (the ``u111105`` case) as extra
evidence about the show inside them.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .audio import (
    AUDIO_EXTS,
    IMAGE_EXTS,
    AudioFile,
    read_audio_file,
)
from .config import Config
from .sidecars import kind_for as sidecar_kind_for

SIDECAR_KINDS = {".ffp": "ffp", ".md5": "md5", ".st5": "st5", ".cue": "cue", ".sfv": "sfv"}
TEXT_EXTS = {".txt", ".nfo", ".info", ".md", ".log"}
SKIP_DIR_NAMES = {"$recycle.bin", "system volume information", ".git", "__macosx"}

_DISC_DIR = re.compile(
    # A disc folder is often named for what is on it as well as which disc it
    # is - "Disc 1_Improvisations", "Disc 3_MMW" - the same way a set marker is
    # found even when something follows it.  A separator is required before the
    # remainder, so "Disc 1_Improvisations" matches and "Disc 12ABC" does not.
    r"^(?:disc|disk|cd|d|set|s)[\s._-]*\d{1,2}(?:[\s._-].*)?\s*$"  # "Disc 1", "d2"
    # A disc folder is sometimes just its number.  jgb91-04-21.dnk holds two
    # folders called "1" and "2"; read as separate shows they resolved to one
    # name and were reported as duplicates of each other, when they are the two
    # discs of a single night.  Only ever with every sibling matching too.
    r"|^\d{1,2}\s*$"
    r"|.+[\[({\s._-](?:disc|disk|cd)\s*\d{1,2}\s*[\])}]?\s*$",  # "... {Disc 1}"
    re.I)

# A folder used to file shows by year or decade - "2012", "1980s", "90s",
# "1972-1974".  These organise a library; they do not name a release, and a
# show inside one belongs to the year, not to a box set.
# The number in a disc folder's name: "Disc 3", "cd2", "... {Disc 1}".
_DISC_NO = re.compile(r"(?:disc|disk|cd|d|set|s)[\s._-]*(\d{1,2})\s*[\])}]?\s*$", re.I)


def disc_number_of(name: str) -> int | None:
    m = _DISC_NO.search(name)
    return int(m.group(1)) if m else None


_YEAR_DIR = re.compile(
    r"^\s*(?:(?:19|20)?\d{2}(?:'?s)?|(?:19|20)\d{2}\s*[-–]\s*(?:19|20)?\d{2})\s*$",
    re.I,
)


def is_year_dir(name: str) -> bool:
    return bool(_YEAR_DIR.match(name))
_ART_DIR = re.compile(r"^(artwork|art|scans?|covers?|images?|photos?)\s*$", re.I)


@dataclass
class DirInfo:
    path: Path
    audio: list[Path] = field(default_factory=list)
    texts: list[Path] = field(default_factory=list)
    sidecars: dict[str, list[Path]] = field(default_factory=dict)
    images: list[Path] = field(default_factory=list)
    others: list[Path] = field(default_factory=list)
    subdirs: list[Path] = field(default_factory=list)
    error: str | None = None

    @property
    def has_audio(self) -> bool:
        return bool(self.audio)


@dataclass
class ShowFolder:
    """One candidate show: a directory plus everything that describes it."""

    path: Path
    root: Path
    artist_dir: str | None
    # Every directory between ROOT and this show, nearest first.  `artist_dir`
    # is the top of that chain; for "Live Music/STS9/<show>" the nearest is
    # "STS9", which is the one that actually names the act.
    artist_dirs: tuple[str, ...] = ()
    container: Path | None = None            # single-child parent, e.g. u111105
    # A multi-show container that is not a release - see filing_containers.
    filing_parent: Path | None = None
    # A parent holding several dated shows and no audio of its own - a box set
    # or official release, e.g. "Spring 1990 (The Other One) (2014)".
    release_dir: Path | None = None
    disc_dirs: list[Path] = field(default_factory=list)
    files: list[AudioFile] = field(default_factory=list)
    texts: list[Path] = field(default_factory=list)
    sidecars: dict[str, list[Path]] = field(default_factory=dict)
    images: list[Path] = field(default_factory=list)
    sibling_names: list[str] = field(default_factory=list)
    child_show_dirs: list[Path] = field(default_factory=list)
    long_paths: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def rel(self) -> str:
        try:
            return str(self.path.relative_to(self.root))
        except ValueError:  # pragma: no cover - defensive
            return str(self.path)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def name_evidence(self) -> list[tuple[str, str]]:
        """(source, text) pairs for date/token extraction, best first."""
        out = [("folder", self.path.name)]
        if self.container is not None:
            out.append(("parent_folder", self.container.name))
        return out


@dataclass
class ScanResult:
    root: Path
    shows: list[ShowFolder] = field(default_factory=list)
    dirs_scanned: int = 0
    multi_show_containers: list[tuple[Path, list[Path]]] = field(default_factory=list)
    # Folders that hold several shows but are not a release: a taper's
    # multi-night bundle, a grab-bag.  Filing, so their shows are named
    # like any other and can be lifted out with --unnest.
    filing_containers: list[tuple[Path, list[Path]]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    long_paths: list[str] = field(default_factory=list)


def _classify_file(path: Path, info: DirInfo) -> None:
    ext = path.suffix.lower()
    kind = sidecar_kind_for(path)
    if ext in AUDIO_EXTS:
        info.audio.append(path)
    elif kind:
        info.sidecars.setdefault(kind, []).append(path)
    elif ext in TEXT_EXTS:
        info.texts.append(path)
    elif ext in IMAGE_EXTS:
        info.images.append(path)
    else:
        info.others.append(path)


def walk_dirs(root: Path, max_depth: int | None = None,
              skip_names: set[str] | None = None,
              include_top: set[str] | None = None,
              ignore: tuple[str, ...] = (),
              errors: list | None = None) -> dict[Path, DirInfo]:
    """Walk ROOT.  `include_top` limits which folders are entered.

    The artist folder is ROOT/<artist>, and the band resolver falls back to it,
    so working one artist at a time means filtering here rather than pointing
    ROOT at the artist folder itself.

    An entry may be a bare top-level name ("Phish") or a relative path
    ("Live Music/STS9"), which scopes a run to one act inside a container
    folder.  A path is kept if it lies on the way down to a wanted folder or
    anywhere beneath one; without the first of those the walk would be pruned
    at the container and quietly find nothing at all.
    """
    out: dict[Path, DirInfo] = {}
    root = root.resolve()
    skip = SKIP_DIR_NAMES | {s.lower() for s in (skip_names or set())}
    wanted = ({s.replace("\\", "/").strip("/").lower() for s in include_top}
              if include_top else None)
    # A bare name matches any folder so called; a path matches just that one.
    ignore_names = {i.lower() for i in ignore if "/" not in i and "\\" not in i}
    ignore_paths = {i.replace("\\", "/").strip("/").lower()
                    for i in ignore if "/" in i or "\\" in i}
    def _unreadable(exc: OSError) -> None:
        # A folder that cannot be listed is not a folder with nothing in it.
        # Dropping it silently made every show inside vanish from every phase,
        # with no line in any report saying so.
        if errors is not None:
            errors.append((str(getattr(exc, "filename", "") or ""),
                           "%s: %s" % (exc.__class__.__name__, exc)))

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=_unreadable):
        here = Path(dirpath)
        def _keep(d: str) -> bool:
            if d.startswith(".") or d.lower() in skip or d.lower() in ignore_names:
                return False
            rel = (here / d).relative_to(root).as_posix().lower()
            return rel not in ignore_paths

        def _in_scope(d: str) -> bool:
            if wanted is None:
                return True
            rel = (here / d).relative_to(root).as_posix().lower()
            return any(rel == w or w.startswith(rel + "/") or rel.startswith(w + "/")
                       for w in wanted)

        dirnames[:] = [d for d in dirnames if _keep(d) and _in_scope(d)]
        if max_depth is not None:
            depth = len(here.relative_to(root).parts)
            if depth >= max_depth:
                dirnames[:] = []
        info = DirInfo(path=here, subdirs=[here / d for d in dirnames])
        for fname in filenames:
            if fname.startswith("."):
                continue
            _classify_file(here / fname, info)
        out[here] = info
    return out


def _artist_dir_for(path: Path, root: Path) -> str | None:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return None
    return parts[0] if len(parts) > 1 else None


def _artist_dirs_for(path: Path, root: Path) -> tuple[str, ...]:
    """The directories above a show, nearest first.

    A container folder like "Live Music" is not an act, and the act's name sits
    one level below it - where nothing used to look, so every show under it
    resolved to no band at all.  Nearest first because the closest folder is the
    most specific: "Live Music/STS9/<show>" offers "STS9" before "Live Music".
    """
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return ()
    return tuple(reversed(parts[:-1]))


def build_shows(
    root: Path,
    dirs: dict[Path, DirInfo],
    cfg: Config,
    read_tags: bool = True,
    tag_sample: int = 0,
    reads=None,
) -> ScanResult:
    root = root.resolve()
    read = reads.read if reads is not None else read_audio_file
    result = ScanResult(root=root, dirs_scanned=len(dirs))
    audio_dirs = {p for p, d in dirs.items() if d.has_audio}
    consumed: set[Path] = set()
    show_paths: list[tuple[Path, list[Path], Path | None]] = []  # (show, disc_dirs, container)

    # First: a folder with no audio of its own whose audio children are ALL
    # disc folders is one show split across discs - "Roadsongs/Disc 1, Disc 2",
    # or a box set laid out the same way.  Without this each disc becomes a
    # separate show and the release looks like several concerts.
    for path, info in sorted(dirs.items()):
        if info.has_audio or path == root:
            continue
        disc_children = [c for c in info.subdirs if c in audio_dirs]
        if len(disc_children) < 1 or not all(_DISC_DIR.match(c.name) for c in disc_children):
            continue
        if any(c in consumed for c in disc_children):
            continue
        show_paths.append((path, disc_children, None))
        consumed.add(path)
        consumed.update(disc_children)

    for path in sorted(audio_dirs):
        if path in consumed:
            continue
        info = dirs[path]
        # Disc subdirectories fold into this folder.
        disc_children = [
            c for c in info.subdirs
            if c in audio_dirs and _DISC_DIR.match(c.name)
        ]
        for c in disc_children:
            consumed.add(c)
        show_paths.append((path, disc_children, None))
        consumed.add(path)

    # Containers that hold shows but no audio of their own.
    for path, info in sorted(dirs.items()):
        if info.has_audio or path == root:
            continue
        child_shows = [c for c, _, _ in show_paths if c.parent == path]
        if not child_shows:
            continue
        if len(child_shows) == 1 and not is_year_dir(path.name):
            # A year folder holding a single show is still a year folder: the
            # show stays filed under it and is never lifted out.
            for i, (sp, discs, _) in enumerate(show_paths):
                if sp == child_shows[0]:
                    show_paths[i] = (sp, discs, path)
        else:
            # An artist folder holds many shows and means nothing by it.  A
            # release folder sits below the artist level: ROOT/artist/release/.
            # A year folder is filing, not a release: Phish/2012/ is not a box
            # set, and its shows keep their ordinary names.
            depth = len(path.relative_to(root).parts)
            if depth >= 2 and not is_year_dir(path.name):
                # Holding several shows is not what makes something a release.
                # A box set and a taper's two-night bundle are the same shape -
                # "Europe '72 Complete Recordings" and "Phish Gorge 1998 gotfob"
                # both hold one folder per night - so shape alone gave a fan
                # bundle a release's naming and buried its shows a level down.
                # Being a release is a stated fact, from config official_series.
                from .classify import match_series
                if match_series(cfg, [path.name]):
                    result.multi_show_containers.append((path, child_shows))
                else:
                    result.filing_containers.append((path, child_shows))

    release_dirs = {c for c, _ in result.multi_show_containers}
    filing_dirs = {c for c, _ in result.filing_containers}
    # Which shows sit directly inside which folder.  A folder that holds shows
    # of its own is a container; loose audio beside them does not make it a
    # show.  Disc subdirectories are already folded in above and never appear
    # here, so a three-disc show is not mistaken for a container.
    children_of: dict[Path, list[Path]] = {}
    for sp, _, _ in show_paths:
        children_of.setdefault(sp.parent, []).append(sp)

    for path, disc_children, container in show_paths:
        info = dirs[path]
        show = ShowFolder(
            path=path,
            root=root,
            artist_dir=_artist_dir_for(path, root),
            artist_dirs=_artist_dirs_for(path, root),
            container=container,
            release_dir=path.parent if path.parent in release_dirs else None,
            filing_parent=path.parent if path.parent in filing_dirs else None,
            disc_dirs=list(disc_children),
            child_show_dirs=sorted(children_of.get(path, [])),
        )
        audio_paths = list(info.audio)
        for child in disc_children:
            cinfo = dirs[child]
            audio_paths.extend(cinfo.audio)
            show.texts.extend(cinfo.texts)
            show.images.extend(cinfo.images)
            for kind, paths in cinfo.sidecars.items():
                show.sidecars.setdefault(kind, []).extend(paths)
        show.texts.extend(info.texts)
        show.images.extend(info.images)
        for kind, paths in info.sidecars.items():
            show.sidecars.setdefault(kind, []).extend(paths)
        if container is not None:
            cinfo = dirs[container]
            show.texts.extend(cinfo.texts)
            for kind, paths in cinfo.sidecars.items():
                show.sidecars.setdefault(kind, []).extend(paths)
            show.notes.append("nested inside container folder %r" % container.name)

        # Which disc a file belongs to is written on the folder it sits in, and
        # that is better evidence than the filename: inside "Disc 2" a track
        # called "01 Ramble on Rose" is disc 2 track 1.  Without this every
        # disc's track 01 collides on one name and the release cannot be
        # renamed at all.
        disc_of = {c: disc_number_of(c.name) for c in disc_children}

        audio_paths.sort()
        sample_idx = _sample_indexes(len(audio_paths), tag_sample)
        for i, ap in enumerate(sorted(audio_paths)):
            want_tags = read_tags and (not tag_sample or i in sample_idx)
            try:
                af = read(ap, read_tags=want_tags)
            except OSError as exc:
                result.errors.append((str(ap), "%s: %s" % (exc.__class__.__name__, exc)))
                continue
            from_disc = disc_of.get(ap.parent)
            if from_disc and not af.name_info.disc:
                af.name_info.disc = from_disc
                af.name_info.disc_from_folder = True
            show.files.append(af)

        limit = cfg.settings.max_path_length
        for p in [path, *audio_paths, *show.texts]:
            if len(str(p)) > limit:
                show.long_paths.append(str(p))
                result.long_paths.append(str(p))

        parent = path.parent
        show.sibling_names = sorted(
            d.name for d in dirs.get(parent, DirInfo(path=parent)).subdirs if d != path
        )
        result.shows.append(show)

    result.shows.sort(key=lambda s: str(s.path).lower())
    return result


def _sample_indexes(count: int, sample: int) -> set[int]:
    if not sample or count <= sample:
        return set(range(count))
    step = max(1, count // sample)
    idx = {min(i * step, count - 1) for i in range(sample)}
    idx.add(0)
    idx.add(count - 1)
    return idx


def scan(
    root: Path | str,
    cfg: Config,
    read_tags: bool = True,
    tag_sample: int = 0,
    max_depth: int | None = None,
    include_top: set[str] | None = None,
    reads=None,
) -> ScanResult:
    """Every show under ROOT.  `reads`, a reads.ReadCache, reuses file reads."""
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise NotADirectoryError("ROOT is not a directory: %s" % root_path)
    # The review folder holds files we set aside; it is not part of the library.
    walk_errors: list[tuple[str, str]] = []
    dirs = walk_dirs(root_path, max_depth=max_depth,
                     skip_names={cfg.settings.review_folder},
                     include_top=include_top,
                     ignore=cfg.settings.ignore_folders,
                     errors=walk_errors)
    result = build_shows(root_path, dirs, cfg, read_tags=read_tags, tag_sample=tag_sample,
                         reads=reads)
    result.errors[:0] = walk_errors
    return result
