"""Executed inside bubblewrap with Python -I -S; only stdlib imports.

The trusted relay can connect to one host Unix socket. The command cannot create
Unix sockets, inspect the relay with ptrace, or bypass socket checks via io_uring.
"""
import ctypes
import errno
import json
import os
import platform
import selectors
import signal
import socket
import sys


def restrict(network, verifier_socketpair=False):
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        arch, socket_nr, forbidden = 0xC000003E, 41, [53, 101, 310, 311, 425, 438]
    elif machine in {"aarch64", "arm64"}:
        arch, socket_nr, forbidden = 0xC00000B7, 198, [199, 117, 270, 271, 425, 438]
    else:
        raise RuntimeError("Linux sandbox supports x86_64 and aarch64 only")
    # classic BPF: seccomp_data.nr at 0, arch at 4, args[0] at 16.
    deny, allow, kill = 0x00050000 | errno.EPERM, 0x7FFF0000, 0x80000000
    code = [(0x20, 0, 0, 4), (0x15, 1, 0, arch), (0x06, 0, 0, kill),
            (0x20, 0, 0, 0), (0x35, 0, 1, 0x40000000), (0x06, 0, 0, deny)]
    if verifier_socketpair:
        # Tokio's signal driver needs an anonymous connected Unix stream pair.
        # Only the trusted verifier gets this exception. socket(AF_UNIX) stays
        # denied, as do datagram socketpairs (which can reconnect to host sockets).
        pair_nr = forbidden.pop(0)
        code += [(0x15, 0, 8, pair_nr), (0x20, 0, 0, 16),
                 (0x15, 1, 0, socket.AF_UNIX), (0x06, 0, 0, deny),
                 (0x20, 0, 0, 24), (0x54, 0, 0, 0xF),
                 (0x15, 0, 1, socket.SOCK_STREAM), (0x06, 0, 0, allow), (0x06, 0, 0, deny)]
    for number in forbidden:
        code += [(0x15, 0, 1, number), (0x06, 0, 0, deny)]
    if network == "none":
        code += [(0x15, 0, 1, socket_nr), (0x06, 0, 0, deny)]
    else:
        code += [(0x15, 0, 3, socket_nr), (0x20, 0, 0, 16),
                 (0x15, 0, 1, socket.AF_UNIX), (0x06, 0, 0, deny)]
    code += [(0x06, 0, 0, allow)]

    class Filter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]

    class Program(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]

    filters = (Filter * len(code))(*(Filter(*item) for item in code))
    program = Program(len(code), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot enable sandbox seccomp filter")


def main():
    options = json.loads(sys.argv[1])
    command = sys.argv[2:]
    if options["network"] != "allowlist":
        restrict(options["network"], options.get("verifier_socketpair", False))
        os.execvpe(command[0], command, os.environ)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(32)
    proxy = f"http://127.0.0.1:{listener.getsockname()[1]}"
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        os.environ[key] = proxy
    # The command must not attach to the trusted bridge to acquire its privileges.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        raise OSError(ctypes.get_errno(), "Cannot protect sandbox relay")
    pid = os.fork()
    if pid == 0:
        listener.close()
        restrict(options["network"], options.get("verifier_socketpair", False))
        os.execvpe(command[0], command, os.environ)
    selector = selectors.DefaultSelector()
    selector.register(listener, selectors.EVENT_READ, None)
    peers = {}
    try:
        while True:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                code = os.waitstatus_to_exitcode(status)
                if code < 0:
                    number = -code
                    print(f"PYCODER_SANDBOX_CHILD_SIGNAL: {signal.Signals(number).name} ({number})", file=sys.stderr)
                    return 128 + number
                return code
            for key, _ in selector.select(0.1):
                if key.fileobj is listener:
                    client, _ = listener.accept()
                    if len(peers) >= 64:
                        client.close()
                        continue
                    upstream = socket.socket(socket.AF_UNIX)
                    upstream.settimeout(2)
                    try:
                        upstream.connect(options["socket"])
                        client.settimeout(2)
                        peers[client], peers[upstream] = upstream, client
                        selector.register(client, selectors.EVENT_READ)
                        selector.register(upstream, selectors.EVENT_READ)
                    except OSError:
                        upstream.close()
                        client.close()
                else:
                    connection = key.fileobj
                    other = peers.get(connection)
                    if other is None:
                        continue
                    try:
                        data = connection.recv(65536)
                        if not data:
                            raise OSError("closed")
                        other.sendall(data)
                    except OSError:
                        for item in (connection, other):
                            selector.unregister(item)
                            peers.pop(item, None)
                            item.close()
    finally:
        selector.close()
        listener.close()
        for item in peers:
            item.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"PYCODER_SANDBOX_SETUP_ERROR: {error}", file=sys.stderr)
        sys.exit(125)
