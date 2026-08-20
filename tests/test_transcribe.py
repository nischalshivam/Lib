"""Tests for making subtitles from audio.

The speech recognition itself is stubbed — the model cannot be downloaded in
this environment, and mocking accuracy would prove nothing anyway. What IS
tested is everything around it, which is where integration bugs live: the
English track is the one extracted, the result becomes a real .srt beside the
video, the index then picks that up through its ordinary sidecar path, a
second run skips work already done, and one bad file does not stop a folder.

Whether Whisper hears the dialogue correctly is a question only a real file
can answer.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import (library, probe, search, subtitles,      # noqa: E402
                         transcribe)
from media_index.demo import make_demo_video as dv               # noqa: E402

HAVE_FFMPEG = probe.ffmpeg_bin() is not None
skip_no_ffmpeg = unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")


def seg(start, end, text):
    return types.SimpleNamespace(start=start, end=end, text=text)


class StubModel:
    """Stands in for WhisperModel. Records what it was handed."""

    def __init__(self, segments=None, fail=False):
        self.segments = segments if segments is not None else [
            seg(52.0, 54.5, " I never wanted the harvest."),
            seg(54.8, 57.5, " I wanted the land it grew on."),
            seg(85.0, 88.0, " Then we burn the field."),
        ]
        self.fail = fail
        self.calls = []

    def transcribe(self, wav, **kw):
        self.calls.append((wav, kw))
        if self.fail:
            raise RuntimeError("simulated decoding failure")
        return iter(self.segments), types.SimpleNamespace(language="en")


class TestPlumbing(unittest.TestCase):
    def test_srt_path_uses_the_english_suffix(self):
        p = transcribe.srt_path_for("/m/Show/Show S01E01.mkv")
        self.assertTrue(p.endswith("Show S01E01.en.srt"))

    def test_segments_become_cues_with_text_trimmed(self):
        cues = transcribe._segments_to_cues(
            [seg(1.0, 2.0, "  hello  "), seg(3.0, 4.0, "   ")])
        self.assertEqual(len(cues), 1)            # blank segment dropped
        self.assertEqual(cues[0].text, "hello")
        self.assertEqual((cues[0].start_ms, cues[0].end_ms), (1000, 2000))

    def test_written_srt_parses_back_identically(self):
        cues = transcribe._segments_to_cues(
            [seg(1.25, 2.5, "first line"), seg(3.0, 4.75, "second line")])
        tmp = tempfile.mkdtemp()
        try:
            path = transcribe.write_srt(cues, os.path.join(tmp, "x.srt"))
            back = subtitles.parse_file(path)
            self.assertEqual([c.text for c in back], ["first line", "second line"])
            self.assertEqual([c.start_ms for c in back], [1250, 3000])
            self.assertEqual([c.end_ms for c in back], [2500, 4750])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_available_matches_the_import(self):
        try:
            import faster_whisper  # noqa: F401
            expected = True
        except ImportError:
            expected = False
        self.assertEqual(transcribe.available(), expected)


@skip_no_ffmpeg
class TestTranscribeFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="tx_")
        cls.vid = dv.build(os.path.join(cls.tmp, "Show S01E01.mkv"),
                           write_srt=False, log=lambda *a: None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        out = transcribe.srt_path_for(self.vid)
        if os.path.exists(out):
            os.remove(out)

    def test_writes_a_subtitle_beside_the_video(self):
        model = StubModel()
        r = transcribe.transcribe_file(self.vid, model=model)
        self.assertEqual(r.status, "done")
        self.assertTrue(os.path.isfile(r.srt_path))
        self.assertEqual(len(r.cues), 3)

    def test_audio_is_extracted_as_16k_mono(self):
        model = StubModel()
        transcribe.transcribe_file(self.vid, model=model)
        wav, kw = model.calls[0]
        self.assertTrue(wav.endswith(".wav"))
        self.assertEqual(kw.get("language"), "en")

    def test_second_run_skips_the_work(self):
        transcribe.transcribe_file(self.vid, model=StubModel())
        model = StubModel()
        r = transcribe.transcribe_file(self.vid, model=model)
        self.assertEqual(r.status, "skipped")
        self.assertEqual(model.calls, [])         # the model was never called

    def test_overwrite_forces_a_redo(self):
        transcribe.transcribe_file(self.vid, model=StubModel())
        model = StubModel()
        r = transcribe.transcribe_file(self.vid, model=model, overwrite=True)
        self.assertEqual(r.status, "done")
        self.assertEqual(len(model.calls), 1)

    def test_silence_is_reported_not_written(self):
        r = transcribe.transcribe_file(self.vid, model=StubModel(segments=[]))
        self.assertEqual(r.status, "failed")
        self.assertIn("no speech", r.note)
        self.assertFalse(os.path.isfile(r.srt_path))

    def test_decoder_failure_is_caught(self):
        r = transcribe.transcribe_file(self.vid, model=StubModel(fail=True))
        self.assertEqual(r.status, "failed")
        self.assertTrue(r.note)

    def test_english_track_is_the_one_extracted(self):
        """A dubbed release lists the dub first; transcribing it would produce
        fluent Hindi against an English script."""
        dual = os.path.join(self.tmp, "dual.mkv")
        subprocess.run(
            [probe.require_ffmpeg(), "-y", "-loglevel", "error",
             "-i", self.vid, "-i", self.vid,
             "-map", "0:v", "-map", "0:a", "-map", "1:a", "-c", "copy",
             "-metadata:s:a:0", "language=hin",
             "-metadata:s:a:1", "language=eng", dual], check=True)
        info = probe.probe(dual)
        self.assertEqual(len(info.audios), 2)
        self.assertEqual(probe.pick_audio(info), 1)

    def test_missing_faster_whisper_is_explained(self):
        real = transcribe._load_model
        transcribe._load_model = lambda *a, **k: (_ for _ in ()).throw(
            transcribe.TranscribeUnavailable("faster-whisper is not installed"))
        try:
            with self.assertRaises(transcribe.TranscribeUnavailable) as ctx:
                transcribe.transcribe_file(self.vid)
            self.assertIn("faster-whisper", str(ctx.exception))
        finally:
            transcribe._load_model = real


@skip_no_ffmpeg
class TestFolderAndIndexIntegration(unittest.TestCase):
    """The point of the whole feature: a folder with no subtitles becomes a
    searchable index without downloading anything."""

    @classmethod
    def setUpClass(cls):
        # Rendered once; each test then works on its own copy, because
        # transcribing MUTATES the folder and tests must not depend on order.
        cls.tmp = tempfile.mkdtemp(prefix="txfolder_")
        cls.template = os.path.join(cls.tmp, "template")
        for e in (1, 2):
            dv.build(os.path.join(cls.template,
                                  f"Breaking Bad Season 2 Episode {e}.mkv"),
                     write_srt=False, log=lambda *a: None)
        # a third file that already has subtitles and must be left alone
        dv.build(os.path.join(cls.template,
                              "Breaking Bad Season 2 Episode 3.mkv"),
                 log=lambda *a: None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="txwork_", dir=self.tmp)
        self.root = os.path.join(self.work, "Breaking Bad Season 2")
        shutil.copytree(self.template, self.root)

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def _transcribe(self):
        real = transcribe._load_model
        transcribe._load_model = lambda *a, **k: StubModel()
        try:
            return transcribe.transcribe_folder(self.root, log=lambda *a: None)
        finally:
            transcribe._load_model = real

    def test_only_files_without_subtitles_are_transcribed(self):
        results = self._transcribe()
        self.assertEqual(len(results), 2)         # episode 3 already had subs
        self.assertTrue(all(r.status == "done" for r in results))

    def test_rerun_transcribes_nothing(self):
        self._transcribe()
        self.assertEqual(self._transcribe(), [])

    def test_index_then_finds_the_transcribed_dialogue(self):
        self._transcribe()
        db = os.path.join(self.work, "library.db")
        res = library.build(self.root, db, log=lambda *a: None)
        self.assertEqual(res.no_subs, [])         # nothing left without subs

        hits = search.find(db, "I never wanted the harvest")
        self.assertTrue(hits)
        self.assertEqual(hits[0].confidence, "high")
        self.assertEqual(hits[0].season, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
