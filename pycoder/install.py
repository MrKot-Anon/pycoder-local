"""Atomically install the running single-file bundle; never touch user state."""

import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import zipfile

DEFAULT_INSTALL_DIR = str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs/PyCoder") if os.name == "nt" else "/usr/local/bin"


def bundle_path() -> Path:
    # zipimport preserves the archive's real path in module.__file__, independent
    # of cwd and of whether argv[0] was a relative name or a symlink in PATH.
    candidate = Path(__file__).resolve().parents[1]
    if not candidate.is_file() or not zipfile.is_zipfile(candidate):
        raise ValueError("--install работает из собранного файла. Сначала выполните python3 build.py, затем python3 dist/pycoder --install.")
    return candidate


def install(directory: Path, *, source: Path | None = None) -> Path:
    source = source or bundle_path()
    # Resolve only the directory: replacing a pre-existing command symlink must
    # replace that symlink, not overwrite its target elsewhere on the filesystem.
    directory = directory.expanduser().resolve()
    windows = os.name == "nt"
    target = directory / ("pycoder.pyz" if windows else "pycoder")
    replaced = target.exists() or target.is_symlink()
    if target.is_dir() and not target.is_symlink():
        raise ValueError(f"Путь занят каталогом: {target}")
    temporary = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".pycoder-install-", dir=directory)
        with os.fdopen(fd, "wb") as destination, source.open("rb") as original:
            shutil.copyfileobj(original, destination)
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(temporary, 0o755)
        os.replace(temporary, target)
    except PermissionError:
        command = shlex.join([sys.executable, str(source), "--install", str(directory)])
        if windows:
            raise ValueError(f"Нет прав на установку в {directory}. Выберите доступный каталог через --install.") from None
        raise ValueError(
            f"Нет прав на установку в {directory}. Выполните:\n  sudo {command}\n"
            "Либо выберите пользовательский каталог: --install ~/.local/bin"
        ) from None
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
    if windows:
        launcher = ('@echo off\r\nsetlocal DisableDelayedExpansion\r\n'
                    'for /f "tokens=2 delims=:" %%A in (\'chcp\') do set "pycoder_old_cp=%%A"\r\n'
                    'chcp 65001 >nul\r\n"' + sys.executable.replace('%', '%%') +
                    '" "%~dp0pycoder.pyz" %*\r\nset "pycoder_exit=%errorlevel%"\r\n'
                    'chcp %pycoder_old_cp% >nul\r\nexit /b %pycoder_exit%\r\n')
        fd, name = tempfile.mkstemp(prefix=".pycoder-launcher-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(launcher)
            os.replace(name, directory / "pycoder.cmd")
        finally:
            if os.path.exists(name):
                os.unlink(name)
    print(f"{'Обновлён' if replaced else 'Установлен'}: {target}")
    path_dirs = [Path(p or os.curdir).expanduser().resolve() for p in os.environ.get("PATH", "").split(os.pathsep)]
    if directory not in path_dirs:
        if windows:
            print(f"Добавьте {directory} в пользовательский PATH через настройки переменных среды Windows.")
            print(f"Или запускайте напрямую: {directory / 'pycoder.cmd'}")
        else:
            print(f"Добавьте каталог в PATH (для bash/zsh):\n  export PATH={shlex.quote(str(directory))}:\"$PATH\"")
            print("Чтобы сохранить PATH для новых терминалов, добавьте эту строку в настройки shell.")
    else:
        found = shutil.which("pycoder")
        if found and Path(found).absolute() != target:
            print(f"В PATH раньше найден другой pycoder: {found}. Используйте {target} или измените порядок PATH.")
    print("Запуск в PowerShell: cd C:\\путь\\к\\проекту; pycoder" if windows else "Запуск: cd /путь/к/проекту && pycoder")
    return target
