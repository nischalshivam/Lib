"""Whether the graphics card can really be used, rather than whether it exists.

Two separate failures put this file here, and they look identical from the
outside — "GPU nahi chala" — while having nothing in common:

  1. A version number typed into a batch file. `torch==2.5.1` on Python
     3.14 produced "Could not find a version that satisfies the requirement
     ... (from versions: none)", which reads like a network fault and is a
     calendar fault.

  2. A wheel that has CUDA but not *this card's* kernels. `is_available()`
     returns True, `.to("cuda")` returns fine, and the first multiply dies.
     Measured in this very container: torch 2.13.0+cu130 is built for
     sm_75 and up, and a Quadro P1000 is sm_61.

The second is the dangerous one, because it fails late — after an index has
been running for forty minutes reporting GPU.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from media_index import gpu                                 # noqa: E402


class TestWhatTheWheelWasBuiltFor(unittest.TestCase):

    def test_a_pascal_card_on_a_modern_wheel_is_caught_before_any_work(self):
        rep = gpu.Report(torch="2.13.0+cu130", cuda_build="13.0",
                         capability=(6, 1), driver_sees_card=True,
                         arch_list=["sm_75", "sm_80", "sm_86", "sm_90"])
        self.assertEqual(rep.sm, "sm_61")
        self.assertTrue(rep.wrong_arch)
        self.assertFalse(rep.usable)

    def test_a_card_the_wheel_covers_is_not_flagged(self):
        rep = gpu.Report(torch="2.13.0+cu130", cuda_build="13.0",
                         capability=(8, 6), driver_sees_card=True,
                         arch_list=["sm_75", "sm_80", "sm_86"])
        self.assertFalse(rep.wrong_arch)

    def test_a_cpu_wheel_knows_it_is_one(self):
        rep = gpu.Report(torch="2.13.0+cpu", cuda_build="")
        self.assertTrue(rep.cpu_only_wheel)
        self.assertFalse(rep.usable)

    def test_an_empty_arch_list_never_accuses_the_card(self):
        """Old torch has no `get_arch_list`. Not knowing is not evidence."""
        rep = gpu.Report(torch="1.9.0", cuda_build="11.1", capability=(6, 1),
                         driver_sees_card=True, arch_list=[])
        self.assertFalse(rep.wrong_arch)

    def test_seeing_the_card_is_not_the_same_as_using_it(self):
        """The whole point. `driver_sees_card` was the old test and it lied."""
        rep = gpu.Report(torch="2.13.0+cu130", cuda_build="13.0",
                         capability=(6, 1), driver_sees_card=True,
                         arch_list=["sm_90"], computed=False)
        self.assertTrue(rep.driver_sees_card)
        self.assertFalse(rep.usable)


class TestProbingThisMachine(unittest.TestCase):

    def test_probe_never_raises_and_always_names_the_interpreter(self):
        rep = gpu.probe()
        self.assertTrue(rep.python)
        self.assertTrue(rep.executable)

    def test_a_machine_that_cannot_compute_is_told_why(self):
        rep = gpu.probe()
        if not rep.usable:
            self.assertTrue(rep.fault, "no GPU must always come with a reason")

    def test_the_picture_model_asks_for_a_device_it_can_actually_use(self):
        self.assertIn(gpu.usable_device(), ("cpu", "cuda"))


class TestAskingTheIndexInsteadOfGuessing(unittest.TestCase):

    def test_no_version_is_hardcoded_anywhere(self):
        """The original bug, as a test.

        A pin is a claim about somebody else's computer. If one ever
        reappears in this module, this fails.
        """
        import ast
        with open(gpu.__file__, encoding="utf-8") as f:
            source = f.read()
        # Prose is allowed to name 2.5.1 — that is the story of the bug.
        # Only what actually executes is under test, so every string the
        # module can hand to pip is examined, and nothing else.
        strings = [n.value for n in ast.walk(ast.parse(source))
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        # "torch==" bare is the f-string prefix in `install` — the version
        # after it comes from the index, which is the entire point.
        allowed = {"torch==", "torch==0.0.0.dev0"}
        pinned = [s for s in strings
                  if s.startswith("torch==") and s not in allowed]
        self.assertEqual(pinned, [], f"a typed version came back: {pinned}")
        self.assertIn("torch==0.0.0.dev0", strings)

    def test_pip_refusal_is_read_as_the_list_of_what_exists(self):
        said = ("ERROR: Could not find a version that satisfies the "
                "requirement torch==0.0.0.dev0 (from versions: 2.5.1, "
                "2.6.0, 2.7.0)\n")
        found = gpu._VERSIONS.search(said)
        self.assertEqual([v.strip() for v in found.group(1).split(",")],
                         ["2.5.1", "2.6.0", "2.7.0"])

    def test_nothing_available_is_read_as_nothing_not_as_a_version(self):
        said = ("ERROR: Could not find a version that satisfies the "
                "requirement torch==0.0.0.dev0 (from versions: none)\n")
        found = gpu._VERSIONS.search(said)
        raw = [v.strip() for v in found.group(1).split(",")]
        self.assertEqual([v for v in raw if v and v.lower() != "none"], [])

    def test_versions_sort_by_number_not_by_string(self):
        have = ["2.9.1", "2.10.0", "2.5.1"]
        self.assertEqual(sorted(have, key=gpu._key)[-1], "2.10.0")

    def test_channels_are_asked_newest_first(self):
        self.assertEqual(gpu.CUDA_CHANNELS[0], "cu130")
        self.assertEqual(gpu.CUDA_CHANNELS[-1], "cu118")


if __name__ == "__main__":
    unittest.main()
