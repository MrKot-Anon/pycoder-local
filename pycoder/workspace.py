"""Exclusive workspace ownership and discovery of existing local conversations."""

from dataclasses import dataclass
import json
import os
from pathlib import Path


class WorkspaceLock:
    """Lock the directory inode, also across symlink aliases and state directories.

    No lock file is needed and a process exit/crash releases the kernel lock.
    An unrelated application that does not take this lock remains unrestricted.
    """

    def __init__(self, root: Path):
        self.native_lock = None
        self.fd = None
        if os.name == "nt":
            from .windows import NamedLock
            stat = root.resolve().stat()
            identity = f"{stat.st_dev}:{stat.st_ino}" if stat.st_ino else os.path.normcase(str(root.resolve()))
            self.native_lock = NamedLock("workspace:" + identity)
            return
        import fcntl
        self.fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise ValueError(f"В этой папке уже запущен PyCoder: {root}. Закройте первый экземпляр.") from None
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def close(self):
        if self.native_lock:
            self.native_lock.close()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


@dataclass(frozen=True)
class PreviousSession:
    session_id: str
    modified: float


def latest_session(directory: Path, root: Path) -> PreviousSession | None:
    """Recognize existing JSONL files; ignore empty startup sessions, never rewrite.

    A journal with a real conversation remains eligible even if a later write was
    interrupted. History owns validation and repair after the user consents.
    """
    candidates = []
    for path in directory.glob("*.jsonl"):
        try:
            if not path.is_symlink():
                candidates.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    for modified, path in sorted(candidates, reverse=True):
        try:
            with path.open("rb") as stream:
                first = stream.readline(1024 * 1024)
                if not first.endswith(b"\n"):
                    continue
                meta = json.loads(first)
                if (meta.get("type") != "meta" or not isinstance(meta.get("data"), dict)
                        or meta["data"].get("workspace") != str(root.resolve())):
                    continue
                for line in stream:
                    if not line.endswith(b"\n"):
                        break
                    event = json.loads(line)
                    kind, data = event.get("type"), event.get("data")
                    if ((kind == "message" and isinstance(data, dict)
                         and data.get("role") in {"user", "assistant"})
                            or (kind == "checkpoint" and isinstance(data, dict) and data.get("messages"))):
                        return PreviousSession(path.stem, modified / 1e9)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return None
