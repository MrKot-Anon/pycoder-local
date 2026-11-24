"""Session-only, direct local model setup, without cloud credentials or onboarding."""

from urllib.parse import urlsplit, urlunsplit

from .config import Config, endpoint, limit_scope
from .transport import Client


def local_url(address):
    address = address.strip()
    if not address:
        raise ValueError("Укажите адрес локальной модели")
    if any(character.isspace() for character in address):
        raise ValueError("Адрес локальной модели не должен содержать пробелы; задачу отделите через --")
    explicit_scheme = "://" in address
    url = address if explicit_scheme else "http://" + address
    endpoint(url)  # Validate before discovery; reject credentials/query/fragment.
    parts = urlsplit(url)
    if not explicit_scheme and parts.port is None:
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        url = urlunsplit((parts.scheme, host + ":8080", parts.path, "", ""))
    return endpoint(url).removesuffix("/chat/completions")


def prepare_local(config, args, emit=print):
    if args.base_url is not None:
        raise ValueError("С --локал укажите адрес сразу после флага, без --base-url")
    if args.configure:
        raise ValueError("--локал действует для текущего запуска; --configure используйте отдельно")
    from .proxy import FIELDS
    defaults = Config()
    saved_limit = config.max_output_tokens
    saved_scope = config.output_limit_scope
    saved_source = config.output_limit_source
    for key in FIELDS:
        setattr(config, key, getattr(defaults, key))
    config.base_url = local_url(args.local)
    config.require_token = False
    args.no_auth = True  # Do not send even an environment-provided cloud API key.
    config.model = args.model or ""
    config.context_window = args.context_window if args.context_window is not None else defaults.context_window
    config.max_output_tokens = args.max_output_tokens if args.max_output_tokens is not None else defaults.max_output_tokens
    if not config.model:
        client = Client(config, "", emit)
        try:
            models = client.list_models()
        finally:
            client.close()
        if len(models) != 1:
            raise ValueError("На локальном сервере несколько моделей; укажите --model: "
                             + ", ".join(model["id"] for model in models))
        model = models[0]
        config.model = model["id"]
        meta = model.get("meta") or {}
        context = model.get("context_length") or model.get("max_model_len") or (meta.get("n_ctx") if isinstance(meta, dict) else None)
        if args.context_window is None and type(context) is int and context >= 4096:
            config.context_window = context
    if args.max_output_tokens is None and saved_source == "manual" and saved_scope == limit_scope(config):
        config.max_output_tokens = saved_limit
    config.output_limit_source = "manual"
    config.output_limit_scope = limit_scope(config)
    emit(f"ЛОКАЛЬНО · напрямую · {config.model} · {config.base_url} · контекст {config.context_window}")
