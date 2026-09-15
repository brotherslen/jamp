"""Phase 0 - inventory.  Reads the library and writes down what is actually
there.  Nothing is renamed, retagged, moved or written inside ROOT.

The point of this phase is to replace guesses with counts before any regex is
finalised: which date formats really occur, how the OFFICIAL/UNOFFICIAL split
falls out, how many folders have an info file, what disc and set layouts exist,
and which names the parser could not read.
"""
from __future__ import annotations

import datetime as _dt
from collections import Counter
from pathlib import Path

from . import classify as _classify
from . import identity as _identity
from . import integrity as _integrity
from . import verify as _verify
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
        # plan writes the same two files from its own scan.
        index, matches, repeats, id_stats = _identity.write_reports(
            out_dir, result.shows, "phase0")
        stats.update(id_stats)

        # Integrity is a different order of cost - a full decode of every file -
        # so it happens only when asked for.
        verdicts = []
        if verify_audio:
            verdicts = _verify_all(result, workers=workers, progress=progress,
                                   ledger=out_dir / _verify.LEDGER_NAME)
            for key in (_integrity.PASS, _integrity.MISMATCH,
                        _integrity.UNREADABLE, _integrity.NO_MD5):
                stats["verify_" + key.lower()] = sum(
                    1 for v in verdicts if v["status"] == key)

        _write_reports(out_dir, result, analyses, stats, cfg,
                       index, matches, repeats, verdicts)
        return stats


def _verify_all(result: ScanResult, workers: int = 4, progress=None, *,
                ledger: Path) -> list:
    """Decode every FLAC and compare it against the MD5 it carries.

    Opt-in, because it is hours rather than minutes.  What it catches is a file
    that decodes perfectly well to audio that is not what its own header
    describes - damage no structural check can see, because from outside such a
    file reads, tags and plays exactly like a healthy one.

    Resumable: verdicts go to a ledger in the reports folder as they are made,
    and a file already there at the same size and modification time is not
    decoded again.
    """
    ffmpeg = _integrity.find_ffmpeg()
    jobs = [(show, f) for show in result.shows for f in show.files if f.ext == ".flac"]
    root = result.root
    verdicts = _verify.verify_files(
        [f.path for _, f in jobs], root,
        ledger, ffmpeg, workers=workers, progress=progress)
    out = []
    for show, f in jobs:
        row = verdicts.get(str(f.path.relative_to(root)))
        if row is None:
            continue
        out.append({"status": row["status"],
                    "stored_md5": row["stored_md5"], "decoded_md5": row["decoded_md5"],
                    "seconds": float(row["seconds"]) if row.get("seconds") else None,
                    "detail": row["detail"],
                    "folder": str(show.path.relative_to(show.root)), "file": f.name})
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


def unresolved_artists(analyses: list[ShowAnalysis]) -> list[tuple[str, int]]:
    """Folders the config names no act for, grouped by the folder they sit in.

    This used to be written out as phase0_bands_stub.yaml, a block of guessed
    abbreviations to paste into the config.  `jamp acts` does that job and
    refuses an abbreviation that would misfile shows, which a pasted guess
    never did, so the inventory only counts them and points there.
    """
    unknown: dict[str, int] = {}
    for a in analyses:
        if a.band.band is not None:
            continue
        key = (a.show.artist_dir or a.show.name).strip()
        unknown[key] = unknown.get(key, 0) + 1
    return sorted(unknown.items())


def _write_reports(out_dir: Path, result: ScanResult, analyses: list[ShowAnalysis],
                   stats: dict, cfg: Config, index=None, matches=None,
                   repeats=None, verdicts=None) -> None:
    out_dir = Path(out_dir)
    unresolved = unresolved_artists(analyses)
    stats["unresolved_artists"] = len(unresolved)
    write_json(out_dir / "phase0_inventory.json", stats)

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

    matches = matches or []
    repeats = repeats or []
    verdicts = verdicts or []

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

    if unresolved:
        rep.heading("Folders no configured act claims (%d)" % len(unresolved))
        rep.line("  Add each as an act, or set it aside, with jamp acts - it checks the")
        rep.line("  abbreviation against every act and token already in use.")
        for key, count in unresolved[:40]:
            rep.line("  %-58s %d folder(s)" % (key[:58], count))
        if len(unresolved) > 40:
            rep.line("  ... and %d more" % (len(unresolved) - 40))

    rep.heading("Audio identity")
    rep.line("  Every FLAC carries an MD5 of its decoded audio, written by the encoder.")
    rep.line("  It survives retagging and renaming, and it is the same number for two")
    rep.line("  encodes of one master - so it answers 'are these the same recording?'")
    rep.line("  where names, track counts and file sizes cannot.")
    rep.kv("tracks identified", stats.get("tracks_identified", 0))
    rep.kv("distinct recordings", stats.get("distinct_recordings", 0))
    rep.kv("folder pairs sharing audio", stats.get("folder_pairs_sharing_audio", 0))

    _identity.report_matches(rep, matches, repeats, "phase0_same_audio.csv")

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
