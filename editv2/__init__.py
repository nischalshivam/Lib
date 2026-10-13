"""editv2 — the grounded, top-tier editing engine (2026-10).

clean script  ->  clue (agy Claude, grounded on OUR catalogs)  ->  voice + word timings
-> line plan + Gemini re-anchor verify + real-cut ranges  ->  pre-cut clips (text-zone safe)
-> look (A pop / B framed / C cinematic / auto) with depth-text, colour isolation,
   annotations, cutouts, kinetic text in each show's own typography
-> HyperFrames render -> mix (-14 LUFS) -> QA report.

Entry point: python -m editv2 build|clue|styles ... (see pipeline.py)."""
