import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pycoder.cli import main
from pycoder.history import History
from pycoder.storage import prepare_state, state_directory
from pycoder.workspace import latest_session


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.config = self.home / ".config/pycoder"
        self.primary = self.home / ".local/state/pycoder"
        self.home.mkdir()
        self.patch_home = patch("pathlib.Path.home", return_value=self.home)
        self.patch_home.start()

    def tearDown(self):
        self.patch_home.stop()
        self.tmp.cleanup()

    def test_fresh_install_uses_primary_when_writable(self):
        self.assertEqual(state_directory(None, self.config), self.primary)
        self.assertTrue((self.primary / "sessions").is_dir())

    def test_permission_failure_selects_stable_fallback_and_finds_history(self):
        messages = []
        def primary_denied(directory):
            if directory == self.primary:
                raise PermissionError("parent belongs to root")
            return prepare_state(directory)
        with patch("pycoder.storage.prepare_state", side_effect=primary_denied):
            chosen = state_directory(None, self.config, messages.append)
        self.assertEqual(chosen, self.config / "state")
        self.assertTrue(messages)
        history = History(chosen / "sessions", self.root)
        history.append({"role": "user", "content": "restore me"})
        session_id = history.session_id
        history.close()
        # Primary has become writable, but history must not move or disappear.
        self.assertEqual(state_directory(None, self.config), chosen)
        self.assertEqual(state_directory(None, self.config, create=False), chosen)
        self.assertEqual(latest_session(chosen / "sessions", self.root).session_id, session_id)

    def test_existing_primary_is_not_silently_abandoned(self):
        prepare_state(self.primary)
        history = History(self.primary / "sessions", self.root)
        history.append({"role": "user", "content": "existing task"})
        original = history.path.read_bytes()
        path = history.path
        history.close()
        with patch("pycoder.storage.prepare_state", side_effect=PermissionError):
            with self.assertRaisesRegex(ValueError, "существующей истории"):
                state_directory(None, self.config)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.config / "state").exists())

    def test_explicit_directory_never_falls_back(self):
        with patch("pycoder.storage.prepare_state", side_effect=PermissionError):
            with self.assertRaisesRegex(ValueError, "явно заданный"):
                state_directory(self.root / "requested", self.config)
        self.assertFalse((self.config / "state").exists())

    def test_listing_does_not_create_storage(self):
        self.assertEqual(state_directory(None, self.config, create=False), self.primary)
        self.assertFalse(self.primary.exists())

    def test_cli_checks_storage_before_asking_for_key_or_catalog(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        with patch("pycoder.cli.state_directory", side_effect=ValueError("storage unavailable")), \
                patch("pycoder.cli.wizard") as wizard, patch("sys.stdin", Terminal()), \
                patch("sys.stderr", io.StringIO()) as errors:
            code = main(["-C", str(self.root), "--config-dir", str(self.config)])
        self.assertEqual(code, 1)
        self.assertIn("storage unavailable", errors.getvalue())
        wizard.assert_not_called()

    def test_env_directory_is_an_explicit_path(self):
        from pycoder.cli import parser
        with patch.dict(os.environ, {"PYCODER_STATE_DIR": str(self.root / "custom")}, clear=True):
            self.assertEqual(parser().parse_args([]).state_dir, self.root / "custom")
