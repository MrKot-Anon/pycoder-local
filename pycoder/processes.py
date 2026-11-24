"""Resumable subprocesses, bounded output, PTY and process-group cancellation."""

import codecs
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import sys
import shutil

from .util import truncate
from .events import Cancelled
from .platform_support import shell_args, display_argv

MAX_BUFFER = 1024 * 1024
MAX_SESSIONS = 16


class ProcessBudget:
    """Shared live-process quota and thread-safe UI snapshot for the entire team."""
    def __init__(self, limit=5):
        self.configured_limit = limit
        self.lock = threading.RLock()
        self.records = {}
        self.next_id = 1

    @property
    def limit(self):
        return self.configured_limit() if callable(self.configured_limit) else self.configured_limit

    def _prune(self):
        for identifier, record in list(self.records.items()):
            if record["process"] is not None and record["process"].done:
                del self.records[identifier]

    def reserve(self, owner, command, cwd, session_id):
        with self.lock:
            self._prune()
            if len(self.records) >= self.limit:
                raise ValueError(f"Лимит фоновых процессов: {len(self.records)}/{self.limit} для всей команды агентов. "
                                 "Дождитесь завершения или остановите ненужный процесс; не перезапускайте уже работающую команду.")
            identifier = self.next_id
            self.next_id += 1
            self.records[identifier] = {"id": identifier, "owner": owner, "command": display_argv(command) if isinstance(command, list) else command,
                "cwd": str(cwd), "session_id": session_id, "started": time.monotonic(), "process": None}
            return identifier

    def attach(self, identifier, process):
        with self.lock:
            self.records[identifier]["process"] = process

    def release(self, identifier):
        with self.lock:
            self.records.pop(identifier, None)

    def snapshot(self):
        with self.lock:
            self._prune()
            sessions = [{key: value for key, value in record.items() if key not in {"process", "started"}}
                        | {"pid": record["process"].proc.pid if record["process"] else None,
                           "elapsed": max(0, int(time.monotonic() - record["started"]))}
                        for record in self.records.values()]
            return {"active_count": len(sessions), "limit": self.limit, "running": sessions}

    def stop(self, identifier):
        with self.lock:
            record = self.records.get(identifier)
            process = record["process"] if record else None
        if process is None:
            raise ValueError("Процесс уже завершён или ещё запускается")
        process.stop()


class Process:
    def __init__(self, command, cwd, shell, tty, timeout, journal=None, sandbox=None):
        self.sandbox = sandbox
        env = dict(os.environ)
        for key in ("PYCODER_API_KEY", "CHUTES_API_KEY"):
            env.pop(key, None)
        self.master = None
        self.buffer = ""
        self.dropped = 0
        self.lock = threading.Condition()
        self.done = False
        self.timed_out = False
        self.job = None
        self.stop_lock = threading.Lock()
        self.journal = journal
        if journal:
            journal.streamed = True
        args = command if isinstance(command, list) else shell_args(command, shell)
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        if sandbox:
            args, env = sandbox.prepare(args, cwd)
        if tty:
            if os.name != "posix":
                raise ValueError("PTY is supported only on POSIX")
            import pty
            master, slave = pty.openpty()
            # A slave fd is not automatically a controlling terminal after
            # setsid(). Acquire it in a tiny isolated launcher before entering
            # the OS sandbox, so /dev/tty works for ssh and password prompts.
            # Avoid preexec_fn: commands are launched from a multithreaded UI.
            python = getattr(sys, "_base_executable", None) or sys.executable
            gate = ("import fcntl,os,sys,termios; "
                    "fcntl.ioctl(0,termios.TIOCSCTTY,0); "
                    "os.execvpe(sys.argv[1],sys.argv[1:],os.environ)")
            try:
                self.proc = subprocess.Popen([python, "-I", "-S", "-c", gate, *args], cwd=cwd, env=env, stdin=slave,
                                             stdout=slave, stderr=slave, start_new_session=True)
            except BaseException:
                os.close(master)
                raise
            finally:
                os.close(slave)
            self.master = master
        elif os.name == "nt":
            from .windows import Job
            executable = shutil.which(args[0])
            if not executable:
                raise FileNotFoundError(f"Executable not found: {args[0]}")
            args = [executable, *args[1:]]
            # No command can spawn descendants before it is assigned to the job.
            gate = "import os,sys; os.read(0,1); os.execv(sys.argv[1],sys.argv[1:])"
            self.proc = subprocess.Popen([sys.executable, "-u", "-c", gate, *args], cwd=cwd, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            try:
                self.job = Job(self.proc)
                self.proc.stdin.write(b"\n")
                self.proc.stdin.flush()
            except BaseException:
                if self.job:
                    self.job.close()
                self.proc.kill()
                self.proc.wait()
                self.proc.stdin.close()
                self.proc.stdout.close()
                raise
        else:
            self.proc = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         start_new_session=os.name == "posix")
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.timer = threading.Timer(timeout, self._timeout)
        self.timer.daemon = True
        self.timer.start()

    def _read(self):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        fd = self.master if self.master is not None else self.proc.stdout.fileno()
        try:
            while True:
                try:
                    chunk = os.read(fd, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if self.journal:
                    self.journal.append(text)
                with self.lock:
                    self.buffer += text
                    if len(self.buffer) > MAX_BUFFER:
                        self.dropped += len(self.buffer) - MAX_BUFFER
                        self.buffer = self.buffer[-MAX_BUFFER:]
                    self.lock.notify_all()
        finally:
            final = decoder.decode(b"", final=True)
            with self.lock:
                self.buffer += final
            self.proc.wait()
            if self.journal:
                self.journal.append(final, final=True, exit_code=self.proc.returncode)
            with self.lock:
                self.done = True
                self.lock.notify_all()

    def _timeout(self):
        if not self.done:
            self.timed_out = True
            self.stop()

    def stop(self):
        if self.job:
            with self.stop_lock:
                self.job.close()
            self.proc.wait()
            return
        try:
            if os.name == "posix":
                os.killpg(self.proc.pid, signal.SIGTERM)
            elif self.proc.poll() is None:
                self.proc.terminate()
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        # Also stop descendants when the shell itself has already exited.
        if os.name == "posix":
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif self.proc.poll() is None:
            self.proc.kill()

    def output(self, wait_ms, budget, cancelled=None):
        deadline = time.monotonic() + min(max(wait_ms, 0), 30000) / 1000
        with self.lock:
            while not self.done and time.monotonic() < deadline:
                if cancelled and cancelled.is_set():
                    raise Cancelled()
                self.lock.wait(min(0.1, max(0, deadline - time.monotonic())))
            text, self.buffer = self.buffer, ""
            if self.dropped:
                text = f"[discarded {self.dropped} characters of earlier output]\n" + text
                self.dropped = 0
            result = {"output": truncate(text, budget), "exit_code": self.proc.poll(),
                      "running": not self.done, "timed_out": self.timed_out}
            if self.sandbox:
                result["sandbox"] = self.sandbox.details()
            return result

    def write(self, text):
        data = text.encode("utf-8")
        if self.done:
            raise ValueError("Process has already finished")
        if self.master is not None:
            os.write(self.master, data)
        else:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def close(self):
        self.timer.cancel()
        self.stop()
        self.reader.join(timeout=2)
        if self.master is not None:
            os.close(self.master)
        for stream in (self.proc.stdin, self.proc.stdout):
            if stream:
                stream.close()
        if self.sandbox:
            self.sandbox.close()


class Processes:
    def __init__(self, limit=5):
        self.sessions = {}
        self.next_id = 1
        self.cancelled = None
        self.budget = ProcessBudget(limit)
        self.owner = "Основной агент"

    def start(self, command, cwd: Path, *, shell=None, tty=False, timeout=300,
              yield_time_ms=1000, max_output_tokens=4000, journal=None, sandbox=None):
        if len(self.sessions) >= MAX_SESSIONS:
            raise ValueError("Too many running processes; poll or terminate existing sessions")
        fallback = tty and os.name == "nt"
        identifier = self.budget.reserve(self.owner, command, cwd, self.next_id)
        try:
            process = Process(command, cwd, shell, tty and not fallback, min(max(timeout, 1), 3600), journal, sandbox)
        except BaseException:
            self.budget.release(identifier)
            if sandbox:
                sandbox.close()
            raise
        process.budget_id = identifier
        self.budget.attach(identifier, process)
        session_id = self.next_id
        self.next_id += 1
        self.sessions[session_id] = process
        result = self.poll(session_id, yield_time_ms=yield_time_ms, max_output_tokens=max_output_tokens)
        if fallback:
            result["notice"] = "Windows: using pipes with interactive stdin; ConPTY is not implemented."
        return result

    def poll(self, session_id, chars="", yield_time_ms=1000, max_output_tokens=4000, terminate=False):
        if session_id not in self.sessions:
            raise ValueError("Unknown process session; processes do not survive agent restart")
        process = self.sessions[session_id]
        if terminate:
            process.stop()
        elif chars:
            process.write(chars)
        output = process.output(yield_time_ms, max_output_tokens, self.cancelled)
        if output["running"]:
            output["session_id"] = session_id
        else:
            process.close()
            self.budget.release(process.budget_id)
            del self.sessions[session_id]
        return output

    def close(self):
        for process in self.sessions.values():
            process.close()
            self.budget.release(process.budget_id)
        self.sessions.clear()
