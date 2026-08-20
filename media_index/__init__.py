"""media_index — the dialogue index.

Turns a folder of movies / series into a searchable index of every spoken
line, so a quote from a script resolves to an exact file + millisecond.

    from media_index import library, search

    library.build("D:/Media", "library.db")
    hits = search.find("library.db", "I am the Armored Titan")

Design rules:
  * The index is built from SUBTITLES only — no video is decoded, so a whole
    series indexes in minutes.
  * Building is incremental; unchanged files are skipped.
  * Matching slides a window over consecutive cues, because a quoted sentence
    is usually split across two or three subtitle cues.
  * Nothing here guesses. Every timestamp comes from a real subtitle file.
"""

__all__ = ["naming", "subtitles", "library", "search"]
__version__ = "0.1.0"
