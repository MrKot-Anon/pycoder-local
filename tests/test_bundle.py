import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from build import build
from pycoder.install import install


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.bundle = build(self.root / "distribution/pycoder")
        self.env = {k: v for k, v in os.environ.items()
                    if k not in {"PYTHONPATH", "PYCODER_API_KEY", "CHUTES_API_KEY"}}
        self.env["PYTHONPATH"] = ""
        self.env["PYCODER_CONFIG_DIR"] = str(self.root / "config")
        self.env["PYCODER_STATE_DIR"] = str(self.root / "state")

    def tearDown(self):
        self.tmp.cleanup()

    def run_bundle(self, bundle, *args, **kwargs):
        return subprocess.run([sys.executable, str(bundle), *args], cwd=self.root,
                              env=self.env, capture_output=True, text=True, timeout=15, **kwargs)

    def test_standalone_reproducible_executable(self):
        other = build(self.root / "second")
        self.assertEqual(self.bundle.read_bytes(), other.read_bytes())
        self.assertEqual(self.bundle.stat().st_mode & 0o777, 0o755)
        result = self.run_bundle(self.bundle, "--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pycoder", result.stdout)
        with zipfile.ZipFile(self.bundle) as archive:
            self.assertIn("LICENSE", archive.namelist())
            self.assertIn("NOTICE", archive.namelist())
            self.assertFalse(any("__pycache__" in name or "credentials" in name
                                 for name in archive.namelist()))

    def test_install_replace_and_reinstall_from_installed_file(self):
        destination = self.root / "commands with spaces"
        destination.mkdir()
        target = destination / "pycoder"
        target.write_text("old broken executable")
        result = self.run_bundle(self.bundle, "--install", str(destination))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_bytes(), self.bundle.read_bytes())
        self.assertFalse((self.root / "config").exists())
        self.assertFalse((self.root / "state").exists())
        self.bundle.unlink()
        result = self.run_bundle(target, "--install", str(destination))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.run_bundle(target, "--version").returncode, 0)
        self.assertFalse(list(destination.glob(".pycoder-install-*")))

    def test_existing_symlink_is_replaced_without_changing_target(self):
        destination = self.root / "bin"
        destination.mkdir()
        unrelated = self.root / "unrelated"
        unrelated.write_text("keep me")
        (destination / "pycoder").symlink_to(unrelated)
        result = self.run_bundle(self.bundle, "--install", str(destination))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(unrelated.read_text(), "keep me")
        self.assertFalse((destination / "pycoder").is_symlink())

    def test_install_failure_preserves_previous_version(self):
        destination = self.root / "bin"
        destination.mkdir()
        target = destination / "pycoder"
        target.write_text("previous version")
        with patch("pycoder.install.shutil.copyfileobj", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                install(destination, source=self.bundle)
        self.assertEqual(target.read_text(), "previous version")
        self.assertFalse(list(destination.glob(".pycoder-install-*")))

    def test_install_from_source_reports_build_instruction(self):
        from pycoder.install import bundle_path
        with self.assertRaisesRegex(ValueError, "build.py"):
            bundle_path()

    def test_no_arguments_uses_terminal_working_directory(self):
        destination = self.root / "bin"
        result = self.run_bundle(self.bundle, "--install", str(destination))
        self.assertEqual(result.returncode, 0, result.stderr)
        workspace = self.root / "actual project"
        workspace.mkdir()
        config = self.root / "config"
        config.mkdir()
        # No live model request is needed to inspect the freshly started session.
        (config / "config.json").write_text(json.dumps({"model": "test", "require_token": False, "output_limit_source": "manual"}))
        env = dict(self.env)
        env["PATH"] = str(destination) + os.pathsep + str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        # Import from the installed archive only. Simulate terminal input to exit
        # after startup without a model request or modifications to real profiles.
        harness = '''import io, runpy, shutil, sys
class Terminal(io.StringIO):
    def isatty(self): return True
sys.stdin = Terminal("\\n/exit\\n")
sys.argv = ["pycoder"]
runpy.run_path(shutil.which("pycoder"), run_name="__main__")
'''
        result = subprocess.run([sys.executable, "-c", harness], cwd=workspace,
                                env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        journals = list((self.root / "state/sessions").glob("*.jsonl"))
        self.assertEqual(len(journals), 1)
        meta = json.loads(journals[0].read_text().splitlines()[0])
        self.assertEqual(meta["data"]["workspace"], str(workspace.resolve()))
        self.assertNotEqual(workspace, destination)

    def test_shebang_direct_execution(self):
        env = dict(self.env)
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        result = subprocess.run([str(self.bundle), "--version"], cwd=self.root,
                                env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_build_script_from_another_directory(self):
        script = Path(__file__).resolve().parents[1] / "build.py"
        result = subprocess.run([sys.executable, str(script), "--output", "custom-app"],
                                cwd=self.root, env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(zipfile.is_zipfile(self.root / "custom-app"))

    def test_bundled_ui_without_site_packages_or_network(self):
        harness = '''import asyncio, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from pycoder.cli import parser
from pycoder.config import Config
from pycoder.runtime import Runtime
from pycoder.tui import CoderApp, QuestionScreen
from pathlib import Path
async def check():
    root = Path.cwd()
    args = parser().parse_args(["--new", "--no-auth", "--config-dir", str(root / "config")])
    runtime = Runtime(args, Config(model="test", require_token=False), root, root / "state")
    app = CoderApp(runtime)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.3)
        assert isinstance(app.screen, QuestionScreen)
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.ready
        assert "pycoder" in app.export_screenshot()
        await pilot.press("ctrl+q")
        await pilot.pause(0.3)
    runtime.stop()
    runtime.thread.join(3)
    assert not runtime.thread.is_alive()
with patch("socket.socket.connect", side_effect=AssertionError("Unexpected network")):
    asyncio.run(check())
print("BUNDLED_UI_OK")
'''
        result = subprocess.run([sys.executable, "-S", "-c", harness, str(self.bundle)], cwd=self.root,
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BUNDLED_UI_OK", result.stdout)
