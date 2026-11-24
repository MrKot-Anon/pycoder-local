from dataclasses import asdict
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

from pycoder.config import Config
from pycoder.sandbox import Sandbox
from pycoder.settings import edit_sandbox, visible_sandbox_fields


class NetworkSettingsTests(unittest.TestCase):
    def test_fields_match_effective_policy_and_keep_saved_hosts(self):
        cfg = Config(model="test", sandbox_hosts=["example.test:443"], sandbox_direct_hosts=["localhost:1234"])
        for proxy in (False, True):
            cfg.proxy_enabled = proxy
            for mode in ("none", "allowlist", "all", "direct"):
                with self.subTest(proxy=proxy, mode=mode):
                    cfg.sandbox_network = mode
                    fields = visible_sandbox_fields(cfg)
                    self.assertEqual("sandbox_hosts" in fields, mode == "allowlist")
                    self.assertEqual("sandbox_direct_hosts" in fields, mode == "allowlist")
                    self.assertEqual("sandbox_localhost" in fields, mode == "allowlist" or (mode == "all" and proxy))
                    self.assertEqual("sandbox_lan" in fields, "sandbox_localhost" in fields)
        cfg.sandbox_enabled = False
        self.assertEqual(visible_sandbox_fields(cfg), {"sandbox_enabled"})
        self.assertEqual(cfg.sandbox_hosts, ["example.test:443"])
        self.assertEqual(cfg.sandbox_direct_hosts, ["localhost:1234"])

    def test_plain_editor_hides_inactive_fields(self):
        cfg = Config(model="test", sandbox_network="direct", sandbox_hosts=["example.test:443"])
        seen = []
        answers = iter(["sandbox_network", "allowlist", "sandbox_network", "direct", "apply"])
        def choose(title, options, **kwargs):
            if title.startswith("Песочница"):
                seen.append({key for key, _ in options})
            return next(answers)
        with tempfile.TemporaryDirectory() as d:
            edit_sandbox(cfg, Path(d), choose, lambda _: "", lambda _: None)
        self.assertNotIn("sandbox_hosts", seen[0])
        self.assertIn("sandbox_hosts", seen[1])
        self.assertNotIn("sandbox_hosts", seen[2])
        self.assertEqual(cfg.sandbox_hosts, ["example.test:443"])


class NetworkSettingsUITests(unittest.IsolatedAsyncioTestCase):
    async def test_host_controls_disappear_and_return_without_losing_values(self):
        from pycoder.tui import CoderApp, SandboxScreen
        from textual.widgets import OptionList
        from tests.test_tui import FakeRuntime
        runtime = FakeRuntime()
        cfg = Config(model="test", sandbox_network="direct", sandbox_hosts=["example.test:443"],
                     sandbox_direct_hosts=["localhost:1234"], sandbox_localhost=True)
        runtime.view_state["config"] = asdict(cfg)
        app = CoderApp(runtime)
        async with app.run_test(size=(120, 48)) as pilot:
            await pilot.pause()
            screen = SandboxScreen(runtime.view_state["config"])
            app.push_screen(screen)
            await pilot.pause()
            self.assertEqual([key for key, _ in screen.options], ["enabled", "network", "apply", "save", "back"])
            screen.query_one(OptionList).highlighted = 1
            await pilot.press("enter", "down", "enter")
            self.assertIn("hosts", dict(screen.options))
            self.assertIn("example.test:443", dict(screen.options)["hosts"])
            screen.query_one(OptionList).highlighted = 1
            await pilot.press("enter", "down", "down", "down", "enter")
            self.assertNotIn("hosts", dict(screen.options))
            self.assertNotIn("direct_hosts", dict(screen.options))
            self.assertNotIn("sandbox_localhost", dict(screen.options))
            self.assertNotIn("sandbox_lan", dict(screen.options))
            screen.query_one(OptionList).highlighted = 2
            await pilot.press("enter")
            self.assertEqual(runtime.view_state["config"]["sandbox_network"], "direct")
            self.assertEqual(runtime.view_state["config"]["sandbox_hosts"], cfg.sandbox_hosts)


@unittest.skipUnless(sys.platform == "darwin", "macOS Seatbelt regression tests")
class MacFullNetworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def run_sandbox(self, command, cfg):
        sandbox = Sandbox(self.root, cfg)
        try:
            argv, env = sandbox.prepare(command, self.root)
            # Broken ping used to flood stderr; keep even a failing regression bounded in memory.
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(argv, cwd=self.root, env=env, stdout=output, stderr=output, timeout=5)
                output.seek(0)
                return result.returncode, output.read(4096).decode(errors="replace")
        finally:
            sandbox.close()

    def test_ping_receives_ipv4_and_ipv6_replies_in_full_network_modes(self):
        for mode, proxy in (("direct", False), ("direct", True), ("all", False)):
            cfg = Config(model="test", sandbox_network=mode, proxy_enabled=proxy)
            for command in (["/sbin/ping", "-n", "-c", "1", "-t", "2", "127.0.0.1"],
                            ["/sbin/ping6", "-n", "-c", "1", "::1"]):
                with self.subTest(mode=mode, proxy=proxy, command=command[0]):
                    code, output = self.run_sandbox(command, cfg)
                    self.assertEqual(code, 0, output)
                    self.assertIn("1 packets received", output)
                    self.assertNotIn("Operation not permitted", output)

    def test_ping_stays_blocked_with_network_disabled_or_allowlisted(self):
        for mode in ("none", "allowlist"):
            with self.subTest(mode=mode):
                cfg = Config(model="test", sandbox_network=mode)
                code, _ = self.run_sandbox(["/sbin/ping", "-n", "-c", "1", "-t", "1", "127.0.0.1"], cfg)
                self.assertNotEqual(code, 0)

    def test_udp_unconnected_receive_and_local_dns(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.settimeout(5)
        def echo():
            try:
                data, address = listener.recvfrom(256)
                listener.sendto(data, address)
            except OSError:
                pass
        thread = threading.Thread(target=echo, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        code = ("import socket; assert socket.getaddrinfo('localhost',80); "
                "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(2); "
                f"s.sendto(b'UDP_OK',('127.0.0.1',{listener.getsockname()[1]})); "
                "assert s.recvfrom(256)[0]==b'UDP_OK'; print('UDP_DNS_OK')")
        status, output = self.run_sandbox([sys.executable, "-c", code], Config(model="test", sandbox_network="direct"))
        self.assertEqual(status, 0, output)
        self.assertIn("UDP_DNS_OK", output)

    def test_full_ip_network_does_not_allow_host_unix_socket_connections(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        address = str(self.root / "host.sock")
        listener.bind(address)
        listener.listen()
        code = f"import socket; s=socket.socket(socket.AF_UNIX); s.connect({address!r})"
        status, _ = self.run_sandbox([sys.executable, "-c", code], Config(model="test", sandbox_network="direct"))
        self.assertNotEqual(status, 0)
