"""Inspect a media folder and say whether it will work — before indexing.

"I downloaded a season, will the tool handle it?" deserves an answer that
takes seconds and needs no index built, no clips cut and no guessing. This
opens each file, reads what is actually inside it, and reports a verdict per
file plus the specific thing to fix.

It is deliberately read-only and fast: probing a file is milliseconds, so a
13-episode folder is checked in a couple of seconds.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import naming, subtitles, term
from .probe import ProbeError, pick_audio, probe

# A file must clear all of these to be usable without further work.
VERDICT_OK = "ok"
VERDICT_NEEDS_SUBS = "needs_subs"
VERDICT_NEEDS_ENGLISH = "needs_english"
VERDICT_UNREADABLE = "unreadable"


@dataclass
class FileReport:
    path: str
    label: str = ""
    kind: str = ""
    duration: float = 0.0
    resolution: str = ""
    audio: list = field(default_factory=list)     # [(lang, title)]
    subs: list = field(default_factory=list)      # [(lang, codec, bitmap)]
    sub_source: str = "none"                      # sidecar | embedded | bitmap_only | none
    sub_script: str = ""
    cue_count: int = 0
    verdict: str = VERDICT_UNREADABLE
    problem: str = ""
    fix: str = ""

    @property
    def icon(self) -> str:
        return {VERDICT_OK: term.sym("ok"),
                VERDICT_NEEDS_SUBS: term.sym("warn"),
                VERDICT_NEEDS_ENGLISH: term.sym("warn"),
                VERDICT_UNREADABLE: term.sym("fail")}[self.verdict]

    @property
    def audio_summary(self) -> str:
        if not self.audio:
            return "no audio"
        return ", ".join(f"{lang or '?'}" + (f" ({t})" if t else "")
                         for lang, t in self.audio)

    @property
    def sub_summary(self) -> str:
        if not self.subs:
            return "none embedded"
        return ", ".join(
            f"{lang or '?'}/{codec}" + (" [image]" if bitmap else "")
            for lang, codec, bitmap in self.subs)


def inspect_file(path: str) -> FileReport:
    """Everything that decides whether this file is usable."""
    mid = naming.parse(path)
    rep = FileReport(path=path, label=mid.label, kind=mid.kind)
    try:
        info = probe(path)
    except ProbeError as exc:
        rep.problem = str(exc)[:140]
        rep.fix = "the file may be corrupt or still downloading"
        return rep

    rep.duration = info.duration
    rep.resolution = info.resolution
    rep.audio = [(a.lang, a.title) for a in info.audios]
    rep.subs = [(s.lang, s.codec,
                 s.codec.lower() in subtitles.BITMAP_CODECS) for s in info.subs]

    if not info.has_audio:
        rep.problem = "no audio track"
        rep.fix = "re-download — sync checking and analysis both need audio"
        return rep

    kind, sub_path, cues = subtitles.load_for_video(path)
    rep.sub_source = kind
    rep.cue_count = len(cues)

    if not cues:
        rep.verdict = VERDICT_NEEDS_SUBS
        if kind == "bitmap_only":
            rep.problem = "subtitles are images (PGS/VobSub), not text"
            rep.fix = f"download an English .srt named '{_srt_name(path)}'"
        else:
            rep.problem = "no subtitles at all"
            rep.fix = ("run 'transcribe' to make them from the audio, or "
                       f"download an English .srt named '{_srt_name(path)}'")
        return rep

    rep.sub_script = subtitles.detect_script(cues)
    if rep.sub_script not in ("latin", "unknown"):
        rep.verdict = VERDICT_NEEDS_ENGLISH
        rep.problem = f"subtitles are in {rep.sub_script} script"
        rep.fix = (f"an English script will not match these — download an "
                   f"English .srt named '{_srt_name(path)}'")
        return rep

    rep.verdict = VERDICT_OK
    # Only worth saying when there is actually a choice to get wrong. With a
    # single audio track there is nothing to pick, and warning about it buries
    # the files that do need attention.
    if len(info.audios) > 1:
        chosen = info.audios[pick_audio(info)]
        if not chosen.lang.startswith("en"):
            rep.problem = (f"{len(info.audios)} audio tracks and none tagged "
                           f"English — '{chosen.lang or '?'}' will be analysed")
            rep.fix = "harmless for cutting; only affects sync checking"
    return rep


def _srt_name(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0] + ".en.srt"


def inspect_folder(root: str, log=lambda *a: None) -> list[FileReport]:
    reports = []
    for i, path in enumerate(naming.walk_media(root), 1):
        log(f"  [{i}] {os.path.basename(path)}")
        try:
            reports.append(inspect_file(path))
        except Exception as exc:                 # a check must never crash
            rep = FileReport(path=path)
            rep.problem = f"{type(exc).__name__}: {exc}"
            reports.append(rep)
    return reports


def format_report(reports: list[FileReport], root: str = "") -> str:
    if not reports:
        return (f"no video files found under {root!r}\n"
                "  → check the path, and that the files are not still downloading")

    lines = ["", f"MEDIA CHECK — {len(reports)} file(s)", ""]
    width = max(len(r.label or os.path.basename(r.path)) for r in reports)
    for r in reports:
        name = r.label or os.path.basename(r.path)
        mins = f"{r.duration/60:.0f}m" if r.duration else "?"
        lines.append(
            f"  {r.icon} {name:<{width}}  {mins:>4}  {r.resolution:<10} "
            f"subs: {r.sub_source:<12} {r.cue_count or '':>5}")
        if r.problem:
            lines.append(f"        {r.problem}")
        if r.fix:
            lines.append(f"        {term.sym('arrow')} {r.fix}")

    ok = [r for r in reports if r.verdict == VERDICT_OK]
    need_subs = [r for r in reports if r.verdict == VERDICT_NEEDS_SUBS]
    need_en = [r for r in reports if r.verdict == VERDICT_NEEDS_ENGLISH]
    broken = [r for r in reports if r.verdict == VERDICT_UNREADABLE]

    lines += ["", f"  {len(ok)} ready · {len(need_subs)} need subtitles · "
                  f"{len(need_en)} need English subtitles · {len(broken)} unreadable"]

    # identity sanity — silently wrong numbering is the expensive kind of wrong
    eps = sorted((r.label for r in reports if r.kind == "episode"))
    if len(eps) != len(set(eps)):
        lines.append(f"  {term.sym('warn')} two files were identified as the SAME episode - "
                     "check the filenames")
    combined = [r for r in reports if r.kind == "season_pack"]
    if combined:
        lines.append(f"  {term.sym('warn')} {len(combined)} file(s) hold several episodes each")

    if ok and not (need_subs or need_en or broken):
        lines.append(f"  {term.sym('ok')} this folder is ready - run 'build' on it")
    elif need_subs or need_en:
        if need_subs:
            lines.append(f"  {term.sym('arrow')} run:  mi.bat transcribe "
                         f'"{root}"' if root else "  run 'transcribe'")
        if need_en:
            lines.append(f"  {term.sym('arrow')} for the non-English ones, "
                         "download an English .srt")
    return "\n".join(lines)
