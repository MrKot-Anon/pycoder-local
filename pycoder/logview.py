"""Read-only live log tail and an optional independent terminal window."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

from .util import terminal_text


def follow(path):
    """No model setup, workspace lock, or agent process dependency."""
    print(f"Журнал: {path}\nCtrl+C — закрыть просмотр (агент продолжит работу).", flush=True)
    identity, offset = None, 0
    try:
        while True:
            try:
                with path.open("rb") as stream:
                    stat = os.fstat(stream.fileno())
                    current = (stat.st_dev, stat.st_ino)
                    if identity != current or stat.st_size < offset:
                        offset = max(0, stat.st_size - 24000) if identity is None else 0
                        identity = current
                    stream.seek(offset)
                    data = stream.read(65536)
                    offset = stream.tell()
                if data:
                    print(terminal_text(data.decode("utf-8", errors="replace")), end="", flush=True)
            except FileNotFoundError:
                pass
            time.sleep(0.5)
    except (KeyboardInterrupt, BrokenPipeError):
        return 0


def open_terminal(config_dir, state_dir):
    entry = Path(sys.argv[0]).resolve()
    command = [sys.executable, str(entry), "--logs", "--config-dir", str(config_dir), "--state-dir", str(state_dir)]
    if sys.platform == "darwin":
        script = 'on run argv\n tell application "Terminal"\n activate\n do script (item 1 of argv)\n end tell\nend run'
        subprocess.run(["osascript", "-e", script, shlex.join(command)], check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    elif os.name == "nt":
        subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        terminal = shutil.which("x-terminal-emulator") or shutil.which("xterm")
        if not terminal:
            raise RuntimeError("Терминал не найден. Запустите pycoder --logs в отдельном терминале.")
        subprocess.Popen([terminal, "-e", *command], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
