from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pycoder.clipboard import write_clipboard, read_clipboard
from pycoder.diagnostics import Diagnostics
from pycoder.presentation import tool_label, user_prose


class PresentationTests(unittest.TestCase):
    def test_command_bullet_has_own_color_and_failure_label(self):
        from pycoder.tui import ToolCard
        card = ToolCard("python -c 'print([1])'", "", True)
        card.finish()
        self.assertEqual(card._title.content.plain, "• Ran python -c 'print([1])'")
        self.assertEqual(card._title.content.spans[0].start, 0)
        self.assertEqual(card._title.content.spans[0].end, 1)
        self.assertEqual(card._title.content.spans[0].style, "#7ee787")
        card.finish(failed=True)
        self.assertTrue(card.title.startswith("Failed "))
        self.assertEqual(card._title.content.spans[0].style, "#ff7b72")

    def test_prose_streaming_hides_code_even_when_fences_are_split(self):
        text = "Сейчас проверю.\n```python\nsecret_code()\n```\nГотово."
        for n in range(1, len(text) + 1):
            visible = user_prose(text[:n])
            self.assertNotIn("secret", visible)
            self.assertNotIn("```", visible)
        self.assertEqual(user_prose(text, final=True), "Сейчас проверю.\nГотово.")
        self.assertNotIn("hidden", user_prose("<think>hidden</think>Answer"))
        self.assertEqual(user_prose("Смотри `file.py`"), "Смотри `file.py`")
        self.assertEqual(user_prose("Text\n~~~js\ncode()\n~~~\nEnd"), "Text\nEnd")

    def test_tool_label_includes_target_and_is_bounded(self):
        self.assertEqual(tool_label("read_file", '{"path":"src/main.py"}'), "read_file · src/main.py")
        self.assertIn("pytest", tool_label("exec_command", {"cmd": "python -m pytest -q"}))
        label = tool_label("exec_command", {"cmd": "python " + "x" * 300 + " tail"})
        self.assertLessEqual(len(label), 88)
        self.assertIn("…", label)
        self.assertTrue(label.endswith("tail"))
        self.assertEqual(tool_label("read_file", "[]"), "read_file")

    def test_native_macos_clipboard_uses_stdin_not_shell(self):
        with patch("pycoder.clipboard.sys.platform", "darwin"), patch("pycoder.clipboard.subprocess.run") as run:
            self.assertTrue(write_clipboard("Ошибка; $(not-a-command)"))
            self.assertEqual(run.call_args.args[0], ["/usr/bin/pbcopy"])
            self.assertEqual(run.call_args.kwargs["input"], "Ошибка; $(not-a-command)".encode())
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_error_log_has_traceback_without_token_or_locals(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Diagnostics(Path(directory), lambda s: s.replace("test-secret", "[REDACTED]"))
            try:
                secret_local = "local-value-must-not-be-logged"
                raise ValueError("bad test-secret")
            except ValueError as error:
                self.assertEqual(log.record(error, "ui"), log.path)
            text = log.tail()
            self.assertIn("Traceback", text)
            self.assertIn("[REDACTED]", text)
            self.assertNotIn(secret_local, text)
            self.assertNotIn("test-secret", text)
            self.assertEqual(log.path.stat().st_mode & 0o777, 0o600)

    def test_read_native_clipboards_without_shell(self):
        cases = [("darwin", {}, ["/usr/bin/pbpaste"]),
                 ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, ["wl-paste", "--no-newline", "--type", "text"]),
                 ("linux", {"DISPLAY": ":0"}, ["xclip", "-selection", "clipboard", "-out"])]
        for platform, environment, command in cases:
            with self.subTest(platform=platform, environment=environment), \
                    patch("pycoder.clipboard.os.name", "posix"), \
                    patch("pycoder.clipboard.sys.platform", platform), \
                    patch.dict("os.environ", environment, clear=True), \
                    patch("pycoder.clipboard.shutil.which", return_value="/usr/bin/tool"), \
                    patch("pycoder.clipboard.subprocess.run") as run:
                run.return_value.stdout = "текст\n".encode()
                self.assertEqual(read_clipboard(), "текст\n")
                self.assertEqual(run.call_args.args[0], command)
                self.assertNotIn("shell", run.call_args.kwargs)
        with patch("pycoder.clipboard.os.name", "posix"), patch("pycoder.clipboard.sys.platform", "linux"), \
                patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(read_clipboard())
