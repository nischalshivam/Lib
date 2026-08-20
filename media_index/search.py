"""Find a spoken line in the library and return its exact timestamp.

The hard part is that a quoted line rarely lines up with one subtitle cue —
subtitles break on reading speed, not on sentences:

    285  00:14:31,220 --> 00:14:33,900   I am the Armored Titan
    286  00:14:34,010 --> 00:14:36,480   and he is the Colossal Titan.

So we never match cue-by-cue. We slide a window of 1..MAX_WINDOW consecutive
cues and match against the merged text, then keep the tightest window that
scores well. That single detail is what makes real-world quotes resolve.

rapidfuzz is used when installed; difflib (stdlib) is the fallback so the tool
runs on a clean Python install.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .library import connect, normalize

MAX_WINDOW = 4          # merge at most this many consecutive cues
LOOKBACK = 3            # windows may start this many cues before a hit
FTS_LIMIT = 600         # candidate cues pulled from the full-text prefilter

# Consecutive cue *indices* are not necessarily consecutive in TIME — a scene
# can be silent for minutes. Merging across such a gap produced a "clip" that
# spanned nine minutes. A window must be temporally continuous.
MAX_CUE_GAP_MS = 4000       # silence allowed between two merged cues
MAX_WINDOW_MS = 20_000      # a merged window may never exceed this

# Two matches closer together than this are the same moment seen through two
# overlapping windows, not two occurrences of the line.
REPEAT_SEPARATION_MS = 30_000

try:                                                     # optional accelerator
    from rapidfuzz import fuzz as _fuzz

    def _partial(a: str, b: str) -> float:
        return _fuzz.partial_ratio(a, b)

    HAVE_RAPIDFUZZ = True
except ImportError:                                      # stdlib fallback
    from difflib import SequenceMatcher

    def _partial(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        if len(a) > len(b):
            a, b = b, a
        best = 0.0
        step = max(1, len(a) // 4)
        for i in range(0, max(1, len(b) - len(a) + 1), step):
            best = max(best, SequenceMatcher(None, a, b[i:i + len(a)]).ratio())
            if best > 0.995:
                break
        return best * 100

    HAVE_RAPIDFUZZ = False

# Words too common to help the full-text prefilter pick candidates
_STOP = {"a", "an", "the", "and", "or", "of", "in", "on", "to", "is", "was",
         "it", "he", "she", "they", "we", "you", "i", "me", "my", "your",
         "that", "this", "for", "at", "by", "be", "am", "are", "were", "with",
         "not", "no", "do", "did", "does", "have", "has", "had", "but", "so",
         "if", "as", "from", "will", "would", "can", "could", "what", "who"}


@dataclass
class Hit:
    media_id: int
    path: str
    show: str
    kind: str
    year: int | None
    season: int | None
    episode: int | None
    start_ms: int
    end_ms: int
    matched_text: str
    score: float
    coverage: float
    confidence: str                      # high | medium | low
    cue_span: tuple = (0, 0)
    alternatives: int = 0                # other places this line also appears
    is_combined: bool = False            # source file holds several episodes
    episode_to: int | None = None
    chapter_title: str = ""              # names the episode inside a pack
    chapter_index: int | None = None
    chapter_offset_ms: int = 0           # position within that episode

    @property
    def label(self) -> str:
        if self.is_combined:
            # Inside a season pack the useful answer is which episode, not an
            # offset into a seven-hour blob. Chapters give us that when the
            # release carries them.
            if self.chapter_index is not None:
                ep = (self.episode or 1) + self.chapter_index
                name = f" — {self.chapter_title}" if self.chapter_title else ""
                return (f"{self.show} S{self.season:02d}E{ep:02d}{name}"
                        if self.season is not None else f"{self.show}{name}")
            span = (f" E{self.episode:02d}-E{self.episode_to:02d}"
                    if self.episode and self.episode_to else "")
            return (f"{self.show} S{self.season:02d}{span} [combined]"
                    if self.season is not None else f"{self.show} [combined]")
        if self.kind == "episode" and self.season is not None:
            return f"{self.show} S{self.season:02d}E{self.episode:02d}"
        return f"{self.show}" + (f" ({self.year})" if self.year else "")

    @property
    def episode_timecode(self) -> str:
        """Position within the episode, when the file holds several."""
        ms = self.chapter_offset_ms if self.chapter_index is not None else self.start_ms
        s, ms = divmod(ms, 1000)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h:d}:{m:02d}:{s:02d}.{ms:03d}"

    @property
    def timecode(self) -> str:
        s, ms = divmod(self.start_ms, 1000)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h:d}:{m:02d}:{s:02d}.{ms:03d}"

    def cut_window(self, pre_ms=1500, post_ms=1000) -> tuple[int, int]:
        """Suggested clip range — a little air before and after the line."""
        return max(0, self.start_ms - pre_ms), self.end_ms + post_ms

    def as_dict(self) -> dict:
        a, b = self.cut_window()
        return {"label": self.label, "path": self.path, "show": self.show,
                "season": self.season, "episode": self.episode,
                "is_combined": self.is_combined,
                "chapter_index": self.chapter_index,
                "chapter_title": self.chapter_title,
                "episode_timecode": self.episode_timecode,
                "start_ms": self.start_ms, "end_ms": self.end_ms,
                "timecode": self.timecode, "cut_start_ms": a, "cut_end_ms": b,
                "score": round(self.score, 1), "coverage": round(self.coverage, 3),
                "confidence": self.confidence, "matched_text": self.matched_text,
                "alternatives": self.alternatives}


def _tokens(s: str) -> list[str]:
    return [t for t in s.split() if t]


def _score(q_norm: str, q_tok: list[str], w_norm: str, w_tok: list[str]):
    """(score 0-100, coverage 0-1) for a query against one merged window."""
    if not q_tok or not w_tok:
        return 0.0, 0.0
    qs, ws = set(q_tok), set(w_tok)
    coverage = len(qs & ws) / len(qs)
    seq = _partial(q_norm, w_norm) / 100.0
    score = 100.0 * (0.5 * coverage + 0.5 * seq)
    # a long window trivially contains more words — prefer the tightest fit
    excess = max(0, len(w_tok) - int(len(q_tok) * 1.5))
    score *= 1.0 - min(0.25, excess * 0.02)
    return score, coverage


def _confidence(score: float, coverage: float) -> str:
    if score >= 88 and coverage >= 0.80:
        return "high"
    if score >= 72 and coverage >= 0.55:
        return "medium"
    return "low"


def _fts_query(q_tok: list[str]) -> str:
    rare = [t for t in q_tok if t not in _STOP and len(t) > 2]
    use = rare or [t for t in q_tok if len(t) > 1] or q_tok
    return " OR ".join(f'"{t}"' for t in use[:20])


def _candidates(con, q_tok, show=None, season=None, episode=None):
    """(media_id, idx) pairs worth expanding into windows."""
    where, params = [], []
    if show:
        where.append("m.show_norm LIKE ?")
        params.append(f"%{normalize(show)}%")
    if season is not None:
        where.append("m.season = ?")
        params.append(season)
    if episode is not None:
        where.append("m.episode = ?")
        params.append(episode)
    scope = (" AND " + " AND ".join(where)) if where else ""

    try:
        rows = con.execute(
            f"""SELECT c.media_id, c.idx
                  FROM cue_fts f
                  JOIN cue c ON c.id = f.rowid
                  JOIN media m ON m.id = c.media_id
                 WHERE cue_fts MATCH ?{scope}
                 ORDER BY bm25(cue_fts) LIMIT ?""",
            [_fts_query(q_tok)] + params + [FTS_LIMIT]).fetchall()
    except Exception:
        rows = []
    if rows:
        return [(r["media_id"], r["idx"]) for r in rows]

    # Fallback: no full-text hit (rare wording, or a tiny library) — scan scope
    rows = con.execute(
        f"""SELECT c.media_id, c.idx FROM cue c JOIN media m ON m.id=c.media_id
             WHERE 1=1{scope} LIMIT 200000""", params).fetchall()
    return [(r["media_id"], r["idx"]) for r in rows]


def find(db_path: str, quote: str, show=None, season=None, episode=None,
         limit=5, min_score=55.0, con=None, retry_fragment=True) -> list[Hit]:
    """Locate `quote`. Returns best hits, highest score first.

    When a long quote does not land cleanly — usually because the line is
    spoken across a pause longer than MAX_CUE_GAP_MS, so it can never be one
    window — we retry with the leading fragment, which is what a human would
    do. The better of the two results wins.
    """
    own = con is None
    con = con or connect(db_path)
    try:
        hits = _find_once(db_path, quote, show, season, episode, limit,
                          min_score, con)
        if retry_fragment and len(_tokens(normalize(quote))) >= 8 and (
                not hits or hits[0].confidence != "high"):
            words = quote.split()
            frag = " ".join(words[:max(4, int(len(words) * 0.6))])
            alt = _find_once(db_path, frag, show, season, episode, limit,
                             min_score, con)
            if alt and (not hits or alt[0].score > hits[0].score):
                return alt
        return hits
    finally:
        if own:
            con.close()


def _find_once(db_path, quote, show, season, episode, limit, min_score, con):
    if True:
        q_norm = normalize(quote)
        q_tok = _tokens(q_norm)
        if not q_tok:
            return []

        cand = _candidates(con, q_tok, show, season, episode)
        if not cand:
            return []

        # Fetch every cue we might need, one query per media file
        need = {}
        for mid, idx in cand:
            s = need.setdefault(mid, set())
            for j in range(idx - LOOKBACK, idx + MAX_WINDOW + 1):
                if j >= 0:
                    s.add(j)

        scored = []                      # every window worth considering
        for mid, idxs in need.items():
            lo, hi = min(idxs), max(idxs)
            cues = con.execute(
                """SELECT idx,start_ms,end_ms,text,text_norm FROM cue
                    WHERE media_id=? AND idx BETWEEN ? AND ? ORDER BY idx""",
                (mid, lo, hi)).fetchall()
            by_idx = {c["idx"]: c for c in cues}
            starts = sorted({i for i in idxs if i in by_idx})

            for s0 in starts:
                for wlen in range(1, MAX_WINDOW + 1):
                    seq = [by_idx.get(s0 + k) for k in range(wlen)]
                    if any(c is None for c in seq):
                        break
                    # reject windows that jump across silence
                    if any(b["start_ms"] - a["end_ms"] > MAX_CUE_GAP_MS
                           for a, b in zip(seq, seq[1:])):
                        break
                    if seq[-1]["end_ms"] - seq[0]["start_ms"] > MAX_WINDOW_MS:
                        break
                    w_norm = " ".join(c["text_norm"] for c in seq)
                    sc, cov = _score(q_norm, q_tok, w_norm, _tokens(w_norm))
                    if sc < min_score:
                        continue
                    scored.append((sc, cov, mid, s0, wlen, seq))

        # Keep the best window, then the next best far enough away from it, and
        # so on. Keeping only one hit per FILE hid every repeat inside a single
        # file — and a season pack is full of them, because each episode opens
        # with a recap of the last one.
        scored.sort(key=lambda t: -t[0])
        kept, taken = [], {}
        for sc, cov, mid, s0, wlen, seq in scored:
            start_ms = seq[0]["start_ms"]
            if any(abs(start_ms - t) < REPEAT_SEPARATION_MS
                   for t in taken.get(mid, ())):
                continue
            taken.setdefault(mid, []).append(start_ms)
            kept.append((sc, cov, mid, s0, wlen, seq))
            if len(kept) >= limit * 3:
                break

        meta = {}
        hits = []
        for sc, cov, mid, s0, wlen, seq in kept:
            if mid not in meta:
                meta[mid] = con.execute(
                    "SELECT path,show,kind,year,season,episode,episode_to,"
                    "       is_combined FROM media WHERE id=?", (mid,)).fetchone()
            m = meta[mid]
            hits.append(Hit(
                media_id=mid, path=m["path"], show=m["show"], kind=m["kind"],
                year=m["year"], season=m["season"], episode=m["episode"],
                episode_to=m["episode_to"], is_combined=bool(m["is_combined"]),
                start_ms=seq[0]["start_ms"], end_ms=seq[-1]["end_ms"],
                matched_text=" ".join(c["text"] for c in seq),
                score=sc, coverage=cov, confidence=_confidence(sc, cov),
                cue_span=(s0, s0 + wlen - 1)))
        hits.sort(key=lambda h: -h.score)
        strong = [h for h in hits if h.confidence in ("high", "medium")]
        for h in hits:
            h.alternatives = max(0, len(strong) - 1)
            if h.is_combined:
                _attach_chapter(con, h)
        return hits[:limit]


def _attach_chapter(con, hit: Hit) -> None:
    """Name the episode a timestamp falls in, inside a season pack."""
    row = con.execute(
        """SELECT idx, start_ms, title FROM chapter
            WHERE media_id=? AND start_ms<=? ORDER BY start_ms DESC LIMIT 1""",
        (hit.media_id, hit.start_ms)).fetchone()
    if row:
        hit.chapter_index = row["idx"]
        hit.chapter_title = row["title"] or ""
        hit.chapter_offset_ms = max(0, hit.start_ms - row["start_ms"])


# ---------------------------------------------------------------------------
# Batch resolution — the pre-flight pass over a whole visual script
# ---------------------------------------------------------------------------

@dataclass
class ShotResolution:
    beat: int
    shot: int
    query: str
    status: str                    # resolved | ambiguous | weak | not_found | no_query
    hit: Hit | None = None
    others: list = field(default_factory=list)
    note: str = ""


def resolve_script(db_path: str, beats: list, log=print) -> list[ShotResolution]:
    """Resolve every shot of a parsed visual script against the library.

    `beats` is the JSON produced by the visual-script prompt: a list of
    {"beat": n, "shots": [{source, season_episode, exact_dialogue, ...}]}.
    """
    con = connect(db_path)
    out = []
    try:
        for b in beats:
            beat_no = b.get("beat", len(out) + 1)
            for si, shot in enumerate(b.get("shots") or [], 1):
                quote = (shot.get("exact_dialogue") or "").strip()
                fallback = (shot.get("nearest_dialogue") or "").strip()
                query = quote or fallback
                if not query:
                    out.append(ShotResolution(beat_no, si, "", "no_query",
                                              note="no dialogue given — needs visual search"))
                    continue

                season = episode = None
                se = str(shot.get("season_episode") or "")
                m = re.match(r"(?i)\s*s(\d{1,2})\s*e(\d{1,3})\s*$", se)
                if m:
                    season, episode = int(m.group(1)), int(m.group(2))

                show = shot.get("source")
                hits = find(db_path, query, show=show, con=con)
                # A wrong episode hint must not hide a correct match elsewhere
                if not hits and (season is not None or show):
                    hits = find(db_path, query, show=show, con=con)
                if not hits:
                    hits = find(db_path, query, con=con)

                if not hits:
                    out.append(ShotResolution(beat_no, si, query, "not_found",
                                              note="no dialogue match in library"))
                    continue

                top = hits[0]
                note = ""
                if season is not None and top.season is not None and (
                        top.season != season or top.episode != episode):
                    note = (f"script said S{season:02d}E{episode:02d}, "
                            f"library says S{top.season:02d}E{top.episode:02d} "
                            "— trusting the library")
                if top.confidence == "high":
                    second = hits[1].score if len(hits) > 1 else 0
                    status = "ambiguous" if second >= top.score - 4 else "resolved"
                elif top.confidence == "medium":
                    status = "weak"
                else:
                    status = "not_found"
                if not quote and status in ("resolved", "weak"):
                    note = (note + "; " if note else "") + \
                        "located via nearest dialogue — verify the shot visually"
                out.append(ShotResolution(beat_no, si, query, status, top,
                                          hits[1:3], note))
        return out
    finally:
        con.close()
