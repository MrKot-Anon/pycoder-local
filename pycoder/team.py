"""Local one-level agent team: isolated contexts, shared workspace, one provider.

Lifecycle and mailbox semantics adapted from Codex multi_agents_v2 (18344a972d).
No analytics, model discovery, remote services or additional dependencies.
"""

from dataclasses import dataclass, field, replace
from queue import Queue, Empty
import copy
import re
import threading
import time
import uuid

from .events import Cancelled
from .history import History, repair_pending
from .presentation import tool_label
from .transport import Client
from .util import truncate


@dataclass
class Member:
    id: str
    task_name: str
    agent: object
    history: object
    client: object
    tools: object
    cancelled: object
    inbox: object = field(default_factory=Queue)
    thread: object = None
    status: str = "running"
    phase: str = "Запуск"
    result: str = ""
    preview: str = ""
    plan: list = field(default_factory=list)
    last_emit: float = 0
    last_activity: float = field(default_factory=time.monotonic)
    tool_preview: str = ""


class Team:
    def __init__(self, parent, directory, *, emit=lambda *a, **kw: None,
                 request=None, on_usage=lambda **kw: None, diagnostic=lambda *a: None,
                 client_factory=Client):
        self.parent, self.directory = parent, directory
        self.emit, self.request = emit, request
        self.on_usage, self.diagnostic, self.client_factory = on_usage, diagnostic, client_factory
        self.members = {}
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.mailbox = Queue()
        self.parent.tools.team = self
        self.parent.incoming = self.drain
        self.parent.before_finish = self.finish_parent
        self.parent.system += ("\nYou may delegate independent subtasks using spawn_agent. All agents share this workspace: "
            "assign disjoint files explicitly, do not overwrite another agent's changes. File patches are serialized and "
            "checked for stale contents; shell commands are NOT isolated. Read tools in the same response may run in parallel. "
            "The root owns integration and validation. Use wait_agent and inspect results; never claim success from a spawn ID. "
            "Subagents use the same configured model, have separate histories, and cannot spawn grandchildren. "
            "Use get_agent_result to retrieve long results in chunks. "
            "Use get_agent_log(target) to inspect a child's journal, live text, plan and activity while it is running. "
            "Use next_offset for incremental reads; omit offset for the latest tail. "
            "Never read agent journals with shell/tail/wc: state files are protected by the sandbox. "
            "Silence alone does not prove a child is stuck: inspect its phase and log before steering or interrupting it. "
            "Do not delegate a trivial task.")

    def _member(self, target):
        with self.lock:
            member = self.members.get(target) or next((m for m in self.members.values() if m.task_name == target), None)
            if member is None:
                raise ValueError("Unknown agent; use list_agents")
            return member

    def snapshot(self):
        with self.lock:
            return [{"id": m.id, "task_name": m.task_name, "status": m.status, "phase": m.phase,
                     "preview": m.preview, "result": truncate(m.result, 4000), "plan": copy.deepcopy(m.plan),
                     "history": str(m.history.path)} for m in self.members.values()]

    def publish(self):
        self.emit("agents", agents=self.snapshot())

    def spawn_agent(self, task_name, message, fork_turns="none"):
        if not re.fullmatch(r"[a-z0-9_]{1,48}", task_name) or not message.strip():
            raise ValueError("Use task_name with 1..48 lowercase letters/digits/underscores and a nonempty message")
        if fork_turns not in {"none", "all"} and not (fork_turns.isdecimal() and 1 <= int(fork_turns) <= 100):
            raise ValueError("fork_turns must be none, all or 1..100")
        from .agent import Agent
        from .tools import Tools, TOOL_SPECS, COLLAB_NAMES
        with self.lock:
            if self.parent.cancelled.is_set():
                raise Cancelled()
            if sum(m.status == "running" for m in self.members.values()) >= self.parent.config.max_subagents:
                raise ValueError("Concurrent subagent limit reached; wait for an agent or change settings")
            if len(self.members) >= 64:
                raise ValueError("This session already contains 64 agents; reuse followup_task")
            if any(m.task_name == task_name for m in self.members.values()):
                raise ValueError("Task name already exists; use followup_task")
            cfg = replace(self.parent.config)
            agent_id = task_name + "-" + uuid.uuid4().hex[:8]
            history = History(self.directory / "agents" / self.parent.history.session_id, self.parent.tools.root)
            try:
                if fork_turns != "none":
                    source = copy.deepcopy(self.parent.history.messages)
                    if fork_turns != "all":
                        indices = [i for i, m in enumerate(source) if m["role"] == "user"]
                        source = source[indices[-int(fork_turns)]:] if len(indices) >= int(fork_turns) else source
                    history.replace(repair_pending(source), "fork from parent")
                cancelled = threading.Event()
                def question(title, options=(), **kwargs):
                    if self.request is None:
                        raise ValueError("Subagent needs user input; return the question to the parent agent")
                    return self.request(f"{task_name}: {title}", options, cancelled=cancelled, **kwargs)
                def approve(kind, detail):
                    if self.request is None:
                        return False
                    if kind == "network":
                        return question("Сетевой доступ песочницы", [("deny", "Отклонить"), ("session", "Разрешить адрес до выхода"),
                            ("remember", "Разрешить и сохранить в настройках")], default="deny", detail=detail)
                    if kind == "escalation":
                        return question("Запуск вне песочницы", [("deny", "Отклонить"), ("once", "Разрешить один раз")], default="deny", detail=detail)
                    return question("Разрешить команду?", [("once", "Один раз"), ("session", "До выхода"),
                        ("remember", "Запомнить"), ("deny", "Отклонить")], detail=detail)
                def ask(q):
                    if isinstance(q, str):
                        return question(q, allow_text=True)
                    return question(q["question"], [(o["label"], o["label"] + " — " + o.get("description", "")) for o in q["options"]], allow_text=True)
                status = lambda text: self.update(agent_id, "status", text=text)
                client = self.client_factory(cfg, self.parent.client.token, status)
                client.servers = self.parent.client.servers
                client.cancelled = cancelled
                client.on_usage = self.on_usage
                client.on_diagnostic = lambda error: self.diagnostic(error, f"subagent:{task_name}")
                client.on_security = lambda **data: self.update(agent_id, "status", text="Защита модели: " + data["detail"])
                if self.request:
                    from .e2ee import fallback_question, background_question
                    client.on_encryption_failure = lambda reason: fallback_question(question, reason)
                    def background_request(title, options, **kwargs):
                        kwargs.pop("cancelled", None)  # recovered() also observes worker shutdown.
                        return question(title, options, **kwargs)
                    client.on_attestation_stale = lambda reason, recovered, stopped: background_question(
                        background_request, reason, recovered, stopped)
                tools = Tools(self.parent.tools.root, cfg, history, approve, ask, status)
                tools.policy = self.parent.tools.policy
                tools.mutation_lock = self.parent.tools.mutation_lock
                tools.sandbox_protected = list(self.parent.tools.sandbox_protected)
                tools.sandbox_config = lambda: self.parent.config
                tools.processes.budget = self.parent.tools.processes.budget
                tools.processes.owner = task_name
                tools.output_directory = self.directory / "tool-output" / history.session_id
                tools.event = lambda kind, **data: self.update(agent_id, kind, **data)
                agent = Agent(cfg, client, history, tools, status,
                    lambda text: self.update(agent_id, "text", text=text), tools.event, cancelled)
                agent.specs = [spec for spec in TOOL_SPECS if spec["function"]["name"] not in COLLAB_NAMES]
                agent.system += (f"\nYou are subagent {task_name}. Complete only the assigned task and report your changes, "
                    "tests, limitations and exact paths to the parent. Other agents share this workspace. "
                    "Respect the assigned file ownership; inspect files before editing and preserve other agents' work. "
                    "Your subprocesses stop when your task ends; ask the parent to launch persistent services. "
                    "Do not spawn further agents. If blocked, describe the blocker explicitly.")
                if self.request:
                    from .output_limits import resolve_output_limit
                    agent.on_output_limit = lambda error: resolve_output_limit(error,
                        lambda title, options, **kw: question(title, options, **kw), lambda title: question(title, allow_text=True))
                member = Member(agent_id, task_name, agent, history, client, tools, cancelled)
                agent.incoming = lambda: self._drain_inbox(member)
                agent.before_finish = agent.incoming
                self.members[agent_id] = member
                self._launch(member, message)
            except BaseException:
                history.close()
                raise
        self.publish()
        return {"agent_id": agent_id, "task_name": task_name, "status": "running"}

    def _launch(self, member, message):
        member.cancelled.clear()
        member.status, member.phase = "running", "Запуск"
        member.result, member.preview = "", ""
        member.tool_preview = ""
        member.last_activity = time.monotonic()
        member.thread = threading.Thread(target=self._run, args=(member, message), name="pycoder-" + member.id, daemon=True)
        member.thread.start()

    def _run(self, member, message):
        result, status = "Agent stopped unexpectedly", "error"
        try:
            result = member.agent.run(message)
            status = "completed"
        except Cancelled:
            result, status = "Работа прервана; проверьте выполненные изменения по журналу.", "interrupted"
        except Exception as error:
            self.diagnostic(error, "subagent:" + member.task_name)
            result, status = f"{type(error).__name__}: {error}", "error"
        finally:
            try:
                if isinstance(member.client, Client):
                    member.client.close()
                member.tools.close()
            except Exception as error:
                result, status = f"Process cleanup failed: {error}", "error"
                self.diagnostic(error, "subagent:" + member.task_name)
        with self.changed:
            member.result, member.status, member.phase = result or "", status, status
            member.last_activity = time.monotonic()
            self.mailbox.put(f"Subagent {member.id} ({status}):\n{truncate(member.result, 4000)}\nInspect with get_agent_log(target={member.id!r}).")
            self.changed.notify_all()
        self.publish()

    def update(self, agent_id, kind, **data):
        with self.lock:
            member = self.members.get(agent_id)
            if member is None:
                return
            member.last_activity = time.monotonic()
            if kind == "text":
                member.preview = (member.preview + data["text"])[-8000:]
            elif kind == "plan":
                member.plan = copy.deepcopy(data["plan"])
            elif kind == "tool_start":
                member.phase = tool_label(data["name"], data["arguments"])
                member.tool_preview = ""
            elif kind == "tool_output":
                member.tool_preview = str(data.get("tail", ""))[-2000:]
            elif kind == "tool_end":
                member.tool_preview = str(data.get("output", ""))[-2000:]
            elif kind in {"status", "error"}:
                member.phase = str(data.get("text", ""))[:200]
            elif kind == "model_start":
                member.phase, member.preview = "Модель отвечает", ""
            elif kind == "model_waiting":
                member.phase = "Ожидается решение пользователя"
            now = time.monotonic()
            if now - member.last_emit < 0.1 and kind in {"text", "tool_output"}:
                return
            member.last_emit = now
        self.publish()

    @staticmethod
    def _drain_inbox(member):
        messages = []
        while True:
            try:
                messages.append(member.inbox.get_nowait())
            except Empty:
                return messages

    def drain(self):
        messages = []
        while True:
            try:
                messages.append(self.mailbox.get_nowait())
            except Empty:
                return messages

    def running(self):
        with self.lock:
            return any(m.status == "running" for m in self.members.values())

    def finish_parent(self):
        messages = self.drain()
        if self.running():
            self.emit("waiting_agents")
        while self.running():
            self.wait_agent(timeout_ms=1000)
            messages.extend(self.drain())
        return messages + self.drain()

    def wait_agent(self, timeout_ms=30000):
        if not 1 <= timeout_ms <= 60000:
            raise ValueError("timeout_ms must be 1..60000")
        deadline = time.monotonic() + timeout_ms / 1000
        with self.changed:
            while self.running() and self.mailbox.empty() and time.monotonic() < deadline:
                if self.parent.cancelled.is_set():
                    raise Cancelled()
                self.changed.wait(min(0.1, max(0, deadline - time.monotonic())))
        return {"agents": self.list_agents()["agents"], "timed_out": self.running() and self.mailbox.empty()}

    def list_agents(self):
        with self.lock:
            now = time.monotonic()
            return {"agents": [{"id": m.id, "task_name": m.task_name,
                "status": m.status, "phase": m.phase,
                "seconds_since_activity": round(now - m.last_activity, 1),
                "log_tool": "get_agent_log"} for m in self.members.values()]}

    def get_agent_log(self, target, offset=None, limit=6000):
        member = self._member(target)
        with self.lock:
            now = time.monotonic()
            state = {"agent_id": member.id, "status": member.status, "phase": member.phase,
                     "seconds_since_activity": round(now - member.last_activity, 1),
                     "plan": copy.deepcopy(member.plan), "live_text": member.preview[-2000:],
                     "tool_output": member.tool_preview}
        # No caller-controlled path: only members owned by this live team.
        state["journal"] = member.history.read_log(offset, limit)
        return state

    def send_message(self, target, message):
        member = self._member(target)
        if not message.strip():
            raise ValueError("Message must not be empty")
        member.inbox.put("Message from parent: " + message)
        return {"queued": True, "agent_id": member.id, "status": member.status,
                "note": "An idle agent requires followup_task to run again"}

    def followup_task(self, target, message):
        member = self._member(target)
        if member.status == "running":
            return self.send_message(target, message)
        member.thread.join()
        with self.lock:
            if self.running() and sum(m.status == "running" for m in self.members.values()) >= self.parent.config.max_subagents:
                raise ValueError("Concurrent subagent limit reached")
            if not message.strip() or self.parent.config.max_subagents == 0:
                raise ValueError("Provide a message and enable subagents in settings")
            self._launch(member, message)
        self.publish()
        return {"agent_id": member.id, "status": "running"}

    def interrupt_agent(self, target):
        member = self._member(target)
        member.cancelled.set()
        member.client.cancel()
        return {"agent_id": member.id, "status": member.status, "interruption_requested": True}

    def get_agent_result(self, target, offset=0, limit=12000):
        if offset < 0 or not 1 <= limit <= 64000:
            raise ValueError("Use offset >=0 and limit 1..64000")
        member = self._member(target)
        with self.lock:
            return {"status": member.status, "total_chars": len(member.result),
                    "offset": offset, "text": member.result[offset:offset + limit]}

    def cancel_all(self):
        with self.lock:
            members = list(self.members.values())
        for member in members:
            member.cancelled.set()
            member.client.cancel()

    def join(self):
        with self.lock:
            threads = [m.thread for m in self.members.values()]
        for thread in threads:
            thread.join()

    def close(self):
        self.cancel_all()
        self.join()
        for member in self.members.values():
            member.history.close()
