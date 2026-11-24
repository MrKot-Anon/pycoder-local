from pathlib import Path
import os
import shlex
import shutil
import sys
import tempfile
import unittest

from pycoder.config import Config
from pycoder.history import History
from pycoder.tools import Tools


PASSWORD_READER = """import os,termios
fd = os.open('/dev/tty', os.O_RDWR)
assert os.tcgetpgrp(fd) == os.getpgrp()
old = termios.tcgetattr(fd)
hidden = list(old)
hidden[3] &= ~termios.ECHO
termios.tcsetattr(fd, termios.TCSANOW, hidden)
try:
    os.write(fd, b'Password: ')
    password = os.read(fd, 256).strip()
finally:
    termios.tcsetattr(fd, termios.TCSANOW, old)
    os.close(fd)
assert password == b'fixture-password'
print('PASSWORD_INPUT_OK', flush=True)
"""


@unittest.skipUnless(os.name == "posix", "Controlling terminals require POSIX")
class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.history = History(self.root / "state", self.root)
        self.addCleanup(self.history.close)
        self.cfg = Config(model="test", sandbox_enabled=False, approval="auto", sandbox_network="direct")
        def unexpected(*args):
            self.fail("Unexpected approval request")
        self.tools = Tools(self.root, self.cfg, self.history, unexpected, unexpected, lambda _: None)
        self.addCleanup(self.tools.close)

    def read_password(self, sandbox):
        self.cfg.sandbox_enabled = sandbox
        result = self.tools.exec_command(shlex.join([sys.executable, "-c", PASSWORD_READER]), tty=True, timeout=5,
                                         yield_time_ms=500)
        output = result["output"]
        for _ in range(10):
            if "Password:" in output or not result.get("running"):
                break
            result = self.tools.write_stdin(result["session_id"], yield_time_ms=100)
            output += result["output"]
        self.assertTrue(result.get("running"), result)
        self.assertIn("Password:", output)
        result = self.tools.write_stdin(result["session_id"], chars="fixture-password\n", yield_time_ms=1000)
        output += result["output"]
        while result.get("running"):
            result = self.tools.write_stdin(result["session_id"], yield_time_ms=1000)
            output += result["output"]
        self.assertEqual(result["exit_code"], 0, result)
        self.assertIn("PASSWORD_INPUT_OK", output)
        self.assertNotIn("fixture-password", output)
        if sandbox:
            self.assertEqual(result["sandbox"]["backend"], "seatbelt")

    def test_controlling_terminal_with_password_input_and_echo_disabled(self):
        self.read_password(False)

    def test_pty_process_group_can_be_terminated(self):
        code = "import os,time; print('READY',flush=True); time.sleep(30)"
        result = self.tools.exec_command(shlex.join([sys.executable, "-c", code]), tty=True, yield_time_ms=500)
        self.assertTrue(result.get("running"), result)
        result = self.tools.write_stdin(result["session_id"], terminate=True, yield_time_ms=1000)
        self.assertFalse(result.get("running"), result)
        self.assertNotEqual(result["exit_code"], 0)

    @unittest.skipUnless(sys.platform == "darwin", "macOS Seatbelt terminal regression")
    def test_sandbox_controlling_terminal_with_password_input(self):
        self.read_password(True)

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("sshpass"), "macOS and sshpass required")
    def test_sshpass_nested_terminal_reads_password_inside_sandbox(self):
        self.cfg.sandbox_enabled = True
        command = [shutil.which("sshpass"), "-p", "fixture-password", sys.executable, "-c", PASSWORD_READER]
        for tty in (False, True):
            with self.subTest(tty=tty):
                result = self.tools.exec_command(shlex.join(command), tty=tty, timeout=5, yield_time_ms=1000)
                output = result["output"]
                while result.get("running"):
                    result = self.tools.write_stdin(result["session_id"], yield_time_ms=1000)
                    output += result["output"]
                self.assertEqual(result["exit_code"], 0, result)
                self.assertIn("PASSWORD_INPUT_OK", output)
                self.assertNotIn("fixture-password", output)

    @unittest.skipUnless(sys.platform == "darwin", "macOS Seatbelt terminal regression")
    def test_other_host_terminal_is_not_opened_for_writing(self):
        import pty
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        self.cfg.sandbox_enabled = True
        code = f"import os; os.open({os.ttyname(slave)!r}, os.O_RDWR)"
        result = self.tools.exec_command(shlex.join([sys.executable, "-c", code]), tty=True, timeout=5)
        self.assertNotEqual(result["exit_code"], 0)
        self.assertIn("Operation not permitted", result["output"])
