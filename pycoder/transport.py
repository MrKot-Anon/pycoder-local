"""Single-provider completions and explicit model discovery; SOCKS support and no redirects."""

from dataclasses import dataclass
import http.client
import json
import time
import socket
import threading
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from .config import Config, endpoint
from .events import Cancelled
from .util import tokens
from .servers import ServerSelectionError


class ModelError(RuntimeError):
    def __init__(self, message, *, retryable=False, context_overflow=False, retry_after=None):
        super().__init__(message)
        self.retryable = retryable
        self.context_overflow = context_overflow
        self.retry_after = retry_after


def retry_after_seconds(value, default):
    if isinstance(value, str):
        if value.strip().isdigit():
            return int(value.strip())
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            pass
    return default


class OutputLimit(ModelError):
    """A server-declared length stop; no calls in this response may be executed."""
    def __init__(self, message):
        super().__init__("Model response truncated by output limit (finish_reason=length); user decision required.")
        self.message = message
        self.limit = None
        self.capacity = None


class TruncatedTool(OutputLimit):
    pass


class EmptyResponse(ModelError):
    """A completed response without actionable output; never an executed tool."""
    def __init__(self, finish, reasoning_chars, usage):
        self.finish = finish
        self.reasoning_chars = reasoning_chars
        count = usage.get("completion_tokens")
        count = count if type(count) is int else "unknown"
        super().__init__(
            f"Model returned no text and no tool calls (finish_reason={finish}, "
            f"reasoning_chars={reasoning_chars}, completion_tokens={count}). "
            "Провайдер завершил ответ без текста и вызовов инструментов. "
            "Это не сигнал достижения лимита вывода; запрос будет повторён автоматически.",
            retryable=True)


def object_value(value, field, *, nullable=False):
    if value is None and nullable:
        return {}
    if not isinstance(value, dict):
        raise ModelError(f"Invalid model response: {field} must be an object, got {type(value).__name__}")
    return value


def array_value(value, field):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ModelError(f"Invalid model response: {field} must be an array or null, got {type(value).__name__}")
    return value


def text_value(value, field):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ModelError(f"Invalid model response: {field} must be a string or null, got {type(value).__name__}")
    return value


@dataclass
class Completion:
    message: dict
    usage: dict
    finish_reason: str


class Client:
    def __init__(self, config: Config, token: str, on_status=lambda message: None):
        self.config = config
        self.token = token
        self.url = urlsplit(endpoint(config.base_url))
        self.on_status = on_status
        self.cancelled = threading.Event()
        self._connection = None
        self._response = None
        self._connection_lock = threading.Lock()
        self.on_metrics = lambda **data: None
        self.on_output = lambda **data: None
        self.on_security = lambda **data: None
        self.on_encryption_failure = lambda reason: "stop"
        self.on_retry = lambda **data: None
        self._encryption = None
        from .servers import Servers
        self.servers = Servers()
        self._attempt_instance = None
        self._attestation = None
        self.on_attestation_stale = None
        self.attestation_proactive = False
        self._direct_attestation = False
        self.on_usage = lambda **data: None
        self._attempt_usage = {}
        self._attempt_received = False
        self.on_diagnostic = lambda error: None
        self._last_stream_read = None
        self._stream_bytes = 0
        self._started = 0.0
        self._generated_bytes = 0
        self._last_metric = 0.0

    def _progress(self, text):
        self._generated_bytes += len(text.encode("utf-8"))
        now = time.monotonic()
        if self._started and now - self._last_metric >= 0.1:
            self._last_metric = now
            self.on_metrics(tokens=(self._generated_bytes + 3) // 4,
                            elapsed=now - self._started, exact=False, finished=False)

    def cancel(self):
        self.cancelled.set()
        with self._connection_lock:
            conn = self._connection
            sock = conn.sock if conn else None
            if sock is None and self._response is not None:
                raw = getattr(getattr(self._response, "fp", None), "raw", None)
                sock = getattr(raw, "_sock", None)
            if sock:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def start_attestation(self):
        if (self._attestation is not None or not self.config.e2ee_verify_attestation
                or self.url.scheme != "https" or self.url.hostname != "llm.chutes.ai"
                or self.url.port not in (None, 443) or self.url.path != "/v1/chat/completions"):
            return
        from dataclasses import replace
        from .background_attestation import BackgroundAttestation
        # Separate cancellation and HTTP connection state from generation.
        background_client = Client(replace(self.config), self.token)
        ask = self.on_attestation_stale or (lambda reason, recovered, stopped: self.on_encryption_failure(reason))
        self._attestation = BackgroundAttestation(self.config.model, background_client, ask,
            lambda **data: self.on_security(**data), proactive=self.attestation_proactive)
        self._attestation.start()

    def close(self):
        if self._attestation is not None:
            self._attestation.close()
            self._attestation = None

    def reset_attestation(self):
        self.close()
        self._encryption = None
        self.start_attestation()

    def _delay(self, seconds):
        if self.cancelled.wait(seconds):
            raise Cancelled()

    def list_models(self):
        """Explicit setup-time discovery on the configured provider only."""
        for attempt in range(self.config.retries + 1):
            try:
                return self._request(None, lambda text: None, catalog=True)
            except ModelError as error:
                if not error.retryable or attempt == self.config.retries:
                    raise
                delay = min(2 ** attempt, 8)
                self.on_status(f"Повтор загрузки моделей через {delay} с: {error}")
                self._delay(delay)
        raise AssertionError("unreachable")

    def complete(self, messages, tools=None, on_text=lambda text: None, *, max_tokens=None):
        self._started = time.monotonic()
        self._generated_bytes = 0
        self._last_metric = 0.0
        capacity = self.config.context_window * 95 // 100 - tokens(messages) - tokens(tools or []) - 512
        if capacity <= 0:
            raise ModelError("No room for model output; compact the context", context_overflow=True)
        initial_budget = min(max_tokens or self.config.max_output_tokens, capacity)
        payload = {"model": self.config.model, "messages": messages,
                   "max_tokens": initial_budget,
                   "stream": self.config.stream}
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        # A failed attempt never enters history or executes tools. Retry the
        # original messages, and mark any displayed partial answer as interrupted.
        # Length stops still require an explicit user decision.
        visible = False
        leading_whitespace = ""

        def emit(text):
            nonlocal visible, leading_whitespace
            if not visible and not text.strip():
                leading_whitespace += text
                return
            visible = True
            on_text(leading_whitespace + text)
            leading_whitespace = ""

        attempt = 0
        server_attempts = {}
        self._server_attempts = server_attempts
        self._retry_instance = None
        self._retry_excluded = set()
        while True:
            self.check_cancelled()
            try:
                result = self._accounted_request(payload, emit)
                count = result.usage.get("completion_tokens")
                exact = type(count) is int and count >= 0
                if not exact:
                    generated = (result.message.get("content") or "") + (result.message.get("reasoning_content") or "")
                    generated += "".join(c["function"]["name"] + c["function"]["arguments"] for c in result.message.get("tool_calls", []))
                    count = tokens(generated)
                self.on_metrics(tokens=count, elapsed=time.monotonic() - self._started, exact=exact, finished=True)
                return result
            except ModelError as error:
                self.check_cancelled()
                self.on_diagnostic(error)
                if isinstance(error, OutputLimit):
                    error.limit, error.capacity = payload["max_tokens"], capacity
                    raise
                policy = self.servers.snapshot()
                if self.url.hostname == "llm.chutes.ai" and not self._attempt_instance and attempt >= policy["total_retries"]:
                    raise ModelError(str(error) + "\nДостигнут общий лимит повторов: " + str(policy["total_retries"])) from error
                if self._attempt_instance:
                    identifier = self._attempt_instance
                    policy = self.servers.snapshot()
                    server_attempts[identifier] = server_attempts.get(identifier, 0) + 1
                    partial = visible or bool(self._generated_bytes)
                    action = policy["partial_action"] if partial else None
                    if partial and action in {"stop", "stop_switch"}:
                        if action == "stop_switch":
                            self.servers.exclude(identifier)
                        suffix = " Следующая попытка выберет другой доступный сервер." if action == "stop_switch" else ""
                        raise ModelError(str(error) + "\nПолучен неполный ответ. Автоповтор остановлен, чтобы не расходовать токены повторно."
                            + suffix + " Частичный ответ остался в журнале; для новой попытки нажмите «Продолжить».") from error
                    if not error.retryable and not (partial and action == "switch_retry"):
                        raise
                    if attempt >= policy["total_retries"]:
                        raise ModelError(str(error) + "\nДостигнут общий лимит повторов: " + str(policy["total_retries"])) from error
                    # A partial response explicitly requests a different server.
                    switch = partial and action == "switch_retry"
                    if server_attempts[identifier] > policy["retries_per_server"]:
                        if policy["auto_switch"]:
                            switch = True
                        elif not switch:
                            raise ModelError(str(error) + "\nДостигнут лимит повторов на сервере; автоматическая смена выключена.") from error
                    if switch:
                        self.servers.exclude(identifier)
                        self._retry_excluded.add(identifier)
                        self._retry_instance = None
                    else:
                        self._retry_instance = identifier
                    if partial and action == "switch_retry":
                        error.retryable = True
                if not error.retryable:
                    raise
                attempt += 1
                delay = (1, 2, 4, 8)[attempt - 1] if attempt <= 4 else 10
                if isinstance(error, EmptyResponse):
                    payload["stream"] = False
                self.on_retry(attempt=attempt, limit=policy["total_retries"] if self.url.hostname == "llm.chutes.ai" else None, partial=visible,
                              delay=delay, error=str(error), streaming=payload["stream"])
                self._generated_bytes = 0
                leading_whitespace = ""
                visible = False
                self._delay(delay)

    def _accounted_request(self, payload, emit):
        self._attempt_instance = None
        self._attempt_usage = {}
        self._attempt_received = False
        self._generated_bytes = 0
        succeeded = False
        self.on_output(channel="request", text="Запрос начат" + (" (streaming)" if payload["stream"] else " (без streaming: ответ придёт целиком)"))
        try:
            result = self._request(payload, emit)
            self._attempt_usage = result.usage
            self._attempt_received = succeeded = True
            return result
        finally:
            self.on_output(channel="end", text="Ответ получен" if succeeded else "Запрос завершился без полного ответа (ошибка или отмена)")
            usage = self._attempt_usage
            incoming, outgoing = usage.get("prompt_tokens"), usage.get("completion_tokens")
            exact_in = type(incoming) is int and incoming >= 0
            exact_out = type(outgoing) is int and outgoing >= 0
            total = usage.get("total_tokens")
            total = total if type(total) is int and total >= 0 else None
            self.on_usage(
                input_tokens=incoming if exact_in else (tokens(payload["messages"]) + tokens(payload.get("tools", [])) if self._attempt_received else 0),
                output_tokens=outgoing if exact_out else (self._generated_bytes + 3) // 4,
                total_tokens=total,
                exact=exact_in and exact_out,
                unknown=not succeeded and not (exact_in and exact_out))

    def _request(self, payload, on_text, *, catalog=False):
        from .e2ee import ChutesEncryption, EncryptionError
        chutes = self.url.hostname == "llm.chutes.ai"
        if chutes and self.url.scheme != "https":
            raise ModelError("Chutes требует HTTPS; API-ключ и запрос не отправлены")
        if catalog or not chutes:
            if not catalog:
                self.on_security(mode="plain", detail="E2EE для этого провайдера не поддерживается")
            return self._http_request(payload, on_text, catalog=catalog)
        direct_attestation = False
        while True:
            self.check_cancelled()
            self.on_security(mode="checking", detail="Подготовка E2EE" if self.config.e2ee_verify_attestation else "E2EE без проверки аттестации · сервер не проверен")
            try:
                if (self.url.scheme != "https" or self.url.port not in (None, 443)
                        or self.url.path != "/v1/chat/completions"):
                    raise EncryptionError("E2EE Chutes требует https://llm.chutes.ai/v1")
                if self._encryption is None:
                    self._encryption = ChutesEncryption(self._encryption_metadata, self.check_cancelled, self.config.e2ee_mrtd, proxy_config=self.config)
                    self._encryption.servers = self.servers
                    self.start_attestation()
                    self._encryption.background = self._attestation
                used_direct = direct_attestation
                self._direct_attestation = direct_attestation
                try:
                    options = {}
                    if not self.config.e2ee_verify_attestation:
                        options["verify_attestation"] = False
                    if direct_attestation:
                        options["direct_attestation"] = True
                    self._encryption.retry_instance = getattr(self, "_retry_instance", None)
                    self._encryption.retry_excluded = getattr(self, "_retry_excluded", set())
                    self._encryption.retry_attempts = getattr(self, "_server_attempts", {})
                    envelope = self._encryption.prepare(self.config.model, payload, **options)
                finally:
                    self._direct_attestation = False
                    direct_attestation = False
                if self.config.e2ee_verify_attestation:
                    detail = "E2EE · Intel TDX и NVIDIA проверены"
                    detail += " · MRTD закреплён" if self.config.e2ee_mrtd else " · образ ПО не закреплён"
                    if used_direct:
                        detail += " · разрешён прямой маршрут аттестации на этот запрос"
                    self.on_security(mode="encrypted", detail=detail)
                else:
                    self.on_security(mode="unverified", detail="E2EE без проверки аттестации · сервер не проверен")
                self._attempt_instance = envelope.instance_id
                self.servers.begin(envelope.instance_id)
                try:
                    result = self._http_request(payload, on_text, encrypted=envelope)
                except Cancelled:
                    self.servers.finish(envelope.instance_id, "cancelled")
                    raise
                except Exception:
                    self.servers.finish(envelope.instance_id, "errors", partial=bool(self._generated_bytes))
                    raise
                else:
                    self.servers.finish(envelope.instance_id, "success")
                    return result
            except ServerSelectionError as error:
                raise ModelError(str(error)) from None
            except (EncryptionError, ValueError, TypeError, KeyError, OSError) as error:
                reason = str(error) if isinstance(error, EncryptionError) else "Некорректные данные или ошибка подготовки E2EE"
                self.on_security(mode="blocked", detail=reason)
                self.check_cancelled()
                # Partial answers are never replayed or silently downgraded.
                if self._generated_bytes:
                    raise ModelError(reason + ". Получена часть ответа.", retryable=True) from None
                from .background_attestation import AttestationDecision
                decision = error.action if isinstance(error, AttestationDecision) else self.on_encryption_failure(reason)
                self.check_cancelled()
                if decision == "attestation_direct_once" and self.can_retry_attestation_direct:
                    direct_attestation = True
                    self.on_status("Разовый прямой маршрут для GET метаданных Chutes (каталог, ключи, evidence) и аттестации Intel/NVIDIA; маршрут генерации не меняется.")
                    continue
                if decision == "retry":
                    continue
                if decision == "plain_once":
                    self.on_security(mode="plain", detail="Без E2EE · разрешено пользователем на один запрос")
                    try:
                        return self._http_request(payload, on_text)
                    except ModelError as failure:
                        # A retry is a new transmission and needs a new consent.
                        failure.retryable = False
                        raise
                raise ModelError(reason + ". Запрос без E2EE не отправлен.") from None

    @property
    def can_retry_attestation_direct(self):
        return self.config.proxy_enabled and self.config.e2ee_verify_attestation and any(
            (self.config.proxy_chutes, self.config.proxy_intel, self.config.proxy_nvidia))

    def _proxy_connection(self, conn, *, direct=False):
        from .proxy import connect, validate, for_service
        validate(self.config)
        route = for_service(self.config, "chutes")
        if route.proxy_enabled and not direct:
            conn._create_connection = lambda address, timeout, source_address=None: connect(route, address, timeout, source_address)
        return conn

    def _encryption_metadata(self, host, path):
        from .e2ee import EncryptionError
        self.check_cancelled()
        conn = http.client.HTTPSConnection(host, timeout=min(self.config.timeout_seconds, 30))
        metadata_host = host in {"api.chutes.ai", "llm.chutes.ai"}
        self._proxy_connection(conn, direct=self._direct_attestation and metadata_host)
        source = f"Chutes {host}{path.split('?', 1)[0]}"
        from .attestation import safe_url
        request_detail = "GET " + safe_url("https://" + host + path)
        with self._connection_lock:
            self._connection = conn
        try:
            conn.request("GET", path, headers={"Authorization": "Bearer " + self.token, "Accept": "application/json"})
            response = conn.getresponse()
            with self._connection_lock:
                self._response = response
            data = response.read(4 * 1024 * 1024 + 1)
            self.check_cancelled()
            if response.status in {408, 429, 500, 502, 503, 504}:
                delay = retry_after_seconds(response.getheader("Retry-After"), 60 if response.status == 429 else None)
                label = "ограничение частоты запросов" if response.status == 429 else "временная ошибка сервера"
                raise ModelError(f"Метаданные E2EE: HTTP {response.status} ({source}) — {label}.\nЗапрос: {request_detail}\nШифрование остаётся обязательным.",
                                 retryable=True, retry_after=delay)
            if response.status != 200:
                raise EncryptionError(f"Метаданные E2EE: HTTP {response.status} ({source})\nЗапрос: {request_detail}")
            if len(data) > 4 * 1024 * 1024:
                raise EncryptionError("Слишком большой ответ метаданных E2EE")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise EncryptionError("Некорректные метаданные E2EE")
            return result
        except (OSError, http.client.HTTPException) as error:
            self.check_cancelled()
            from .attestation import safe_detail
            raise ModelError(f"Не удалось получить метаданные E2EE.\nЗапрос: {request_detail}\nПричина: {type(error).__name__}: {safe_detail(error)}\nШифрование остаётся обязательным.",
                             retryable=True) from None
        finally:
            conn.close()
            with self._connection_lock:
                if self._connection is conn:
                    self._connection = self._response = None

    def _http_request(self, payload, on_text, *, catalog=False, encrypted=None):
        self.check_cancelled()
        cls = http.client.HTTPSConnection if encrypted or self.url.scheme == "https" else http.client.HTTPConnection
        conn = cls("api.chutes.ai" if encrypted else self.url.hostname, None if encrypted else self.url.port, timeout=self.config.timeout_seconds)
        self._proxy_connection(conn)
        with self._connection_lock:
            self._connection = conn
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream, application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        started = time.monotonic()
        phase = "connect/send"
        response = None
        self._last_stream_read = None
        self._stream_bytes = 0
        try:
            path = self.url.path.removesuffix("/chat/completions") + "/models" if catalog else self.url.path
            if encrypted:
                path = "/e2e/invoke"
                headers.update(encrypted.headers)
            conn.request("GET" if catalog else "POST", path,
                         encrypted.body if encrypted else None if catalog else json.dumps(payload).encode("utf-8"), headers)
            # A reasoning model or gateway can buffer output for minutes. This is
            # an idle socket timeout, never a total generation duration limit.
            phase = "response headers"
            if not catalog and conn.sock is not None:
                conn.sock.settimeout(self.config.generation_timeout_seconds)
            response = conn.getresponse()
            with self._connection_lock:
                self._response = response
            self.check_cancelled()
            phase = "response body"
            if response.status != 200:
                if encrypted and response.status in {404, 410}:
                    raise ModelError(f"Экземпляр Chutes больше недоступен: HTTP {response.status}", retryable=True)
                if encrypted and response.status in {400, 403, 409, 422}:
                    from .e2ee import EncryptionError
                    raise EncryptionError(f"Chutes отклонил E2EE-запрос: HTTP {response.status}")
                body = response.read(8192).decode("utf-8", errors="replace")
                if self.token:
                    body = body.replace(self.token, "[REDACTED]")
                overflow = (response.status in {400, 413, 422} and any(
                    term in body.lower() for term in ("context_length", "context window", "maximum context", "too many tokens")))
                labels = {401: "Ошибка авторизации у провайдера", 403: "Провайдер запретил доступ",
                          408: "Провайдер не успел обработать запрос", 429: "Провайдер ограничил запросы",
                          500: "Внутренняя ошибка провайдера", 502: "Ошибка шлюза провайдера",
                          503: "Сервис модели временно недоступен", 504: "Шлюз не дождался ответа модели"}
                label = labels.get(response.status, "Ошибка HTTP от провайдера")
                if response.status == 429 and "maximum capacity" in body.lower():
                    label = "Экземпляр модели перегружен: достигнут предел одновременных запросов"
                provider = self.url.hostname or "неизвестен"
                message = (f"{label}. HTTP {response.status}.\n"
                           f"Сервис: {provider} · модель: {self.config.model}\n"
                           f"Ответ сервера: {body[:1500]}")
                if self.token:
                    message = message.replace(self.token, "[REDACTED]")
                raise ModelError(message,
                                 retryable=response.status in {408, 429, 500, 502, 503, 504},
                                 context_overflow=overflow)
            if not catalog and "text/event-stream" in response.getheader("Content-Type", ""):
                self._attempt_received = True
                phase = "SSE stream"
                if encrypted:
                    from .e2ee import DecryptedStream
                    return self._stream(DecryptedStream(response, encrypted.response_key, self.check_cancelled), on_text)
                return self._stream(response, on_text)
            if encrypted:
                from .e2ee import decrypt_body
                data = decrypt_body(response, encrypted.response_key)
            else:
                data = response.read()
            result = object_value(json.loads(data), "response")
            if result.get("error"):
                raise ModelError("Provider returned an error inside a successful response")
            if catalog:
                models = result.get("data")
                if not isinstance(models, list) or not models:
                    raise ModelError("Сервер вернул пустой или некорректный список моделей")
                if any(not isinstance(m, dict) or not isinstance(m.get("id"), str)
                       or not m["id"].strip() or any(ord(c) < 32 for c in m["id"]) for m in models):
                    raise ModelError("Некорректный ID в списке моделей")
                return models
            choices = array_value(result.get("choices"), "choices")
            if not choices:
                raise ModelError("Invalid model response: no completion choices")
            choice = object_value(choices[0], "choices[0]")
            message = self._message(choice["message"])
            self._attempt_received = True
            self._attempt_usage = object_value(result.get("usage"), "usage", nullable=True)
            generated = (message.get("content") or "") + (message.get("reasoning_content") or "")
            for call in message.get("tool_calls", []):
                if isinstance(call, dict) and isinstance(call.get("function"), dict):
                    generated += str(call["function"].get("name") or "") + str(call["function"].get("arguments") or "")
            self._progress(generated)
            for field, channel in (("reasoning_content", "reasoning"), ("content", "content")):
                if message.get(field):
                    self.on_output(channel=channel, text=message[field])
            for index, call in enumerate(message.get("tool_calls", [])):
                call = object_value(call, "tool_calls[]")
                function = object_value(call.get("function"), "tool_calls[].function")
                for field in ("name", "arguments"):
                    value = text_value(function.get(field), f"tool_calls[].function.{field}")
                    if value:
                        self.on_output(channel=f"tool:{index}:{field}", text=value)
            self._validate(message, choice.get("finish_reason"), object_value(result.get("usage"), "usage", nullable=True))
            if message.get("content"):
                on_text(message["content"])
            return Completion(message, object_value(result.get("usage"), "usage", nullable=True), choice["finish_reason"])
        except (OSError, http.client.HTTPException) as error:
            self.check_cancelled()
            # Do not expose request headers or credentials in diagnostics.
            elapsed = time.monotonic() - started
            idle = time.monotonic() - self._last_stream_read if self._last_stream_read is not None else elapsed
            raise ModelError(
                f"Connection failed: {type(error).__name__}; phase={phase}; "
                f"elapsed={elapsed:.1f}s; idle={idle:.1f}s; received={self._stream_bytes} bytes; "
                f"connect_timeout={self.config.timeout_seconds}s; generation_idle_timeout={self.config.generation_timeout_seconds}s",
                retryable=True) from None
        except (ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
            raise ModelError(f"Invalid model response: {type(error).__name__}") from None
        finally:
            if response is not None:
                response.close()
            conn.close()
            with self._connection_lock:
                if self._connection is conn:
                    self._connection = None
                    self._response = None

    @staticmethod
    def _message(raw):
        raw = object_value(raw, "message")
        message = {"role": "assistant", "content": text_value(raw.get("content"), "message.content") or None}
        # Some reasoning models require this field alongside tool calls on the next turn.
        reasoning = text_value(raw.get("reasoning_content"), "message.reasoning_content")
        if reasoning:
            message["reasoning_content"] = reasoning
        calls = array_value(raw.get("tool_calls"), "message.tool_calls")
        if calls:
            message["tool_calls"] = calls
        return message

    @staticmethod
    def _validate(message, finish, usage=None):
        finish = text_value(finish, "finish_reason")
        if finish not in {"stop", "tool_calls", "length"}:
            raise ModelError(f"Incomplete or unsupported response finish_reason: {finish!r}")
        calls = message.get("tool_calls", [])
        if finish == "length":
            raise TruncatedTool(message) if calls else OutputLimit(message)
        ids = set()
        for call in calls:
            call = object_value(call, "tool_calls[]")
            function = object_value(call.get("function"), "tool_calls[].function")
            call_id = text_value(call.get("id"), "tool_calls[].id")
            name = text_value(function.get("name"), "tool_calls[].function.name")
            if (call.get("type") != "function" or not call_id or call_id in ids
                    or not name or not isinstance(function.get("arguments"), str)):
                raise ModelError("Malformed or duplicate tool call")
            ids.add(call["id"])
        if not calls and not (message.get("content") or "").strip():
            raise EmptyResponse(finish, len(message.get("reasoning_content") or ""), usage or {})

    def _stream(self, response, on_text):
        content, reasoning, calls = [], [], {}
        usage, finish = {}, None
        byte_count = 0
        event_lines = []

        def consume(event):
            nonlocal usage, finish
            if event == "[DONE]":
                return True
            data = object_value(json.loads(event), "stream event")
            if data.get("error"):
                raise ModelError("Provider reported an error in the response stream")
            if data.get("usage") is not None:
                usage.update(object_value(data["usage"], "usage"))
                self._attempt_usage = usage
            for choice in array_value(data.get("choices"), "choices"):
                choice = object_value(choice, "choices[]")
                if choice.get("index", 0) != 0:
                    continue
                finish = text_value(choice.get("finish_reason"), "finish_reason") or finish
                delta = object_value(choice.get("delta"), "delta", nullable=True)
                text = text_value(delta.get("content"), "delta.content")
                if text:
                    content.append(text)
                    self._progress(text)
                    self.on_output(channel="content", text=text)
                    on_text(text)
                thought = text_value(delta.get("reasoning_content"), "delta.reasoning_content")
                if thought:
                    reasoning.append(thought)
                    self._progress(thought)
                    self.on_output(channel="reasoning", text=thought)
                for fragment in array_value(delta.get("tool_calls"), "delta.tool_calls"):
                    fragment = object_value(fragment, "delta.tool_calls[]")
                    index = fragment.get("index")
                    if type(index) is not int or index < 0:
                        raise ModelError("Invalid model response: tool call index must be a nonnegative integer")
                    call = calls.setdefault(index, {"id": "", "type": "function",
                                                    "function": {"name": "", "arguments": ""}})
                    call["id"] += text_value(fragment.get("id"), "tool call id")
                    function = object_value(fragment.get("function"), "tool call function", nullable=True)
                    for field in ("name", "arguments"):
                        value = text_value(function.get(field), f"tool call function.{field}")
                        call["function"][field] += value
                        self._progress(value)
                        if value:
                            self.on_output(channel=f"tool:{index}:{field}", text=value)
            return False

        while True:
            self.check_cancelled()
            raw = response.readline()
            self._last_stream_read = time.monotonic()
            byte_count += len(raw)
            self._stream_bytes = byte_count
            if not raw:
                if event_lines:
                    consume("\n".join(event_lines))
                break
            line = raw.decode("utf-8").rstrip("\r\n")
            if not line:
                if event_lines and consume("\n".join(event_lines)):
                    break
                event_lines = []
            elif line.startswith("data:"):
                event_lines.append(line[5:].lstrip(" "))
        message = {"role": "assistant", "content": "".join(content) or None}
        if calls:
            message["tool_calls"] = [calls[i] for i in sorted(calls)]
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if finish is None:
            raise ModelError("Incomplete response stream: no finish_reason", retryable=True)
        self._validate(message, finish, usage)
        return Completion(message, usage, finish)
