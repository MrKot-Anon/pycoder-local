"""Python adaptation of Codex apply-patch parser/seek_sequence/file_update.

Derived from OpenAI Codex (Apache-2.0), revision 18344a972d.
Modified: Python, workspace paths, preflight validation, per-file atomic writes,
best-effort multi-file rollback. This implements the LF-normalizing algorithm.
"""

from dataclasses import dataclass, field
import difflib
import os
from pathlib import Path
import stat
import tempfile

from .util import workspace_path

MAX_FILE_BYTES = 8 * 1024 * 1024


@dataclass
class Chunk:
    context: str | None = None
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    eof: bool = False


@dataclass
class Hunk:
    kind: str
    path: str
    move: str | None = None
    added: list[str] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)


def parse(text: str) -> list[Hunk]:
    lines = text.strip().splitlines()
    if lines and lines[0] in {"<<'EOF'", '<<"EOF"', "<<EOF"} and lines[-1] == "EOF":
        lines = lines[1:-1]
    lines = [line.strip() if line.lstrip().startswith("*** ") else line for line in lines]
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("Expected *** Begin Patch and *** End Patch")
    hunks, i = [], 1
    while i < len(lines) - 1:
        marker = lines[i]
        kind = next((kind for kind in ("Add", "Delete", "Update")
                     if marker.startswith(f"*** {kind} File: ")), None)
        if not kind:
            raise ValueError(f"Invalid file marker at line {i + 1}")
        path = marker.split(": ", 1)[1]
        if not path.strip():
            raise ValueError("Empty patch path")
        hunk = Hunk(kind, path)
        hunks.append(hunk)
        i += 1
        if kind == "Delete":
            continue
        if kind == "Add":
            while i < len(lines) - 1 and lines[i].startswith("+"):
                hunk.added.append(lines[i][1:])
                i += 1
            if i < len(lines) - 1 and not lines[i].startswith("*** "):
                raise ValueError(f"Invalid Add File content at line {i + 1} for {path}: "
                                 "every content line must start with '+', including blank lines and docstrings. "
                                 "Use '+' for an empty added line. Correct the prefix and retry the complete patch.")
            continue
        if lines[i].startswith("*** Move to: "):
            hunk.move = lines[i][13:]
            i += 1
        while i < len(lines) - 1 and not lines[i].startswith("*** "):
            chunk = Chunk()
            if lines[i] == "@@" or lines[i].startswith("@@ "):
                chunk.context = lines[i][3:] if lines[i].startswith("@@ ") else None
                i += 1
            while i < len(lines) - 1:
                line = lines[i]
                if line.startswith("*** ") or line == "@@" or line.startswith("@@ "):
                    break
                # Codex permits an unprefixed blank context line.
                if line == "":
                    line = " "
                if line[0] not in " +-":
                    raise ValueError(f"Invalid patch line {i + 1}")
                if line[0] in " -":
                    chunk.old.append(line[1:])
                if line[0] in " +":
                    chunk.new.append(line[1:])
                i += 1
            if i < len(lines) and lines[i] == "*** End of File":
                chunk.eof = True
                i += 1
            if not chunk.old and not chunk.new:
                raise ValueError("Empty update chunk")
            hunk.chunks.append(chunk)
        if not hunk.chunks and not hunk.move:
            raise ValueError("Update needs changes or a move destination")
    if not hunks:
        raise ValueError("Empty patch")
    return hunks


_PUNCTUATION = str.maketrans({**dict.fromkeys("‐‑‒–—―−", "-"),
                            **dict.fromkeys("‘’‚‛", "'"),
                            **dict.fromkeys("“”„‟", '"'),
                            **dict.fromkeys("\u00a0\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000", " ")})


def seek(lines, pattern, start=0, eof=False):
    if not pattern:
        return start
    if len(pattern) > len(lines):
        return None
    start = len(lines) - len(pattern) if eof else start
    normalizers = (lambda s: s, str.rstrip, str.strip, lambda s: s.strip().translate(_PUNCTUATION))
    for normalize in normalizers:
        target = [normalize(s) for s in pattern]
        for i in range(start, len(lines) - len(pattern) + 1):
            if [normalize(s) for s in lines[i:i + len(pattern)]] == target:
                return i
    return None


def update(text: str, chunks: list[Chunk]) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    cursor, replacements = 0, []
    for chunk in chunks:
        if chunk.context is not None:
            found = seek(lines, [chunk.context], cursor)
            if found is None:
                raise ValueError(f"Context not found: {chunk.context}")
            cursor = found + 1
        old, new = chunk.old, chunk.new
        if not old:
            insertion = len(lines) - 1 if lines and lines[-1] == "" else len(lines)
            replacements.append((insertion, 0, new))
            continue
        found = seek(lines, old, cursor, chunk.eof)
        if found is None and old[-1] == "":
            old = old[:-1]
            new = new[:-1] if new and new[-1] == "" else new
            found = seek(lines, old, cursor, chunk.eof)
        if found is None or found < cursor:
            raise ValueError("Expected lines not found (or overlapping hunks):\n" + "\n".join(old[:8]))
        replacements.append((found, len(old), new))
        cursor = found + len(old)
    for start, count, new in reversed(sorted(replacements, key=lambda item: item[0])):
        lines[start:start + count] = new
    return "\n".join(lines) + ("\n" if lines else "")


def atomic_write(path: Path, data: bytes, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pycoder-patch-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class PreparedPatch:
    def __init__(self, root: Path, text: str):
        self.root = root.resolve()
        self.before, self.after, self.modes = {}, {}, {}
        operations = {}
        for hunk in parse(text):
            path = workspace_path(self.root, hunk.path)
            dest = workspace_path(self.root, hunk.move) if hunk.move else path
            for candidate in {path, dest}:
                if candidate in self.before:
                    if hunk.kind == "Update" and not hunk.move and operations[candidate] == "Update":
                        continue
                    raise ValueError(f"Conflicting file operations for {candidate.relative_to(self.root)}. "
                                     "Only repeated Update File blocks without moves are allowed. "
                                     "Use Update File to replace existing contents; do not combine Add/Delete/Move "
                                     "with another operation on the same path. No files were changed.")
                if candidate.exists() and (not candidate.is_file() or candidate.stat().st_size > MAX_FILE_BYTES):
                    raise ValueError("Patch target must be a regular file <=8 MiB")
                self.before[candidate] = candidate.read_bytes() if candidate.exists() else None
                self.modes[candidate] = stat.S_IMODE(candidate.stat().st_mode) if candidate.exists() else 0o644
                operations[candidate] = "Move" if hunk.move else hunk.kind
            original = self.after.get(path, self.before[path])
            if hunk.kind == "Add":
                if original is not None:
                    raise ValueError(f"Refusing to overwrite existing file: {hunk.path}")
                self.after[path] = ("\n".join(hunk.added) + ("\n" if hunk.added else "")).encode()
            elif original is None:
                raise ValueError(f"File not found: {hunk.path}")
            elif hunk.kind == "Delete":
                self.after[path] = None
            else:
                if b"\0" in original:
                    raise ValueError("Binary patches are not supported")
                result = update(original.decode("utf-8"), hunk.chunks).encode("utf-8") if hunk.chunks else original
                if dest != path:
                    if self.before[dest] is not None:
                        raise ValueError("Move destination already exists")
                    self.after[path] = None
                    self.modes[dest] = self.modes[path]
                self.after[dest] = result

    def diff(self):
        output = []
        for path, after in self.after.items():
            before = self.before[path]
            name = path.relative_to(self.root).as_posix()
            output.extend(difflib.unified_diff(
                (before or b"").decode("utf-8", errors="replace").splitlines(keepends=True),
                (after or b"").decode("utf-8", errors="replace").splitlines(keepends=True),
                fromfile=f"a/{name}" if before is not None else "/dev/null",
                tofile=f"b/{name}" if after is not None else "/dev/null"))
        return "".join(output) or "Changes empty files / file names: " + ", ".join(str(p.relative_to(self.root)) for p in self.after)

    def apply(self):
        for path, expected in self.before.items():
            if workspace_path(self.root, str(path)) != path:
                raise ValueError("Path changed during patch approval")
            current = path.read_bytes() if path.exists() else None
            if current != expected:
                raise ValueError("File changed since patch preparation; read it and retry")
        changed = []
        try:
            for path, data in self.after.items():
                if data is None:
                    path.unlink()
                else:
                    atomic_write(path, data, self.modes[path])
                changed.append(path)
        except BaseException:
            for path in reversed(changed):
                original = self.before[path]
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, original, self.modes[path])
            raise
        return {"changed": [path.relative_to(self.root).as_posix() for path in self.after]}
