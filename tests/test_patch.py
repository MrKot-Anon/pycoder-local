from pathlib import Path
import shutil
import tempfile
import unittest

from pycoder.patch import PreparedPatch, seek


class PatchTests(unittest.TestCase):
    def test_repeated_updates_are_sequential_and_written_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "a"
            target.write_text("one\n")
            patch = PreparedPatch(root, "*** Begin Patch\n*** Update File: a\n@@\n-one\n+two\n"
                                  "*** Update File: ./a\n@@\n-two\n+three\n*** End Patch")
            self.assertEqual(target.read_text(), "one\n")
            self.assertIn("+three", patch.diff())
            self.assertEqual(patch.apply(), {"changed": ["a"]})
            self.assertEqual(target.read_text(), "three\n")

    def test_repeated_update_failure_changes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a").write_text("one\n")
            with self.assertRaisesRegex(ValueError, "Expected lines not found"):
                PreparedPatch(root, "*** Begin Patch\n*** Update File: a\n@@\n-one\n+two\n"
                              "*** Update File: a\n@@\n-missing\n+three\n*** End Patch")
            self.assertEqual((root / "a").read_text(), "one\n")

    def test_conflicting_operations_have_recovery_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a").write_text("one\n")
            patches = ["*** Delete File: a\n*** Add File: a\n+two\n",
                       "*** Update File: a\n*** Move to: b\n*** Update File: b\n@@\n-one\n+two\n"]
            for body in patches:
                with self.subTest(body=body), self.assertRaisesRegex(ValueError, "No files were changed"):
                    PreparedPatch(root, "*** Begin Patch\n" + body + "*** End Patch")
                self.assertEqual((root / "a").read_text(), "one\n")
                self.assertFalse((root / "b").exists())

    def test_upstream_scenarios(self):
        fixtures = Path(__file__).parent / "fixtures"
        self.assertTrue(fixtures.is_dir())
        failures = {"005", "006", "007", "008", "009", "012", "013"}
        # Deliberate safety differences: overwrite and partial-success behavior.
        differences = {"010", "011", "015"}
        for scenario in sorted(fixtures.iterdir()):
            with self.subTest(scenario=scenario.name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                if (scenario / "input").exists():
                    shutil.copytree(scenario / "input", root, dirs_exist_ok=True)
                before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                patch = (scenario / "patch.txt").read_text()
                if scenario.name[:3] in failures | differences:
                    with self.assertRaises((ValueError, OSError)):
                        PreparedPatch(root, patch).apply()
                    self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()})
                else:
                    PreparedPatch(root, patch).apply()
                    expected = {str(p.relative_to(scenario / "expected")): p.read_bytes()
                                for p in (scenario / "expected").rglob("*") if p.is_file()}
                    actual = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                    self.assertEqual(expected, actual)

    def test_unicode_matching(self):
        self.assertEqual(seek(["  x = “hello”—world\u00a0 "], ['x = "hello"-world']), 0)
        self.assertIsNone(seek(["a"], ["a", "b"]))

    def test_conflict_after_preview(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a").write_text("old\n")
            patch = PreparedPatch(root, "*** Begin Patch\n*** Update File: a\n@@\n-old\n+new\n*** End Patch")
            (root / "a").write_text("user edit\n")
            with self.assertRaisesRegex(ValueError, "changed since"):
                patch.apply()
            self.assertEqual((root / "a").read_text(), "user edit\n")

    def test_outside_and_symlink_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "workspace"
            root.mkdir()
            (root / "link").symlink_to(Path(d), target_is_directory=True)
            for path in ("../escape", "link/escape"):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, "outside"):
                    PreparedPatch(root, f"*** Begin Patch\n*** Add File: {path}\n+x\n*** End Patch")

    def test_mode_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            file = root / "run"
            file.write_text("old\n")
            file.chmod(0o755)
            PreparedPatch(root, "*** Begin Patch\n*** Update File: run\n@@\n-old\n+new\n*** End Patch").apply()
            self.assertEqual(file.stat().st_mode & 0o777, 0o755)
