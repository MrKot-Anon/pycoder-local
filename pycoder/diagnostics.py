"""Private, bounded local diagnostics. No locals, credentials or network logging."""

from datetime import datetime, timezone
import os
import threading
import traceback


class Diagnostics:
    def __init__(self, directory, redact=lambda text: text):
        self.path = directory / "errors.log"
        self.redact = redact
        self.lock = threading.Lock()

    def record(self, error, where="agent"):
        detail = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        text = self.redact(f"{datetime.now(timezone.utc).isoformat()} [{where}]\n{detail}\n")
        try:
            with self.lock:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if self.path.exists() and self.path.stat().st_size > 1024 * 1024:
                    os.replace(self.path, self.path.with_suffix(".log.1"))
                fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(text[-65536:])
        except OSError:
            return None  # Reporting must not mask the original failure.
        return self.path

    def tail(self):
        with self.lock:
            if not self.path.exists():
                return "Ошибок в новом журнале пока нет."
            with self.path.open("rb") as stream:
                stream.seek(max(0, self.path.stat().st_size - 24000))
                return self.redact(stream.read().decode("utf-8", errors="replace"))

    def clear(self):
        """Clear persisted diagnostics, including the single rotated archive."""
        with self.lock:
            self.path.unlink(missing_ok=True)
            self.path.with_suffix(".log.1").unlink(missing_ok=True)
