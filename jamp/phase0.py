"""Phase 0 - inventory.  Reads the library and writes down what is actually
there.  Nothing is renamed, retagged, moved or written inside ROOT.

The point of this phase is to replace guesses with counts before any regex is
finalised: which date formats really occur, how the OFFICIAL/UNOFFICIAL split
falls out, how many folders have an info file, what disc and set layouts exist,
and which names the parser could not read.
"""
from __future__ import annotations

import datetime as _dt
import re
from collections import Counter
from pathlib import Path

from . import classify as _classify
from . import identity as _identity
from . import integrity as _integrity
from .analyze import BOXSET, SEVERITY_BLOCK, ShowAnalysis, analyze_show
from .config import Config
from .report import TextReport, ensure_out_dir, run_lock, write_csv, write_json
from .scan import ScanResult, scan

UNPARSED_SAMPLE = 25


def run(
    root: Path,
    out_dir: Path,
    cfg: Config,
    tag_sample: int | None = None,
    today: _dt.date | None = None,
    include_top: set[str] | None = None,
    overrides=None,
    verify_audio: bool = False,
    workers: int = 4,
    progress=None,
) -> dict:
    # Guard here as well as in the CLI, so no caller can drop reports into the
    # library by going around the front door.
    out_dir = ensure_out_dir(out_dir, root)
    with run_lock(out_dir, "phase0"):
        sample = cfg.settings.phase0_tag_sample if tag_sample is None else tag_sample
        result = scan(root, cfg, read_tags=True, tag_sample=sample,
                      include_top=include_top)
        analyses = [analyze_show(show, cfg, today=today, overrides=overrides)
                    for show in result.shows]
        stats = summarize(result, analyses, cfg)

        # Identity is read from every file as it is scanned, so this costs
        # nothing beyond the arithmetic: which folders hold the same audio.
        index = _identity.index_folders(result.shows)
        matches = _identity.find_matches(index)
        repeats = _identity.repeated_within(index)
        stats.update(_identity.summarize(index, matches))

        # Integrity is a different order of cost - a full decode of every file -
        # so it happens only when asked for.
        verdicts = []
        if verify_audio:
            verdicts = _verify_all(result, workers=workers, progress=progress)
            for key in (_integrity.PASS, _integrity.MISMATCH,
                        _integrity.UNREADABLE, _integrity.NO_MD5):
                stats["verify_" + key.lower()] = sum(
                    1 for v in verdicts if v["status"] == key)

        _write_reports(out_dir, result, analyses, stats, cfg,
                       index, matches, repeats, verdicts)
        return stats


def _verify_all(result: ScanResult, workers: int = 4, progress=None) -> list:
    """Decode every FLAC and compare it against the MD5 it carries.

    Opt-in, because it is hours rather than minutes.  What it catches is a file
    that decodes perfectly well to audio that is not what its own header
    describes - damage no structural check can see, because from outside such a
    file reads, tags and plays exactly like a healthy one.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    ffmpeg = _integrity.find_ffmpeg()
    jobs = [(show, f) for show in result.shows for f in show.files
            if f.ext == ".flac"]
    out = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_integrity.verify, ffmpeg, f.path): (show, f)
                   for show, f in jobs}
        for i, fut in enumerate(as_completed(futures), 1):
            show, f = futures[fut]
            try:
                v = dict(fut.result())
            except Exception as exc:                # noqa: BLE001 - never silent
                v = {"status": _integrity.UNREADABLE, "stored_md5": "",
                     "decoded_md5": "", "seconds": None,
                     "detail": "verifier raised: %s" % str(exc)[:200]}
            v["folder"] = str(show.path.relative_to(show.root))
            v["file"] = f.name
            out.append(v)
            if progress and (i % 50 == 0 or i == len(futures)):
                progress("  verified %d/%d" % (i, len(futures)))
    out.sort(key=lambda v: (v["folder"], v["file"]))
    return out


def summarize(result: ScanResult, analyses: list[ShowAnalysis], cfg: Config) -> dict:
    ext_counts: Counter = Counter()
    fmt_counts: Counter = Counter()
    date_patterns: Counter = Counter()
    kinds: Counter = Counter()
    shapes: Counter = Counter()
    bands: Counter = Counter()
    band_evidence: Counter = Counter()
    issues: Counter = Counter()
    signals: Counter = Counter()
    track_patterns: Counter = Counter()
    disc_layouts: Counter = Counter()
    sources: Counter = Counter()
    provenance: Counter = Counter()
    confidence_buckets: Counter = Counter()
    sidecars: Counter = Counter()

    total_bytes = 0
    audio_files = 0
    with_info = 0
    with_torrent_marker = 0
    undated = 0
    boxsets = 0
    blocked = 0

    for a in analyses:
        show = a.show
        audio_files += len(show.files)
        total_bytes += show.total_bytes
        for f in show.files:
            ext_counts[f.ext or "(none)"] += 1
            fmt_counts[f.fmt or "(unknown)"] += 1
            track_patterns[f.name_info.pattern] += 1

        kinds[a.classification.kind] += 1
        shapes[a.classification.shape or "-"] += 1
        bands[a.band.abbrev or "(unresolved)"] += 1
        band_evidence[a.band.matched_by or "(none)"] += 1
        sources[a.source.value or "(none)"] += 1
        provenance[a.provenance or "(none)"] += 1
        for code in a.issue_codes:
            issues[code] += 1
        for sig in a.classification.signals:
            signals[sig.name] += 1
        for kind, paths in show.sidecars.items():
            sidecars[kind] += len(paths)

        if a.info.chosen:
            with_info += 1
        if a.info.torrent_markers:
            with_torrent_marker += 1

        if a.date.date is None:
            undated += 1
            date_patterns["(no date)"] += 1
            confidence_buckets["0 (no date)"] += 1
        else:
            winner = None
            for ev in a.date_evidence:
                for cand in ev.candidates:
                    if cand.date == a.date.date:
                        winner = cand
                        break
                if winner:
                    break
            date_patterns[winner.pattern if winner else "(unknown)"] += 1
            confidence_buckets[_bucket(a.date.confidence)] += 1

        if a.category == BOXSET:
            boxsets += 1
        if a.blocked:
            blocked += 1

        discs = {f.name_info.disc for f in show.files if f.name_info.disc}
        sets_ = {f.name_info.set_no for f in show.files if f.name_info.set_no}
        if show.disc_dirs:
            layout = "disc subfolders (%d)" % len(show.disc_dirs)
        elif sets_:
            layout = "set numbering in filenames (%d sets)" % len(sets_)
        elif len(discs) > 1:
            layout = "multi-disc in filenames (%d discs)" % len(discs)
        elif discs:
            layout = "single disc in filenames"
        else:
            layout = "flat, no disc or set numbering"
        disc_layouts[layout] += 1

    return {
        "root": str(result.root),
        "dirs_scanned": result.dirs_scanned,
        "show_folders": len(analyses),
        "audio_files": audio_files,
        "total_bytes": total_bytes,
        "extensions": dict(ext_counts),
        "formats": dict(fmt_counts),
        "date_patterns": dict(date_patterns),
        "date_confidence": dict(confidence_buckets),
        "classification": dict(kinds),
        "official_shapes": dict(shapes),
        "bands": dict(bands),
        "band_evidence": dict(band_evidence),
        "sources": dict(sources),
        "provenance": dict(provenance),
        "issues": dict(issues),
        "classifier_signals": dict(signals),
        "track_filename_patterns": dict(track_patterns),
        "disc_layouts": dict(disc_layouts),
        "sidecars": dict(sidecars),
        "folders_with_info_file": with_info,
        "folders_with_torrent_marker": with_torrent_marker,
        "undated_folders": undated,
        "multi_date_releases": boxsets,
        "blocked_from_rename": blocked,
        "multi_show_containers": [
            {"container": str(c), "shows": [str(s) for s in shows]}
            for c, shows in result.multi_show_containers
        ],
        "long_paths": result.long_paths,
        "scan_errors": [{"path": p, "error": e} for p, e in result.errors],
    }


def _bucket(conf: int) -> str:
    if conf >= 90:
        return "90-99"
    if conf >= 70:
        return "70-89 (commit threshold is 70)"
    if conf >= 50:
        return "50-69"
    if conf >= 25:
        return "25-49"
    return "1-24"


def unparsed_sample(analyses: list[ShowAnalysis], limit: int = UNPARSED_SAMPLE):
    """Folders the parser is least sure about, worst first."""
    scored = []
    for a in analyses:
        if not a.blocked:
            continue
        penalty = a.date.confidence
        if a.date.date is None:
            penalty = -1
        scored.append((penalty, a))
    scored.sort(key=lambda t: (t[0], str(t[1].show.path)))
    return [a for _, a in scored[:limit]]


def _band_stub_name(folder_name: str) -> tuple[str, str]:
    """A guessed abbreviation and display name for an unrecognised artist."""
    lead = re.split(r"\d", folder_name, maxsplit=1)[0]
    words = [w for w in re.split(r"[^A-Za-z']+", lead) if w and w.lower() not in ("the", "and")]
    if not words:
        return "xx", folder_name.strip()
    if len(words) == 1:
        abbrev = words[0][:3].lower()
    else:
        abbrev = "".join(w[0] for w in words[:4]).lower()
    return abbrev, " ".join(words)


def write_bands_stub(out_dir: Path, analyses: list[ShowAnalysis]) -> int:
    """Emit a `bands:` block for every artist the config does not know.

    Paste it into jamp.yaml, fix the abbreviations, and re-run.
    """
    unknown: dict[str, list[ShowAnalysis]] = {}
    for a in analyses:
        if a.band.band is not None:
            continue
        key = (a.show.artist_dir or a.show.name).strip()
        unknown.setdefault(key, []).append(a)

    lines = [
        "# Artists Phase 0 could not resolve, as a paste-ready config block.",
        "# Check every abbreviation before pasting - they are guesses, and the",
        "# abbreviation becomes the folder-name prefix for every one of these shows.",
        "",
    ]
    if not unknown:
        lines.append("# (none - every folder resolved to a band in the config)")
    else:
        lines.append("bands:")
        for key, group in sorted(unknown.items()):
            abbrev, name = _band_stub_name(key)
            years = sorted({a.date.date.year for a in group if a.date.date})
            span = "[%d, %d]" % (years[0], years[-1]) if years else "[1960, 2035]"
            suggestions = sorted({s for a in group for s in a.band.suggestions})
            lines.append("  - abbrev: %s" % abbrev)
            lines.append("    name: %s" % name)
            lines.append("    prefixes: [%s]" % abbrev)
            lines.append("    aliases: [%r]" % name)
            lines.append("    active_years: %s" % span)
            lines.append("    # %d folder(s), e.g. %s"
                         % (len(group), group[0].show.rel))
            if suggestions:
                lines.append("    # fuzzy matches against the existing config: %s"
                             % ", ".join(suggestions))
            lines.append("")

    (out_dir / "phase0_bands_stub.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(unknown)


def _write_reports(out_dir: Path, result: ScanResult, analyses: list[ShowAnalysis],
                   stats: dict, cfg: Config, index=None, matches=None,
                   repeats=None, verdicts=None) -> None:
    out_dir = Path(out_dir)
    write_json(out_dir / "phase0_inventory.json", stats)
    stats["unresolved_artists"] = write_bands_stub(out_dir, analyses)

    write_csv(
        out_dir / "phase0_folders.csv",
        ["relative_path", "artist_dir", "band", "band_evidence", "date", "date_confidence",
         "date_pattern_reasons", "classification", "shape", "source", "source_confidence",
         "provenance", "format", "audio_files", "bytes", "has_info_file", "info_file",
         "sidecars", "disc_dirs", "container", "issues"],
        (
            [
                a.show.rel, a.show.artist_dir or "", a.band.abbrev or "",
                a.band.matched_by or "", a.date.iso or "", a.date.confidence,
                " | ".join(a.date.reasons), a.classification.kind,
                a.classification.shape or "", a.source.value or "",
                a.source.confidence, a.provenance or "", a.fmt or "",
                len(a.show.files), a.show.total_bytes,
                "yes" if a.info.chosen else "no",
                a.info.chosen.path.name if a.info.chosen else "",
                ";".join("%s=%d" % (k, len(v)) for k, v in sorted(a.show.sidecars.items())),
                len(a.show.disc_dirs),
                a.show.container.name if a.show.container else "",
                ";".join(a.issue_codes),
            ]
            for a in analyses
        ),
    )

    write_csv(
        out_dir / "phase0_unparsed.csv",
        ["relative_path", "band", "date", "confidence", "classification", "why",
         "look_it_up_at"],
        (
            [a.show.rel, a.band.abbrev or "", a.date.iso or "", a.date.confidence,
             a.classification.kind,
             " | ".join("%s: %s" % (i.code, i.detail) for i in a.issues
                        if i.severity == SEVERITY_BLOCK),
             cfg.reference_for(a.band.abbrev)]
            for a in unparsed_sample(analyses)
        ),
    )

    index = index or {}
    matches = matches or []
    repeats = repeats or []
    verdicts = verdicts or []

    # One row per track that could be identified: the recording's own MD5, its
    # exact length in samples, and what it is filed as.  This is the durable
    # artifact - everything below is a reading of it, and a later question we
    # have not thought of yet can be answered from the CSV without re-walking
    # the library.
    write_csv(
        out_dir / "phase0_audio_identity.csv",
        ["relative_path", "file", "audio_md5", "samples", "seconds",
         "bits", "rate", "channels", "bytes"],
        (
            [str(show.path.relative_to(show.root)), f.name, f.audio_md5 or "",
             f.samples if f.samples is not None else "",
             "%.3f" % f.length if f.length else "",
             f.bits or "", f.rate or "", f.channels or "", f.size]
            for show in result.shows for f in show.files
            if f.audio_md5 or f.samples
        ),
    )

    write_csv(
        out_dir / "phase0_same_audio.csv",
        ["kind", "folder_a", "folder_b", "shared_tracks", "tracks_a", "tracks_b",
         "example_file", "note"],
        (
            [m.kind, m.left, m.right, m.shared, m.left_total, m.right_total,
             m.examples[0] if m.examples else "", m.note]
            for m in matches
        ),
    )

    if verdicts:
        write_csv(
            out_dir / "phase0_verify_audio.csv",
            ["status", "relative_path", "file", "stored_md5", "decoded_md5",
             "seconds", "detail"],
            (
                [v["status"], v["folder"], v["file"], v["stored_md5"],
                 v["decoded_md5"],
                 "%.1f" % v["seconds"] if v.get("seconds") else "", v["detail"]]
                for v in verdicts
            ),
        )

    rep = TextReport("Phase 0 inventory - %s" % stats["root"])
    rep.heading("Scale")
    rep.kv("directories walked", stats["dirs_scanned"])
    rep.kv("show folders found", stats["show_folders"])
    rep.kv("audio files", stats["audio_files"])
    rep.kv("total size", "%.1f GB" % (stats["total_bytes"] / 1e9))
    rep.kv("folders with an info file", "%d of %d" % (stats["folders_with_info_file"],
                                                      stats["show_folders"]))
    rep.kv("folders with a torrent marker", stats["folders_with_torrent_marker"])
    rep.kv("blocked from renaming as-is", stats["blocked_from_rename"])

    rep.heading("Audio formats (files)")
    rep.histogram(stats["formats"], total=stats["audio_files"] or None)
    rep.heading("Extensions (files)")
    rep.histogram(stats["extensions"], total=stats["audio_files"] or None)

    rep.heading("Date formats that actually occur (folders)")
    rep.histogram(stats["date_patterns"], total=stats["show_folders"] or None)
    rep.heading("Date confidence")
    rep.histogram(stats["date_confidence"], total=stats["show_folders"] or None)

    rep.heading("Origin")
    rep.histogram(stats["classification"], total=stats["show_folders"] or None)
    rep.heading("Official release shape")
    rep.histogram(stats["official_shapes"])
    rep.heading("Classifier signals fired")
    rep.histogram(stats["classifier_signals"], width=40)

    rep.heading("Bands")
    rep.histogram(stats["bands"])
    rep.heading("How the band was decided")
    rep.histogram(stats["band_evidence"])

    rep.heading("Source")
    rep.histogram(stats["sources"])
    rep.heading("Provenance")
    rep.histogram(stats["provenance"], limit=20)

    rep.heading("Disc and set layouts (folders)")
    rep.histogram(stats["disc_layouts"], width=40)
    rep.heading("Track filename patterns (files)")
    rep.histogram(stats["track_filename_patterns"], width=40,
                  total=stats["audio_files"] or None)
    rep.heading("Sidecar files")
    rep.histogram(stats["sidecars"])

    rep.heading("Issues raised")
    rep.histogram(stats["issues"], width=40)

    if stats["multi_show_containers"]:
        rep.heading("Containers holding several show folders")
        for entry in stats["multi_show_containers"][:20]:
            rep.line("  %s" % entry["container"])
            for s in entry["shows"][:6]:
                rep.line("      %s" % Path(s).name)

    if stats["long_paths"]:
        rep.heading("Paths over %d characters" % cfg.settings.max_path_length)
        for p in stats["long_paths"][:20]:
            rep.line("  %d  %s" % (len(p), p))

    shn = [a for a in analyses if any(f.ext == ".shn" for f in a.show.files)]
    if shn:
        rep.heading("SHN folders to convert (%d)" % len(shn))
        rep.line("  mutagen cannot read or write SHN tags, so these can be renamed but")
        rep.line("  never tagged until they are converted to FLAC.")
        for a in shn:
            rep.line("  %-58s %d files" % (a.show.rel[:58],
                                           sum(1 for f in a.show.files if f.ext == ".shn")))

    if stats.get("unresolved_artists"):
        rep.heading("Artists not in the config")
        rep.line("  %d unresolved; a paste-ready block is in phase0_bands_stub.yaml"
                 % stats["unresolved_artists"])

    rep.heading("Audio identity")
    rep.line("  Every FLAC carries an MD5 of its decoded audio, written by the encoder.")
    rep.line("  It survives retagging and renaming, and it is the same number for two")
    rep.line("  encodes of one master - so it answers 'are these the same recording?'")
    rep.line("  where names, track counts and file sizes cannot.")
    rep.kv("tracks identified", stats.get("tracks_identified", 0))
    rep.kv("distinct recordings", stats.get("distinct_recordings", 0))
    rep.kv("folder pairs sharing audio", stats.get("folder_pairs_sharing_audio", 0))

    if matches:
        rep.heading("Folders holding the same audio (%d)" % len(matches))
        rep.line("  Reported, never resolved - nothing here is deleted or moved.")
        for m in matches[:60]:
            rep.line("  %s" % m.kind)
            rep.line("      %s" % m.left)
            rep.line("      %s" % m.right)
            rep.line("      %s" % m.note)
        if len(matches) > 60:
            rep.line("  ... and %d more, all of them in phase0_same_audio.csv"
                     % (len(matches) - 60))

    if repeats:
        rep.heading("One folder holding the same audio twice (%d)" % len(repeats))
        rep.line("  Two filenames, one recording - easy to read as a longer show.")
        for folder, _h, names in repeats[:25]:
            rep.line("  %s" % folder)
            rep.line("      %s" % ", ".join(n[:40] for n in names[:4]))

    if verdicts:
        bad = [v for v in verdicts
               if v["status"] in (_integrity.MISMATCH, _integrity.UNREADABLE)]
        rep.heading("Audio verified against its own fingerprint")
        rep.line("  Each file decoded and compared with the MD5 in its own header.")
        for key in (_integrity.PASS, _integrity.MISMATCH, _integrity.UNREADABLE,
                    _integrity.NO_MD5):
            rep.kv(key.lower(), stats.get("verify_" + key.lower(), 0))
        if bad:
            rep.line("")
            rep.line("  MISMATCH means the file decodes but not to the audio its own")
            rep.line("  header describes. It plays. It shows its tags. It is damaged,")
            rep.line("  and no retag or rename repairs it.")
            for v in bad[:60]:
                rep.line("  %-11s %s" % (v["status"], v["folder"][:60]))
                rep.line("      %s" % v["file"][:70])
            if len(bad) > 60:
                rep.line("  ... and %d more, in phase0_verify_audio.csv" % (len(bad) - 60))

    sample = unparsed_sample(analyses)
    rep.heading("%d folder names that could not be confidently parsed" % len(sample))
    for a in sample:
        rep.line("  %s" % a.show.rel)
        for issue in a.issues:
            if issue.severity == SEVERITY_BLOCK:
                rep.line("      %-22s %s" % (issue.code, issue.detail[:110]))
        reference = cfg.reference_for(a.band.abbrev)
        if reference:
            rep.line("      %-22s %s" % ("look it up at", reference))

    rep.save(out_dir / "phase0_summary.txt")
