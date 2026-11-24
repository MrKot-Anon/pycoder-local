"""Append-only rollout with compaction checkpoints and crash recovery.

Adapted from Codex session/turn.rs, compact.rs and session/mod.rs (Apache-2.0).
Modified to store standard Chat Completions messages in a standalone JSONL journal.
"""

import copy
import codecs
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid
import threading

from .util import tokens


def new_id():
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]


def repair_pending(messages):
    """Never replay a possibly executed tool after a crash."""
    repaired, pending = [], []

    def flush():
        for call_id in pending:
            repaired.append({"role": "tool", "tool_call_id": call_id, "content":
                             '{"error":"Agent interrupted before durable result. Action may have run; inspect workspace before retrying. Process sessions do not survive restart."}'})
        pending.clear()

    for message in messages:
        if message["role"] == "tool":
            if message.get("tool_call_id") not in pending:
                raise ValueError("Session has an orphan or duplicate tool result")
            pending.remove(message["tool_call_id"])
        else:
            flush()
        repaired.append(message)
        if message.get("tool_calls"):
            pending = [call["id"] for call in message["tool_calls"]]
    flush()
    return repaired


def groups(messages):
    """Assistant calls and all their results must remain an indivisible group."""
    output = []
    for message in messages:
        if message["role"] == "tool" and output:
            output[-1].append(message)
        else:
            output.append([message])
    return output


class History:
    def __init__(self, directory: Path, root: Path, session_id=None):
        self.event_lock = threading.RLock()
        self.native_lock = None
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.session_id = session_id or new_id()
        if not all(c.isalnum() or c in "-_" for c in self.session_id):
            raise ValueError("Invalid session ID")
        self.path = directory / (self.session_id + ".jsonl")
        if session_id and not self.path.is_file():
            raise ValueError("Session not found")
        self.messages, self.plan = [], []
        self.meta = {"version": 1, "workspace": str(root.resolve())}
        self.file = open(os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600), "r+b")
        try:
            if os.name == "nt":
                from .windows import NamedLock
                self.native_lock = NamedLock("history:" + os.path.normcase(str(self.path.resolve())))
            if os.name == "posix":
                import fcntl
                try:
                    fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError("Session is already open in another process") from None
            if session_id:
                self._load()
                if self.meta["workspace"] != str(root.resolve()):
                    raise ValueError("Resume requires the original --workspace")
                repaired = repair_pending(self.messages)
                if repaired != self.messages:
                    self.replace(repaired, "interrupted tool recovery")
            else:
                self.event("meta", self.meta)
        except BaseException:
            self.file.close()
            if self.native_lock:
                self.native_lock.close()
            raise

    def _load(self):
        self.file.seek(0)
        # Discard only an unfinished final write; a corrupt complete record is an error.
        valid_end = 0
        saw_meta = False
        while line := self.file.readline():
            if not line.endswith(b"\n"):
                self.file.seek(valid_end)
                self.file.truncate()
                break
            event = json.loads(line)
            if event["type"] == "meta":
                self.meta = event["data"]
                saw_meta = True
            elif event["type"] == "message":
                self.messages.append(event["data"])
            elif event["type"] == "checkpoint":
                self.messages = event["data"]["messages"]
            elif event["type"] == "plan":
                self.plan = event["data"]
            valid_end = self.file.tell()
        if not saw_meta:
            raise ValueError("Session has no complete metadata record")
        self.file.seek(0, os.SEEK_END)

    def event(self, kind, data):
        with self.event_lock:
            self.file.seek(0, os.SEEK_END)
            self.file.write((json.dumps({"type": kind, "data": data}, ensure_ascii=False) + "\n").encode("utf-8"))
            self.file.flush()
            os.fsync(self.file.fileno())

    def append(self, message):
        self.event("message", message)
        self.messages.append(copy.deepcopy(message))

    def replace(self, messages, reason):
        self.event("checkpoint", {"reason": reason, "messages": messages})
        self.messages = copy.deepcopy(messages)

    def set_plan(self, plan):
        self.event("plan", plan)
        self.plan = copy.deepcopy(plan)

    def estimate(self):
        return tokens(self.messages)

    def read_log(self, offset=None, limit=6000):
        """Read a bounded UTF-8 journal excerpt without disturbing the writer.

        Offsets are bytes; None selects the tail. Never expose a partial write.
        """
        if (offset is not None and (type(offset) is not int or offset < 0)
                or type(limit) is not int or not 256 <= limit <= 24000):
            raise ValueError("Use offset >= 0 and limit 256..24000 bytes")
        with self.event_lock, self.path.open("rb") as source:
            size = os.fstat(source.fileno()).st_size
            start = max(0, size - limit) if offset is None else min(offset, size)
            source.seek(start)
            data = source.read(min(limit, size - start))
        # A tail can start inside a UTF-8 character. Returned cursors always end
        # at a character boundary so subsequent reads lose no characters.
        while data and data[0] & 0xC0 == 0x80:
            start += 1
            data = data[1:]
        decoder = codecs.getincrementaldecoder("utf-8")()
        text = decoder.decode(data, final=False)
        end = start + len(data) - len(decoder.getstate()[0])
        return {"offset": start, "next_offset": end, "total_bytes": size,
                "has_more": end < size, "text": text}

    def close(self):
        self.file.close()
        if self.native_lock:
            self.native_lock.close()
