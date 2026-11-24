"""Lossless local tool transcripts with bounded, throttled UI previews."""

import codecs
import json
import os
from pathlib import Path
import tempfile
import threading
import time

PAGE_BYTES = 64 * 1024


def format_output(result):
    if isinstance(result, dict):
        for key in ("output", "content"):
            if isinstance(result.get(key), str):
                return result[key]
        if isinstance(result.get("files"), list):
            return "\n".join(result["files"]) + ("\n[Список ограничен инструментом]" if result.get("truncated") else "")
    return json.dumps(result, ensure_ascii=False, indent=2)


class OutputJournal:
    def __init__(self, directory, call_id, emit):
        self.emit, self.call_id = emit, call_id
        self.path = None
        self.tail = ""
        self.error = ""
        self.streamed = False
        self.last_emit = 0.0
        self.lock = threading.Lock()
        try:
            Path(directory).mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, name = tempfile.mkstemp(prefix="tool-", suffix=".log", dir=directory)
            os.close(fd)
            self.path = Path(name)
        except OSError as error:
            self.error = f"Полный вывод не сохранён: {type(error).__name__}"

    def append(self, text, *, final=False, exit_code=None):
        with self.lock:
            self.tail = (self.tail + text)[-8192:]
            if self.path and not self.error:
                try:
                    with self.path.open("a", encoding="utf-8", newline="") as stream:
                        stream.write(text)
                except OSError as error:
                    self.error = f"Запись полного вывода оборвалась: {type(error).__name__}"
            now = time.monotonic()
            if final or now - self.last_emit >= 0.1:
                self.last_emit = now
                self.emit("tool_output", id=self.call_id, tail=self.tail, path=str(self.path) if self.path else "",
                          error=self.error, completed=final and self.streamed, exit_code=exit_code)

    def finish_result(self, result):
        if not self.streamed:
            self.append(format_output(result), final=True)


def read_page(path, offset=0, *, last=False):
    """Read UTF-8 pages without splitting a character or loading the whole log."""
    with Path(path).open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        if last:
            offset = max(0, size - PAGE_BYTES)
            stream.seek(offset)
            # A tail seek can land inside a code point.
            while offset < size:
                byte = stream.read(1)
                if byte[0] & 0xc0 != 0x80:
                    break
                offset += 1
        stream.seek(offset)
        raw = stream.read(PAGE_BYTES)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        text = decoder.decode(raw, final=offset + len(raw) >= size)
        end = offset + len(raw) - len(decoder.getstate()[0])
        return text, offset, end, size
