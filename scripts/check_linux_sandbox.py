#!/usr/bin/env python3
"""Standalone, stdlib-only Bubblewrap diagnostics. Run as your normal user."""

import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile


def run(label, argv, env=None):
    print("\n--- " + label + " ---", flush=True)
    try:
        result = subprocess.run(argv, capture_output=True, text=True,
                                errors="replace", timeout=20, env=env)
    except subprocess.TimeoutExpired:
        print("TIMEOUT (20 seconds)")
        return None
    except OSError as error:
        print("ERROR:", error)
        return None
    print("exit_code:", result.returncode)
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print("stderr:", result.stderr.rstrip())
    return result.returncode


def show_file(label, path):
    try:
        print(label + ":", Path(path).read_text().strip())
    except OSError as error:
        print(label + ": unavailable (" + str(error) + ")")


def main():
    print("PYCODER BUBBLEWRAP DIAGNOSTICS v1")
    print("No system settings are changed. No external network requests.")
    print("Python:", platform.python_version())
    print("OS:", platform.system(), platform.release(), platform.machine())
    if not sys.platform.startswith("linux"):
        print("This diagnostic requires Linux.")
        return 2
    print("uid:", os.getuid(), "euid:", os.geteuid())
    if os.geteuid() == 0:
        print("WARNING: running as root; repeat as the user who runs PyCoder.")
    show_file("Distribution", "/etc/os-release")
    show_file("AppArmor process profile", "/proc/self/attr/current")
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith(("NoNewPrivs:", "Seccomp:", "Seccomp_filters:",
                                "CapEff:", "CapBnd:")):
                print(line)
    except OSError as error:
        print("Process status unavailable:", error)
    for name in ("kernel/unprivileged_userns_clone",
                 "kernel/apparmor_restrict_unprivileged_userns",
                 "user/max_user_namespaces", "user/max_net_namespaces"):
        show_file(name.replace("/", "."), "/proc/sys/" + name)
    virtual = shutil.which("systemd-detect-virt")
    if virtual:
        run("Virtualization (exit 1 can mean none)", [virtual])
    bwrap = shutil.which("bwrap", path="/usr/bin:/bin:/usr/local/bin")
    sed = shutil.which("sed", path="/usr/bin:/bin")
    print("\nSystem bwrap:", bwrap)
    print("System sed:", sed)
    if not bwrap or not sed:
        print("Missing required executable. Install it and repeat.")
        return 2
    stat = Path(bwrap).stat()
    print("bwrap mode:", oct(stat.st_mode & 0o7777), "owner uid:", stat.st_uid)
    run("Bubblewrap version", [bwrap, "--version"])
    getcap = shutil.which("getcap")
    if getcap:
        run("Bubblewrap file capabilities (empty output means none)", [getcap, bwrap])
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "LANG": "C", "LC_ALL": "C",
           "PYTHONDONTWRITEBYTECODE": "1"}
    results = {}
    with tempfile.TemporaryDirectory(prefix="pycoder-bwrap-check-") as directory:
        root = Path(directory)
        sample = root / "sample.txt"
        sample.write_text("SANDBOX_READ_OK\nsecond line\n", encoding="utf-8")
        command = [sed, "-n", "1,2p", str(sample)]
        results["direct_read"] = run("Direct sed", command, env)
        base = [bwrap, "--die-with-parent", "--new-session", "--unshare-user"]
        results["user_namespace"] = run(
            "User namespace, no network isolation",
            [*base, "--ro-bind", "/", "/", "--", *command], env)
        results["network_namespace"] = run(
            "User + network namespace",
            [*base, "--unshare-net", "--ro-bind", "/", "/", "--", *command], env)
        # Matches PyCoder's namespace/capability setup. Only the disposable
        # work directory is writable here; the host /tmp remains read-only.
        full = [*base, "--unshare-pid", "--unshare-ipc", "--unshare-uts",
                "--cap-drop", "ALL", "--ro-bind", "/", "/",
                "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/run",
                "--unshare-net"]
        results["pycoder_namespace_flags"] = run(
            "PyCoder namespace flags + sed (not the full PyCoder runner)",
            [*full, "--", *command], env)
        work = root / "work"
        work.mkdir()
        probe = r'''
import errno
from pathlib import Path
import socket
import sys

sample, work = map(Path, sys.argv[1:])
assert sample.read_text().startswith("SANDBOX_READ_OK")
print("READ: PASS", flush=True)
try:
    with sample.open("a") as stream:
        stream.write("UNEXPECTED_WRITE\n")
except OSError as error:
    if error.errno not in (errno.EROFS, errno.EACCES, errno.EPERM):
        raise
    print("READ_ONLY_MOUNT: PASS errno=" + str(error.errno), flush=True)
else:
    raise RuntimeError("READ_ONLY_MOUNT: FAIL (write unexpectedly allowed)")
(work / "created.txt").write_text("ok")
print("WORKSPACE_WRITE: PASS", flush=True)
# This socket exists only in the disposable network namespace.
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    with socket.create_connection(listener.getsockname(), timeout=3) as client:
        connection, _ = listener.accept()
        connection.close()
print("ISOLATED_LOOPBACK: PASS", flush=True)
'''
        results["mount_and_loopback"] = run(
            "Read-only protection, writable temp workspace, isolated loopback",
            [*full, "--bind", str(work), str(work), "--", sys.executable,
             "-I", "-S", "-c", probe, str(sample), str(work)], env)
    print("\n--- SUMMARY ---")
    for name, code in results.items():
        print(name + ": " + ("PASS" if code == 0 else "FAIL (exit=" + str(code) + ")"))
    print("This tests Bubblewrap setup, not PyCoder's proxy or seccomp runner.")
    print("No kernel logs collected. You can share this complete output.")
    return 0 if all(code == 0 for code in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
