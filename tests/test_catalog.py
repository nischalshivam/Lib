"""Index a whole title into a searchable, tagged shot library — offline."""
import json
import os
import shutil
import tempfile
import unittest

from media_index import catalog


class Cue:
    def __init__(self, start_ms, end_ms, text):
        self.start_ms, self.end_ms, self.text = start_ms, end_ms, text


class TestSegmentation(unittest.TestCase):

    def test_cuts_become_windows_and_long_takes_are_split(self):
        # a 35s middle take (and the 12s first take) must not ship whole
        got = catalog.shots_from_cuts([12.0, 47.0], 60.0)
        self.assertTrue(all(b - a <= catalog.MAX_SHOT_S + 1e-6 for a, b in got))
        # a real cut point is always preserved as a window boundary
        boundaries = {a for a, _b in got} | {b for _a, b in got}
        self.assertIn(12.0, boundaries)
        self.assertIn(47.0, boundaries)
        # windows tile the whole duration with no gaps
        self.assertAlmostEqual(got[0][0], 0.0)
        self.assertAlmostEqual(got[-1][1], 60.0)
        for (a1, b1), (a2, b2) in zip(got, got[1:]):
            self.assertAlmostEqual(b1, a2)

    def test_a_sliver_is_folded_into_the_previous_shot(self):
        got = catalog.shots_from_cuts([10.0, 10.3], 20.0)   # 0.3s sliver
        self.assertTrue(all(b - a >= catalog.MIN_SHOT_S for a, b in got))

    def test_fixed_windows_tile_the_duration(self):
        got = catalog.fixed_windows(13.0, win_s=5.0)
        self.assertEqual(got, [(0.0, 5.0), (5.0, 10.0), (10.0, 13.0)])

    def test_zero_duration_is_no_shots(self):
        self.assertEqual(catalog.shots_from_cuts([], 0), [])
        self.assertEqual(catalog.fixed_windows(0), [])


class TestDialogueOverlap(unittest.TestCase):

    def test_only_overlapping_cues_are_attached(self):
        cues = [Cue(1000, 3000, "before"), Cue(4000, 6000, "inside"),
                Cue(9000, 9500, "after")]
        self.assertEqual(catalog.dialogue_for(cues, 3.5, 7.0), "inside")


class TestParseTags(unittest.TestCase):

    def test_a_clean_answer_parses(self):
        out = catalog.parse_tags(json.dumps({
            "description": "Arthur alone in a dim room",
            "tags": ["Arthur", "DIM", "alone"], "characters": ["Arthur"],
            "action": "sits", "shot_type": "Close-Up", "quality": "High",
            "safe": True}))
        self.assertEqual(out["characters"], ["Arthur"])
        self.assertEqual(out["tags"], ["arthur", "dim", "alone"])
        self.assertEqual(out["shot_type"], "close-up")
        self.assertEqual(out["quality"], "high")

    def test_unknown_is_never_stored_as_a_character(self):
        out = catalog.parse_tags(json.dumps(
            {"description": "a crowd", "characters": ["unknown", "none"]}))
        self.assertEqual(out["characters"], [])

    def test_fenced_or_junky_answer_is_survived(self):
        text = "```json\n{\"description\": \"x\", \"safe\": false}\n```"
        out = catalog.parse_tags(text)
        self.assertEqual(out["description"], "x")
        self.assertFalse(out["safe"])

    def test_garbage_is_an_empty_dict_not_a_crash(self):
        self.assertEqual(catalog.parse_tags("no json here"), {})


class TestCharacterCanon(unittest.TestCase):

    def test_alias_lines_map_every_alias_to_one_name(self):
        canon = catalog.alias_map([
            "Arthur = Arthur Fleck, Joker, Joaquin Phoenix",
            "Murray = Murray Franklin",
            "Penny"])
        self.assertEqual(canon["joker"], "Arthur")
        self.assertEqual(canon["joaquin phoenix"], "Arthur")
        self.assertEqual(canon["arthur fleck"], "Arthur")
        self.assertEqual(canon["murray franklin"], "Murray")
        self.assertEqual(canon["penny"], "Penny")

    def test_the_actor_persona_and_name_collapse_to_one(self):
        canon = catalog.alias_map(["Arthur = Arthur Fleck, Joker, Joaquin Phoenix"])
        got = catalog.canonicalize(
            ["Joaquin Phoenix", "Joker", "Arthur Fleck"], canon)
        self.assertEqual(got, ["Arthur"])

    def test_an_unknown_name_is_kept_not_dropped(self):
        canon = catalog.alias_map(["Arthur = Joker"])
        got = catalog.canonicalize(["Joker", "Randall"], canon)
        self.assertEqual(got, ["Arthur", "Randall"])


class TestReferenceIdentity(unittest.TestCase):
    """Reference photos at catalogue time are the foundation fix: without them
    the model cannot name a minor character and every silent shot lands with
    `characters: []`, so retrieval's character filter has nothing to surface."""

    def _flatten(self, messages):
        """All text across the user message, so a rule/image can be asserted."""
        text = " ".join(m["content"] for m in messages
                        if isinstance(m["content"], str))
        for m in messages:
            if isinstance(m["content"], list):
                text += " " + " ".join(p.get("text", "") for p in m["content"]
                                       if p.get("type") == "text")
        return text

    def _images(self, messages):
        return [p for m in messages if isinstance(m["content"], list)
                for p in m["content"] if p.get("type") == "image_url"]

    def test_without_refs_the_prompt_still_refuses_to_guess(self):
        msgs = catalog.tag_messages([b"\xff\xd8shot"])
        text = self._flatten(msgs)
        self.assertIn("Never guess", text)
        # only the one shot frame, no reference images
        self.assertEqual(len(self._images(msgs)), 1)

    def test_refs_add_labelled_reference_images_before_the_shot(self):
        refs = {"Victor": [b"\xff\xd8victorface"], "Hank": [b"\x89PNG\r\n\x1a\nhank"]}
        msgs = catalog.tag_messages([b"\xff\xd8shot"], refs=refs)
        text = self._flatten(msgs)
        self.assertIn("Reference — Victor:", text)
        self.assertIn("Reference — Hank:", text)
        # match against references, not a blind guess
        self.assertIn("SAME person", text)
        # two reference images + one shot frame
        self.assertEqual(len(self._images(msgs)), 3)

    def test_refs_cap_photos_per_character(self):
        refs = {"Victor": [b"\xff\xd8" + bytes([i]) for i in range(10)]}
        msgs = catalog.tag_messages([b"\xff\xd8shot"], refs=refs)
        # at most CATALOG_REF_PHOTOS reference photos + 1 shot frame — the cap
        # keeps a large cast from ballooning every shot's call.
        self.assertEqual(len(self._images(msgs)),
                         catalog.CATALOG_REF_PHOTOS + 1)

    def test_build_catalog_threads_refs_into_every_shot_prompt(self):
        seen = {}

        def ask(messages):
            seen["msgs"] = messages
            return json.dumps({"description": "Victor at the cook",
                               "characters": ["Victor"], "quality": "high"})
        refs = {"Victor": [b"\xff\xd8face"]}
        tmp = tempfile.mkdtemp(prefix="ref_")
        try:
            out = os.path.join(tmp, "c.json")
            catalog.build_catalog("BB S04E01", "/e.mp4", 5.0, out,
                                  lambda a, b: [b"\xff\xd8x"], ask,
                                  windows=[(0.0, 5.0)], refs=refs)
            imgs = [p for m in seen["msgs"] if isinstance(m["content"], list)
                    for p in m["content"] if p.get("type") == "image_url"]
            self.assertEqual(len(imgs), 2)   # 1 reference + 1 shot frame
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestBuildCatalog(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cat_")
        self.out = os.path.join(self.tmp, "catalog.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_ask(self, calls):
        def ask(messages):
            calls.append(1)
            return json.dumps({"description": "a man in a room",
                               "tags": ["man", "room"], "characters": ["Arthur"],
                               "action": "stands", "shot_type": "medium",
                               "quality": "high", "safe": True})
        return ask

    def test_every_window_becomes_a_saved_shot(self):
        calls = []
        lib = catalog.build_catalog(
            "Joker (2019)", "/movies/joker.mp4", duration=15.0,
            out_json=self.out, grab=lambda a, b: [b"jpeg"],
            ask=self._fake_ask(calls), windows=catalog.fixed_windows(15.0, 5.0))
        self.assertEqual(len(lib), 3)
        self.assertEqual(len(calls), 3)
        # persisted, and readable back
        on_disk = catalog.load_library(self.out)
        self.assertEqual(len(on_disk), 3)
        self.assertEqual(next(iter(on_disk.values())).source, "Joker (2019)")

    def test_a_resumed_run_does_not_re_tag_done_shots(self):
        windows = catalog.fixed_windows(15.0, 5.0)
        first = []
        catalog.build_catalog("J", "/j.mp4", 15.0, self.out,
                              lambda a, b: [b"x"], self._fake_ask(first),
                              windows=windows)
        self.assertEqual(len(first), 3)
        second = []            # same out file → everything already described
        catalog.build_catalog("J", "/j.mp4", 15.0, self.out,
                              lambda a, b: [b"x"], self._fake_ask(second),
                              windows=windows)
        self.assertEqual(second, [])          # nothing re-asked

    def test_a_grab_that_fails_still_records_the_shot(self):
        def bad_grab(a, b):
            raise RuntimeError("ffmpeg fell over")
        lib = catalog.build_catalog(
            "J", "/j.mp4", 5.0, self.out, bad_grab, self._fake_ask([]),
            windows=[(0.0, 5.0)])
        self.assertEqual(len(lib), 1)          # a blank entry, not a crash
        self.assertEqual(next(iter(lib.values())).description, "")

    def test_a_characters_file_fixes_already_tagged_shots_on_resume(self):
        """Supplying names on a resume must relabel existing shots without
        re-describing a single frame."""
        pre = {"j__00000": catalog.Shot(
            "j__00000", "J", "/j.mp4", 0, 5, description="a man",
            characters=["Joaquin Phoenix", "Joker"])}
        catalog.save_library(self.out, pre)
        canon = catalog.alias_map(["Arthur = Arthur Fleck, Joker, Joaquin Phoenix"])
        asked = []
        catalog.build_catalog("J", "/j.mp4", 5, self.out,
                              lambda a, b: [b"x"], self._fake_ask(asked),
                              canon=canon, windows=[])       # nothing to tag
        self.assertEqual(asked, [])                          # no re-describe
        self.assertEqual(catalog.load_library(self.out)["j__00000"].characters,
                         ["Arthur"])

    def test_dialogue_backfills_onto_old_shots_when_the_srt_arrives_later(self):
        """The first pass ran before the subtitle was found, so old shots have
        no dialogue. A resume with cues must fill it in — without re-tagging."""
        pre = {"j__00000": catalog.Shot("j__00000", "J", "/j.mp4", 0.0, 5.0,
                                        description="a man", dialogue="")}
        catalog.save_library(self.out, pre)
        cues = [Cue(1000, 4000, "Is it just me?")]
        asked = []
        catalog.build_catalog("J", "/j.mp4", 5, self.out,
                              lambda a, b: [b"x"], self._fake_ask(asked),
                              cues=cues, windows=[])
        self.assertEqual(asked, [])
        self.assertIn("Is it just me?",
                      catalog.load_library(self.out)["j__00000"].dialogue)

    def test_dialogue_is_attached_from_cues(self):
        cues = [Cue(1000, 4000, "Is it just me?")]
        lib = catalog.build_catalog(
            "J", "/j.mp4", 5.0, self.out, lambda a, b: [b"x"],
            self._fake_ask([]), cues=cues, windows=[(0.0, 5.0)])
        self.assertIn("Is it just me?", next(iter(lib.values())).dialogue)


class TestRealGrab(unittest.TestCase):
    """real_grab was never exercised by the injected-fake tests, so a missing
    `import tempfile` shipped and failed every shot on a real run. These
    monkeypatch ffmpeg away but run the actual real_grab body."""

    def setUp(self):
        from media_index import frames as frames_mod
        from media_index import cutter
        self._scan, self._pick = frames_mod.scan, frames_mod.pick
        self._extract = cutter.extract_frame
        self.frames_mod, self.cutter = frames_mod, cutter

    def tearDown(self):
        self.frames_mod.scan, self.frames_mod.pick = self._scan, self._pick
        self.cutter.extract_frame = self._extract

    def test_real_grab_writes_and_reads_jpeg_bytes(self):
        class C:
            def __init__(self, t):
                self.time = t
        self.frames_mod.scan = lambda path, a, b: [C(a + 0.5), C(a + 1.5)]
        self.frames_mod.pick = lambda cands, n: cands[:n]

        def fake_extract(path, t, out, width=None):
            with open(out, "wb") as f:
                f.write(b"\xff\xd8jpegbytes")
        self.cutter.extract_frame = fake_extract

        grab = catalog.real_grab("/movie.mp4")
        out = grab(10.0, 15.0)
        self.assertTrue(out and all(b.startswith(b"\xff\xd8") for b in out))

    def test_real_grab_falls_back_to_the_midpoint_when_scan_fails(self):
        def boom(*a, **k):
            raise RuntimeError("ffmpeg gone")
        self.frames_mod.scan = boom
        seen = []

        def fake_extract(path, t, out, width=None):
            seen.append(t)
            with open(out, "wb") as f:
                f.write(b"\xff\xd8x")
        self.cutter.extract_frame = fake_extract
        catalog.real_grab("/m.mp4")(10.0, 20.0)
        self.assertEqual(seen, [15.0])          # window midpoint


class TestMergingLibraries(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="merge_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_folder_of_episode_catalogues_loads_as_one_library(self):
        for ep in ("s03e01", "s03e02"):
            d = os.path.join(self.tmp, ep)
            os.makedirs(d)
            catalog.save_library(os.path.join(d, "x.catalog.json"), {
                f"{ep}__00000": catalog.Shot(f"{ep}__00000", ep, "/v.mp4",
                                             0, 5, description="d")})
        merged = catalog.load_library(self.tmp)         # a folder, not a file
        self.assertEqual(len(merged), 2)
        self.assertIn("s03e01__00000", merged)
        self.assertIn("s03e02__00000", merged)

    def test_episode_ids_never_collide_across_the_series(self):
        # same shot index in two episodes -> two distinct ids via the slug
        a = catalog._slug("Breaking Bad Season 3 Episode 1.mp4") + "__00000"
        b = catalog._slug("Breaking Bad Season 3 Episode 2.mp4") + "__00000"
        self.assertNotEqual(a, b)


class TestSearch(unittest.TestCase):

    def _lib(self):
        return {
            "s1": catalog.Shot("s1", "J", "/j.mp4", 0, 5,
                               description="Arthur dances alone in a dim bathroom",
                               tags=["dance", "bathroom", "alone", "dim"],
                               characters=["Arthur"], quality="high"),
            "s2": catalog.Shot("s2", "J", "/j.mp4", 5, 10,
                               description="Murray on a bright talk show stage",
                               tags=["stage", "talk show", "bright"],
                               characters=["Murray"], quality="high"),
            "s3": catalog.Shot("s3", "J", "/j.mp4", 10, 15,
                               description="a caption-covered recap frame",
                               tags=["text"], characters=[], quality="high",
                               safe=False),
        }

    def test_meaning_query_finds_the_right_shot(self):
        got = catalog.search(self._lib(), "the bathroom dance scene")
        self.assertEqual(got[0].id, "s1")

    def test_character_filter_is_decisive(self):
        got = catalog.search(self._lib(), "on stage", character="Murray")
        self.assertEqual(got[0].id, "s2")

    def test_unsafe_shots_are_excluded(self):
        got = catalog.search(self._lib(), "text recap")
        self.assertTrue(all(s.id != "s3" for s in got))


if __name__ == "__main__":
    unittest.main()
