"""Exact-host HTTP CONNECT proxy. Direct egress must be blocked by the OS."""

import ipaddress
import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit


def host_port(value):
    if not isinstance(value, str) or any(c.isspace() for c in value) or any(c in value for c in "/@*?#\\"):
        raise ValueError("Укажите точный host:port, например github.com:443 (без URL и wildcard)")
    parsed = urlsplit("//" + value)
    if not parsed.hostname or parsed.port is None or not 1 <= parsed.port <= 65535:
        raise ValueError("Нужны имя хоста и порт 1..65535")
    host = parsed.hostname.rstrip(".").lower().encode("idna").decode("ascii")
    if not host or len(host) > 253 or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-:" for c in host):
        raise ValueError("Некорректный хост")
    return host, parsed.port


class NetworkProxy:
    def __init__(self, hosts, localhost=False, lan=False, unix_path=None, upstream=None, allow_all=False, route=None, direct_hosts=()):
        self.upstream, self.allow_all = upstream, allow_all
        self.route = route
        self.allowed = {host_port(h) for h in hosts}
        self.direct_allowed = {host_port(h) for h in direct_hosts}
        if self.allowed & self.direct_allowed:
            raise ValueError("Host appears in both proxy and direct lists")
        self.localhost, self.lan = localhost, lan
        self.denied = []
        self.connections = set()
        self.lock = threading.Lock()
        self.closed = False
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                owner.handle(self.request)

        server_type = socketserver.ThreadingUnixStreamServer if unix_path else socketserver.ThreadingTCPServer
        self.server = server_type(str(unix_path) if unix_path else ("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()

    def connect(self, host, port):
        host = host.rstrip('.').lower().encode('idna').decode('ascii')
        direct = not self.allow_all and (host, port) in self.direct_allowed
        upstream = self.route(host) if self.route else self.upstream
        if direct:
            upstream = None
        if not self.allow_all and not direct and (host, port) not in self.allowed:
            raise PermissionError(f"Хост не разрешён: {host}:{port}")
        if upstream and upstream.proxy_enabled and upstream.proxy_remote_dns:
            from .proxy import connect
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                # The SOCKS server resolves this name. Its DNS/address policy is
                # authoritative; resolving locally would leak the destination.
                ip = None
            if ip is not None and not self.allow_all:
                self.check_address(ip, host, port)
            return connect(upstream, (host, port), 10)
        # Resolve once and connect to the checked numeric address, avoiding DNS rebinding.
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        for family, kind, proto, _, address in addresses:
            ip = ipaddress.ip_address(address[0].split("%")[0])
            if not self.allow_all and not direct:
                self.check_address(ip, host, port)
            elif direct and (ip.is_multicast or ip.is_unspecified):
                raise PermissionError(f"Недопустимый адрес прямого подключения: {host}:{port}")
        last = None
        for family, kind, proto, _, address in addresses:
            if upstream and upstream.proxy_enabled:
                from .proxy import connect
                try:
                    return connect(upstream, (address[0], port), 10)
                except OSError as error:
                    last = error
                    continue
            connection = socket.socket(family, kind, proto)
            connection.settimeout(10)
            try:
                connection.connect(address)
                return connection
            except OSError as error:
                last = error
                connection.close()
        raise OSError(str(last or "DNS returned no addresses"))

    def check_address(self, ip, host, port):
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if ip.is_loopback:
            allowed = self.localhost
        elif ip.is_private and not ip.is_link_local and not ip.is_unspecified and not ip.is_reserved:
            allowed = self.lan
        else:
            allowed = ip.is_global and not ip.is_multicast
        if not allowed:
            raise PermissionError(f"Адрес {host}:{port} относится к запрещённой локальной/служебной сети")

    def handle(self, client):
        remote = None
        with self.lock:
            if self.closed:
                return
            self.connections.add(client)
        try:
            client.settimeout(10)
            # Read exactly through headers; don't consume TLS bytes after CONNECT.
            header = bytearray()
            while not header.endswith(b"\r\n\r\n"):
                if len(header) >= 32768:
                    raise ValueError("Proxy header too large")
                byte = client.recv(1)
                if not byte:
                    return
                header.extend(byte)
            lines = header.decode("iso-8859-1").split("\r\n")
            method, target, version = lines[0].split(" ")
            if version not in {"HTTP/1.0", "HTTP/1.1"}:
                raise ValueError("Unsupported proxy protocol")
            if method == "CONNECT":
                host, port = host_port(target)
            else:
                parsed = urlsplit(target)
                if method not in {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"} or parsed.scheme != "http" or parsed.username or parsed.password or parsed.fragment:
                    raise ValueError("Use HTTP or HTTPS CONNECT")
                host, port = host_port(f"{('[' + parsed.hostname + ']') if ':' in (parsed.hostname or '') else parsed.hostname}:{parsed.port or 80}")
            remote = self.connect(host, port)
            with self.lock:
                if self.closed:
                    return
                self.connections.add(remote)
            if method == "CONNECT":
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                # One origin per connection. Prevent a conflicting Host header from
                # routing the request to a different virtual host.
                headers = []
                for line in lines[1:]:
                    if not line:
                        continue
                    key, value = line.split(":", 1)
                    if key.lower() not in {"host", "connection", "proxy-connection", "proxy-authorization"}:
                        headers.append(line)
                authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                lines = [f"{method} {path} HTTP/1.1", f"Host: {authority}", "Connection: close", *headers, "", ""]
                remote.sendall("\r\n".join(lines).encode("iso-8859-1"))
            while not self.closed:
                ready, _, _ = select.select([client, remote], [], [], 0.5)
                for connection in ready:
                    data = connection.recv(65536)
                    if not data:
                        return
                    (remote if connection is client else client).sendall(data)
        except PermissionError as error:
            with self.lock:
                self.denied.append(str(error))
                self.denied[:] = self.denied[-20:]
            try:
                client.sendall(b"HTTP/1.1 403 Sandbox network policy denied\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
        except (OSError, ValueError, UnicodeError):
            try:
                client.sendall(b"HTTP/1.1 502 Proxy connection failed\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
        finally:
            with self.lock:
                self.connections.discard(client)
                self.connections.discard(remote)
            if remote:
                remote.close()

    def close(self):
        with self.lock:
            self.closed = True
            for connection in self.connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
