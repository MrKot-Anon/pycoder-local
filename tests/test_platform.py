import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pycoder.platform_support import default_shell, shell_args, shortcut_help
from pycoder.processes import Processes
from pycoder.workspace import WorkspaceLock


class PlatformTests(unittest.TestCase):
    def test_shell_quoting_and_os_shortcuts(self):
        with patch("pycoder.platform_support.shutil.which", return_value="powershell.exe"):
            self.assertEqual(default_shell("win32"), "powershell.exe")
        self.assertEqual(default_shell("linux"), "/bin/sh")
        self.assertEqual(shell_args("echo hello", "/bin/sh"), ["/bin/sh", "-c", "echo hello"])
        self.assertEqual(shell_args("echo hello", r"C:\Windows\System32\cmd.exe")[1:], ["/d", "/s", "/c", "echo hello"])
        ps = shell_args("Write-Output 'Привет'", r"C:\Windows\powershell.exe")
        self.assertEqual(ps[1:5], ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertIn("UTF8Encoding", ps[-1])
        self.assertIn("⌘C", shortcut_help("darwin"))
        for platform in ("linux", "win32"):
            self.assertNotIn("⌘", shortcut_help(platform))
            self.assertIn("Ctrl+C", shortcut_help(platform))
            self.assertIn("Ctrl+V", shortcut_help(platform))
            self.assertIn("Ctrl+J", shortcut_help(platform))

    def test_direct_argv_preserves_spaces_unicode_and_metacharacters(self):
        with tempfile.TemporaryDirectory() as tmp:
            processes = Processes()
            try:
                expected = "привет ' \" ; & $HOME"
                result = processes.start([sys.executable, "-c", "import sys; print(sys.argv[1])", expected], Path(tmp), yield_time_ms=3000)
                self.assertEqual(result["exit_code"], 0)
                self.assertEqual(result["output"].strip(), expected)
            finally:
                processes.close()

    def test_workspace_lock_same_path_and_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = WorkspaceLock(root)
            try:
                with self.assertRaises(ValueError):
                    WorkspaceLock(root)
            finally:
                lock.close()
            WorkspaceLock(root).close()


@unittest.skipUnless(os.name == "nt", "Native Windows checks")
class WindowsNativeTests(unittest.TestCase):
    def test_job_stops_descendants_and_tty_falls_back_to_pipes(self):
        import ctypes as C
        from ctypes import wintypes as W
        from pycoder.windows import api, kernel
        with tempfile.TemporaryDirectory() as tmp:
            processes = Processes()
            code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); print(p.pid,flush=True); time.sleep(60)"
            try:
                result = processes.start([sys.executable, "-u", "-c", code], Path(tmp), tty=True, yield_time_ms=3000)
                self.assertIn("ConPTY", result["notice"])
                pid = int(result["output"].strip())
                dll = kernel()
                handle = api(dll, "OpenProcess", W.HANDLE, [W.DWORD, W.BOOL, W.DWORD])(0x100000, False, pid)
                self.assertTrue(handle)
                try:
                    stopped = processes.poll(result["session_id"], terminate=True, yield_time_ms=3000)
                    self.assertFalse(stopped["running"])
                    self.assertEqual(api(dll, "WaitForSingleObject", W.DWORD, [W.HANDLE, W.DWORD])(handle, 3000), 0)
                finally:
                    api(dll, "CloseHandle", W.BOOL, [W.HANDLE])(handle)
            finally:
                processes.close()

    def test_windows_bundle_install_launcher_and_replacement(self):
        from build import build
        from pycoder.install import install
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = build(root / "app.pyz")
            target = install(root / "commands с пробелом", source=bundle)
            self.assertEqual(target.name, "pycoder.pyz")
            launcher = target.with_suffix(".cmd")
            result = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(launcher), "--version"],
                                    cwd=root, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("pycoder", result.stdout)
            install(target.parent, source=target)
            self.assertEqual(target.read_bytes(), bundle.read_bytes())
