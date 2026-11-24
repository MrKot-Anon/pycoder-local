"""Restore the editable project from its zipapp, without importing dependencies."""

import json
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import zipfile

from .install import bundle_path


def unpack(directory: Path, *, source: Path | None = None) -> Path:
    source = source or bundle_path()
    directory = directory.expanduser().absolute()
    if directory.exists() or directory.is_symlink():
        raise ValueError(f"Каталог уже существует: {directory}. Выберите новый каталог для распаковки.")
    with zipfile.ZipFile(source) as archive:
        if "_source/manifest.json" not in archive.namelist():
            raise ValueError("Эта сборка не содержит исходного проекта. Пересоберите её новым build.py.")
        manifest = json.loads(archive.read("_source/manifest.json"))
        if not isinstance(manifest, dict) or not manifest:
            raise ValueError("Invalid source manifest")
        for name, member in manifest.items():
            if (not isinstance(name, str) or not isinstance(member, str)
                    or not name or "\\" in name or ":" in name
                    or PurePosixPath(name).is_absolute()
                    or any(part in {"", ".", ".."} for part in name.split("/"))):
                raise ValueError("Unsafe path in source manifest")
            info = archive.getinfo(member)
            if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Source manifest must reference regular files")
        directory.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".pycoder-unpack-", dir=directory.parent))
        try:
            for name, member in manifest.items():
                target = temporary.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as original, target.open("xb") as destination:
                    shutil.copyfileobj(original, destination)
            # Reserve the destination so existing files/directories are never replaced.
            directory.mkdir()
            try:
                temporary.replace(directory)
            except OSError:
                # Windows cannot replace an existing directory, even an empty one.
                for child in temporary.iterdir():
                    child.rename(directory / child.name)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
    print(f"Исходники распакованы: {directory}")
    print("Запуск: python3 run.py\nСборка: python3 build.py\nПроверки: python3 -m tests.run_portable")
    return directory
