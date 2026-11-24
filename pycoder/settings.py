"""Session limit editor; called by the worker between turns, never mid-request."""

from dataclasses import asdict, replace

from .config import endpoint
from .events import Cancelled
from .util import private_json

LIMITS = {
    "max_output_tokens": "Ответ модели, токенов",
    "context_window": "Окно контекста, токенов",
    "auto_compact_percent": "Автосжатие, % (30–95)",
    "max_steps": "Шагов модели за ход",
    "tool_output_tokens": "Вывод инструмента для модели, токенов",
    "timeout_seconds": "Подключение, секунд",
    "generation_timeout_seconds": "Ожидание данных модели, секунд",
    "retries": "Повторов загрузки списка моделей (0–8)",
    "parallel_tools": "Параллельных чтений (1–16)",
    "max_subagents": "Одновременно субагентов (0–8)",
    "max_background_processes": "Фоновых процессов всего (1–32, по умолчанию 5)",
}

SECURITY_FIELDS = {"e2ee_verify_attestation"}


def encryption_label(config):
    return "E2EE: " + ("с проверкой аттестации" if config.e2ee_verify_attestation else "БЕЗ проверки аттестации — сервер не проверен")


SANDBOX_FIELDS = {"sandbox_enabled", "full_access", "approval", "sandbox_network", "sandbox_hosts", "sandbox_direct_hosts", "sandbox_localhost", "sandbox_lan"}

EXECUTION_MODES = [("sandbox", "Песочница"),
                   ("confirm", "Без песочницы · с подтверждениями"),
                   ("full", "Без песочницы · полный доступ без подтверждений")]
FULL_ACCESS_WARNING = ("Агент и субагенты смогут выполнять любые команды без подтверждений, "
    "изменять и удалять файлы вне проекта и обращаться ко всей сети с правами вашего пользователя. "
    "Команды смогут работать напрямую даже при настроенном прокси модели. "
    "Ошибочная команда может привести к потере данных. Ограничения macOS и права пользователя сохраняются.")


def execution_mode(config):
    return "sandbox" if config.sandbox_enabled else "full" if config.full_access else "confirm"


def set_execution_mode(config, mode):
    if mode not in dict(EXECUTION_MODES):
        raise ValueError("Unknown execution mode")
    config.sandbox_enabled = mode == "sandbox"
    config.full_access = mode == "full"
    config.approval = "auto" if config.full_access else "ask"


def network_modes(config):
    modes = [("none", "Запрещена"), ("allowlist", "По спискам адресов")]
    if config.proxy_enabled:
        modes += [("all", "Через прокси · без списка публичных адресов")]
    else:
        modes += [("all", "Вся сеть разрешена · включая localhost и LAN")]
    modes += [("direct", "Вся сеть напрямую · включая localhost и LAN, без прокси")]
    return modes


def visible_sandbox_fields(config):
    fields = {"sandbox_enabled"}
    if not config.sandbox_enabled:
        return fields
    fields.add("sandbox_network")
    if config.sandbox_network == "allowlist":
        fields.update({"sandbox_hosts", "sandbox_direct_hosts"})
    if config.sandbox_network == "allowlist" or (config.sandbox_network == "all" and config.proxy_enabled):
        fields.update({"sandbox_localhost", "sandbox_lan"})
    return fields


def edit_sandbox(config, directory, choose, ask, status):
    draft = replace(config, sandbox_hosts=list(config.sandbox_hosts), sandbox_direct_hosts=list(config.sandbox_direct_hosts))
    while True:
        options = [
            ("sandbox_enabled", "Режим: " + dict(EXECUTION_MODES)[execution_mode(draft)]),
            ("sandbox_network", "Сеть песочницы: " + dict(network_modes(draft))[draft.sandbox_network]),
            ("sandbox_hosts", ("Через прокси: " if draft.proxy_enabled else "Разрешённые хосты: ") + ", ".join(draft.sandbox_hosts)),
            ("sandbox_direct_hosts", "Напрямую (включая локальные): " + ", ".join(draft.sandbox_direct_hosts)),
            ("sandbox_localhost", f"Разрешать localhost через прокси: {draft.sandbox_localhost}"),
            ("sandbox_lan", f"Разрешать локальную сеть через прокси: {draft.sandbox_lan}")]
        options = [(key, label) for key, label in options if key in visible_sandbox_fields(draft)]
        options += [("apply", "Применить"), ("save", "Применить и сохранить"), ("cancel", "Назад")]
        key = choose("Песочница · macOS/Linux", options,
            detail="Windows: системная песочница не применяется. Настройки действуют только на новые процессы, включая субагентов.\n"
                   "Модель и аттестация настраиваются отдельно. Прямой список разрешает указанные host:port, включая localhost/LAN.")
        if key in {None, "cancel"}:
            return False
        if key in {"apply", "save"}:
            draft.validate()
            if key == "save":
                private_json(directory / "config.json", asdict(draft))
            for name in SANDBOX_FIELDS:
                setattr(config, name, getattr(draft, name))
            status("Песочница: настройки применены к новым процессам.")
            return True
        if key == "sandbox_network":
            mode = choose("Сеть команд", network_modes(draft))
            if mode in {"none", "allowlist", "all", "direct"}:
                draft.sandbox_network = mode
        elif key in {"sandbox_hosts", "sandbox_direct_hosts"}:
            value = ask("Точные host:port через запятую; пустая строка очищает список")
            if value is not None:
                candidate = replace(draft, **{key: [v.strip() for v in value.split(",") if v.strip()]})
                try:
                    candidate.validate()
                    draft = candidate
                except ValueError as error:
                    status(str(error))
        elif key == "sandbox_enabled":
            mode = choose("Режим выполнения команд", EXECUTION_MODES, default=execution_mode(draft))
            if mode not in dict(EXECUTION_MODES):
                continue
            if mode == "full" and choose("Включить полный доступ без подтверждений?",
                    [("no", "Отмена"), ("yes", "Включить полный доступ")], detail=FULL_ACCESS_WARNING) != "yes":
                continue
            set_execution_mode(draft, mode)
        elif key in {"sandbox_localhost", "sandbox_lan"}:
            setattr(draft, key, not getattr(draft, key))


def edit_limits(config, directory, choose, ask, status):
    try:
        return _edit_limits(config, directory, choose, ask, status)
    except Cancelled:
        status("Настройки отменены.")
        return False


def _edit_limits(config, directory, choose, ask, status):
    draft = replace(config)
    while True:
        options = [(key, f"{label}: {getattr(draft, key)}") for key, label in LIMITS.items()]
        options += [("e2ee_verify_attestation", encryption_label(draft)), ("sandbox", "Песочница и сеть"), ("apply", "Применить к текущей сессии"),
                    ("save", "Применить и сохранить для следующих запусков"), ("cancel", "Отмена")]
        key = choose("Лимиты", options, detail="Изменения действуют со следующего хода. История сохраняется.\n"
                     "Значения не увеличивают реальные возможности сервера. Лимит инструмента не ограничивает просмотр журнала.")
        if key in (None, "cancel"):
            return False
        if key == "e2ee_verify_attestation":
            draft.e2ee_verify_attestation = not draft.e2ee_verify_attestation
            continue
        if key == "sandbox":
            edit_sandbox(config, directory, choose, ask, status)
            draft = replace(config)
            continue
        if key in ("apply", "save"):
            draft.validate()
            if draft.max_output_tokens != config.max_output_tokens:
                draft.output_limit_source = "manual"
                draft.output_limit_scope = endpoint(draft.base_url) + "\n" + draft.model
            if key == "save":
                private_json(directory / "config.json", asdict(draft))
            for field, value in asdict(draft).items():
                setattr(config, field, value)
            status("Лимиты применены" + (" и сохранены." if key == "save" else " к текущей сессии."))
            return True
        if key not in LIMITS:
            continue
        value = ask(f"{LIMITS[key]} [{getattr(draft, key)}]")
        if value is None or not value.strip():
            continue
        try:
            candidate = replace(draft, **{key: int(value)})
            candidate.validate()
        except ValueError as error:
            status(f"Лимит не изменён: {error}")
        else:
            draft = candidate
