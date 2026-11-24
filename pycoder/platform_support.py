"""Platform defaults and terminal-safe shortcuts; no third-party OS helpers."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import ntpath


def default_shell(platform=None):
    if (platform or sys.platform) == "win32":
        return shutil.which("pwsh.exe") or shutil.which("powershell.exe") or os.environ.get("COMSPEC", "cmd.exe")
    return "/bin/sh"


def shell_args(command, shell=None):
    shell = shell or default_shell()
    name = ntpath.basename(shell).lower()
    if name in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        prefix = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); $OutputEncoding = [Console]::OutputEncoding; "
        return [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", prefix + command]
    if name in {"cmd", "cmd.exe"}:
        return [shell, "/d", "/s", "/c", command]
    return [shell, "-c", command]


def display_argv(argv):
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    import shlex
    return shlex.join(argv)


def shortcut_help(platform=None):
    platform = platform or sys.platform
    copy = "Ctrl+C / ⌘C" if platform == "darwin" else "Ctrl+C"
    return (f"{copy} — копировать; Ctrl+V — вставить; F2 / ПКМ — буфер обмена.\n"
            "Ctrl+G — остановить задачу. Ctrl+Q — выход с подтверждением.\n"
            "Enter — отправить; Ctrl+J — новая строка; Tab — меню; Esc — назад.\n"
            "F3 — журнал: поток модели и ошибки. На ноутбуках для F2/F3 может требоваться Fn.\n"
            "Если терминал перехватывает сочетание копирования, используйте F2 или правую кнопку мыши.")


def environment_description():
    return f"OS: {sys.platform}; Python executable: {sys.executable}; default shell: {default_shell()}. Use commands and quoting appropriate to this OS. Use the shell tool for direct argv execution without shell quoting."
