"""Interactive CLI and one-shot execution."""

import argparse
from dataclasses import asdict
from datetime import datetime
import getpass
import json
import os
from pathlib import Path
import sys

from .agent import Agent
from . import __version__
from .config import config_dir, endpoint, get_token, load, wizard, ensure_output_limit
from .history import History
from .install import DEFAULT_INSTALL_DIR, install
from .storage import state_directory
from .tools import Tools
from .transport import Client, ModelError
from .util import private_json, terminal_text, truncate
from .workspace import WorkspaceLock, latest_session
from .permissions import CommandPolicy
from .diagnostics import Diagnostics


def parser():
    p = argparse.ArgumentParser(description="Python coding agent: Chutes or your Chat Completions endpoint.")
    p.add_argument("--version", action="version", version=f"pycoder {__version__}")
    distribution = p.add_mutually_exclusive_group()
    distribution.add_argument("--unpack", nargs="?", const=Path("pycoder-source"), type=Path, metavar="DIR",
                              help="Restore editable sources, tests, builder and dependencies into a new directory (default: ./pycoder-source)")
    distribution.add_argument("--install", nargs="?", const=DEFAULT_INSTALL_DIR, type=Path, metavar="DIR",
                   help="Install this bundle as pycoder, replacing an existing version (default: /usr/local/bin)")
    distribution.add_argument("--setup-e2ee", action="store_true",
                   help="Install Chutes encryption and Intel/NVIDIA verification libraries into a private Python environment")
    p.add_argument("prompt", nargs="?", help="Run one task and exit")
    p.add_argument("--workspace", "-C", type=Path, default=Path.cwd())
    p.add_argument("--config-dir", type=Path, default=config_dir())
    p.add_argument("--state-dir", type=Path, default=os.environ.get("PYCODER_STATE_DIR"),
                   help="History directory; default ~/.local/state/pycoder with a writable config/state fallback")
    p.add_argument("--configure-proxy", action="store_true", help="Configure SOCKS and DNS before any network access, save and exit")
    p.add_argument("--configure", action="store_true", help="Configure model and credentials")
    p.add_argument("--model", help="Explicit model ID (Chutes setup can load the catalog)")
    p.add_argument("--base-url", help="Base URL or complete /chat/completions URL")
    p.add_argument("--локал", "--local", dest="local", nargs="?", const="127.0.0.1:8080", metavar="ADDRESS",
                   help="Direct local model, no proxy or API key; default 127.0.0.1:8080; discovers model automatically")
    p.add_argument("--context-window", type=int)
    p.add_argument("--max-output-tokens", type=int)
    p.add_argument("--max-steps", type=int)
    p.add_argument("--generation-timeout-seconds", type=int, help="Maximum idle wait for model headers/data (default 600), not total generation time")
    p.add_argument("--e2ee-unverified", action="store_true", help="Encrypt Chutes requests without Intel/NVIDIA attestation; server key is not verified")
    p.add_argument("--no-stream", action="store_true")
    p.add_argument("--plain", action="store_true", help="Use the simple text interface instead of Textual")
    p.add_argument("--no-auth", action="store_true", help="Use a custom server without a token")
    p.add_argument("--yes", action="store_true", help="Auto-approve ordinary commands when sandbox is disabled; never bypass sandbox or explicit escalation/network approvals")
    p.add_argument("--read-only", action="store_true", help="Deny patches, shell commands and process input")
    sessions = p.add_mutually_exclusive_group()
    sessions.add_argument("--resume", metavar="SESSION_ID")
    sessions.add_argument("--new", action="store_true", help="Start a new session without the restore question")
    p.add_argument("--list-sessions", action="store_true")
    p.add_argument("--logs", action="store_true", help="Follow the error log independently; no model or workspace lock")
    return p


def main(argv=None):
    if os.name == "nt":
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    history, tools, workspace_lock, client = None, None, None, None
    interactive = sys.stdin.isatty()
    team = None
    token = ""
    cfg = None
    diagnostics = None

    def safe(text):
        secrets = [token]
        if cfg:
            secrets += [cfg.proxy_password, cfg.proxy_second_password]
        for secret in sorted((value for value in secrets if value), key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return terminal_text(text)

    def emit(text):
        print("\n" + safe(str(text)), file=sys.stderr, flush=True)

    def approve(kind, detail):
        if not interactive:
            emit(f"Отклонено ({kind}): нужен интерактивный запуск.")
            return False
        emit(f"Разрешение: {kind}\n{truncate(detail, 12000)}")
        if kind == "network":
            answer = input("1 — разрешить адрес до выхода; 2 — сохранить в настройках; Enter — отказ: ").strip()
            return {"1": "session", "2": "remember"}.get(answer, False)
        if kind == "escalation":
            return "once" if input("ВНЕ ПЕСОЧНИЦЫ. 1 — разрешить один раз; Enter — отказ: ").strip() == "1" else False
        answer = input("1 — один раз; 2 — до выхода; 3 — запомнить для проекта; Enter — отказ: ").strip().lower()
        return {"1": "once", "2": "session", "3": "remember", "y": "once", "yes": "once", "д": "once", "да": "once"}.get(answer, False)

    def ask(question):
        if not interactive:
            return "No interactive user is available. Report the missing information."
        if isinstance(question, dict):
            options = question.get("options", [])
            emit(question["question"] + "\n" + "\n".join(f"{i}. {o['label']} — {o.get('description', '')}" for i, o in enumerate(options, 1)))
            answer = input("> ")
            if answer.isdecimal() and 1 <= int(answer) <= len(options):
                return options[int(answer) - 1]["label"]
            return answer
        return input(safe(question) + "\n> ")

    try:
        if args.configure_proxy:
            from .proxy import configure, label, save as save_proxy
            cfg = load(args.config_dir)
            def choose_proxy(title, options):
                print(title)
                for key, text in options:
                    print(f"  {key}: {text}")
                while True:
                    value = input("> ").strip()
                    if value in dict(options):
                        return value
            configure(cfg, input, getpass.getpass, choose_proxy)
            save_proxy(cfg, args.config_dir)
            print(label(cfg) or "Прокси выключен")
            return 0
        if args.setup_e2ee:
            from .e2ee_setup import setup
            setup(args.config_dir)
            return 0
        if args.unpack is not None:
            from .unpack import unpack
            unpack(args.unpack)
            return 0
        if args.install is not None:
            install(args.install)
            return 0
        if argv is None and args.local is None:
            from .e2ee_setup import activate
            activate(args.config_dir)
        if args.logs:
            from .logview import follow
            return follow(state_directory(args.state_dir, args.config_dir, create=False) / "errors.log")
        if args.list_sessions:
            state_dir = state_directory(args.state_dir, args.config_dir, create=False)
            for path in sorted((state_dir / "sessions").glob("*.jsonl"), reverse=True)[:50]:
                print(path.stem)
            return 0
        root = args.workspace.resolve()
        if not root.is_dir():
            raise ValueError("Workspace is not a directory")
        if not args.configure:
            workspace_lock = WorkspaceLock(root)
            state_dir = state_directory(args.state_dir, args.config_dir, emit)
            diagnostics = Diagnostics(state_dir, safe)
        cfg = load(args.config_dir, include_proxy=args.local is None)
        if args.local is not None:
            from .local import prepare_local
            prepare_local(cfg, args, emit)
        from .proxy import validate as validate_proxy, label as proxy_label
        validate_proxy(cfg)
        if cfg.proxy_enabled:
            emit(proxy_label(cfg))
        for name in ("model", "base_url", "context_window", "max_output_tokens", "max_steps", "generation_timeout_seconds"):
            if getattr(args, name) is not None:
                setattr(cfg, name, getattr(args, name))
        if args.local is None and (args.e2ee_unverified or not cfg.e2ee_verify_attestation):
            emit("E2EE БЕЗ ПРОВЕРКИ АТТЕСТАЦИИ · сервер не проверен")
        if args.no_auth:
            cfg.require_token = False
        if interactive and sys.stdout.isatty() and args.prompt is None and not args.plain:
            try:
                from .tui import run_interactive
            except ImportError as error:
                raise ValueError("Textual недоступен. Используйте полную сборку или установите зависимости проекта; --plain запускает текстовый режим.") from error
            return run_interactive(args, cfg, root, None if args.configure else state_dir)
        if args.configure or not cfg.model:
            if not interactive:
                raise ValueError("Run interactively to configure, or provide --model and credentials in the environment")
            cfg = wizard(cfg, args.config_dir)
        elif interactive and args.prompt is None and args.local is None:
            print(safe(f"Модель: {cfg.model}\nСервер: {endpoint(cfg.base_url)}"))
            if input("Enter — продолжить; c — настроить модель: ").strip().lower() == "c":
                cfg = wizard(cfg, args.config_dir)
        if args.no_stream:
            cfg.stream = False
        if args.yes and args.read_only:
            raise ValueError("Choose only one of --yes / --read-only")
        cfg.approval = "auto" if args.yes else "never" if args.read_only else cfg.approval
        if args.read_only:
            cfg.full_access = False
        cfg.validate()
        token = "" if args.no_auth else get_token(cfg, args.config_dir)
        if cfg.require_token and not token:
            if not interactive:
                raise ValueError("Missing token: set CHUTES_API_KEY (Chutes) or PYCODER_API_KEY (custom endpoint)")
            token = getpass.getpass("API-токен (ввод скрыт): ").strip()
            if not token:
                raise ValueError("Токен не задан")
            private_json(args.config_dir / "credentials.json", {"endpoint": endpoint(cfg.base_url), "token": token})
        if not args.configure:
            from .e2ee_setup import ensure
            ensure(cfg, args.config_dir, event=lambda **data: emit(data["text"]),
                   verify_attestation=cfg.e2ee_verify_attestation and not args.e2ee_unverified)
        changed = ensure_output_limit(cfg, token, input_fn=input if interactive else None,
                                      print_fn=emit, explicit=args.max_output_tokens)
        cfg.validate()
        if changed and args.local is None:
            private_json(args.config_dir / "config.json", asdict(cfg))
        if args.configure:
            private_json(args.config_dir / "config.json", asdict(cfg))
            print("Конфигурация сохранена.")
            return 0
        if args.e2ee_unverified:
            cfg.e2ee_verify_attestation = False
        session_id = args.resume
        if not session_id and not args.new and interactive:
            previous = latest_session(state_dir / "sessions", root)
            if previous:
                stamp = datetime.fromtimestamp(previous.modified).strftime("%Y-%m-%d %H:%M")
                answer = input(f"Восстановить контекст прошлой сессии этой папки ({previous.session_id}, {stamp})? [Y/n]: ").strip().lower()
                while answer not in {"", "y", "yes", "д", "да", "n", "no", "н", "нет"}:
                    answer = input("Введите y (восстановить) или n (новая сессия): ").strip().lower()
                if answer in {"", "y", "yes", "д", "да"}:
                    session_id = previous.session_id
        history = History(state_dir / "sessions", root, session_id)
        if session_id:
            emit(f"Контекст восстановлен: {len(history.messages)} сообщений. /continue — продолжить задачу.")
        history.event("provider", {"endpoint": endpoint(cfg.base_url), "model": cfg.model})
        tools = Tools(root, cfg, history, approve, ask, emit)
        tools.sandbox_protected = [args.config_dir, state_dir]
        tools.policy = CommandPolicy(root, args.config_dir)
        client = Client(cfg, token, emit)
        client.on_diagnostic = lambda error: diagnostics.record(error, "transport")
        client.on_security = lambda **data: emit(f"Защита модели: {data['detail']}")

        def on_text(text):
            print(safe(text), end="", flush=True)

        agent = Agent(cfg, client, history, tools, emit, on_text)
        from .usage import TurnUsage, summary_text
        from .team import Team
        team = Team(agent, state_dir, on_usage=lambda **data: client.on_usage(**data),
                    diagnostic=diagnostics.record)

        def measured(action):
            usage = TurnUsage()
            client.on_usage = usage.add
            outcome = "done"
            try:
                return action()
            except KeyboardInterrupt:
                outcome = "cancelled"
                raise
            except Exception:
                outcome = "error"
                raise
            finally:
                if team.running():
                    team.cancel_all()
                team.join()
                data = usage.result(outcome)
                emit(summary_text(data))
                history.event("turn_summary", data)
                client.on_usage = lambda **data: None

        if interactive:
            from .output_limits import resolve_output_limit
            def limit_choice(title, options, default="", detail=""):
                emit(title + "\n" + detail)
                for index, (_, label) in enumerate(options, 1):
                    emit(f"{index}. {label}")
                while True:
                    answer = input("Номер варианта [1]: ").strip() or "1"
                    if answer.isdecimal() and 1 <= int(answer) <= len(options):
                        return options[int(answer) - 1][0]
            agent.on_output_limit = lambda error: resolve_output_limit(error, limit_choice, input)
            from .e2ee import fallback_question
            client.on_encryption_failure = lambda reason: fallback_question(limit_choice, safe(reason), allow_direct=client.can_retry_attestation_direct)
            def background_choice(reason, recovered, stopped):
                from .e2ee import background_question
                def request(title, options, **kwargs):
                    emit(title + "\n" + kwargs["detail"])
                    for index, (_, label) in enumerate(options, 1):
                        emit(f"{index}. {label}")
                    while not stopped.is_set():
                        if recovered():
                            emit("Аттестация успешна. Продолжаю автоматически.")
                            return "recovered"
                        if os.name == "nt":
                            import msvcrt
                            if not msvcrt.kbhit():
                                stopped.wait(0.1)
                                continue
                            answer = msvcrt.getwch()
                        else:
                            import select
                            if not select.select([sys.stdin], [], [], 0.1)[0]:
                                continue
                            answer = sys.stdin.readline()
                            if not answer:
                                return "stop"
                            answer = answer.strip()
                        if answer.isdecimal() and 1 <= int(answer) <= len(options):
                            return options[int(answer) - 1][0]
                    return "stop"
                return background_question(request, safe(reason), recovered, stopped,
                                           allow_direct=client.can_retry_attestation_direct)
            client.on_attestation_stale = background_choice
        client.start_attestation()
        emit(f"Сессия: {history.session_id}\nИстория: {history.path}\nПроект: {root}\nМодель: {cfg.model}\nСервер: {endpoint(cfg.base_url)}")
        if cfg.full_access:
            emit("ПОЛНЫЙ ДОСТУП · без песочницы и подтверждений команд · права текущего пользователя")
        elif cfg.approval == "auto":
            emit("Автоматическое разрешение команд включено. Shell работает с правами пользователя, без OS sandbox.")
        if args.prompt is not None:
            measured(lambda: agent.run(args.prompt))
            print()
            return 0
        if not interactive:
            raise ValueError("Provide a prompt argument for noninteractive execution")
        emit("/help — команды; /exit — выход. Ctrl+C прерывает текущую работу.")
        while True:
            try:
                text = input("\nВы> ").strip()
            except EOFError:
                break
            except KeyboardInterrupt:
                print()
                continue
            if not text:
                continue
            if text in {"/exit", "/quit"}:
                break
            try:
                if text == "/help":
                    emit("/settings — лимиты\n/compact — сжать контекст\n/continue — продолжить текущую задачу\n/context — бюджет\n/plan — план\n/session — ID журнала\n/exit — выход\nСервер и модель: перезапуск с --configure; новая сессия: перезапуск с --new.")
                elif text == "/settings":
                    from .settings import edit_limits
                    edit_limits(cfg, args.config_dir, limit_choice, input, emit)
                elif text == "/compact":
                    measured(agent.compact)
                elif text == "/context":
                    emit(f"Оценка: {agent.estimate()} токенов; порог сжатия: {agent.threshold()}; окно: {cfg.context_window}")
                elif text == "/plan":
                    emit(json.dumps(history.plan, ensure_ascii=False, indent=2))
                elif text == "/session":
                    emit(f"{history.session_id}\n{history.path}")
                elif text.startswith("/") and text != "/continue":
                    emit("Неизвестная команда. /help — справка.")
                else:
                    measured(lambda: agent.run(None if text == "/continue" else text))
                    print()
            except KeyboardInterrupt:
                tools.close()
                emit("Прервано. Процессы остановлены; история сохранена. /continue — продолжить.")
            except (ModelError, ValueError, OSError) as error:
                diagnostics.record(error)
                emit(f"Ошибка: {error}\nИстория сохранена. Исправьте причину и используйте /continue.")
        return 0
    except KeyboardInterrupt:
        emit("Прервано.")
        return 130
    except (ValueError, OSError, ModelError, TypeError, EOFError) as error:
        if diagnostics:
            diagnostics.record(error, "cli")
        emit(f"Ошибка: {error}")
        return 1
    finally:
        if client:
            client.close()
        if team:
            team.close()
        if tools:
            tools.close()
        if history:
            history.close()
        if workspace_lock:
            workspace_lock.close()
