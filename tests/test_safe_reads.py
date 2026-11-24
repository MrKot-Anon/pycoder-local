import os
from pathlib import Path
import tempfile
import unittest

from pycoder.safe_reads import trusted_read
from pycoder.config import Config
from pycoder.history import History
from pycoder.tools import Tools


@unittest.skipUnless(os.name == "posix", "POSIX readers; Windows uses structured tools")
class SafeReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "a.py").write_text("first\nsecond\nthird\n")
        (self.root / "space name").write_text("hello")
        (self.base / "secret").write_text("outside")
        (self.root / "escape").symlink_to(self.base / "secret")
        self.history = History(self.base / "state", self.root)
        self.questions = []
        self.tools = Tools(self.root, Config(model="test", sandbox_enabled=False), self.history,
                           lambda *a: self.questions.append(a) or False, lambda q: "", lambda _: None)

    def tearDown(self):
        self.tools.close()
        self.history.close()
        self.tmp.cleanup()

    def check(self, cmd):
        return trusted_read(cmd, self.root, self.root, "/bin/sh")

    def test_composite_read_executes_without_question(self):
        result = self.tools.exec_command("head -2 a.py; echo ====; tail -1 a.py", yield_time_ms=1000)
        self.assertFalse(result.get("denied"))
        self.assertEqual(self.questions, [])
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("first", result["output"])
        self.assertIn("====", result["output"])
        self.assertIn("third", result["output"])

    def test_supported_readers(self):
        for cmd in ("head -30 a.py", "head -n 2 a.py && tail -c 3 a.py", "cat 'space name'",
                    "wc -l a.py", "ls -la .", "grep -n first a.py", "sed -n '1,2p' a.py",
                    "echo 'a;b'", "echo 'a&&b'", "cat -- a.py"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.check(cmd))
        result = self.tools.shell(["head", "-1", "a.py"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(self.questions, [])

    def test_unsupported_or_unsafe_commands_require_approval_as_a_whole(self):
        commands = ["head -1 a.py; touch changed", "cat ../secret", "cat escape",
                    "head $(touch changed) a.py", "head `touch changed` a.py", "head a.py > changed",
                    "head a.py 2>&1", "cat < a.py", "cat a.py | sh", "cat a.py &", "cat a.py\ntouch changed",
                    "cat *.py", "cat /dev/zero", "cat -", "cat", "tail -f a.py", "grep -R first .",
                    "grep -f ../secret a.py", "sed -i s/a/b/ a.py", "sed -n '1e touch changed' a.py",
                    "sed -n '1w changed' a.py", "env head a.py", "X=1 head a.py", "./head a.py",
                    "python3 -c 'print(1)'", "find . -exec sh -c x \\;", "rg --pre sh first a.py"]
        for cmd in commands:
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.check(cmd))
                self.assertTrue(self.tools.exec_command(cmd)["denied"])
        self.assertEqual(len(self.questions), len(commands))
        self.assertFalse((self.root / "changed").exists())

    def test_fifo_and_custom_shell_are_not_autoapproved(self):
        os.mkfifo(self.root / "pipe")
        self.assertIsNone(self.check("cat pipe"))
        self.assertIsNone(trusted_read("head a.py", self.root, self.root, "/bin/bash"))
        self.tools.config.approval = "never"
        self.assertTrue(self.tools.exec_command("head a.py")["denied"])

    def test_local_executable_cannot_shadow_system_reader(self):
        (self.root / "head").write_text("malicious")
        normalized = self.check("head a.py")
        self.assertTrue(normalized.startswith(("/usr/bin/head ", "/bin/head ")))
        self.assertIn(str(self.root / "a.py"), normalized)
