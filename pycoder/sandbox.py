"""OS-enforced command sandbox. Never falls back to unrestricted execution."""

from importlib import resources
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile

from .network_policy import NetworkProxy


class SandboxUnavailable(ValueError):
    pass


def supported():
    return sys.platform == "darwin" or sys.platform.startswith("linux")


class Sandbox:
    def __init__(self, root, config, protected=(), proxy_route=None, *, verifier_socketpair=False):
        self.proxy_route = proxy_route
        self.verifier_socketpair = verifier_socketpair
        self.root = root.resolve()
        self.upstream = config if config.proxy_enabled and config.sandbox_network != "direct" else None
        self.allow_all = bool(self.upstream and config.sandbox_network == "all")
        self.network = "allowlist" if self.allow_all else "all" if config.sandbox_network == "direct" else config.sandbox_network
        self.proxy = None
        self.scratch = None
        self.temp_roots = {Path(p).resolve() for p in ("/tmp", "/var/tmp", tempfile.gettempdir()) if Path(p).is_dir()}
        self.backend = "seatbelt" if sys.platform == "darwin" else "bubblewrap"
        self.protected = {Path(p).expanduser().resolve() for p in protected}
        self.protected.update((Path.home() / p).resolve() for p in (".ssh", ".aws", ".gnupg", ".codex"))
        self.readonly = {self.root / p for p in (".git", ".agents", ".codex")}
        git = self.root / ".git"
        if git.is_file():
            text = git.read_text(errors="replace")[:8192]
            if text.startswith("gitdir:"):
                self.readonly.add((self.root / text[7:].strip()).resolve())
        self.hosts = list(config.sandbox_hosts)
        # Command exceptions do not alter the independently selected verifier routes.
        self.direct_hosts = [] if proxy_route is not None else list(config.sandbox_direct_hosts)
        self.localhost = config.sandbox_localhost
        self.lan = config.sandbox_lan

    def prepare(self, command, cwd):
        if not supported():
            raise SandboxUnavailable("Песочница доступна только на macOS и Linux")
        if sys.platform == "darwin":
            executable = "/usr/bin/sandbox-exec"
            if not Path(executable).is_file():
                raise SandboxUnavailable("Seatbelt недоступен: нет /usr/bin/sandbox-exec")
        else:
            executable = shutil.which("bwrap", path="/usr/bin:/bin:/usr/local/bin")
            if not executable or Path(executable).resolve().is_relative_to(self.root):
                raise SandboxUnavailable("Нужен системный bubblewrap (Ubuntu: sudo apt install bubblewrap). Команда не запускалась.")
            if platform.machine().lower() not in {"x86_64", "amd64", "aarch64", "arm64"}:
                raise SandboxUnavailable("Linux sandbox поддерживает только x86_64 и aarch64")
        self.scratch = Path(tempfile.mkdtemp(prefix="pycoder-sandbox-")).resolve()
        (self.scratch / "home").mkdir()
        # The project and system temp directories are writable. Linux masks the
        # relay directory read-only after writable mounts are applied.
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "LANG", "TERM", "COLORTERM", "TZ"} or key.startswith("LC_")}
        temporary = str(Path(tempfile.gettempdir()).resolve())
        env.update(HOME=str(self.scratch / "home"), TMPDIR=temporary, TMP=temporary, TEMP=temporary,
                   PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
                   NO_PROXY="", no_proxy="")
        try:
            if self.network == "allowlist":
                if self.backend == "seatbelt":
                    self.proxy = NetworkProxy(self.hosts, self.localhost, self.lan, upstream=self.upstream, allow_all=self.allow_all, route=self.proxy_route, direct_hosts=self.direct_hosts)
                    proxy = f"http://127.0.0.1:{self.proxy.server.server_address[1]}"
                    env.update({key: proxy for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy")})
                else:
                    (self.scratch / "relay").mkdir()
                    self.proxy = NetworkProxy(self.hosts, self.localhost, self.lan, self.scratch / "relay/p.sock", upstream=self.upstream, allow_all=self.allow_all, route=self.proxy_route, direct_hosts=self.direct_hosts)
            if self.backend == "seatbelt":
                args = [executable, "-p", self.mac_profile(), *command]
            else:
                args = self.linux_args(executable, command, cwd)
            return args, env
        except BaseException:
            self.close()
            raise

    def mac_profile(self):
        literal = lambda p: json.dumps(str(p), ensure_ascii=False)
        profile = resources.files("pycoder").joinpath("instructions/seatbelt.sbpl").read_text()
        profile += "\n(allow file-read*)\n"
        for path in {self.root, self.scratch, *self.temp_roots}:
            profile += f"(allow file-write* (subpath {literal(path)}))\n"
        for path in self.readonly:
            profile += f"(deny file-write* (subpath {literal(path)}))\n"
        for path in self.protected:
            profile += f"(deny file-read* file-write* (subpath {literal(path)}))\n"
        if self.network != "none":
            profile += resources.files("pycoder").joinpath("instructions/seatbelt_network.sbpl").read_text()
        if self.network == "all":
            # No Unix sockets: a host Docker/agent socket could defeat filesystem isolation.
            # recvmsg on an unconnected ICMP/UDP socket is checked against its
            # local endpoint. A remote-only inbound rule blocks ping replies.
            profile += '(allow network-outbound (remote ip))\n(allow network-inbound (local ip))\n(allow network-bind (local ip))\n'
            profile += '(allow mach-lookup (global-name "com.apple.mDNSResponder"))\n(allow system-socket (socket-domain AF_SYSTEM) (socket-domain AF_ROUTE))\n'
        elif self.proxy:
            port = self.proxy.server.server_address[1]
            profile += f'(allow network-outbound (remote tcp "localhost:{port}"))\n'
        return profile

    def linux_args(self, executable, command, cwd):
        args = [executable, "--die-with-parent", "--new-session", "--unshare-user", "--unshare-pid",
                "--unshare-ipc", "--unshare-uts", "--cap-drop", "ALL", "--ro-bind", "/", "/",
                "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/run"]
        for path in sorted({self.root, self.scratch, *self.temp_roots}, key=lambda p: (len(p.parts), str(p))):
            args += ["--bind", str(path), str(path)]
        if self.network != "all":
            args += ["--unshare-net"]
        else:
            # /etc/resolv.conf may point into /run, which is deliberately masked.
            for name in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf"):
                source = Path(name)
                if source.is_file():
                    target = source.resolve()
                    args += ["--ro-bind", str(target), str(target)]
        for path in sorted(self.readonly, key=lambda p: len(p.parts)):
            if path.exists():
                args += ["--ro-bind", str(path.resolve()), str(path.resolve())]
        masked_dirs = set()
        for path in sorted(self.protected, key=lambda p: (len(p.parts), str(p))):
            # A masked ancestor already hides this path. Mounting a child
            # would require recreating it inside the empty read-only tmpfs.
            if any(parent in masked_dirs for parent in path.parents):
                continue
            if path.is_dir():
                args += ["--tmpfs", str(path), "--remount-ro", str(path)]
                masked_dirs.add(path)
            elif path.exists():
                args += ["--ro-bind", "/dev/null", str(path)]
        if self.proxy:
            args += ["--ro-bind", str(self.scratch / "relay"), str(self.scratch / "relay")]
        runner = resources.files("pycoder").joinpath("linux_sandbox_runner.py").read_text()
        options = json.dumps({"network": self.network, "socket": str(self.scratch / "relay/p.sock"),
                              "verifier_socketpair": self.verifier_socketpair})
        # The application may run from e2ee-env inside the masked config dir.
        # This stdlib-only launcher needs the base Python, never that private venv.
        python = Path(getattr(sys, "_base_executable", None) or sys.executable).resolve()
        if any(python == path or path in python.parents for path in self.protected):
            raise SandboxUnavailable("Базовый Python находится в скрытом каталоге; установите Python вне каталога настроек. Команда не запускалась.")
        args += ["--chdir", str(cwd), "--", str(python), "-I", "-S", "-c", runner, options, *command]
        return args

    def details(self):
        return {"backend": self.backend, "network": self.network,
                "network_denials": list(self.proxy.denied) if self.proxy else [],
                "note": "No automatic retry outside sandbox. Nonzero exit alone is not proof of a sandbox violation."}

    def close(self):
        if self.proxy:
            self.proxy.close()
            self.proxy = None
        if self.scratch:
            shutil.rmtree(self.scratch, ignore_errors=True)
            self.scratch = None
