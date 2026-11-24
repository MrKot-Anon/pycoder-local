"""Interactive configuration; credentials are scoped to the exact endpoint."""

from dataclasses import asdict, dataclass, field
import getpass
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .util import private_json, terminal_text

DEFAULT_URL = "https://llm.chutes.ai/v1"
DEFAULT_CHUTES_MODEL = "moonshotai/Kimi-K3-TEE"


def endpoint(url: str) -> str:
    parts = urlsplit(url.strip())
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ValueError("Use an http(s) URL without credentials, query or fragment")
    _ = parts.port  # Validate the port before making any request.
    path = parts.path.rstrip("/")
    if not path:
        path = "/v1"
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


@dataclass
class Config:
    proxy_enabled: bool = False
    proxy_version: int = 5
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 1080
    proxy_username: str = ""
    proxy_password: str = field(default="", repr=False)
    proxy_chain_enabled: bool = False
    proxy_second_version: int = 5
    proxy_second_host: str = "127.0.0.1"
    proxy_second_port: int = 1080
    proxy_second_username: str = ""
    proxy_second_password: str = field(default="", repr=False)
    proxy_remote_dns: bool = True
    proxy_chutes: bool = True
    proxy_intel: bool = True
    proxy_nvidia: bool = True
    e2ee_verify_attestation: bool = True
    base_url: str = DEFAULT_URL
    model: str = ""
    context_window: int = 32768
    max_output_tokens: int = 4096
    output_limit_source: str = "manual"
    output_limit_scope: str = ""
    auto_compact_percent: int = 80
    max_steps: int = 100
    parallel_tools: int = 4
    max_subagents: int = 3
    max_background_processes: int = 5
    tool_output_tokens: int = 4000
    timeout_seconds: int = 120
    generation_timeout_seconds: int = 600
    retries: int = 3
    stream: bool = True
    require_token: bool = True
    approval: str = "ask"
    sandbox_enabled: bool = True
    full_access: bool = False
    sandbox_network: str = "none"
    sandbox_hosts: list[str] = field(default_factory=list)
    sandbox_direct_hosts: list[str] = field(default_factory=list)
    sandbox_localhost: bool = False
    sandbox_lan: bool = False
    e2ee_mrtd: list[str] = field(default_factory=list)

    def validate(self):
        from .proxy import validate
        validate(self)
        if type(self.e2ee_verify_attestation) is not bool:
            raise ValueError("e2ee_verify_attestation must be boolean")
        from .network_policy import host_port
        if (not isinstance(self.e2ee_mrtd, list) or any(
                not isinstance(value, str) or len(value) != 96
                or any(c not in "0123456789abcdef" for c in value) for value in self.e2ee_mrtd)):
            raise ValueError("e2ee_mrtd must contain lowercase SHA-384 hex measurements")
        if any(type(getattr(self, key)) is not bool for key in ("sandbox_enabled", "full_access", "sandbox_localhost", "sandbox_lan")):
            raise ValueError("Sandbox switches must be boolean")
        if self.full_access and (self.sandbox_enabled or self.approval != "auto"):
            raise ValueError("Full access requires sandbox_enabled=false and approval=auto")
        if self.sandbox_network not in {"none", "allowlist", "all", "direct"}:
            raise ValueError("Sandbox network must be none, allowlist, all or direct")
        for hosts in (self.sandbox_hosts, self.sandbox_direct_hosts):
            if not isinstance(hosts, list) or len(hosts) > 100:
                raise ValueError("Sandbox hosts must be a list of up to 100 host:port entries")
            for host in hosts:
                host_port(host)
        if {host_port(h) for h in self.sandbox_hosts} & {host_port(h) for h in self.sandbox_direct_hosts}:
            raise ValueError("Один host:port нельзя одновременно разрешить через прокси и напрямую")
        endpoint(self.base_url)
        if not self.model.strip():
            raise ValueError("A model ID is required")
        for key in ("context_window", "max_output_tokens", "max_steps",
                    "tool_output_tokens", "timeout_seconds", "generation_timeout_seconds"):
            if type(getattr(self, key)) is not int or getattr(self, key) <= 0:
                raise ValueError(f"{key} must be a positive integer")
        if self.context_window < 4096:
            raise ValueError("context_window must be >=4096")
        if type(self.parallel_tools) is not int or not 1 <= self.parallel_tools <= 16:
            raise ValueError("parallel_tools must be 1..16")
        if type(self.max_subagents) is not int or not 0 <= self.max_subagents <= 8:
            raise ValueError("max_subagents must be 0..8")
        if type(self.max_background_processes) is not int or not 1 <= self.max_background_processes <= 32:
            raise ValueError("max_background_processes must be 1..32")
        if not 30 <= self.auto_compact_percent <= 95 or not 0 <= self.retries <= 8:
            raise ValueError("Invalid compaction percent (30..95) or retries (0..8)")
        if self.approval not in {"ask", "never", "auto"}:
            raise ValueError("approval must be ask, never (deny mutations), or auto")


def config_dir() -> Path:
    legacy = Path.home() / ".config/pycoder"
    default = Path(os.environ.get("APPDATA", Path.home())) / "pycoder" if os.name == "nt" and not legacy.exists() else Path((os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")) / "pycoder"
    return Path(os.environ.get("PYCODER_CONFIG_DIR", default))


def load(directory: Path, *, include_proxy=True) -> Config:
    path = directory / "config.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"output_limit_source": "unset"}
    data.setdefault("output_limit_source", "legacy")
    proxy_path = directory / "proxy.json"
    if include_proxy and proxy_path.exists():
        from .proxy import FIELDS, LEGACY_FIELDS
        proxy = json.loads(proxy_path.read_text(encoding="utf-8"))
        legacy_fields = set(LEGACY_FIELDS)
        if not isinstance(proxy, dict) or set(proxy) - set(FIELDS) or not legacy_fields <= set(proxy):
            raise ValueError("Invalid proxy.json; network access blocked")
        defaults = Config()
        data.update({key: getattr(defaults, key) for key in FIELDS})
        data.update(proxy)
    if not include_proxy:
        from .proxy import FIELDS
        defaults = Config()
        data.update({key: getattr(defaults, key) for key in FIELDS})
    return Config(**data)


def get_token(config: Config, directory: Path) -> str:
    explicit = os.environ.get("PYCODER_API_KEY")
    if explicit:
        return explicit
    if urlsplit(config.base_url).hostname == "llm.chutes.ai":
        chutes = os.environ.get("CHUTES_API_KEY")
        if chutes:
            return chutes
    path = directory / "credentials.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("endpoint") == endpoint(config.base_url):
            return data.get("token", "")
    return ""


def wizard(config: Config, directory: Path, *, input_fn=input, secret_fn=getpass.getpass,
           fetch_models=None, choose_fn=None, print_fn=print) -> Config:
    print = print_fn
    print("Выберите сервер модели:\n  1. Chutes.ai\n  2. Свой сервер / локальная модель")
    default_provider = "1" if endpoint(config.base_url) == endpoint(DEFAULT_URL) else "2"
    provider = (choose_fn("Сервер модели", [("1", "Chutes.ai"), ("2", "Свой сервер / локальная модель")], default_provider)
                if choose_fn else input_fn(f"Ваш выбор [{default_provider}]: ").strip() or default_provider)
    if provider not in {"1", "2"}:
        raise ValueError("Выберите 1 (Chutes) или 2 (свой сервер)")
    chutes = provider == "1"
    if chutes:
        config.base_url = DEFAULT_URL
    else:
        config.base_url = input_fn(f"URL [{config.base_url}]: ").strip() or config.base_url
        endpoint(config.base_url)
    # The API key is requested before any model discovery, including first setup.
    token = get_token(config, directory)
    entered_token = False
    if not token:
        prompt = "API-ключ Chutes (ввод скрыт): " if chutes else "API-токен (ввод скрыт; '-' без авторизации): "
        token = secret_fn(prompt).strip()
        if token == "-":
            if chutes:
                raise ValueError("Для Chutes необходим API-токен")
            token = ""
            config.require_token = False
        elif not token:
            raise ValueError("Токен не задан")
        else:
            config.require_token = True
        entered_token = True
    else:
        print("API-ключ уже задан; использую сохранённый ключ или переменную окружения.")
        config.require_token = True
    if chutes:
        if fetch_models is None:
            from .transport import Client
            fetch_models = lambda cfg, key: Client(cfg, key, print).list_models()
        print("Загружаю список моделей Chutes…")
        models = fetch_models(config, token)
        if not models:
            raise ValueError("Chutes вернул пустой список моделей")
        by_id = {model["id"]: model for model in models}
        default = DEFAULT_CHUTES_MODEL if DEFAULT_CHUTES_MODEL in by_id else ""
        if not default:
            print("Kimi K3 отсутствует в полученном каталоге. Выберите другую модель явно.")
        for index, model in enumerate(models, 1):
            marker = " — по умолчанию" if model["id"] == default else ""
            length = model.get("context_length") or model.get("max_model_len")
            info = f", контекст {length}" if type(length) is int else ""
            print(terminal_text(f"  {index}. {model['id']}{info}{marker}"))
        choice = (choose_fn("Выберите модель", [(m["id"], m["id"] + (" — по умолчанию" if m["id"] == default else "")) for m in models], default)
                  if choose_fn else input_fn(f"Номер или ID модели [{default or 'обязательный выбор'}]: ").strip() or default)
        if choice.isdecimal() and 1 <= int(choice) <= len(models):
            choice = models[int(choice) - 1]["id"]
        if choice not in by_id:
            raise ValueError("Выберите модель из полученного списка")
        model = by_id[choice]
        config.model = choice
        context = model.get("context_length") or model.get("max_model_len")
        if type(context) is int and context >= 4096:
            config.context_window = context
        set_output_limit(config, model, input_fn)
        print(terminal_text(f"Выбрана {config.model}; окно {config.context_window}, ответ до {config.max_output_tokens} токенов."))
    else:
        config.model = input_fn(f"ID модели [{config.model}]: ").strip() or config.model
        config.context_window = int(input_fn(f"Размер контекста [{config.context_window}]: ") or config.context_window)
        config.output_limit_source = "unset"
        ensure_output_limit(config, token, input_fn=input_fn, fetch_models=fetch_models, print_fn=print)
    config.validate()
    if entered_token:
        private_json(directory / "credentials.json", {"endpoint": endpoint(config.base_url), "token": token})
    private_json(directory / "config.json", asdict(config))
    return config


def limit_scope(config):
    return endpoint(config.base_url) + "\n" + config.model


def set_output_limit(config, model, input_fn):
    maximum = (model or {}).get("max_output_length")
    if type(maximum) is int and maximum > 0:
        config.max_output_tokens = maximum
        config.output_limit_source = "catalog"
    else:
        if input_fn is None:
            raise ValueError("Каталог не сообщает max_output_length. Задайте --max-output-tokens или запустите интерактивную настройку.")
        while True:
            answer = input_fn(f"Каталог не сообщает лимит ответа. Максимум токенов ответа [{config.max_output_tokens}]: ").strip()
            if not answer:
                answer = str(config.max_output_tokens)
            if answer.isdecimal() and int(answer) > 0:
                config.max_output_tokens = int(answer)
                break
        config.output_limit_source = "manual"
    config.output_limit_scope = limit_scope(config)


def ensure_output_limit(config, token, *, input_fn=None, fetch_models=None, print_fn=print, explicit=None):
    if explicit is not None:
        if explicit <= 0:
            raise ValueError("max_output_tokens must be positive")
        config.max_output_tokens = explicit
        config.output_limit_source = "manual"
        config.output_limit_scope = limit_scope(config)
        return False
    if config.output_limit_source in {"manual", "catalog"} and config.output_limit_scope in {"", limit_scope(config)}:
        return False
    if fetch_models is None:
        from .transport import Client
        from dataclasses import replace
        fetch_models = lambda cfg, key: Client(replace(cfg, retries=0), key).list_models()
    from .transport import ModelError
    print_fn("Проверяю лимит ответа в каталоге выбранного сервера…")
    try:
        models = fetch_models(config, token)
    except ModelError as error:
        print_fn(f"Каталог недоступен: {error}")
        models = []
    model = next((m for m in models if m.get("id") == config.model), {})
    context = model.get("context_length") or model.get("max_model_len")
    if type(context) is int and context >= 4096:
        config.context_window = context
    set_output_limit(config, model, input_fn)
    print_fn(f"Лимит ответа: {config.max_output_tokens} токенов ({config.output_limit_source}).")
    return True
