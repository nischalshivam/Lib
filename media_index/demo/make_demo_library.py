"""Build a fake media library so the index can be proven without real films.

Everything here is invented dialogue, but the *shapes* are real:
  - messy release filenames (Show.Name.S01E02.1080p.WEB-DL.x265-GROUP.mkv)
  - sidecar subs named three different ways (.srt, .en.srt, Subs/ folder)
  - one .ass subtitle instead of .srt
  - quotes split across two cues (the case that breaks naive matching)
  - italic tags, speaker labels, [SOUND EFFECTS], musical notes
  - the same line spoken in two different episodes (ambiguity)

Run:  python -m media_index.demo.make_demo_library <output_dir>
"""
from __future__ import annotations

import os
import sys

FAKE_VIDEO_BYTES = 6_000_000       # > 5 MB so the scanner accepts it


def srt(cues) -> str:
    """cues = [(start_ms, end_ms, text)] -> SRT text."""
    def tc(ms):
        s, ms = divmod(ms, 1000)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    return "\n\n".join(
        f"{i}\n{tc(a)} --> {tc(b)}\n{t}" for i, (a, b, t) in enumerate(cues, 1)
    ) + "\n"


def ass(cues) -> str:
    def tc(ms):
        cs, ms = divmod(ms, 10)
        s, cs = divmod(cs, 100)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"
    head = ("[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\n"
            "Format: Name, Fontname\nStyle: Default,Arial\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text\n")
    body = "\n".join(
        f"Dialogue: 0,{tc(a)},{tc(b)},Default,,0,0,0,,{t}" for a, b, t in cues)
    return head + body + "\n"


# --- Iron Harvest S01E01 -----------------------------------------------------
S01E01 = [
    (62_400,  65_100, "You said this town was finished."),
    (65_400,  68_000, "It was finished long before we got here."),
    (301_200, 303_800, "[ENGINE TURNS OVER]"),
    (612_500, 615_900, "MARLOW: Every man in this valley owes me something."),
    # split across two cues on purpose — the hard case
    (842_300, 845_100, "I never wanted the harvest."),
    (845_400, 848_200, "I wanted the land it grew on."),
    (1_204_000, 1_207_400, "<i>You can't bury a thing that keeps growing back.</i>"),
    (1_530_000, 1_533_100, "Then we burn the field."),
    (1_802_600, 1_805_000, "♪ ♪"),
    (2_101_000, 2_104_800, "Nobody walks out of Kessler County clean."),
]

# --- Iron Harvest S01E02 -----------------------------------------------------
S01E02 = [
    (44_000,   47_200, "The bank called again this morning."),
    (250_500,  253_900, "Let them call."),
    (700_000,  703_600, "You brought a gun to a courthouse."),
    (703_900,  706_400, "I brought an argument."),
    # deliberately repeats a line from S01E01 -> ambiguity test
    (1_400_000, 1_403_100, "Then we burn the field."),
    (1_910_200, 1_914_000, "There's a difference between owning a place "
                           "and belonging to it."),
    (2_400_000, 2_403_500, "[DOOR SLAMS]"),
]

# --- Iron Harvest S02E01 -----------------------------------------------------
S02E01 = [
    (30_000,   33_400, "Three winters, and the ground still won't take."),
    (455_800,  459_200, "You came back for the land."),
    (459_500,  462_100, "I came back for the people on it."),
    (1_100_000, 1_103_900, "MARLOW: I built every road you drove in on."),
    (1_650_000, 1_654_200, "A debt isn't a rope. It's a road. "
                           "It goes both directions."),
    (2_205_000, 2_208_600, "Sign it, and the valley is yours."),
]

# --- The Long Winter (2019), a movie ----------------------------------------
MOVIE = [
    (128_000,  131_500, "We ration from tonight. No exceptions."),
    (540_000,  543_800, "HOLT: The radio went quiet four days ago."),
    (901_200,  904_000, "Something is walking the perimeter."),
    (1_802_000, 1_805_400, "You keep counting the days like they owe you."),
    (2_640_000, 2_643_900, "The cold doesn't negotiate."),
    (3_915_000, 3_919_200, "<i>If we open that door, we decide who freezes.</i>"),
    (4_720_000, 4_723_600, "Then we decide together."),
]


def write(path: str, text: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def write_video(path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\x00" * FAKE_VIDEO_BYTES)


def build(root: str):
    # 1. plain .srt sidecar, messy release name
    v = os.path.join(root, "Iron Harvest", "Season 01",
                     "Iron.Harvest.S01E01.1080p.WEB-DL.x265-KOGi.mkv")
    write_video(v)
    write(v[:-4] + ".srt", srt(S01E01))

    # 2. .en.srt sidecar, different release group
    v = os.path.join(root, "Iron Harvest", "Season 01",
                     "Iron.Harvest.S01E02.1080p.BluRay.DD5.1.x264-NTb.mkv")
    write_video(v)
    write(v[:-4] + ".en.srt", srt(S01E02))

    # 3. subtitle in a Subs/ folder, "1x01"-style numbering
    v = os.path.join(root, "Iron Harvest", "Season 02",
                     "Iron Harvest - 2x01 - Frost Line.mkv")
    write_video(v)
    write(os.path.join(root, "Iron Harvest", "Season 02", "Subs",
                       "Iron Harvest - 2x01 - Frost Line", "2_English.srt"),
          srt(S02E01))

    # 4. a movie, .ass subtitle, year in the folder name
    v = os.path.join(root, "The Long Winter (2019)",
                     "The.Long.Winter.2019.2160p.UHD.BluRay.x265-TERMiNAL.mkv")
    write_video(v)
    write(v[:-4] + ".ass", ass(MOVIE))

    # 5. a file with NO subtitles at all — pre-flight must report this
    write_video(os.path.join(root, "Iron Harvest", "Season 02",
                             "Iron Harvest - 2x02 - The Auction.mkv"))


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "demo_media"
    build(out)
    print(f"demo library written to {out}")
