"""What the tool owns, grouped the way a person thinks about it.

Everywhere else in the tool the unit of work is a FILE — one episode, one
path, one row. That is the right unit for indexing and the wrong unit for
looking at, because nobody owns 62 files; they own Breaking Bad. The Library
screen asks a question no other module answers: *what have I got, and is it
ready?*

A **title** is a show inside a database. A **library** is a database and the
frames stored beside it. One database can hold several titles, and that is
how the tool has always worked — so a person with one `library.db` holding
four shows sees four rows here without moving a single byte. Keeping one
database per title is then just the tidier arrangement of the same thing,
not a different system, which is why nothing here has to be migrated.

## Why status is computed and never stored

A stored status goes stale the moment someone deletes a folder outside the
tool, and a Library screen that confidently reports "ready" for footage that
is gone is worse than no screen at all. Everything here is read from the
database and the disk each time it is asked for. It is cheap: counting rows
and stat-ing a few files, not opening a single video.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import library, visual

# A title whose footage is fully picture-indexed can be checked shot by shot.
# Below that the tool still builds videos, but the placements in the
# un-indexed episodes are inferred from dialogue alone — worth saying out
# loud on the screen rather than discovering in a finished render.
READY = "ready"             # everything indexed, subtitles everywhere
PARTIAL = "partial"         # usable, but some episodes have no frames yet
ATTENTION = "attention"     # something needs a person: missing subtitles
EMPTY = "empty"             # named, but nothing indexed at all


@dataclass
class Title:
    """One show, and everything the Library screen says about it."""
    name: str
    kind: str = "series"                # episode -> series, movie -> movie
    db: str = ""
    media_root: str = ""
    files: int = 0
    indexed: int = 0                    # how many have their frames
    no_subs: list = field(default_factory=list)     # labels, not paths
    lines: int = 0
    seasons: tuple = ()
    frame_bytes: int = 0
    missing: int = 0                    # rows whose video is not on disk

    @property
    def status(self) -> str:
        # Order matters. A title with no subtitles has a person-sized problem
        # whether or not anything is indexed yet, and saying only "not
        # indexed" would send someone off to run the slow step — which cannot
        # succeed, because it is the subtitles that are missing.
        if not self.files:
            return EMPTY
        if self.no_subs or self.missing:
            return ATTENTION
        if not self.indexed:
            return EMPTY
        return READY if self.indexed >= self.files else PARTIAL

    @property
    def detail(self) -> str:
        """The one line under the badge. Says the actionable thing first."""
        if self.missing:
            return f"{self.missing} file(s) missing"
        if self.no_subs:
            n = len(self.no_subs)
            return f"{n} without subtitle{'s' if n > 1 else ''}"
        if not self.files or not self.indexed:
            return "not indexed"
        if self.indexed < self.files:
            return f"{self.indexed} of {self.files} indexed"
        return "ready"

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "db": self.db,
            "media_root": self.media_root,
            "files": self.files,
            "indexed": self.indexed,
            "no_subs": self.no_subs[:20],
            "lines": self.lines,
            "seasons": list(self.seasons),
            "frame_bytes": self.frame_bytes,
            "size": human_size(self.frame_bytes),
            "missing": self.missing,
            "status": self.status,
            "detail": self.detail,
        }


def human_size(n: int) -> str:
    if n <= 0:
        return "—"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.0f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"


def _label(row) -> str:
    """S04E01, or the file name for anything that is not an episode."""
    season, episode = row["season"], row["episode"]
    if season is not None and episode is not None:
        return f"S{int(season):02d}E{int(episode):02d}"
    return os.path.splitext(os.path.basename(row["path"]))[0][:40]


def _common_root(paths: list) -> str:
    """The folder a title lives in, as far as its files agree on one.

    `os.path.commonpath` raises on an empty list and on paths from different
    drives — both of which are ordinary here (a show split across D: and E:
    is untidy, not broken), so neither may reach the screen as a crash.
    """
    folders = [os.path.dirname(p) for p in paths if p]
    if not folders:
        return ""
    try:
        return os.path.commonpath(folders)
    except ValueError:
        return folders[0]


def titles(db_path: str) -> list:
    """Every show in one database, with its readiness worked out.

    One pass over `media`, one over `visual`. No video is opened and no
    vectors are loaded — this runs every time the Library screen is drawn,
    so it has to stay cheap enough that nobody notices it running.
    """
    if not os.path.isfile(db_path):
        return []
    con = library.connect(db_path)
    try:
        rows = con.execute(
            "SELECT path, kind, show, show_norm, season, episode, cue_count "
            "FROM media ORDER BY show, season, episode").fetchall()
        indexed = {r["path"]: r["vectors"] for r in
                   con.execute("SELECT path, vectors FROM visual").fetchall()}
    finally:
        con.close()

    store = visual.store_dir(db_path)
    sizes = {}
    try:
        for name in os.listdir(store):
            try:
                sizes[name] = os.path.getsize(os.path.join(store, name))
            except OSError:
                pass
    except OSError:
        pass                            # no frames indexed here yet

    grouped: dict = {}
    for r in rows:
        t = grouped.setdefault(r["show_norm"], Title(
            name=r["show"], kind="movie" if r["kind"] == "movie" else "series",
            db=os.path.abspath(db_path)))
        t.files += 1
        t.lines += int(r["cue_count"] or 0)
        if not r["cue_count"]:
            t.no_subs.append(_label(r))
        if not os.path.isfile(r["path"]):
            t.missing += 1
        vectors = indexed.get(r["path"])
        if vectors:
            t.indexed += 1
            # By basename, so a library folder that moved still adds up: the
            # row may still name yesterday's drive while the file sits in the
            # store beside this database under the same hashed name.
            t.frame_bytes += sizes.get(os.path.basename(vectors), 0)
        if r["season"] is not None:
            t.seasons = tuple(sorted(set(t.seasons) | {int(r["season"])}))
        t.media_root = ""               # filled below, once all paths are in

    paths_by_show: dict = {}
    for r in rows:
        paths_by_show.setdefault(r["show_norm"], []).append(r["path"])
    for key, t in grouped.items():
        t.media_root = _common_root(paths_by_show.get(key, []))
    return sorted(grouped.values(), key=lambda t: t.name.lower())


def databases(root: str, fallback: str = "") -> list:
    """Every library under a Libraries folder, newest arrangement first.

    Looks one level down — `E:\\Libraries\\Breaking Bad\\library.db` — and
    also accepts a database sitting directly in the folder, because that is
    where the tool put its first one and telling someone their existing work
    is in the wrong place is not an answer.
    """
    found = []
    if root and os.path.isdir(root):
        direct = os.path.join(root, "library.db")
        if os.path.isfile(direct):
            found.append(direct)
        try:
            for name in sorted(os.listdir(root)):
                nested = os.path.join(root, name, "library.db")
                if os.path.isfile(nested):
                    found.append(nested)
        except OSError:
            pass
    if fallback:
        here = os.path.abspath(fallback)
        if os.path.isfile(here) and here not in [os.path.abspath(f)
                                                 for f in found]:
            found.append(here)
    return found


def catalogue(root: str, fallback: str = "") -> dict:
    """Everything the Library screen needs, from wherever it happens to be."""
    dbs = databases(root, fallback)
    rows, seen = [], set()
    for db in dbs:
        for t in titles(db):
            key = t.name.strip().lower()
            if key in seen:
                continue            # the same show in two databases is one
            seen.add(key)           # show; the first one found wins
            rows.append(t.as_dict())
    counts = {"all": len(rows)}
    counts["series"] = sum(1 for r in rows if r["kind"] == "series")
    counts["movies"] = sum(1 for r in rows if r["kind"] == "movie")
    counts["attention"] = sum(1 for r in rows
                              if r["status"] in (ATTENTION, EMPTY, PARTIAL))
    return {"root": os.path.abspath(root) if root else "",
            "databases": dbs, "titles": rows, "counts": counts}
