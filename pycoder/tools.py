"""Tool schemas and local dispatch. No network clients in this module."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
import shlex
import base64

from .patch import PreparedPatch
from .processes import Processes
from .util import truncate, workspace_path
from .events import Cancelled
from .plans import step_schema, validate_plan, plan_lines
from .prompts import APPLY_PATCH
from .tool_output import OutputJournal
from .platform_support import default_shell, display_argv


def schema(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(required), "additionalProperties": False}}}


def arg(kind, description):
    return {"type": kind, "description": description}


TOOL_SPECS = [
    schema("read_file", 'Read UTF-8 text inside the workspace, or any readable path in full access mode, returning total_lines, numbered content, and applicable scoped AGENTS.md instructions. start_line is 1-based, limit counts lines (1..2000, default 200); files above 8 MiB are rejected. Use focused ranges. Line numbers are display metadata and must not be copied into patches.',
           {"path": arg("string", "File path; absolute paths outside the workspace require full access"), "start_line": arg("integer", "First line, 1-based"),
            "limit": arg("integer", "Maximum lines, default 200, max 2000")}, ["path"]),
    schema("list_files", 'List paths in a subtree or a single file. Full access mode permits paths outside the workspace and returns absolute paths for them. Does not follow directory symlinks; excludes .git, .venv, venv, node_modules, __pycache__, target and .pycoder. Limit 1..2000, default 300. This does not read file contents; use read_file for that.',
           {"path": arg("string", "Subdirectory, default ."), "limit": arg("integer", "Maximum files, default 300")}),
    schema("search", 'Search for literal text in files, using rg when installed and a local fallback otherwise. Full access mode permits paths outside the workspace. pattern is a literal string, not a regular expression. Returns matching paths/lines with a bounded result limit. Use a focused path and read_file around matches before editing.',
           {"pattern": arg("string", "Literal text"), "path": arg("string", "File or directory, default ."),
            "limit": arg("integer", "Maximum matching lines, default 100")}, ["pattern"]),
    schema("apply_patch", APPLY_PATCH,
           {"patch": arg("string", "Complete Codex patch text including Begin/End markers. Every Add File content line must start with +, including blank lines. See the tool description and grammar.")}, ["patch"]),
    schema("exec_command", 'Runs a command, returning output or a session ID for ongoing interaction. On macOS/Linux, commands run inside the configured OS sandbox by default without approval; sandbox network defaults to disabled. In full access mode, commands have unrestricted network access with the user\'s OS permissions. For a justified retry outside the sandbox explicitly set sandbox_permissions=require_escalated and provide justification: the runtime asks before execution unless the user explicitly enabled full_access. Never treat a generic nonzero exit or Permission denied as proof of sandbox blocking; consider partial side effects before retry. A session_id means execution continues: poll with write_stdin. Windows retains ordinary command approvals without an OS sandbox.',
           {"cmd": arg("string", 'Shell command to execute.'), "workdir": arg("string", "Workspace directory, default ."),
            "tty": arg("boolean", 'True allocates a PTY for interactive commands such as ssh password prompts; send input using write_stdin. False or omitted uses plain pipes.'), "shell": arg("string", "Shell executable; default /bin/sh on POSIX, PowerShell on Windows"),
            "timeout": arg("integer", "Lifetime limit in seconds, default 300, max 3600"),
            "yield_time_ms": arg("integer", "Wait before returning, default 1000, max 30000"),
            "max_output_tokens": arg("integer", "Output budget, capped by configuration"),
            "sandbox_permissions": arg("string", "use_default (default) or require_escalated"),
            "justification": arg("string", "Specific reason for requesting execution outside the sandbox")}, ["cmd"]),
    schema("write_stdin", 'Writes characters to an existing exec session and returns recent output. Omit chars or use an empty string to poll without writing. Use only a session_id returned by exec_command, shell, or list_processes. terminate=true stops the process group. Further polls return new output, not the entire transcript. A completed process returns exit_code.',
           {"session_id": arg("integer", 'Identifier of the running exec session.'), "chars": arg("string", 'Characters to write to stdin. Defaults to empty, which polls without writing.'),
            "yield_time_ms": arg("integer", 'Wait before yielding output. PyCoder defaults to 1000 ms and caps waits at 30000 ms.'),
            "max_output_tokens": arg("integer", "Output budget"), "terminate": arg("boolean", "Stop process")}, ["session_id"]),
    schema("update_plan", 'Updates the task plan. Provide an optional explanation and a list of plan items, each with a step and status. At most one sibling step can be in_progress at a time. PyCoder additionally supports children, up to three levels and 90 total steps (30 per sibling list); active children require active ancestors, completed parents require completed children. Update immediately after completing each step or substep and BEFORE starting the next; do not batch status updates for independent steps at the end. A parent and its last child may complete together. Before the final answer reconcile statuses with actual tool results; preserve unfinished steps and explain blockers instead of falsely marking them completed. Update the remaining plan when scope changes. Do not make a plan for a trivial task.',
           {"plan": {"type": "array", "items": step_schema()},
            "explanation": arg("string", 'Optional explanation for this plan update.')}, ["plan"]),
    schema("request_user_input", 'Request user input for one to three short questions and wait for the response. Available during normal work; no Plan mode restriction. Use only for missing information, preferences, constraints or clarification. Prefer one question. Provide 2-3 mutually exclusive choices; put the recommended choice first and label it (Recommended). Do not add an Other option: the UI supplies free text. Answers are returned by question id. Use questions for structured choices; question is a legacy single-text alternative.',
           {"question": arg("string", "Single question, for compatibility"),
            "questions": {"type": "array", "items": {"type": "object", "properties": {
                "id": arg("string", 'Stable identifier for mapping answers (snake_case).'), "header": arg("string", 'Short header label shown in the UI (12 or fewer chars).'),
                "question": arg("string", 'Single-sentence prompt shown to the user.'), "options": {"type": "array", "items": {
                    "type": "object", "properties": {"label": arg("string", 'User-facing label (1-5 words).'),
                    "description": arg("string", 'One short sentence explaining impact/tradeoff if selected.')}, "required": ["label", "description"],
                    "additionalProperties": False}}}, "required": ["id", "question", "options"],
                    "additionalProperties": False}}}),
    schema("get_context_remaining", 'Get the remaining tokens in the current context window. PyCoder returns tokens_left_approx, an estimate of available input before automatic compaction, not an exact tokenizer count.', {}),
    schema("shell", "Run an explicit argv command without implicit shell parsing. Uses the same approval policy as exec_command.",
           {"command": {"type": "array", "items": {"type": "string"}},
            "workdir": arg("string", "Workspace directory"), "timeout_ms": arg("integer", "Lifetime limit in milliseconds"),
            "sandbox_permissions": arg("string", "use_default (default) or require_escalated"),
            "justification": arg("string", "Reason for execution outside sandbox; approval required unless full_access is enabled")}, ["command"]),
    schema("current_time", "Return the current UTC time (Codex clock.curr_time).", {}),
    schema("request_network_access", "In sandbox mode, ask the user to allow one exact host:port for future sandbox commands via HTTP/HTTPS proxy. Full access mode already permits network connections; do not call this tool there. Does not retry any command. Localhost and LAN still require their separate settings inside the sandbox. The user may approve for the session or save to config.",
           {"host": arg("string", "Exact host:port, e.g. pypi.org:443, no URL or wildcard"),
            "justification": arg("string", "Why this endpoint is needed")}, ["host", "justification"]),
    schema("sleep", "Wait without blocking the interface; cancelled by Esc or interrupted by queued input.",
           {"duration_ms": arg("integer", "1..43200000 milliseconds")}, ["duration_ms"]),
    schema("new_context", 'Start a new context window. Does not clear, reset, or otherwise affect environment state. In PyCoder this schedules same-model summarization after the current tool batch; the durable journal, current plan and running process IDs are preserved. Do not repeat completed actions after the checkpoint.', {}),
    schema("list_processes", "List running command sessions so they can be polled or stopped.", {}),
    schema("view_image", 'View a local image file from the filesystem when visual inspection is needed. Full access mode permits paths outside the workspace. PyCoder accepts PNG/JPEG/GIF/WebP images up to 4 MiB each, at most four per batch; it attaches the image to the next request to the configured model. Requires vision support. No URL fetching, detail parameter or image hosting is provided.',
           {"path": arg("string", "Image path")}, ["path"]),
]

COLLAB_NAMES = {"spawn_agent", "send_message", "followup_task", "wait_agent", "list_agents", "interrupt_agent", "get_agent_result", "get_agent_log"}
TOOL_SPECS += [
    schema("spawn_agent", "Start an independent subagent on the SAME configured model and shared workspace. Returns immediately. Assign disjoint files and a concrete task. Separate context/history; child tools cannot spawn further agents. Child subprocesses stop at task completion. The parent must integrate and verify results. Default fork_turns=none; all or a positive number copies parent context. Default concurrency is three children; configurable in settings.",
           {"task_name": arg("string", "Unique lowercase task name: letters, digits, underscores"), "message": arg("string", "Task and file ownership"), "fork_turns": arg("string", "none, all, or 1..100")}, ["task_name", "message"]),
    schema("send_message", "Queue steering for an existing subagent; delivered between steps. Does not start an idle agent.",
           {"target": arg("string", "Agent ID or task name"), "message": arg("string", "Instructions")}, ["target", "message"]),
    schema("followup_task", "Continue an idle/interrupted subagent with its existing history; if running, queue the message.",
           {"target": arg("string", "Agent ID or task name"), "message": arg("string", "Follow-up task")}, ["target", "message"]),
    schema("wait_agent", "Wait for subagent completion or mailbox activity. Reports statuses; completed results are also injected into parent context at the next step. Never assume a timeout means completion.",
           {"timeout_ms": arg("integer", "1..60000, default 30000")}),
    schema("list_agents", "List subagents and their statuses. Running threads cannot survive application restart; journals are preserved.", {}),
    schema("interrupt_agent", "Request cancellation of a subagent's current model request and tools. Other agents continue. Use followup_task to restart it once interrupted.",
           {"target": arg("string", "Agent ID or task name")}, ["target"]),
    schema("get_agent_result", "Read the final result of a subagent in chunks. Status must be completed for success; error/interrupted are not success.",
           {"target": arg("string", "Agent ID or task name"), "offset": arg("integer", "Character offset, default 0"), "limit": arg("integer", "Characters, default 12000, max 64000")}, ["target"]),
    schema("get_agent_log", "Inspect an owned subagent while running or after completion: phase, seconds since activity, plan, live assistant text, latest tool output and a bounded JSONL journal excerpt (messages, tool calls/results, checkpoints). Default is the latest tail; it may begin mid-record. Use journal.next_offset to read new data or offset=0 to read from the beginning. Offsets and limit are UTF-8 bytes. No shell, filesystem path, sandbox escalation or approval is needed. Silence alone does not mean the agent is stuck. Available only for this session's team.",
           {"target": arg("string", "Agent ID or task name, never a file path"), "offset": arg("integer", "Byte offset >=0; omit for latest tail"), "limit": arg("integer", "Journal bytes, default 6000, range 256..24000; reduce if tool output is truncated")}, ["target"]),
]

EXCLUDE = {".git", ".venv", "venv", "node_modules", "__pycache__", "target", ".pycoder"}


class Tools:
    def __init__(self, root, config, history, approve, ask, emit):
        self.root, self.config, self.history = root.resolve(), config, history
        self.approve, self.ask, self.emit = approve, ask, emit
        self.processes = Processes(limit=lambda: self.config.max_background_processes)
        self.local = threading.local()
        self.instruction_lock = threading.RLock()
        self.mutation_lock = threading.RLock()
        self.team = None
        self.output_directory = None
        self.current_call_id = None
        self.output_journal = None
        self.instructions_seen = set()
        self.context_remaining = lambda: None
        self.policy = None
        self.event = lambda kind, **data: None
        self.cancelled = threading.Event()
        self.activity = threading.Event()
        self.compaction_requested = False
        self.pending_images = []
        self.sandbox_protected = []
        self.sandbox_config = lambda: self.config

    @property
    def current_call_id(self):
        return getattr(self.local, "call_id", None)

    @current_call_id.setter
    def current_call_id(self, value):
        self.local.call_id = value

    @property
    def output_journal(self):
        return getattr(self.local, "journal", None)

    @output_journal.setter
    def output_journal(self, value):
        self.local.journal = value

    def instructions(self, path):
        with self.instruction_lock:
            return self._instructions(path)

    def _instructions(self, path):
        if not path.is_relative_to(self.root):
            return ""
        directory = path if path.is_dir() else path.parent
        paths = [self.root]
        if directory != self.root:
            paths += list(reversed([p for p in directory.parents if p.is_relative_to(self.root) and p != self.root]))
            paths.append(directory)
        found = []
        for base in paths:
            candidate = base / "AGENTS.md"
            if candidate.is_file():
                candidate = workspace_path(self.root, str(candidate))
                signature = (str(candidate), candidate.stat().st_mtime_ns)
                if signature not in self.instructions_seen:
                    with candidate.open("rb") as stream:
                        body = stream.read(48000).decode("utf-8", errors="replace")
                    found.append(f"Instructions from {candidate.relative_to(self.root)}:\n{truncate(body, 3000)}")
                    self.instructions_seen.add(signature)
        return "\n\n".join(found)

    def _allow(self, kind, detail):
        if self.sandbox_config().approval == "never":
            return False
        if self.sandbox_config().approval == "auto":
            return True
        if kind == "patch":
            return True  # PreparedPatch validates every path against this trusted workspace.
        return self.approve(kind, detail)

    def _read_path(self, path):
        if self.sandbox_config().full_access:
            return (self.root / path).resolve()
        return workspace_path(self.root, path)

    def _display_path(self, path):
        return str(path.relative_to(self.root)) if path.is_relative_to(self.root) else str(path)

    def execute(self, name, arguments):
        journal = (OutputJournal(self.output_directory, self.current_call_id, self.event)
                   if self.output_directory and self.current_call_id else None)
        if journal and journal.path:
            self.history.event("tool_output", {"id": self.current_call_id, "name": name, "path": str(journal.path)})
        self.output_journal = journal
        try:
            args = json.loads(arguments)
            spec = next((s["function"] for s in TOOL_SPECS if s["function"]["name"] == name), None)
            if spec is None:
                raise ValueError(f"Unknown tool: {name}")
            self._validate_args(args, spec["parameters"])
            if self.cancelled.is_set():
                raise Cancelled()
            if name in COLLAB_NAMES:
                if self.team is None:
                    raise ValueError("Subagent tools are unavailable in this agent")
                result = getattr(self.team, name)(**args)
            elif name in {"apply_patch", "exec_command", "shell", "request_network_access"}:
                with self.mutation_lock:
                    if self.cancelled.is_set():
                        raise Cancelled()
                    result = getattr(self, name)(**args)
            else:
                result = getattr(self, name)(**args)
            if journal:
                journal.finish_result(result)
            return truncate(json.dumps(result, ensure_ascii=False), self.config.tool_output_tokens)
        except (ValueError, OSError, TypeError, KeyError, subprocess.SubprocessError) as error:
            if journal:
                journal.append(str(error), final=True)
            return json.dumps({"error": str(error)}, ensure_ascii=False)
        finally:
            self.output_journal = None

    @staticmethod
    def _validate_args(args, spec):
        if not isinstance(args, dict):
            raise ValueError("Tool arguments must be a JSON object")
        unknown = set(args) - set(spec["properties"])
        missing = set(spec["required"]) - set(args)
        if unknown or missing:
            raise ValueError(f"Unknown arguments: {sorted(unknown)}; missing: {sorted(missing)}")
        types = {"string": str, "integer": int, "boolean": bool, "array": list}
        for key, value in args.items():
            expected = types[spec["properties"][key]["type"]]
            if type(value) is not expected:
                raise ValueError(f"{key} must be {expected.__name__}")

    def read_file(self, path, start_line=1, limit=200):
        target = self._read_path(path)
        if start_line < 1 or not 1 <= limit <= 2000:
            raise ValueError("Invalid line range")
        if target.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("File exceeds 8 MiB; use a targeted command")
        lines = target.read_text(encoding="utf-8").splitlines()
        return {"instructions": self.instructions(target), "total_lines": len(lines),
                "content": "\n".join(f"{i + start_line}: {line}" for i, line in enumerate(lines[start_line - 1:start_line - 1 + limit]))}

    def _files(self, path):
        if path.is_file():
            yield path
            return
        if not path.is_dir():
            raise ValueError("Directory not found")
        for current, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDE and not (Path(current) / d).is_symlink())
            for name in sorted(files):
                candidate = Path(current) / name
                if not candidate.is_symlink():
                    yield candidate

    def list_files(self, path=".", limit=300):
        if not 1 <= limit <= 2000:
            raise ValueError("limit must be between 1 and 2000")
        result = []
        for candidate in self._files(self._read_path(path)):
            if len(result) == limit:
                return {"files": result, "truncated": True}
            result.append(self._display_path(candidate))
        return {"files": result, "truncated": False}

    def search(self, pattern, path=".", limit=100):
        if not pattern or not 1 <= limit <= 1000:
            raise ValueError("Use nonempty pattern and limit 1..1000")
        target = self._read_path(path)
        matches = []
        # Read a bounded pipe instead of capture_output (which can grow without bound).
        if shutil.which("rg"):
            timed_out = threading.Event()
            proc = subprocess.Popen(["rg", "--no-config", "--color", "never", "--line-number", "--fixed-strings",
                                     "--max-columns", "2000", "--max-count", str(limit), "--", pattern, str(target)],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            def stop_search():
                if proc.poll() is None:
                    timed_out.set()
                    proc.kill()

            timer = threading.Timer(10, stop_search)
            timer.start()
            try:
                while len(matches) < limit:
                    raw = proc.stdout.readline(16384)
                    if not raw:
                        break
                    matches.append(raw.decode("utf-8", errors="replace").rstrip())
            finally:
                timer.cancel()
                if proc.poll() is None:
                    proc.terminate()
                proc.wait()
                proc.stdout.close()
            if timed_out.is_set():
                return {"matches": matches, "truncated": True, "note": "Search stopped after 10 seconds; narrow the path"}
            if proc.returncode == 2 and not matches:
                raise ValueError("rg could not search this path; check permissions and path")
        else:
            scanned = 0
            for candidate in self._files(target):
                if self.cancelled.is_set():
                    raise Cancelled()
                scanned += 1
                if scanned > 10000:
                    return {"matches": matches, "truncated": True, "note": "Install rg for larger repositories"}
                if candidate.stat().st_size > 1024 * 1024:
                    continue
                try:
                    text = candidate.read_text(encoding="utf-8")
                except (UnicodeError, OSError):
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    if pattern in line:
                        matches.append(f"{self._display_path(candidate)}:{number}:{line[:2000]}")
                        if len(matches) >= limit:
                            break
                if len(matches) >= limit:
                    break
        return {"matches": matches, "truncated": len(matches) >= limit}

    def apply_patch(self, patch):
        prepared = PreparedPatch(self.root, patch)
        instructions = "\n".join(filter(None, (self.instructions(path) for path in prepared.after)))
        if instructions:
            return {"instructions": instructions, "applied": False,
                    "next": "Review these scoped instructions and call apply_patch again if compliant."}
        if not self._allow("patch", prepared.diff()):
            return {"denied": True, "applied": False}
        result = prepared.apply()
        self.event("patch", diff=prepared.diff(), changed=result["changed"])
        return result

    def exec_command(self, cmd, workdir=".", tty=False, shell="", timeout=300,
                     yield_time_ms=1000, max_output_tokens=4000, sandbox_permissions="use_default", justification=""):
        return self._exec(cmd, workdir, tty, shell or default_shell(), timeout, yield_time_ms, max_output_tokens,
                          sandbox_permissions=sandbox_permissions, justification=justification)

    def _exec(self, cmd, workdir, tty, shell, timeout, yield_time_ms, max_output_tokens, argv=None,
              sandbox_permissions="use_default", justification=""):
        from .sandbox import Sandbox, SandboxUnavailable, supported
        from .config import config_dir
        cfg = self.sandbox_config()
        cwd = ((self.root / Path(workdir).expanduser()).resolve() if cfg.full_access
               else workspace_path(self.root, workdir))
        if sandbox_permissions not in {"use_default", "require_escalated"}:
            raise ValueError("sandbox_permissions must be use_default or require_escalated")
        if cfg.approval == "never":
            return {"denied": True}
        if cfg.full_access:
            if self.cancelled.is_set():
                raise Cancelled()
            result = self.processes.start(argv if argv is not None else cmd, cwd, shell=shell, tty=tty, timeout=timeout,
                yield_time_ms=yield_time_ms, max_output_tokens=max(1, min(max_output_tokens, self.config.tool_output_tokens)),
                journal=self.output_journal)
            result["sandbox"] = {"backend": "none", "full_access": True}
            return result
        if cfg.sandbox_enabled and cfg.proxy_enabled and cfg.sandbox_network != "direct":
            if sandbox_permissions == "require_escalated" or not supported():
                return {"error": "SOCKS включён: запуск без сетевой изоляции запрещён", "command_started": False}
        sandbox = None
        if sandbox_permissions == "require_escalated":
            if not justification.strip():
                raise ValueError("Explain why execution outside sandbox is necessary in justification")
            decision = self.approve("escalation", "ВНЕ ПЕСОЧНИЦЫ: обычные права пользователя, доступ к файлам и сети.\n"
                                    f"cwd: {cwd}\nshell: {shell}\nКоманда: {cmd}\nПричина: {justification}")
            if decision not in {True, "once"}:
                return {"denied": True, "sandbox_permissions": sandbox_permissions}
        elif cfg.sandbox_enabled and supported():
            protected = [config_dir(), self.history.path.parent, *self.sandbox_protected]
            if self.policy:
                protected.append(self.policy.path.parent.parent)
            if self.output_directory:
                protected.append(self.output_directory)
            sandbox = Sandbox(self.root, cfg, protected)
        if sandbox or sandbox_permissions == "require_escalated":
            if self.cancelled.is_set():
                raise Cancelled()
            try:
                result = self.processes.start(argv if argv is not None else cmd, cwd, shell=shell, tty=tty, timeout=timeout,
                    yield_time_ms=yield_time_ms, max_output_tokens=max(1, min(max_output_tokens, self.config.tool_output_tokens)),
                    journal=self.output_journal, sandbox=sandbox)
                if sandbox is None:
                    result["sandbox"] = {"backend": "none", "user_approved_escalation": True}
                return result
            except SandboxUnavailable as error:
                return {"error": str(error), "sandbox_error": "unavailable", "command_started": False,
                        "hint": "Do not retry automatically outside sandbox. Ask via require_escalated with justification if needed."}
        from .safe_reads import trusted_read
        safe_command = trusted_read(cmd, self.root, cwd, shell, argv)
        key = self.policy.key(cmd, cwd, shell) if self.policy else None
        if cfg.approval == "never":
            return {"denied": True}
        if safe_command is None and cfg.approval != "auto" and not (self.policy and self.policy.allows(key)):
            decision = self.approve("command", f"cwd: {cwd}\nshell: {shell}\n{cmd}")
            if decision not in {True, "once", "session", "remember"}:
                return {"denied": True}
            if self.policy and decision in {"session", "remember"}:
                self.policy.remember(key, permanent=decision == "remember")
        if self.cancelled.is_set():
            raise Cancelled()
        if safe_command is not None:
            if argv is None:
                cmd = safe_command
            else:
                argv = safe_command
        return self.processes.start(argv if argv is not None else cmd, cwd, shell=shell, tty=tty, timeout=timeout,
                                    yield_time_ms=yield_time_ms,
                                    max_output_tokens=max(1, min(max_output_tokens, self.config.tool_output_tokens)),
                                    journal=self.output_journal)

    def write_stdin(self, session_id, chars="", yield_time_ms=1000, max_output_tokens=4000, terminate=False):
        if chars and not terminate and self.sandbox_config().approval == "never":
            return {"denied": True}
        return self.processes.poll(session_id, chars, yield_time_ms,
                                   max(1, min(max_output_tokens, self.config.tool_output_tokens)), terminate)

    def update_plan(self, plan, explanation=""):
        validate_plan(plan)
        self.history.set_plan(plan)
        self.event("plan", plan=plan)
        self.emit("\n".join(plan_lines(plan)))
        return {"updated": True}

    def request_user_input(self, question=None, questions=None):
        if question is not None and questions is None:
            return {"answer": self.ask(question)}
        if not isinstance(questions, list) or not 1 <= len(questions) <= 3:
            raise ValueError("Provide question or 1..3 questions")
        ids = set()
        for q in questions:
            if (not isinstance(q, dict) or not isinstance(q.get("id"), str) or not q["id"]
                    or q["id"] in ids or not isinstance(q.get("question"), str)
                    or not isinstance(q.get("options"), list) or len(q["options"]) > 5):
                raise ValueError("Invalid question")
            ids.add(q["id"])
            for option in q["options"]:
                if not isinstance(option, dict) or not isinstance(option.get("label"), str) or not isinstance(option.get("description", ""), str):
                    raise ValueError("Invalid question option")
        return {"answers": {q["id"]: {"answers": [self.ask(q)]} for q in questions}}

    def shell(self, command, workdir=".", timeout_ms=300000, sandbox_permissions="use_default", justification=""):
        if not command or any(not isinstance(part, str) or "\x00" in part for part in command):
            raise ValueError("command must be a nonempty array of strings")
        return self._exec(display_argv(command), workdir, False, "[direct argv]", max(1, timeout_ms // 1000), 1000, self.config.tool_output_tokens,
                          argv=command, sandbox_permissions=sandbox_permissions, justification=justification)

    def current_time(self):
        return {"current_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}

    def request_network_access(self, host, justification):
        from dataclasses import asdict
        from .network_policy import host_port
        from .util import private_json
        from .config import config_dir
        from .sandbox import supported
        parsed = host_port(host)
        cfg = self.sandbox_config()
        if cfg.approval == "never":
            return {"denied": True}
        if cfg.full_access:
            return {"allowed": True, "route": "direct", "note": "Full access: no sandbox network restrictions"}
        if not justification.strip():
            raise ValueError("Provide justification")
        if not supported() or not cfg.sandbox_enabled:
            return {"error": "OS sandbox is not enabled; host rules cannot restrict this platform/mode"}
        if cfg.sandbox_network == "direct" or (cfg.sandbox_network == "allowlist" and parsed in {host_port(h) for h in cfg.sandbox_direct_hosts}):
            return {"allowed": True, "route": "direct", "note": "Explicit direct network policy"}
        if cfg.sandbox_network == "all" or (cfg.sandbox_network == "allowlist" and parsed in {host_port(h) for h in cfg.sandbox_hosts}):
            return {"allowed": True, "note": ("All IP networking allowed" if cfg.sandbox_network == "all" and not cfg.proxy_enabled
                                              else "Localhost/LAN restrictions still apply")}
        decision = self.approve("network", f"Разрешить {host} для новых команд песочницы?\nПричина: {justification}\n"
            "Только через HTTP/HTTPS-прокси. Файловые ограничения сохраняются. Localhost/LAN настраиваются отдельно.")
        if decision not in {"session", "remember"}:
            return {"denied": True}
        from dataclasses import replace
        draft = replace(cfg, sandbox_network="allowlist", sandbox_hosts=[*cfg.sandbox_hosts, host])
        draft.validate()
        if decision == "remember":
            directory = self.policy.path.parent.parent if self.policy else config_dir()
            private_json(directory / "config.json", asdict(draft))
        cfg.sandbox_hosts = draft.sandbox_hosts
        cfg.sandbox_network = draft.sandbox_network
        return {"allowed": True, "host": host, "scope": decision, "note": "Applies to new processes only. No command was retried. Localhost/LAN restrictions still apply."}

    def sleep(self, duration_ms):
        if not 1 <= duration_ms <= 43200000:
            raise ValueError("duration_ms must be 1..43200000")
        start = time.monotonic()
        while time.monotonic() - start < duration_ms / 1000:
            if self.cancelled.wait(min(0.1, max(0, duration_ms / 1000 - (time.monotonic() - start)))):
                raise Cancelled()
            if self.activity.is_set():
                break
        return {"wall_time_seconds": time.monotonic() - start, "interrupted": self.activity.is_set()}

    def new_context(self):
        self.compaction_requested = True
        return {"scheduled": True}

    def list_processes(self):
        return {"sessions": [{"session_id": sid, "pid": p.proc.pid, "running": not p.done}
                             for sid, p in list(self.processes.sessions.items())],
                "team": self.processes.budget.snapshot()}

    def view_image(self, path):
        target = self._read_path(path)
        with target.open("rb") as stream:
            data = stream.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024 or len(self.pending_images) >= 8:
            raise ValueError("Images are limited to 4 MiB each and 4 per tool batch")
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif data.startswith((b"GIF87a", b"GIF89a")):
            mime = "image/gif"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            raise ValueError("Unsupported image format; use PNG, JPEG, GIF or WebP")
        name = self._display_path(target)
        self.pending_images.extend([{"type": "text", "text": f"Local image from view_image: {name}"},
                                    {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + base64.b64encode(data).decode()}}])
        return {"attached": name, "mime_type": mime, "bytes": len(data)}

    def get_context_remaining(self):
        return {"tokens_left_approx": self.context_remaining()}

    def close(self):
        self.processes.close()
