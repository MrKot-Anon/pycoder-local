"""Bounded output and private, atomic local storage."""

import json
import math
import os
from pathlib import Path
import tempfile


def tokens(value) -> int:
    """Codex-style estimate; use provider usage when available, not a tokenizer."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return math.ceil(len(text.encode("utf-8")) / 4)


def truncate(text: str, budget: int) -> str:
    data = text.encode("utf-8")
    limit = max(0, budget * 4)
    if len(data) <= limit:
        return text
    marker = b"\n... [output truncated] ...\n"
    if limit < len(marker):
        return data[:limit].decode("utf-8", errors="ignore")
    half = (limit - len(marker)) // 2
    return (data[:half].decode("utf-8", errors="ignore") + marker.decode()
            + data[-half:].decode("utf-8", errors="ignore") if half else marker.decode())


def private_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def workspace_path(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Path is outside the workspace (including symlink targets)")
    return path


def terminal_text(text: str) -> str:
    """Do not interpret terminal escapes supplied by a model or subprocess."""
    return "".join(c if c in "\n\t" or (ord(c) >= 32 and not 127 <= ord(c) <= 159)
                   else "?" for c in text)
