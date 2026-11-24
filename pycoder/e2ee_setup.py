"""Startup dependency checks and a logged, direct E2EE installer."""

import json
import os
from pathlib import Path
import subprocess
import sys
import importlib
import queue
import signal
import threading
import time

from .util import private_json

REQUIREMENTS = ("chutes-e2ee==0.1.0", "pqcrypto==0.3.4", "cryptography==43.0.1",
                "dcap-qvl==0.6.5", "nv-attestation-sdk==2.7.3")


def interpreter(directory):
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


_runtime_python = None


def python_executable():
    return str(_runtime_python or sys.executable)


def direct_environment():
    # pip's configuration files and inherited proxy/index settings must not
    # silently change the explicitly announced direct PyPI route.
    env = {key: value for key, value in os.environ.items()
           if not key.lower().endswith("_proxy") and not key.upper().startswith("PIP_")}
    env.update(PIP_CONFIG_FILE=os.devnull, PYTHONUNBUFFERED="1", NO_PROXY="*", no_proxy="*")
    return env


def probe(python=None, *, attestation=True):
    imports = "import chutes_e2ee.crypto, pqcrypto, cryptography"
    if attestation:
        imports += "; import dcap_qvl; from nv_attestation_sdk.attestation import Attestation"
    try:
        result = subprocess.run([str(python or sys.executable), "-I", "-c", imports],
                                capture_output=True, text=True, timeout=45, env=direct_environment())
        return "" if result.returncode == 0 else (result.stderr or result.stdout or
            f"Проверка библиотек завершилась с кодом {result.returncode}")[-6000:]
    except (OSError, subprocess.SubprocessError) as error:
        return f"{type(error).__name__}: {error}"


def run_logged(args, emit, check_cancelled, *, cwd=None):
    process = subprocess.Popen([str(arg) for arg in args], stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, errors="replace", env=direct_environment(),
        cwd=cwd, start_new_session=os.name != "nt")
    lines = queue.Queue()
    def read():
        try:
            for line in process.stdout:
                lines.put(line.rstrip("\r\n"))
        finally:
            lines.put(None)
    reader = threading.Thread(target=read, name="pycoder-install-log", daemon=True)
    reader.start()
    deadline = time.monotonic() + 1200
    try:
        while True:
            check_cancelled()
            if time.monotonic() >= deadline:
                raise ValueError("Установка превысила 20 минут")
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                break
            emit(line)
        if process.wait() != 0:
            raise ValueError(f"Процесс установки завершился с кодом {process.returncode}; подробности выше")
    finally:
        if process.poll() is None:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
        process.wait()
        reader.join(timeout=2)
        process.stdout.close()


def setup(config_directory, emit=print, check_cancelled=lambda: None):
    from .workspace import WorkspaceLock
    directory = config_directory.expanduser().absolute() / "e2ee-env"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    waited = False
    deadline = time.monotonic() + 1200
    while True:
        check_cancelled()
        try:
            lock = WorkspaceLock(directory)
            break
        except ValueError:
            if not waited:
                emit("Другое окно PyCoder устанавливает E2EE. Ожидаю завершения…")
                waited = True
            if time.monotonic() >= deadline:
                raise ValueError("Истекло время ожидания другой установки E2EE") from None
            time.sleep(0.1)
    try:
        if waited and not probe(interpreter(directory)):
            emit("Библиотеки E2EE установлены другим окном и проверены.")
            return
        _setup(config_directory, emit, check_cancelled)
    finally:
        lock.close()


def _setup(config_directory, emit, check_cancelled):
    directory = config_directory.expanduser().absolute() / "e2ee-env"
    emit("ВНИМАНИЕ: библиотеки E2EE скачиваются напрямую с PyPI, БЕЗ ПРОКСИ. Маршруты запросов модели не меняются.")
    emit(f"Установка в {directory}")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = directory / "pycoder-ready.json"
    marker.unlink(missing_ok=True)
    try:
        python = interpreter(directory)
        if python.is_file() and (directory / "pyvenv.cfg").is_file():
            # PyCoder may already be running from this environment. Do not
            # overwrite its live executable when repairing missing packages.
            emit("Подготовка pip в существующем окружении…")
            run_logged([python, "-I", "-m", "ensurepip", "--upgrade"], emit, check_cancelled)
        else:
            emit("Создание окружения Python…")
            run_logged([sys.executable, "-I", "-m", "venv", str(directory)], emit, check_cancelled)
        emit("Скачивание и установка зависимостей…")
        run_logged([python, "-I", "-u", "-m", "pip", "--isolated", "install",
            "--disable-pip-version-check", "--no-input", "--progress-bar", "off",
            "--index-url", "https://pypi.org/simple", "--proxy", "", "--force-reinstall", *REQUIREMENTS],
            emit, check_cancelled)
        emit("Проверка установленных библиотек…")
        check_cancelled()
        error = probe(python)
        if error:
            emit(error)
            raise ValueError("Проверка установленных библиотек E2EE не пройдена")
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("Установка E2EE не завершена. Проверьте лог и наличие python3-venv; повторный запуск повторит установку.") from error
    private_json(marker, {"python": list(sys.version_info[:2]), "requirements": list(REQUIREMENTS)})
    emit("Библиотеки E2EE установлены и проверены.")


def enable(directory):
    """Load dependencies before any E2EE imports, without restarting the chat UI."""
    global _runtime_python
    python = interpreter(directory)
    result = subprocess.run([str(python), "-I", "-c",
        "import json, sysconfig; print(json.dumps([sysconfig.get_path('purelib'), sysconfig.get_path('platlib')]))"],
        capture_output=True, text=True, check=True, timeout=30, env=direct_environment())
    for path in reversed(list(dict.fromkeys(json.loads(result.stdout)))):
        if path not in sys.path:
            sys.path.insert(0, path)
    importlib.invalidate_caches()
    _runtime_python = python


def ensure(config, config_directory, *, event=lambda **data: None, check_cancelled=lambda: None,
           verify_attestation=None):
    from urllib.parse import urlsplit
    if urlsplit(config.base_url).hostname != "llm.chutes.ai":
        return
    verify = config.e2ee_verify_attestation if verify_attestation is None else verify_attestation
    event(state="checking", text="Проверка библиотек E2EE…")
    check_cancelled()
    error = probe(python_executable(), attestation=verify)
    check_cancelled()
    if not error:
        event(state="ready", text="Библиотеки E2EE доступны")
        return
    directory = config_directory.expanduser().absolute() / "e2ee-env"
    python = interpreter(directory)
    if python.is_file() and not probe(python, attestation=verify):
        check_cancelled()
        try:
            enable(directory)
        except Exception as failure:
            event(state="failed", text=f"Не удалось подключить окружение E2EE: {failure}")
            raise
        event(state="ready", text="Используется установленное окружение E2EE")
        return
    event(state="installing", text="Библиотеки E2EE отсутствуют или неисправны. Начинается автоматическая установка.")
    event(state="log", text=error)
    try:
        setup(config_directory, lambda text: event(state="log", text=text), check_cancelled)
        check_cancelled()
        enable(directory)
    except Exception as failure:
        event(state="failed", text=f"{type(failure).__name__}: {failure}")
        raise
    event(state="ready", text="E2EE готово. Продолжаю запуск чата.")


def activate(config_directory):
    directory = config_directory.expanduser().absolute() / "e2ee-env"
    if Path(sys.prefix).resolve() == directory.resolve():
        return
    marker = directory / "pycoder-ready.json"
    if not marker.is_file():
        return
    try:
        data = json.loads(marker.read_text())
    except (OSError, ValueError):
        return
    if data != {"python": list(sys.version_info[:2]), "requirements": list(REQUIREMENTS)}:
        return
    python = interpreter(directory)
    if not python.is_file():
        return
    # Import the same source checkout or zipapp, even when the original launch
    # used a console script installed in another Python environment.
    source = str(Path(__file__).resolve().parent.parent)
    bootstrap = (f"import sys; sys.path.insert(0, {source!r}); "
                 "from pycoder.cli import main; raise SystemExit(main())")
    os.execv(str(python), [str(python), "-I", "-c", bootstrap, *sys.argv[1:]])
