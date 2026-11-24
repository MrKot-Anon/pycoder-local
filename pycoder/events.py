"""UI-independent events and cancellable request/reply channels."""

from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass, field
from queue import Queue
import threading


class Cancelled(Exception):
    pass


@dataclass
class Event:
    kind: str
    data: dict = field(default_factory=dict)


class EventBus:
    def __init__(self):
        self.queue = Queue()
        self.cancelled = threading.Event()
        self.question_lock = threading.Lock()

    def emit(self, kind, **data):
        self.queue.put(Event(kind, data))

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled("Операция прервана")

    def request(self, title, options=(), *, detail="", secret=False, default="", allow_text=False, cancelled=None,
                auto_resolve=None, auto_value="recovered"):
        def check():
            self.check()
            if cancelled is not None and cancelled.is_set():
                raise Cancelled()
        check()
        while not self.question_lock.acquire(timeout=0.1):
            check()
            if auto_resolve and auto_resolve():
                return auto_value
        try:
            check()
            if auto_resolve and auto_resolve():
                return auto_value
            return self._request(title, options, detail=detail, secret=secret, default=default, allow_text=allow_text,
                                 check=check, auto_resolve=auto_resolve, auto_value=auto_value)
        finally:
            self.question_lock.release()

    def _request(self, title, options, *, detail, secret, default, allow_text, check, auto_resolve=None, auto_value="recovered"):
        reply = Future()
        self.emit("question", title=title, options=list(options), detail=detail,
                  secret=secret, default=default, allow_text=allow_text, reply=reply)
        try:
            while True:
                check()
                if not reply.done() and auto_resolve and auto_resolve() and reply.cancel():
                    return auto_value
                try:
                    value = reply.result(timeout=0.1)
                    if value is None:
                        raise Cancelled("Вопрос отменён")
                    return value
                except TimeoutError:
                    pass
        finally:
            if not reply.done():
                reply.cancel()
