import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
import json

from build import build
from pycoder.unpack import unpack


class UnpackTests(unittest.TestCase):
    def test_roundtrip_offline_from_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = build(root / "pycoder")
            env = dict(os.environ, PYCODER_CONFIG_DIR=str(root / "config"),
                       PYCODER_STATE_DIR=str(root / "state"), PYTHONPATH="")
            target = root / "исходники с пробелами"
            result = subprocess.run([sys.executable, "-S", str(bundle), "--unpack", str(target)],
                                    env=env, cwd=root, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ("build.py", "run.py", "pyproject.toml", "README.md", "tests/test_unpack.py",
                         "scripts/probe_model.py", "vendor/textual/__init__.py"):
                self.assertTrue((target / name).is_file(), name)
            self.assertFalse((root / "config").exists())
            self.assertFalse((root / "state").exists())
            self.assertFalse((target / "errors.log").exists())
            # Block networking during rebuild, then verify a byte-identical distribution.
            harness = "import runpy, socket; socket.socket.connect=lambda *a: (_ for _ in ()).throw(AssertionError('network')); runpy.run_path('build.py', run_name='__main__')"
            result = subprocess.run([sys.executable, "-S", "-c", harness], cwd=target,
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(bundle.read_bytes(), (target / "dist/pycoder").read_bytes())
            # Editing the restored project changes the executable it builds.
            init = target / "pycoder/__init__.py"
            init.write_text(init.read_text(encoding="utf-8") + '\n__version__ = "edited"\n', encoding="utf-8")
            subprocess.run([sys.executable, "-S", "build.py"], cwd=target, env=env,
                           capture_output=True, check=True, timeout=30)
            result = subprocess.run([sys.executable, "-S", str(target / "dist/pycoder"), "--version"],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.stdout.strip(), "pycoder edited")
            with self.assertRaisesRegex(ValueError, "уже существует"):
                unpack(target, source=bundle)
            self.assertIn('"edited"', init.read_text(encoding="utf-8"))

    def test_rejects_unsafe_archive_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("../escape", "/absolute", "C:/escape", "a\\escape", "a/../escape"):
                bundle = root / "bad.zip"
                with zipfile.ZipFile(bundle, "w") as archive:
                    archive.writestr("_source/manifest.json", json.dumps({name: "payload"}))
                    archive.writestr("payload", "bad")
                with self.assertRaisesRegex(ValueError, "Unsafe path"):
                    unpack(root / "destination", source=bundle)
                self.assertFalse((root / "destination").exists())

    def test_old_archive_has_clear_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with zipfile.ZipFile(root / "old.zip", "w") as archive:
                archive.writestr("__main__.py", "pass")
            with self.assertRaisesRegex(ValueError, "не содержит"):
                unpack(root / "destination", source=root / "old.zip")
