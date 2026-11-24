import socket
import struct
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pycoder.config import Config
from pycoder.proxy import connect, validate, label
from pycoder.sandbox import Sandbox
from pycoder.transport import Client


class Socket:
    def __init__(self, response):
        self.response = bytearray(response)
        self.sent = bytearray()
        self.closed = False

    def recv(self, size):
        # Exercise partial SOCKS replies.
        data = self.response[:1]
        del self.response[:1]
        return bytes(data)

    def sendall(self, data):
        self.sent.extend(data)

    def close(self):
        self.closed = True


class ProxyTests(unittest.TestCase):
    def config(self, **values):
        return Config(model='test', proxy_enabled=True, **values)

    def test_socks5_auth_remote_dns_no_local_resolution(self):
        sock = Socket(b'\x05\x02\x01\0\x05\0\0\x01' + bytes(6))
        cfg = self.config(proxy_username='user', proxy_password='secret')
        with patch('socket.create_connection', return_value=sock) as dial, patch('socket.getaddrinfo', side_effect=AssertionError('DNS leak')):
            self.assertIs(connect(cfg, ('example.com', 443), 3), sock)
        dial.assert_called_once_with(('127.0.0.1', 1080), 3, None)
        self.assertEqual(sock.sent, b'\x05\x01\x02\x01\x04user\x06secret\x05\x01\0\x03\x0bexample.com\x01\xbb')

    def test_socks4a_userid(self):
        sock = Socket(b'\0\x5a' + bytes(6))
        with patch('socket.create_connection', return_value=sock), patch('socket.getaddrinfo', side_effect=AssertionError('DNS leak')):
            connect(self.config(proxy_version=4, proxy_username='user'), ('example.com', 80))
        self.assertEqual(sock.sent, b'\x04\x01\0\x50\0\0\0\x01user\0example.com\0')

    def test_local_dns_and_no_auth_both_versions(self):
        for version in (4, 5):
            response = b'\0\x5a' + bytes(6) if version == 4 else b'\x05\0\x05\0\0\x01' + bytes(6)
            sock = Socket(response)
            with patch('socket.create_connection', return_value=sock) as dial, patch('socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.2.3.4', 80))]) as dns:
                connect(self.config(proxy_version=version, proxy_remote_dns=False), ('example.com', 80))
            dns.assert_called_once()
            self.assertIn(b'\x01\x02\x03\x04', sock.sent)
            self.assertEqual(dial.call_count, 1)

    def test_failure_never_dials_destination(self):
        for reply in (b'', b'\x05\xff', b'\x05\x00'):
            sock = Socket(reply)
            with patch('socket.create_connection', return_value=sock) as dial:
                with self.assertRaises(OSError):
                    connect(self.config(proxy_username='u', proxy_password='p'), ('example.com', 443))
            self.assertTrue(sock.closed)
            self.assertEqual(dial.call_count, 1)
            self.assertEqual(dial.call_args.args[0], ('127.0.0.1', 1080))

    def test_proxy_unavailable_no_fallback(self):
        with patch('socket.create_connection', side_effect=OSError('offline')) as dial:
            with self.assertRaises(OSError):
                connect(self.config(), ('example.com', 443))
        self.assertEqual(dial.call_count, 1)

    def test_invalid_proxy_rejected_before_dial(self):
        for cfg in (self.config(proxy_host='proxy.example'), self.config(proxy_version=4, proxy_password='p'), self.config(proxy_port=0)):
            with patch('socket.create_connection') as dial, self.assertRaises(ValueError):
                connect(cfg, ('example.com', 80))
            dial.assert_not_called()

    def test_http_tls_connections_use_socks_socket_factory(self):
        import http.client
        cfg = self.config()
        for cls in (http.client.HTTPConnection, http.client.HTTPSConnection):
            with patch('pycoder.proxy.connect') as dial:
                conn = Client(cfg, '')._proxy_connection(cls('example.com'))
                conn._create_connection(('example.com', 443), 2)
            dial.assert_called_once_with(cfg, ('example.com', 443), 2, None)

    def test_commands_cannot_use_unrestricted_network(self):
        sandbox = Sandbox(Path.cwd(), self.config(sandbox_network='all', sandbox_enabled=False))
        self.assertEqual(sandbox.network, 'allowlist')
        self.assertTrue(sandbox.allow_all)
        self.assertIsNotNone(sandbox.upstream)

    def test_indicator_has_no_credentials(self):
        value = label(self.config(proxy_username='private-user', proxy_password='private-pass'))
        self.assertIn('ЧЕРЕЗ ПРОКСИ', value)
        self.assertNotIn('private', value)
        self.assertEqual(label(Config()), '')

    def test_proxy_settings_survive_other_settings_saved_before_restart(self):
        import tempfile
        from dataclasses import asdict
        from pycoder.config import load
        from pycoder.proxy import save
        from pycoder.util import private_json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save(self.config(proxy_password='secret', proxy_username='u'), root)
            private_json(root / 'config.json', asdict(Config(model='test')))
            cfg = load(root)
            self.assertTrue(cfg.proxy_enabled)
            self.assertEqual(cfg.proxy_password, 'secret')
            self.assertEqual((root / 'proxy.json').stat().st_mode & 0o777, 0o600)

    def test_escalation_and_unsupported_platform_are_blocked(self):
        import tempfile
        from pycoder.history import History
        from pycoder.tools import Tools
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = History(root / 'state', root)
            tools = Tools(root, self.config(sandbox_enabled=True), history, lambda *a: True, lambda _: '', lambda _: None)
            try:
                with patch.object(tools.processes, 'start') as start:
                    result = tools.exec_command('echo test', sandbox_permissions='require_escalated', justification='test')
                    self.assertFalse(result['command_started'])
                    with patch('pycoder.sandbox.supported', return_value=False):
                        result = tools.exec_command('echo test')
                    self.assertFalse(result['command_started'])
                    start.assert_not_called()
            finally:
                tools.close()
                history.close()

    def test_real_socks_server_carries_catalog_without_destination_dns(self):
        import json
        import socketserver
        import threading
        from pycoder.proxy import read
        seen = []

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                sock = self.request
                sock.settimeout(3)
                seen.append(read(sock, 3))
                sock.sendall(b'\x05\0')
                seen.append(read(sock, 4))
                seen.append(read(sock, read(sock, 1)[0]))
                seen.append(read(sock, 2))
                sock.sendall(b'\x05\0\0\x01' + bytes(6))
                headers = bytearray()
                while not headers.endswith(b'\r\n\r\n'):
                    headers.extend(read(sock, 1))
                seen.append(bytes(headers))
                body = json.dumps({'data': [{'id': 'test'}]}).encode()
                sock.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ' + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)

        with socketserver.TCPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.handle_request, daemon=True)
            thread.start()
            cfg = self.config(proxy_port=server.server_address[1], base_url='http://unresolvable.invalid/v1', retries=0)
            original = socket.getaddrinfo
            def dns(host, *args, **kwargs):
                self.assertEqual(host, '127.0.0.1', 'destination DNS leaked')
                return original(host, *args, **kwargs)
            with patch('socket.getaddrinfo', side_effect=dns):
                self.assertEqual(Client(cfg, '').list_models()[0]['id'], 'test')
            thread.join(4)
            self.assertFalse(thread.is_alive())
        self.assertEqual(seen[2], b'unresolvable.invalid')
        self.assertIn(b'GET /v1/models HTTP/1.1', seen[4])

    def test_independent_service_routes_and_unknown_host_stay_proxied(self):
        from pycoder.proxy import for_service, attestation_route
        cfg = self.config(proxy_intel=False)
        self.assertTrue(for_service(cfg, 'chutes').proxy_enabled)
        self.assertTrue(attestation_route(cfg, 'nras.attestation.nvidia.com').proxy_enabled)
        self.assertFalse(attestation_route(cfg, 'api.trustedservices.intel.com').proxy_enabled)
        self.assertFalse(attestation_route(cfg, 'certificates.trustedservices.intel.com').proxy_enabled)
        self.assertTrue(attestation_route(cfg, 'intel.com.evil.invalid').proxy_enabled)
        cfg.proxy_chutes = False
        self.assertFalse(for_service(cfg, 'chutes').proxy_enabled)
        self.assertTrue(cfg.proxy_enabled)
        cfg.proxy_enabled = False
        self.assertFalse(for_service(cfg, 'nvidia').proxy_enabled)

    def test_old_proxy_file_defaults_all_routes_to_proxy(self):
        import tempfile
        from pycoder.proxy import FIELDS
        from pycoder.config import load
        from pycoder.util import private_json
        with tempfile.TemporaryDirectory() as directory:
            cfg = self.config()
            legacy = {k: getattr(cfg, k) for k in FIELDS if k not in {'proxy_chutes', 'proxy_intel', 'proxy_nvidia'}}
            private_json(Path(directory) / 'proxy.json', legacy)
            loaded = load(Path(directory))
            self.assertTrue(loaded.proxy_chutes and loaded.proxy_intel and loaded.proxy_nvidia)


class ChainedProxyTests(unittest.TestCase):
    def config(self, **values):
        return Config(model='test', proxy_enabled=True, proxy_chain_enabled=True,
                      proxy_host='127.0.0.1', proxy_port=1080,
                      proxy_second_host='198.51.100.7', proxy_second_port=2080, **values)

    @staticmethod
    def reply(version, auth=False):
        if version == 4:
            return b'\0\x5a' + bytes(6)
        return (b'\x05\x02\x01\0' if auth else b'\x05\0') + b'\x05\0\0\x01' + bytes(6)

    def test_all_version_and_auth_combinations_use_one_physical_socket(self):
        for first in (4, 5):
            for second in (4, 5):
                for first_auth in (False, True):
                    for second_auth in (False, True):
                        with self.subTest(first=first, second=second, first_auth=first_auth, second_auth=second_auth):
                            cfg = self.config(proxy_version=first, proxy_second_version=second,
                                proxy_username='first-user' if first_auth else '',
                                proxy_password='first-pass' if first_auth and first == 5 else '',
                                proxy_second_username='second-user' if second_auth else '',
                                proxy_second_password='second-pass' if second_auth and second == 5 else '')
                            sock = Socket(self.reply(first, first_auth) + self.reply(second, second_auth))
                            with patch('socket.create_connection', return_value=sock) as dial, patch('socket.getaddrinfo', side_effect=AssertionError('DNS leaked')):
                                self.assertIs(connect(cfg, ('destination.invalid', 443), 3), sock)
                            dial.assert_called_once_with(('127.0.0.1', 1080), 3, None)
                            self.assertFalse(sock.closed)
                            self.assertEqual(sock.response, b'')
                            self.assertLess(sock.sent.index(socket.inet_aton('198.51.100.7')), sock.sent.index(b'destination.invalid'))
                            for value in (cfg.proxy_username, cfg.proxy_password, cfg.proxy_second_username, cfg.proxy_second_password):
                                if value:
                                    self.assertIn(value.encode(), sock.sent)

    def test_either_hop_failure_closes_tunnel_without_shortcut(self):
        for response, stage in ((b'\x05\xff', 1),
                                (self.reply(5) + b'\x05\xff', 2),
                                (self.reply(5), 2),
                                (self.reply(5) + b'\x05\x02\x01\x01', 2)):
            cfg = self.config(proxy_second_username='user', proxy_second_password='secret')
            sock = Socket(response)
            with patch('socket.create_connection', return_value=sock) as dial, self.assertRaisesRegex(OSError, f'звено {stage}'):
                connect(cfg, ('destination.invalid', 443))
            self.assertTrue(sock.closed)
            self.assertEqual(dial.call_count, 1)
            self.assertEqual(dial.call_args.args[0], ('127.0.0.1', 1080))
            self.assertNotIn(b'destination.invalid', sock.sent)

    def test_local_dns_is_used_only_for_final_destination(self):
        cfg = self.config(proxy_remote_dns=False)
        sock = Socket(self.reply(5) + self.reply(5))
        with patch('socket.create_connection', return_value=sock) as dial, patch('socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('203.0.113.9', 443))]) as dns:
            connect(cfg, ('destination.invalid', 443))
        dns.assert_called_once_with('destination.invalid', 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
        self.assertIn(socket.inet_aton('203.0.113.9'), sock.sent)
        self.assertNotIn(b'destination.invalid', sock.sent)
        self.assertEqual(dial.call_count, 1)

    def test_disabled_chain_keeps_single_proxy_and_explicit_direct_skips_both(self):
        cfg = self.config()
        cfg.proxy_chain_enabled = False
        sock = Socket(self.reply(5))
        with patch('socket.create_connection', return_value=sock) as dial:
            connect(cfg, ('destination.invalid', 443))
        self.assertNotIn(socket.inet_aton(cfg.proxy_second_host), sock.sent)
        self.assertEqual(dial.call_count, 1)
        from pycoder.proxy import for_service
        cfg.proxy_chain_enabled = True
        cfg.proxy_intel = False
        with patch('socket.create_connection') as dial:
            connect(for_service(cfg, 'intel'), ('intel.com', 443), 3)
        dial.assert_called_once_with(('intel.com', 443), 3, None)

    def test_invalid_second_hop_is_rejected_before_connecting(self):
        for changes in ({'proxy_second_port': 0}, {'proxy_second_version': 6},
                        {'proxy_second_host': 'proxy.invalid'},
                        {'proxy_second_version': 4, 'proxy_second_password': 'secret'},
                        {'proxy_version': 4, 'proxy_second_host': '::1'}):
            cfg = self.config()
            for key, value in changes.items():
                setattr(cfg, key, value)
            with patch('socket.create_connection') as dial, self.assertRaises(ValueError):
                connect(cfg, ('destination.invalid', 443))
            dial.assert_not_called()

    def test_saved_chain_and_old_single_hop_configs(self):
        import tempfile
        from pycoder.config import load
        from pycoder.proxy import save, LEGACY_FIELDS
        from pycoder.util import private_json
        cfg = self.config(proxy_second_username='user', proxy_second_password='secret')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save(cfg, root)
            loaded = load(root)
            self.assertTrue(loaded.proxy_chain_enabled)
            self.assertEqual(loaded.proxy_second_password, 'secret')
            private_json(root / 'proxy.json', {key: getattr(cfg, key) for key in LEGACY_FIELDS})
            loaded = load(root)
            self.assertFalse(loaded.proxy_chain_enabled)
            self.assertTrue(loaded.proxy_chutes and loaded.proxy_intel and loaded.proxy_nvidia)

    def test_chain_indicator_has_order_and_no_credentials(self):
        cfg = self.config(proxy_username='private-first', proxy_password='private-first-password',
                          proxy_second_username='private-second', proxy_second_password='private-second-password')
        text = label(cfg)
        self.assertIn('SOCKS5 127.0.0.1:1080 → SOCKS5 198.51.100.7:2080', text)
        self.assertNotIn('private', text)
        self.assertNotIn('private-second-password', repr(cfg))

    def test_real_two_hop_tunnel_with_separate_auth_and_remote_dns(self):
        import json
        import select
        import socketserver
        import threading
        from pycoder.proxy import read
        observed, errors = [], []
        case = self

        def handshake(sock, user, password):
            case.assertEqual(read(sock, 3), b'\x05\x01\x02')
            sock.sendall(b'\x05\x02')
            case.assertEqual(read(sock, 1), b'\x01')
            case.assertEqual(read(sock, read(sock, 1)[0]), user)
            case.assertEqual(read(sock, read(sock, 1)[0]), password)
            sock.sendall(b'\x01\0')
            prefix = read(sock, 4)
            case.assertEqual(prefix[:3], b'\x05\x01\0')
            host = read(sock, 4) if prefix[3] == 1 else read(sock, read(sock, 1)[0])
            return prefix[3], host, struct.unpack('!H', read(sock, 2))[0]

        class Second(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    self.request.settimeout(3)
                    target = handshake(self.request, b'two', b'two-pass')
                    case.assertEqual(target, (3, b'unresolvable.invalid', 80))
                    observed.append(('second', target))
                    self.request.sendall(b'\x05\0\0\x01' + bytes(6))
                    headers = bytearray()
                    while not headers.endswith(b'\r\n\r\n'):
                        headers.extend(read(self.request, 1))
                    case.assertIn(b'GET /v1/models HTTP/1.1', headers)
                    body = json.dumps({'data': [{'id': 'through-two-proxies'}]}).encode()
                    self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ' + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
                except Exception as error:
                    errors.append(error)

        class First(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    self.request.settimeout(3)
                    target = handshake(self.request, b'one', b'one-pass')
                    case.assertEqual(target, (1, socket.inet_aton('127.0.0.1'), second.server_address[1]))
                    observed.append(('first', target))
                    # The first proxy, not the application, dials the second.
                    with socket.socket() as remote:
                        remote.settimeout(3)
                        remote.connect(second.server_address)
                        self.request.sendall(b'\x05\0\0\x01' + bytes(6))
                        while True:
                            ready, _, _ = select.select([self.request, remote], [], [], 3)
                            if not ready:
                                raise TimeoutError('test tunnel stalled')
                            for source in ready:
                                data = source.recv(65536)
                                if not data:
                                    return
                                (remote if source is self.request else self.request).sendall(data)
                except Exception as error:
                    errors.append(error)

        with socketserver.TCPServer(('127.0.0.1', 0), Second) as second, socketserver.TCPServer(('127.0.0.1', 0), First) as first:
            threads = [threading.Thread(target=s.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True) for s in (first, second)]
            for thread in threads:
                thread.start()
            cfg = self.config(proxy_username='one', proxy_password='one-pass',
                              proxy_second_username='two', proxy_second_password='two-pass',
                              base_url='http://unresolvable.invalid/v1', retries=0, timeout_seconds=3)
            cfg.proxy_port = first.server_address[1]
            cfg.proxy_second_host, cfg.proxy_second_port = second.server_address
            original_dial = socket.create_connection
            def dial(address, *args):
                case.assertEqual(address, first.server_address, 'application bypassed first hop')
                return original_dial(address, *args)
            original_dns = socket.getaddrinfo
            def dns(host, *args, **kwargs):
                case.assertEqual(host, '127.0.0.1', 'destination DNS leaked')
                return original_dns(host, *args, **kwargs)
            try:
                with patch('socket.create_connection', side_effect=dial) as calls, patch('socket.getaddrinfo', side_effect=dns):
                    models = Client(cfg, '').list_models()
                self.assertEqual(models[0]['id'], 'through-two-proxies')
                self.assertEqual(calls.call_count, 1)
            finally:
                first.shutdown()
                second.shutdown()
                for thread in threads:
                    thread.join(4)
            self.assertFalse(errors, errors)
            self.assertEqual([item[0] for item in observed], ['first', 'second'])
