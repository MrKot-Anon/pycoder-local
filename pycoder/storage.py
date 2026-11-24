"""Choose writable local state storage before starting model configuration."""

from pathlib import Path
import tempfile
import os


def prepare_state(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    sessions = directory / "sessions"
    sessions.mkdir(exist_ok=True, mode=0o700)
    # Probe actual file creation, not just access bits (ACLs may differ).
    with tempfile.TemporaryFile(dir=sessions):
        pass
    return directory


def state_directory(explicit: Path | None, config_directory: Path, notify=lambda text: None,
                    *, create=True) -> Path:
    if explicit is not None:
        directory = explicit.expanduser().resolve()
        try:
            return prepare_state(directory) if create else directory
        except PermissionError:
            raise ValueError(f"Нет прав на запись истории в явно заданный каталог {directory}. "
                             "Проверьте права или задайте другой --state-dir / PYCODER_STATE_DIR.") from None

    legacy = Path.home() / ".local/state/pycoder"
    primary = (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "pycoder/state"
               if os.name == "nt" and not legacy.exists()
               else Path((os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")) / "pycoder")
    fallback = config_directory.expanduser().resolve() / "state"
    # Keep using a previously selected fallback, even if primary permissions
    # subsequently change; otherwise existing conversations would disappear.
    directory = fallback if (fallback / "sessions").is_dir() else primary
    if not create:
        return directory
    primary_existed = primary.exists()
    try:
        return prepare_state(directory)
    except PermissionError:
        if directory == fallback or primary_existed:
            raise ValueError(f"Нет прав на запись в каталог истории {directory}. "
                             "Автоматический перенос существующей истории не выполнен. "
                             "Проверьте права или укажите другой --state-dir.") from None
    try:
        result = prepare_state(fallback)
    except PermissionError:
        raise ValueError(f"Оба каталога истории недоступны: {primary} и {fallback}. "
                         "Укажите доступную папку через --state-dir.") from None
    notify(f"Нет прав на создание {primary}. История будет храниться в {result}.")
    return result
