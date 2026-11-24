"""Native clipboard bridge, used only for user clipboard actions."""

import os
import shutil
import subprocess
import sys


def read_clipboard():
    """Return text, empty text for an empty clipboard, or None if unavailable."""
    if os.name == "nt":
        from .windows import read_clipboard as read_windows
        return read_windows()
    if sys.platform == "darwin":
        commands = [["/usr/bin/pbpaste"]]
    else:
        commands = []
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
            commands.append(["wl-paste", "--no-newline", "--type", "text"])
        if os.environ.get("DISPLAY"):
            if shutil.which("xclip"):
                commands.append(["xclip", "-selection", "clipboard", "-out"])
            if shutil.which("xsel"):
                commands.append(["xsel", "--clipboard", "--output"])
    for command in commands:
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=2, check=True)
            return result.stdout.decode("utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def write_clipboard(text):
    if os.name == "nt":
        from .windows import clipboard
        return clipboard(text)
    if sys.platform == "darwin":
        command = ["/usr/bin/pbcopy"]
    elif os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        command = ["wl-copy"]
    elif os.environ.get("DISPLAY") and shutil.which("xclip"):
        command = ["xclip", "-selection", "clipboard"]
    elif os.environ.get("DISPLAY") and shutil.which("xsel"):
        command = ["xsel", "--clipboard", "--input"]
    else:
        return False
    try:
        subprocess.run(command, input=text.encode("utf-8"), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=2, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
