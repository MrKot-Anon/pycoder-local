from dataclasses import replace
import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pycoder.config import Config
from pycoder.history import History
from pycoder.tools import Tools
from pycoder.sandbox import Sandbox, SandboxUnavailable, supported
from pycoder.network_policy import NetworkProxy, host_port
from tests.support import server, json_response


class SandboxPolicyTests(unittest.TestCase):
    def test_command_modes_and_verifier_routes_are_independent(self):
        from pycoder.settings import network_modes
        cfg = Config(model="test", proxy_enabled=True, sandbox_network="direct",
                     sandbox_direct_hosts=["localhost:8000"])
        cfg.validate()
        self.assertEqual([v for v, _ in network_modes(cfg)], ["none", "allowlist", "all", "direct"])
        command = Sandbox(Path.cwd(), cfg)
        self.assertEqual(command.network, "all")
        self.assertIsNone(command.upstream)
        verifier = Sandbox(Path.cwd(), replace(cfg, sandbox_network="all"), proxy_route=lambda _: cfg)
        self.assertTrue(verifier.allow_all)
        self.assertEqual(verifier.direct_hosts, [])
        with self.assertRaises(ValueError):
            replace(cfg, sandbox_hosts=["LOCALHOST.:8000"]).validate()

    def test_mixed_routes_block_unknown_hosts_and_never_fallback(self):
        cfg = Config(proxy_enabled=True)
        proxy = NetworkProxy(["remote.invalid:443"], upstream=cfg, direct_hosts=["localhost:8000"])
        self.addCleanup(proxy.close)
        with patch("socket.getaddrinfo", side_effect=AssertionError("DNS leak")), patch("pycoder.proxy.connect", side_effect=OSError("offline")) as dial:
            with self.assertRaises(PermissionError):
                proxy.connect("unknown.invalid", 443)
            dial.assert_not_called()
            with self.assertRaises(OSError):
                proxy.connect("remote.invalid", 443)
            dial.assert_called_once_with(cfg, ("remote.invalid", 443), 10)
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8000))]), patch("socket.socket") as sock, patch("pycoder.proxy.connect") as dial:
            proxy.connect("LOCALHOST.", 8000)
            sock.return_value.connect.assert_called_once_with(("127.0.0.1", 8000))
            dial.assert_not_called()

    def test_launcher_uses_base_python_outside_private_venv(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            private = root / "config"
            private.mkdir()
            base = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
            with patch("pycoder.sandbox.sys.executable", str(private / "e2ee-env/bin/python")), \
                    patch("pycoder.sandbox.sys._base_executable", base), \
                    patch("pycoder.sandbox.sys.platform", "linux"), \
                    patch("pycoder.sandbox.shutil.which", return_value="/usr/bin/bwrap"):
                sandbox = Sandbox(root, Config(model="test"), [private])
                try:
                    args, _ = sandbox.prepare(["/bin/echo", "hi"], root)
                    self.assertEqual(args[args.index("--") + 1], base)
                    self.assertIn(str(private), args)
                finally:
                    sandbox.close()

    def test_network_permission_is_explicit_and_persists_only_on_request(self):
        from pycoder.permissions import CommandPolicy
        with tempfile.TemporaryDirectory() as d, patch("pycoder.sandbox.supported", return_value=True):
            root = Path(d)
            history = History(root / "state", root)
            cfg = Config(model="test", approval="auto")
            tools = Tools(root, cfg, history, lambda *a: "deny", lambda _: "", lambda _: None)
            tools.policy = CommandPolicy(root, root / "config")
            try:
                self.assertTrue(tools.request_network_access("pypi.org:443", "install dependency")["denied"])
                self.assertEqual(cfg.sandbox_network, "none")
                tools.approve = lambda *a: "session"
                tools.request_network_access("pypi.org:443", "install dependency")
                self.assertEqual(cfg.sandbox_hosts, ["pypi.org:443"])
                self.assertFalse((root / "config/config.json").exists())
                tools.approve = lambda *a: "remember"
                tools.request_network_access("files.pythonhosted.org:443", "download wheel")
                saved = json.loads((root / "config/config.json").read_text())
                self.assertEqual(saved["sandbox_network"], "allowlist")
                self.assertIn("files.pythonhosted.org:443", saved["sandbox_hosts"])
            finally:
                tools.close()
                history.close()

    @unittest.skipUnless(os.name == "posix", "Unix bridge plan")
    def test_linux_launch_has_namespace_and_seccomp_layers(self):
        with tempfile.TemporaryDirectory() as d, patch("pycoder.sandbox.sys.platform", "linux"), patch("pycoder.sandbox.shutil.which", return_value="/usr/bin/bwrap"):
            root = Path(d)
            for mode in ("none", "allowlist", "all"):
                sandbox = Sandbox(root, Config(model="test", sandbox_network=mode))
                try:
                    args, env = sandbox.prepare(["/bin/true"], root)
                    self.assertIn("--unshare-user", args)
                    self.assertIn("--unshare-pid", args)
                    self.assertIn("--cap-drop", args)
                    self.assertEqual("--unshare-net" in args, mode != "all")
                    self.assertTrue(any("def restrict(network, verifier_socketpair=False)" in arg for arg in args))
                    self.assertNotIn("CHUTES_API_KEY", env)
                    self.assertNotIn("PYCODER_API_KEY", env)
                finally:
                    sandbox.close()

    def test_host_validation(self):
        self.assertEqual(host_port("Example.COM:443"), ("example.com", 443))
        self.assertEqual(host_port("[::1]:8000"), ("::1", 8000))
        for value in ("https://example.com", "*.example.com:443", "example.com", "x:0", "x:99999", "user@host:443", "x:80/path"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                host_port(value)

    def test_proxy_checks_host_and_resolved_addresses(self):
        proxy = NetworkProxy(["example.com:443"])
        try:
            with self.assertRaises(PermissionError):
                proxy.connect("other.com", 443)
            with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]):
                with self.assertRaises(PermissionError):
                    proxy.connect("example.com", 443)
        finally:
            proxy.close()

    def test_escalation_always_asks_even_auto_and_never_remembers(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            history = History(root / "state", root)
            questions = []
            tools = Tools(root, Config(model="test", approval="auto"), history,
                          lambda *a: questions.append(a) or "deny", lambda _: "", lambda _: None)
            try:
                result = tools.exec_command("echo nope", sandbox_permissions="require_escalated", justification="test")
                self.assertTrue(result["denied"])
                self.assertEqual(questions[0][0], "escalation")
                tools.approve = lambda *a: "once"
                with patch.object(tools.processes, "start", return_value={"exit_code": 0}) as start:
                    result = tools.shell([sys.executable, "-c", "print(1)"], sandbox_permissions="require_escalated", justification="test")
                    self.assertIsNone(start.call_args.kwargs["sandbox"])
                    self.assertTrue(result["sandbox"]["user_approved_escalation"])
                with self.assertRaises(ValueError):
                    tools.exec_command("echo x", sandbox_permissions="require_escalated")
            finally:
                tools.close()
                history.close()

    def test_no_fallback_when_backend_missing(self):
        with tempfile.TemporaryDirectory() as d, patch("pycoder.sandbox.sys.platform", "linux"), patch("pycoder.sandbox.shutil.which", return_value=None):
            history = History(Path(d) / "state", Path(d))
            tools = Tools(Path(d), Config(model="test"), history, lambda *a: self.fail("unexpected approval"), lambda _: "", lambda _: None)
            try:
                result = tools.exec_command("touch should-not-exist")
                self.assertEqual(result["sandbox_error"], "unavailable")
                self.assertFalse(result["command_started"])
                self.assertFalse((Path(d) / "should-not-exist").exists())
            finally:
                tools.close()
                history.close()

    def test_windows_keeps_legacy_approval(self):
        with tempfile.TemporaryDirectory() as d, patch("pycoder.tools.default_shell", return_value="cmd.exe"), patch("pycoder.sandbox.supported", return_value=False):
            history = History(Path(d) / "state", Path(d))
            calls = []
            tools = Tools(Path(d), Config(model="test"), history, lambda *a: calls.append(a) or False, lambda _: "", lambda _: None)
            try:
                self.assertTrue(tools.exec_command("echo x")["denied"])
                self.assertEqual(len(calls), 1)
            finally:
                history.close()


@unittest.skipUnless(supported(), "Native sandbox requires macOS/Linux")
class NativeSandboxTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux seccomp policy')
    def test_verifier_socketpairs_cannot_connect_to_host_and_native_runtime_works(self):
        from pycoder import attestation
        self.config.proxy_enabled = True
        self.config.sandbox_network = 'all'
        native = importlib.util.find_spec('dcap_qvl') is not None
        host = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(host.close)
        address = str(self.base / 'host.sock')
        host.bind(address)
        host.listen()
        source = Path(attestation.__file__).read_text()
        code = f"__name__ = 'test_verifier'\nexec({source!r}, globals())\naddress = {address!r}\n"
        code += '''
import socket, errno, ctypes
for create in (lambda: socket.socket(socket.AF_UNIX),
               lambda: socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM),
               lambda: socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)):
    try:
        create()
    except PermissionError:
        pass
    else:
        raise AssertionError('Unexpected Unix socket permission')
left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_NONBLOCK)
left.send(b'x')
assert right.recv(1) == b'x'
libc = ctypes.CDLL(None, use_errno=True)
unspec = ctypes.c_ushort(socket.AF_UNSPEC)
assert libc.connect(left.fileno(), ctypes.byref(unspec), ctypes.sizeof(unspec)) == -1
for closed in (False, True):
    if closed:
        right.close()
    try:
        left.connect(address)
    except OSError as error:
        assert error.errno == errno.EISCONN, error
    else:
        raise AssertionError('Connected stream pair escaped to host')
left.bind('local.sock')
try:
    left.listen()
except OSError:
    pass
else:
    raise AssertionError('Connected stream pair became a listener')
left.close()
'''
        if native:
            code += '''
from dcap_qvl import get_collateral_from_pcs
async def check():
    try:
        await get_collateral_from_pcs(bytes(632))
    except ValueError as error:
        assert 'parse quote' in str(error), error
    else:
        raise AssertionError('Invalid quote accepted')
with asyncio.Runner(loop_factory=PipeEventLoop) as runner:
    runner.run(check())
print('NATIVE_TOKIO_OK')
'''
        code += "print('LOCAL_PAIRS_ONLY')\n"
        sandbox = Sandbox(self.root, self.config, verifier_socketpair=True)
        self.addCleanup(sandbox.close)
        args, env = sandbox.prepare([sys.executable, '-I', '-c', code], self.root)
        result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('LOCAL_PAIRS_ONLY', result.stdout)
        if native:
            self.assertIn('NATIVE_TOKIO_OK', result.stdout)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux child signal forwarding')
    def test_verifier_abort_reports_signal_instead_of_wrapped_exit_250(self):
        self.config.proxy_enabled = True
        self.config.sandbox_network = 'all'
        sandbox = Sandbox(self.root, self.config, verifier_socketpair=True)
        self.addCleanup(sandbox.close)
        code = 'import resource,os; resource.setrlimit(resource.RLIMIT_CORE,(0,0)); os.abort()'
        args, env = sandbox.prepare([sys.executable, '-I', '-c', code], self.root)
        result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 134, result.stderr)
        self.assertIn('SIGABRT (6)', result.stderr)

    def test_attestation_async_runtime_under_socks_isolation(self):
        from pycoder import attestation
        self.config.proxy_enabled = True
        self.config.sandbox_network = "all"
        source = Path(attestation.__file__).read_text()
        code = f"exec({source!r}, globals())\n" + '''
import socket
import errno
async def verify(data):
    assert await asyncio.to_thread(lambda: 42) == 42
    await asyncio.sleep(0.01)
    return {"verified": True}
assert run_verification({}) == {"verified": True}
for create in (socket.socketpair, lambda: socket.socket(socket.AF_UNIX)):
    try:
        create()
    except PermissionError as error:
        assert error.errno == errno.EPERM
    else:
        raise AssertionError("Unix sockets must remain blocked")
print("ASYNC_VERIFIER_OK")
'''
        # Import the standalone verifier without running its stdin entry point.
        code = "__name__ = 'verifier_test'\n" + code
        result = self.run_code(code)
        self.assertEqual(result["exit_code"], 0, result)
        self.assertIn("ASYNC_VERIFIER_OK", result["output"])

    def test_nested_protected_state_paths_stay_hidden(self):
        state = self.root / "private-state"
        sessions = state / "sessions"
        output = state / "tool-output" / "session-1"
        sessions.mkdir(parents=True)
        output.mkdir(parents=True)
        secret = sessions / "secret.txt"
        secret.write_text("private")
        sibling = self.root / "private-state-other"
        sibling.mkdir()
        (sibling / "secret.txt").write_text("private")
        self.tools.sandbox_protected = [state, sessions, output, secret, sibling]
        result = self.run_code(
            "from pathlib import Path; "
            "assert list(Path('private-state').iterdir()) == []; "
            "assert list(Path('private-state-other').iterdir()) == []; "
            "Path('visible.txt').write_text('ok'); print('PROTECTED_STATE_OK')")
        self.assertEqual(result["exit_code"], 0, result)
        self.assertIn("PROTECTED_STATE_OK", result["output"])
        result = self.run_code("from pathlib import Path; Path('private-state/new').write_text('bad')")
        self.assertNotEqual(result["exit_code"], 0, result)
        self.assertFalse((state / "new").exists())
        self.assertEqual(secret.read_text(), "private")

    def test_system_tmp_is_writable_and_persists_between_commands(self):
        import uuid
        name = "pycoder-tmp-test-" + uuid.uuid4().hex
        paths = {Path("/tmp") / name, Path(tempfile.gettempdir()).resolve() / name}
        try:
            code = f"from pathlib import Path; import os; Path('/tmp/{name}').write_text('persistent'); (Path(os.environ['TMPDIR']) / {name!r}).write_text('persistent')"
            result = self.run_code(code)
            self.assertEqual(result["exit_code"], 0, result)
            result = self.run_code(f"from pathlib import Path; import os; print(Path('/tmp/{name}').read_text()); print((Path(os.environ['TMPDIR']) / {name!r}).read_text())")
            self.assertEqual(result["exit_code"], 0, result)
            self.assertEqual(result["output"].count("persistent"), 2)
        finally:
            for path in paths:
                path.unlink(missing_ok=True)

    def test_bundled_sandbox_resources(self):
        from build import build
        bundle = build(self.base / "bundle")
        harness = """import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from pycoder.config import Config
from pycoder.sandbox import Sandbox
import subprocess
s = Sandbox(Path(sys.argv[2]), Config(model='test'))
try:
    args, env = s.prepare(['/bin/sh', '-c', 'echo BUNDLED_SANDBOX_OK'], Path(sys.argv[2]))
    r = subprocess.run(args, env=env, capture_output=True, text=True)
    print(r.stdout, r.stderr)
    sys.exit(r.returncode)
finally:
    s.close()
"""
        result = subprocess.run([sys.executable, "-I", "-S", "-c", harness, str(bundle), str(self.root)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("BUNDLED_SANDBOX_OK", result.stdout)

    def setUp(self):
        # /tmp is explicitly writable. Outside-write checks need a temporary
        # directory outside those allowed roots; remove it in tearDown.
        self.tmp = tempfile.TemporaryDirectory(prefix="pycoder-sandbox-test-", dir=Path.home())
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.config = Config(model="test")
        self.history = History(self.base / "state", self.root)
        self.tools = Tools(self.root, self.config, self.history,
                           lambda *a: self.fail("Default sandbox must not prompt"), lambda _: "", lambda _: None)

    def tearDown(self):
        self.tools.close()
        self.history.close()
        self.tmp.cleanup()

    def run_code(self, code):
        result = self.tools.shell([sys.executable, "-I", "-S", "-c", code])
        while result.get("running"):
            result = self.tools.write_stdin(result["session_id"], yield_time_ms=1000)
        self.assertNotIn("error", result, result)
        return result

    def test_workspace_write_outside_denial_and_protected_credentials(self):
        outside = self.base / "outside"
        outside.write_text("original")
        result = self.run_code("from pathlib import Path; Path('inside').write_text('ok')")
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual((self.root / "inside").read_text(), "ok")
        result = self.run_code(f"from pathlib import Path; Path({str(outside)!r}).write_text('bad')")
        self.assertNotEqual(result["exit_code"], 0, result)
        self.assertEqual(outside.read_text(), "original")
        self.assertFalse(result["sandbox"]["network_denials"])
        secret = self.root / "private"
        secret.mkdir()
        (secret / "token").write_text("secret")
        self.tools.sandbox_protected = [secret]
        result = self.run_code("from pathlib import Path; print(Path('private/token').read_text())")
        self.assertNotEqual(result["exit_code"], 0)
        self.assertNotIn("secret", result["output"])

    def test_symlink_and_child_cannot_escape(self):
        outside = self.base / "outside"
        outside.write_text("original")
        (self.root / "link").symlink_to(outside)
        result = self.run_code("import subprocess,sys; sys.exit(subprocess.call([sys.executable,'-c',\"open('link','w').write('bad')\"]))")
        self.assertNotEqual(result["exit_code"], 0)
        self.assertEqual(outside.read_text(), "original")

    def test_network_modes_and_no_proxy_bypass(self):
        with server(lambda _: json_response({"ok": True})) as (url, requests):
            from urllib.parse import urlsplit
            port = urlsplit(url).port
            connect = f"import socket; s=socket.create_connection(('127.0.0.1',{port}), timeout=2); s.close(); print('CONNECTED')"
            self.assertNotEqual(self.run_code(connect)["exit_code"], 0)
            self.config.sandbox_network = "allowlist"
            self.config.sandbox_localhost = True
            self.config.sandbox_hosts = [f"127.0.0.1:{port}"]
            via_proxy = "import os,http.client,urllib.parse; p=urllib.parse.urlsplit(os.environ['http_proxy']); c=http.client.HTTPConnection(p.hostname,p.port,timeout=3); "
            result = self.run_code(via_proxy + f"c.request('GET','http://127.0.0.1:{port}/v1'); r=c.getresponse(); print(r.status, r.read())")
            self.assertEqual(result["exit_code"], 0, result)
            self.assertIn("200", result["output"])
            self.assertTrue(requests)
            self.assertNotEqual(self.run_code(connect)["exit_code"], 0)
            result = self.run_code(via_proxy + "c.request('CONNECT','not-allowed.invalid:443'); print(c.getresponse().status)")
            self.assertIn("403", result["output"])
            self.assertTrue(result["sandbox"]["network_denials"])
            self.config.sandbox_network = "all"
            result = self.run_code(connect)
            self.assertEqual(result["exit_code"], 0, result)

    def test_direct_list_and_explicit_direct_mode_under_socks(self):
        from urllib.parse import urlsplit
        with server(lambda _: json_response({"ok": True})) as (url, requests):
            port = urlsplit(url).port
            self.config.proxy_enabled = True
            self.config.sandbox_network = "allowlist"
            self.config.sandbox_direct_hosts = [f"127.0.0.1:{port}"]
            self.config.sandbox_hosts = ["remote.invalid:443"]
            raw = f"import socket; socket.create_connection(('127.0.0.1',{port}), timeout=2)"
            relay = "import os,http.client,urllib.parse; p=urllib.parse.urlsplit(os.environ['http_proxy']); c=http.client.HTTPConnection(p.hostname,p.port,timeout=3); "
            request = relay + f"c.request('GET','{url}'); print(c.getresponse().status)"
            with patch("pycoder.proxy.connect", side_effect=OSError("offline")) as dial:
                self.assertNotEqual(self.run_code(raw)["exit_code"], 0)
                self.assertIn("200", self.run_code(request)["output"])
                dial.assert_not_called()
                self.assertIn("403", self.run_code(relay + "c.request('CONNECT','unknown.invalid:443'); print(c.getresponse().status)")["output"])
                self.assertIn("502", self.run_code(relay + "c.request('CONNECT','remote.invalid:443'); print(c.getresponse().status)")["output"])
                self.assertEqual(len(requests), 1)
                self.config.sandbox_network = "all"
                self.assertIn("502", self.run_code(request)["output"])
                self.assertEqual(len(requests), 1)
                self.config.sandbox_network = "none"
                self.assertNotEqual(self.run_code(raw)["exit_code"], 0)
                self.config.sandbox_network = "direct"
                self.assertEqual(self.run_code(raw)["exit_code"], 0)
                outside = self.base / "outside-direct"
                self.assertNotEqual(self.run_code(f"open({str(outside)!r}, 'w').write('escape')")["exit_code"], 0)
                self.assertFalse(outside.exists())

    def test_disabling_sandbox_does_not_force_command_isolation_from_model_proxy(self):
        from urllib.parse import urlsplit
        approvals = []
        self.tools.approve = lambda *args: approvals.append(args) or True
        with server(lambda _: json_response({"ok": True})) as (url, requests):
            self.config.proxy_enabled = True
            self.config.sandbox_enabled = False
            self.config.sandbox_network = "all"
            port = urlsplit(url).port
            direct = f"import socket; socket.create_connection(('127.0.0.1',{port}), timeout=2)"
            self.assertEqual(self.run_code(direct)["exit_code"], 0)
            self.assertEqual(requests, [])
            with patch("pycoder.proxy.socket.create_connection", side_effect=OSError("proxy down")) as dial:
                code = f"import http.client; c=http.client.HTTPConnection('127.0.0.1',{port},timeout=3); "
                result = self.run_code(code + "c.request('GET','/'); print(c.getresponse().status)")
                self.assertIn("200", result["output"])
                dial.assert_not_called()
            self.assertEqual(len(requests), 1)
            self.assertEqual([kind for kind, _ in approvals], ["command", "command"])

    def test_process_session_keeps_proxy_until_termination(self):
        self.config.sandbox_network = "allowlist"
        result = self.tools.shell([sys.executable, "-I", "-S", "-c",
                                   "import time; print('READY', flush=True); time.sleep(10)"])
        self.assertIn("session_id", result, result)
        self.assertIn("READY", result["output"], result)
        process = self.tools.processes.sessions[result["session_id"]]
        sandbox = process.sandbox
        scratch = sandbox.scratch
        self.assertIsNotNone(sandbox.proxy)
        self.tools.write_stdin(result["session_id"], terminate=True)
        self.assertIsNone(sandbox.proxy)
        self.assertFalse(scratch.exists())
