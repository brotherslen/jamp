"""Loading and validation of the user-maintained YAML config.

Nothing in this module invents data.  If a band, a microphone or a store is not
in the config it simply does not exist as far as the pipeline is concerned - it
becomes a report line, never a guess.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path

from .userdir import ACTS_NAME, SHIPPED_DIR, user_config_path
from .venues import VENUES_NAME, Gazetteer
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = SHIPPED_DIR / "jamp.yaml"

# Lists of entries that have a name of their own.  A user entry with the same
# name replaces the shipped one whole; any other is added.  Merging them field
# by field would let a user's Phish keep a shipped alias they had removed.
_KEYED_LISTS = {"bands": "abbrev", "side_projects": "abbrev",
                "official_series": "name"}
_BAND_LISTS = ("bands", "side_projects")


class ConfigError(ValueError):
    pass


def _union(base: list, extra: list) -> list:
    out = list(base)
    for item in extra:
        if item not in out:
            out.append(item)
    return out


def _merge_mapping(base: dict, over: dict) -> dict:
    """Mappings merge key by key, lists add what is new, scalars are replaced."""
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge_mapping(out[key], value)
        elif isinstance(value, list) and isinstance(out.get(key), list):
            out[key] = _union(out[key], value)
        else:
            out[key] = value
    return out


def merge_layers(shipped: dict, user: dict) -> dict:
    """The config a run uses: the user's file laid over the shipped one."""
    out = dict(shipped)
    plain = {k: v for k, v in user.items() if k not in _KEYED_LISTS}
    out = _merge_mapping(out, plain)

    # An act is one entry whichever list it sits in, so a user who files a
    # shipped band as a side project moves it rather than duplicating it.
    def ident(entry, key):
        return str(entry.get(key, "")).strip().lower() if isinstance(entry, dict) else ""

    user_abbrevs = {ident(b, "abbrev")
                    for name in _BAND_LISTS for b in (user.get(name) or [])}
    for name, key in _KEYED_LISTS.items():
        mine = {ident(e, key): e for e in (user.get(name) or [])}
        # Taken over in place, so the order of the shipped list - which the
        # matcher can depend on - is kept.
        merged = []
        for entry in shipped.get(name) or []:
            k = ident(entry, key)
            if k in mine:
                merged.append(mine.pop(k))
            elif name in _BAND_LISTS and k in user_abbrevs:
                continue    # the user filed this act in the other list
            else:
                merged.append(entry)
        if merged or mine or name in shipped:
            out[name] = merged + list(mine.values())
    return out


def _read_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return data


@dataclass(frozen=True)
class Band:
    abbrev: str
    name: str
    prefixes: tuple[str, ...]
    aliases: tuple[str, ...]
    active_years: tuple[int, int] | None
    is_side_project: bool = False
    parent: str | None = None
    # This abbreviation also files shows by related acts, so a more specific
    # member of the family named later in the folder wins over it.
    family_prefix: bool = False
    # From `defaults_from_year` on, a folder that names only this family is
    # taken to mean `defaults_to` - the act that by then WAS the family - unless
    # the name mentions one of `defaults_unless_named`, which are the billings
    # that mean some other lineup played.
    defaults_to: str | None = None
    defaults_from_year: int | None = None
    defaults_unless_named: tuple[str, ...] = ()

    def plausible_year(self, year: int) -> bool:
        if not self.active_years:
            return True
        lo, hi = self.active_years
        return lo <= year <= hi


@dataclass(frozen=True)
class Provenance:
    key: str
    aliases: tuple[str, ...]
    source: str | None
    official: bool


@dataclass(frozen=True)
class Series:
    name: str
    band: str | None
    patterns: tuple[str, ...]
    release_id: str | None
    multi_date: bool


@dataclass
class Settings:
    genre: str = "Live"
    preserve_official_genre: bool = True
    folder_location: str = "venue_only"     # venue_only / any_known / off
    folder_location_max: int = 60
    review_folder: str = "_etree_review"
    ignore_folders: tuple[str, ...] = ()
    unknown_defaults_to_unofficial: bool = True
    rename_attempts: int = 5
    rename_retry_delay: float = 0.15
    min_date_confidence_commit: int = 70
    min_date_confidence_plan: int = 25
    earliest_show_date: _dt.date = _dt.date(1960, 1, 1)
    phase0_tag_sample: int = 4
    max_path_length: int = 260
    classify_min_score: int = 50
    classify_min_margin: int = 15


@dataclass
class Config:
    settings: Settings
    bands: tuple[Band, ...]
    provenance: tuple[Provenance, ...]
    series: tuple[Series, ...]
    microphones: tuple[str, ...]
    source_tokens: dict[str, tuple[str, ...]]
    non_show_tokens: tuple[str, ...]
    format_suffixes: tuple[str, ...]
    quality_tokens: dict[str, tuple[str, ...]]
    us_states: frozenset[str]
    broadcast_stations: tuple[str, ...] = ()
    weights: dict[str, dict[str, int]] = field(default_factory=dict)
    tapers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    references: dict[str, str] = field(default_factory=dict)
    reference_default: str = ""
    reference_albums: str = ""
    path: Path | None = None
    # The user's own jamp.yaml laid over `path`, or None when there is none.
    user_path: Path | None = None
    # The acts.yaml `jamp acts` wrote, laid under user_path, or None.
    acts_path: Path | None = None
    # The rooms this library knows, from venues.yaml beside this config. Empty
    # when that file is absent, which is a normal state: the pipeline worked
    # before the gazetteer existed and must keep working for anyone without one.
    venues: object = None

    # -- lookups ----------------------------------------------------------
    _by_abbrev: dict[str, Band] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._by_abbrev = {b.abbrev: b for b in self.bands}

    def band(self, abbrev: str | None) -> Band | None:
        return self._by_abbrev.get(abbrev) if abbrev else None

    def reference_for(self, abbrev: str | None, album: bool = False) -> str:
        """Where to look a show - or a studio release - up by hand."""
        if album and self.reference_albums:
            return self.reference_albums
        return self.references.get(abbrev or "", self.reference_default)

    def canonical_taper(self, raw: str | None) -> str | None:
        """Map a raw taper string onto its one canonical slug, if we know it."""
        if not raw:
            return None
        needle = re.sub(r"[^a-z0-9]", "", raw.lower())
        if not needle:
            return None
        for slug, aliases in self.tapers.items():
            for alias in (slug, *aliases):
                flat = re.sub(r"[^a-z0-9]", "", alias.lower())
                if flat and (flat == needle or flat in needle):
                    return slug
        return None

    def provenance_for(self, key: str | None) -> Provenance | None:
        if not key:
            return None
        for p in self.provenance:
            if p.key == key:
                return p
        return None

    @property
    def all_band_names(self) -> list[str]:
        out: list[str] = []
        for b in self.bands:
            out.append(b.name)
            out.extend(b.aliases)
        return out


def _as_date(value: Any, label: str) -> _dt.date:
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str):
        return _dt.date.fromisoformat(value)
    raise ConfigError(f"{label}: expected a date, got {value!r}")


def _tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


def _band_from(raw: dict, *, side: bool) -> Band:
    try:
        abbrev = str(raw["abbrev"]).strip().lower()
        name = str(raw["name"]).strip()
    except KeyError as exc:  # pragma: no cover - config authoring error
        raise ConfigError(f"band entry missing {exc}") from exc
    years = raw.get("active_years")
    if years is not None:
        if len(years) != 2:
            raise ConfigError(f"{abbrev}: active_years must be [start, end]")
        years = (int(years[0]), int(years[1]))
    return Band(
        abbrev=abbrev,
        name=name,
        prefixes=tuple(p.lower() for p in _tuple(raw.get("prefixes"))),
        aliases=_tuple(raw.get("aliases")),
        active_years=years,
        is_side_project=side,
        parent=raw.get("parent"),
        family_prefix=bool(raw.get("family_prefix", False)),
        defaults_to=raw.get("defaults_to"),
        defaults_from_year=raw.get("defaults_from_year"),
        defaults_unless_named=tuple(
            str(w).lower() for w in raw.get("defaults_unless_named", ()) or ()),
    )


_USER_DEFAULT = object()


def load_config(path: Path | str | None = None,
                user_path: Path | str | None = _USER_DEFAULT) -> Config:
    """The shipped config (or ``path``), with the user's own laid over it.

    ``user_path`` defaults to the user folder's jamp.yaml; a missing file is
    the normal state for a new user and means the shipped config alone.  Pass
    None to read no user layer at all.
    """
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise ConfigError(f"config file not found: {cfg_path}")
    data = _read_yaml(cfg_path)
    if user_path is _USER_DEFAULT:
        user_path = user_config_path()
    user_file = Path(user_path) if user_path else None
    # The user's venues and acts sit beside their jamp.yaml, whether or not
    # that exists.  acts.yaml is what `jamp acts` writes; jamp.yaml is laid
    # over it, so a hand edit always has the last word.
    user_venues = user_file.parent / VENUES_NAME if user_file else None
    acts_file = user_file.parent / ACTS_NAME if user_file else None
    for layer in (acts_file, user_file):
        if layer is not None and layer.exists():
            try:
                data = merge_layers(data, _read_yaml(layer))
            except (AttributeError, TypeError) as exc:
                raise ConfigError(f"{layer}: {exc}") from exc
    if user_file is not None and not user_file.exists():
        user_file = None
    if acts_file is not None and not acts_file.exists():
        acts_file = None

    s = data.get("settings") or {}
    settings = Settings(
        genre=s.get("genre", "Live"),
        preserve_official_genre=bool(s.get("preserve_official_genre", True)),
        folder_location=str(s.get("folder_location", "venue_only")).lower(),
        folder_location_max=int(s.get("folder_location_max", 60)),
        review_folder=str(s.get("review_folder", "_etree_review")),
        ignore_folders=tuple(str(x) for x in (s.get("ignore_folders") or ())),
        unknown_defaults_to_unofficial=bool(
            s.get("unknown_defaults_to_unofficial", True)),
        rename_attempts=int(s.get("rename_attempts", 5)),
        rename_retry_delay=float(s.get("rename_retry_delay", 0.15)),
        min_date_confidence_commit=int(s.get("min_date_confidence_commit", 70)),
        min_date_confidence_plan=int(s.get("min_date_confidence_plan", 25)),
        earliest_show_date=_as_date(
            s.get("earliest_show_date", "1960-01-01"), "earliest_show_date"
        ),
        phase0_tag_sample=int(s.get("phase0_tag_sample", 4)),
        max_path_length=int(s.get("max_path_length", 260)),
        classify_min_score=int(s.get("classify_min_score", 50)),
        classify_min_margin=int(s.get("classify_min_margin", 15)),
    )

    if data.get("date_confidence"):
        from . import dates as _dates

        try:
            _dates.apply_confidence_overrides(data["date_confidence"])
        except KeyError as exc:
            raise ConfigError("date_confidence: %s" % exc) from exc

    if settings.folder_location not in ("venue_only", "any_known", "off"):
        raise ConfigError(
            "folder_location must be venue_only, any_known or off, not %r"
            % settings.folder_location
        )

    bands = [_band_from(b, side=False) for b in data.get("bands") or []]
    bands += [_band_from(b, side=True) for b in data.get("side_projects") or []]
    seen: set[str] = set()
    for b in bands:
        if b.abbrev in seen:
            raise ConfigError(f"duplicate band abbreviation: {b.abbrev}")
        seen.add(b.abbrev)

    prov = []
    for key, raw in (data.get("provenance") or {}).items():
        prov.append(
            Provenance(
                key=str(key).lower(),
                aliases=tuple(a.lower() for a in _tuple(raw.get("aliases")) or (key,)),
                source=(raw.get("source") or None),
                official=bool(raw.get("official", False)),
            )
        )

    series = [
        Series(
            name=str(r["name"]),
            band=r.get("band"),
            patterns=tuple(p.lower() for p in _tuple(r.get("patterns"))),
            release_id=r.get("release_id"),
            multi_date=bool(r.get("multi_date", False)),
        )
        for r in data.get("official_series") or []
    ]

    src_tokens = {
        k: tuple(t.lower() for t in _tuple(v))
        for k, v in (data.get("source_tokens") or {}).items()
    }
    for required in ("matrix", "soundboard", "audience"):
        src_tokens.setdefault(required, ())

    quality = {
        k: tuple(t.lower() for t in _tuple(v))
        for k, v in (data.get("quality_tokens") or {}).items()
    }

    return Config(
        settings=settings,
        bands=tuple(bands),
        provenance=tuple(prov),
        series=tuple(series),
        microphones=tuple(str(m).lower() for m in data.get("microphones") or []),
        source_tokens=src_tokens,
        non_show_tokens=tuple(str(t).lower() for t in data.get("non_show_tokens") or []),
        format_suffixes=tuple(str(t).lower() for t in data.get("format_suffixes") or []),
        quality_tokens=quality,
        us_states=frozenset(str(x).upper() for x in data.get("us_states") or []),
        broadcast_stations=tuple(
            str(s).lower() for s in (data.get("broadcast") or {}).get("stations") or []
        ),
        weights={
            group: {k: int(v) for k, v in (items or {}).items()}
            for group, items in (data.get("classifier_weights") or {}).items()
        },
        tapers={
            str(slug).lower(): _tuple(aliases)
            for slug, aliases in (data.get("tapers") or {}).items()
        },
        references={
            str(k): str(v)
            for k, v in ((data.get("references") or {}).get("bands") or {}).items()
        },
        reference_default=str((data.get("references") or {}).get("default", "")),
        reference_albums=str((data.get("references") or {}).get("albums", "")),
        path=cfg_path,
        user_path=user_file,
        acts_path=acts_file,
        venues=Gazetteer.load_many([cfg_path.parent / VENUES_NAME, user_venues]),
    )
