"""One worker owns the agent, history and processes; the UI exchanges events only."""

from dataclasses import asdict, replace
import json
from queue import Queue, Empty
import threading
import time
from pathlib import Path

from .agent import Agent
from .config import endpoint, get_token, wizard, ensure_output_limit
from .events import EventBus, Cancelled
from .history import History
from .permissions import CommandPolicy
from .tools import Tools
from .transport import Client
from .util import private_json, terminal_text
from .workspace import latest_session
from .diagnostics import Diagnostics
from .settings import edit_limits, LIMITS, SANDBOX_FIELDS, SECURITY_FIELDS
from .usage import TurnUsage, summary_text

HELP = """/help — справка · /compact — сжать контекст · /continue — продолжить
/context — бюджет · /plan — план · /session — журнал · /permissions — разрешения
/settings — лимиты · /processes — процессы · /logs — ошибки · /exit — выход
Enter — отправить · Ctrl+J — новая строка · Esc — назад · Ctrl+G — прервать работу
Во время работы новые сообщения дополняют задачу перед следующим запросом модели. Tab — команды.
Файлы внутри проекта доверены. macOS/Linux: shell по умолчанию в песочнице,
её сеть закрыта. В полном доступе сеть открыта. Настройки → Песочница и сеть.
Windows: прежние подтверждения."""


class Runtime:
    def __init__(self, args, config, root, state_dir):
        self.args, self.config, self.root, self.state_dir = args, config, root, state_dir
        self.bus = EventBus()
        self.jobs = Queue()
        self.steering = Queue()
        self.controls = Queue()
        self.view_state = {}
        self.panel_action = None
        self.stopping = threading.Event()
        self.activity = threading.Event()
        self.thread = None
        self.agent = self.tools = self.history = self.client = self.policy = None
        self.token = ""
        self.turn_usage = None
        self.team = None
        self.owner_ident = threading.get_ident()
        self._state_lock = threading.Lock()
        self._generation = 0
        self.active = True  # Includes the startup wizard.
        self.diagnostics = Diagnostics(state_dir or (args.config_dir / "state" if args else Path(".")), self.safe)

    def report_error(self, error, where="agent"):
        return self.diagnostics.record(error, where)

    def safe(self, value):
        text = str(value)
        secrets = [self.token]
        if self.config:
            secrets += [self.config.proxy_password, self.config.proxy_second_password]
        for secret in sorted((value for value in secrets if value), key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return terminal_text(text)

    def emit(self, kind, **data):
        self.bus.emit(kind, **data)
        if kind in {"ready", "context", "plan", "tool_end", "idle"} and self.agent and threading.get_ident() == self.owner_ident:
            self.publish_state()

    def status(self, text):
        kind = "control_result" if self.agent is None else "action_status" if self.panel_action else "status"
        self.emit(kind, text=self.safe(text))

    def publish_state(self):
        self.view_state = dict(config=asdict(self.config), used=self.agent.estimate(),
            threshold=self.agent.threshold(), plan=json.loads(json.dumps(self.history.plan)),
            session=self.history.session_id, path=str(self.history.path), root=str(self.root),
            processes=self.tools.list_processes(),
            permissions=[json.loads(rule) for rule in sorted(self.policy.saved | self.policy.session)])

    def submit_action(self, action):
        if action not in {"continue", "compact"}:
            raise ValueError("Unknown agent action")
        self.submit({"action": action})

    def update_limits(self, values, save=False):
        if set(values) - (set(LIMITS) | SANDBOX_FIELDS | SECURITY_FIELDS):
            raise ValueError("Unknown limit")
        self.controls.put(("limits", dict(values), save))

    def clear_permissions(self):
        self.controls.put(("permissions", {}, False))

    def server_state(self):
        client = self.client
        if not client or not self.agent or client.url.hostname != "llm.chutes.ai":
            return None
        return client.servers.snapshot()

    def select_server(self, identifier):
        if self.server_state() is not None:
            self.client.servers.choose(identifier)

    def server_option(self, name, value):
        if self.server_state() is not None:
            self.client.servers.set_option(name, value)

    def refresh_servers(self):
        if self.server_state() is None:
            return
        owner = self.client
        registry = owner.servers
        with registry.lock:
            if registry.refreshing:
                return
            registry.refreshing = True
            registry.refresh_error = ""
        def refresh():
            # Dedicated connection: never interrupt an active generation or
            # mutate its crypto/attestation state from the UI thread.
            client = Client(replace(owner.config), owner.token)
            client.cancelled = self.stopping
            try:
                from .e2ee import ChutesEncryption
                engine = ChutesEncryption(client._encryption_metadata, client.check_cancelled)
                chute = engine.discover(client.config.model)
                registry.publish(chute, engine.pool, engine.pool_until)
            except Exception as error:
                with registry.lock:
                    registry.refresh_error = self.safe(error)
            finally:
                client.close()
                with registry.lock:
                    registry.refreshing = False
        threading.Thread(target=refresh, name="pycoder-server-list", daemon=True).start()

    def background_processes(self):
        if self.tools:
            return self.tools.processes.budget.snapshot()
        return {"active_count": 0, "limit": self.config.max_background_processes if self.config else 5, "running": []}

    def stop_background_process(self, identifier):
        def stop():
            try:
                self.tools.processes.budget.stop(identifier)
                self.emit("control_result", text="Процесс остановлен.")
            except Exception as error:
                self.emit("control_result", text=self.safe(error), error=True)
        threading.Thread(target=stop, name="pycoder-stop-process", daemon=True).start()

    def interrupt_subagent(self, target):
        if self.team:
            try:
                self.team.interrupt_agent(target)
            except ValueError as error:
                self.emit("control_result", text=self.safe(error), error=True)

    def apply_controls(self, during_shutdown=False):
        while during_shutdown or not self.stopping.is_set():
            try:
                action, values, save = self.controls.get_nowait()
            except Empty:
                return
            try:
                if action == "limits":
                    candidate = replace(self.config, **values)
                    candidate.validate()
                    if (not during_shutdown and candidate.e2ee_verify_attestation
                            and not self.config.e2ee_verify_attestation):
                        from .e2ee_setup import ensure
                        ensure(candidate, self.args.config_dir,
                               event=lambda **data: self.emit("e2ee_setup", **data), check_cancelled=self.bus.check)
                    if candidate.max_output_tokens != self.config.max_output_tokens:
                        from .config import limit_scope
                        candidate.output_limit_source = "manual"
                        candidate.output_limit_scope = limit_scope(candidate)
                    if save:
                        private_json(self.args.config_dir / "config.json", asdict(candidate))
                    for key, value in asdict(candidate).items():
                        setattr(self.config, key, value)
                    if self.client and set(values) & SECURITY_FIELDS:
                        self.client.reset_attestation()
                    message = "Настройки применены" + (" и сохранены." if save else " к сессии.") + " Уже запущенные процессы сохраняют прежние ограничения."
                else:
                    self.policy.clear()
                    message = "Запомненные разрешения удалены."
                self.publish_state()
                self.emit("control_result", text=message)
            except Exception as error:
                self.report_error(error, "settings")
                self.emit("control_result", text=self.safe(str(error)), error=True)

    def choose(self, title, options, default="", detail=""):
        return self.bus.request(title, options, default=default, detail=detail)

    def approve(self, kind, detail):
        if kind == "network":
            return self.choose("Сетевой доступ песочницы", [("deny", "Отклонить"), ("session", "Разрешить адрес до выхода"),
                ("remember", "Разрешить и сохранить в настройках")], default="deny", detail=self.safe(detail))
        if kind == "escalation":
            return self.choose("Запуск вне песочницы", [("deny", "Отклонить"), ("once", "Разрешить один раз")],
                               default="deny", detail=self.safe(detail))
        return self.choose("Разрешить команду?", [
            ("once", "Разрешить один раз"),
            ("session", "Разрешить эту команду до выхода"),
            ("remember", "Запомнить эту команду для проекта"),
            ("deny", "Отклонить")], default="once", detail=self.safe(detail))

    def ask(self, question):
        if isinstance(question, str):
            return self.bus.request(self.safe(question), allow_text=True)
        options = [(o["label"], o["label"] + (" — " + o["description"] if o.get("description") else ""))
                   for o in question["options"]]
        return self.bus.request(self.safe(question["question"]), options, allow_text=True)

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="pycoder-agent", daemon=True)
        self.thread.start()

    def submit(self, text):
        with self._state_lock:
            if not self.stopping.is_set():
                if self.active and self.agent is not None and isinstance(text, str) and not text.startswith("/"):
                    self.steering.put(text)
                else:
                    self.jobs.put((self._generation, text))
                self.activity.set()

    def drain_steering(self):
        messages = []
        while True:
            try:
                messages.append(self.steering.get_nowait())
            except Empty:
                break
        if messages:
            self.emit("steering_received", count=len(messages))
        return messages

    def cancel(self):
        with self._state_lock:
            self._generation += 1
            self.bus.cancelled.set()
            if self.client:
                self.client.cancel()
            if self.team:
                self.team.cancel_all()
            while True:
                try:
                    self.jobs.get_nowait()
                except Empty:
                    break
            while True:
                try:
                    self.steering.get_nowait()
                except Empty:
                    break
            if not self.active and not self.stopping.is_set():
                self.bus.cancelled.clear()
                self.emit("idle")

    def stop(self):
        self.stopping.set()
        self.cancel()

    def _catalog(self, config, token):
        self.token = token
        self.client = Client(config, token, self.status)
        self.client.cancelled = self.bus.cancelled
        return self.client.list_models()

    def _initialize(self):
        self.owner_ident = threading.get_ident()
        cfg, args = self.config, self.args
        configure = args.configure or not cfg.model
        if not configure and getattr(args, "local", None) is None:
            configure = self.choose("Модель", [("continue", "Продолжить"), ("configure", "Настроить модель")],
                                    detail=f"{cfg.model}\n{endpoint(cfg.base_url)}") == "configure"
        if configure:
            cfg = wizard(cfg, args.config_dir,
                         input_fn=lambda q: self.bus.request(q, allow_text=True),
                         secret_fn=lambda q: self.bus.request(q, secret=True, allow_text=True),
                         choose_fn=self.choose, fetch_models=self._catalog, print_fn=self.status)
        if args.no_stream:
            cfg.stream = False
        if args.yes and args.read_only:
            raise ValueError("Choose only one of --yes / --read-only")
        cfg.approval = "auto" if args.yes else "never" if args.read_only else cfg.approval
        if args.read_only:
            cfg.full_access = False
        cfg.validate()
        self.token = "" if args.no_auth else get_token(cfg, args.config_dir)
        if cfg.require_token and not self.token:
            self.token = self.bus.request("API-токен", secret=True, allow_text=True).strip()
            if not self.token:
                raise ValueError("Токен не задан")
            private_json(args.config_dir / "credentials.json", {"endpoint": endpoint(cfg.base_url), "token": self.token})
        if not args.configure:
            from .e2ee_setup import ensure
            ensure(cfg, args.config_dir, event=lambda **data: self.emit("e2ee_setup", **data),
                   check_cancelled=self.bus.check,
                   verify_attestation=cfg.e2ee_verify_attestation and not getattr(args, "e2ee_unverified", False))
        changed = ensure_output_limit(cfg, self.token,
            input_fn=lambda q: self.bus.request(q, allow_text=True), fetch_models=self._catalog,
            print_fn=self.status, explicit=args.max_output_tokens)
        cfg.validate()
        if changed and getattr(args, "local", None) is None:
            private_json(args.config_dir / "config.json", asdict(cfg))
        if args.configure:
            private_json(args.config_dir / "config.json", asdict(cfg))
            self.status("Конфигурация сохранена.")
            self.stopping.set()
            return
        if getattr(args, "e2ee_unverified", False):
            cfg.e2ee_verify_attestation = False
        session = args.resume
        if not session and not args.new:
            previous = latest_session(self.state_dir / "sessions", self.root)
            if previous and self.choose("Восстановить контекст прошлой сессии?", [
                    ("restore", "Восстановить"), ("new", "Начать новую сессию")],
                    detail=f"{self.root}\n{previous.session_id}") == "restore":
                session = previous.session_id
        self.history = History(self.state_dir / "sessions", self.root, session)
        self.history.event("provider", {"endpoint": endpoint(cfg.base_url), "model": cfg.model})
        self.policy = CommandPolicy(self.root, args.config_dir)
        self.tools = Tools(self.root, cfg, self.history, self.approve, self.ask, self.status)
        self.tools.sandbox_protected = [self.args.config_dir, self.state_dir]
        self.tools.policy, self.tools.event, self.tools.activity = self.policy, self.emit, self.activity
        self.tools.output_directory = self.state_dir / "tool-output" / self.history.session_id
        self.client = Client(cfg, self.token, self.status)
        from hashlib import sha256
        from .servers import Servers
        self.client.servers = Servers(self.state_dir / "server-stats" / (sha256(cfg.model.encode()).hexdigest() + ".json"))
        self.client.cancelled = self.bus.cancelled
        self.client.on_diagnostic = lambda error: self.report_error(error, "transport")
        self.client.on_metrics = lambda **data: self.emit("generation", **data)
        self.client.on_retry = lambda **data: self.emit("model_restart", automatic=True, **data)
        self.client.on_output = lambda **data: self.emit("model_output", received_at=time.monotonic(), **data)
        self.client.on_security = lambda **data: self.emit("model_security", **data)
        from .e2ee import fallback_question, background_question
        self.client.on_encryption_failure = lambda reason: fallback_question(self.choose, self.safe(reason), allow_direct=self.client.can_retry_attestation_direct)
        self.client.on_attestation_stale = lambda reason, recovered, stopped: background_question(
            self.bus.request, self.safe(reason), recovered, stopped, allow_direct=self.client.can_retry_attestation_direct)
        self.client.attestation_proactive = True
        self.client.start_attestation()
        self.client.on_usage = self.record_usage
        self.config = cfg
        self.agent = Agent(cfg, self.client, self.history, self.tools, self.status,
                           lambda text: self.emit("text", text=self.safe(text)), self.emit, self.bus.cancelled)
        self.agent.on_checkpoint = self.apply_controls
        self.agent.incoming = self.agent.before_finish = self.drain_steering
        from .team import Team
        self.team = Team(self.agent, self.state_dir, emit=self.emit, request=self.bus.request,
                         on_usage=self.record_usage, diagnostic=self.report_error)
        from .output_limits import resolve_output_limit
        self.agent.on_output_limit = lambda error: resolve_output_limit(
            error, self.choose, lambda q: self.bus.request(q, allow_text=True))
        self.emit("ready", model=cfg.model, root=str(self.root), session=self.history.session_id,
                  messages=list(self.history.messages), plan=list(self.history.plan), approval=cfg.approval)
        self.emit("context", used=self.agent.estimate(), window=cfg.context_window)

    def _job(self, text):
        if isinstance(text, dict):
            if text["action"] == "compact":
                self.agent.compact()
            else:
                self.agent.run(None)
            return
        if text == "/help":
            self.status(HELP)
        elif text == "/context":
            self.status(f"Оценка: {self.agent.estimate()} / {self.config.context_window}; сжатие при {self.agent.threshold()}")
        elif text == "/settings":
            edit_limits(self.config, self.args.config_dir, self.choose,
                        lambda q: self.bus.request(q, allow_text=True), self.status)
            self.emit("context", used=self.agent.estimate(), window=self.config.context_window)
        elif text == "/compact":
            self.agent.compact()
        elif text == "/plan":
            self.emit("plan", plan=list(self.history.plan))
        elif text == "/session":
            self.status(f"{self.history.session_id}\n{self.history.path}")
        elif text == "/logs":
            self.emit("error_log", text=self.diagnostics.tail(), path=str(self.diagnostics.path))
        elif text == "/processes":
            self.status(json.dumps(self.tools.list_processes(), ensure_ascii=False, indent=2))
        elif text == "/permissions":
            rules = "\n\n".join(f"cwd: {cwd}\nshell: {shell}\n{command}" for cwd, shell, command in
                                (json.loads(rule) for rule in sorted(self.policy.saved | self.policy.session)))
            decision = self.choose("Разрешения этого проекта", [("keep", "Оставить"), ("clear", "Удалить запомненные разрешения")],
                                   detail=self.safe(f"Сохранено: {len(self.policy.saved)}; до выхода: {len(self.policy.session)}\n{self.policy.path}\n\n{rules}"))
            if decision == "clear":
                self.policy.clear()
                self.status("Запомненные разрешения удалены.")
        elif text.startswith("/") and text != "/continue":
            self.status("Неизвестная команда. /help — справка.")
        else:
            self.agent.run(None if text == "/continue" else text)

    def record_usage(self, **data):
        if self.turn_usage is not None:
            self.turn_usage.add(**data)

    def _loop(self):
        try:
            self._initialize()
            with self._state_lock:
                self.active = False
            self.emit("idle")
            while not self.stopping.is_set():
                self.apply_controls()
                try:
                    generation, text = self.jobs.get(timeout=0.1)
                except Empty:
                    continue
                with self._state_lock:
                    if generation != self._generation or self.stopping.is_set():
                        continue
                    self.active = True
                    self.bus.cancelled.clear()
                if self.jobs.empty():
                    self.activity.clear()
                self.emit("busy", text=text)
                action = text.get("action") if isinstance(text, dict) else None
                self.panel_action = action if action == "compact" else None
                self.turn_usage = TurnUsage()
                outcome = "done"
                try:
                    self.bus.check()
                    self._job(text)
                except Cancelled:
                    outcome = "cancelled"
                    self.tools.close()
                    self.status("Прервано. Процессы остановлены; история сохранена. Tab → Продолжить задачу.")
                except Exception as error:
                    outcome = "error"
                    path = self.report_error(error)
                    self.emit("action_status" if self.panel_action else "error", text=self.safe(f"{type(error).__name__}: {error}\nИстория сохранена.\nЖурнал: {path or 'не удалось записать'}"))
                finally:
                    if self.team:
                        if self.team.running():
                            self.team.cancel_all()
                        self.team.join()
                    if action or not text.startswith("/") or text in {"/continue", "/compact"}:
                        summary = self.turn_usage.result(outcome)
                        if self.panel_action:
                            self.emit("action_status", text=summary_text(summary))
                        else:
                            self.emit("turn_summary", **summary)
                        self.history.event("turn_summary", summary)
                    self.turn_usage = None
                    self.panel_action = None
                    self.tools.compaction_requested = False
                    with self._state_lock:
                        self.active = False
                        while True:
                            try:
                                pending = self.steering.get_nowait()
                            except Empty:
                                break
                            if not self.stopping.is_set():
                                self.jobs.put((self._generation, pending))
                        if not self.stopping.is_set():
                            self.bus.cancelled.clear()
                    self.emit("idle")
        except Cancelled:
            pass
        except Exception as error:
            self.report_error(error, "startup")
            self.emit("error", text=self.safe(f"{type(error).__name__}: {error}"), fatal=True)
        finally:
            if self.team:
                self.team.close()
            if self.client:
                self.client.close()
            if self.agent:
                self.apply_controls(during_shutdown=True)
            if self.tools:
                self.tools.close()
            if self.history:
                self.history.close()
            self.emit("stopped")
