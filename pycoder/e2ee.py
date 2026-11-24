"""Chutes E2EE protocol, using upstream crypto and independent hardware verification."""

import base64
from collections import deque
from dataclasses import dataclass
from importlib import resources
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote


class EncryptionError(Exception):
    pass


def crypto():
    try:
        from chutes_e2ee import crypto as implementation
        return implementation
    except (ImportError, OSError):
        raise EncryptionError("Нет совместимых библиотек E2EE. Выполните pycoder --setup-e2ee и перезапустите приложение.") from None


def verification_failure(result):
    from .attestation import safe_detail
    stage = result.get("stage")
    stage = stage if stage in {"Intel TDX", "NVIDIA GPU"} else "Intel/NVIDIA"
    lines = [f"Аппаратная аттестация {stage} не пройдена"]
    if result.get("step"):
        lines.append("Этап: " + safe_detail(result["step"]))
    if result.get("reason"):
        lines.append("Причина: " + safe_detail(result["reason"]))
    else:
        lines.append("Ошибка: " + safe_detail(result.get("error", "неизвестна")))
    for event in result.get("requests", [])[-16:]:
        status = event.get("status")
        outcome = f"HTTP {status}" if type(status) is int else "ответ HTTP не получен"
        line = f"Запрос: {event.get('method', '?')} {event.get('url', '?')} → {outcome}"
        if event.get("error"):
            line += "; " + str(event["error"])
        lines.append(safe_detail(line))
    for mismatch in result.get("checks", [])[-16:]:
        lines.append("Проверка: " + safe_detail(mismatch))
    if result.get("stderr"):
        lines.append("Диагностика процесса (stderr):\n" + safe_detail(result["stderr"]))
    if result.get("step") == "Загрузка проверочных данных Intel PCS" and not result.get("requests"):
        if result.get("error") == "VerifierProcessError":
            lines.append("Процесс завершился без результата проверки. HTTP-запрос не подтверждён; точный URL неизвестен.")
        else:
            lines.append("Intel dcap-qvl не сообщил точный URL; выше приведена причина из библиотеки.")
    if result.get("error") in {"ImportError", "ModuleNotFoundError"}:
        lines.append("Установите библиотеки проверки: pycoder --setup-e2ee.")
    return '\n'.join(lines)[:16000]


def verify_hardware(evidence, nonce, public_key, allowed_mrtd, check_cancelled, proxy_config=None):
    source = resources.files("pycoder").joinpath("attestation.py").read_text()
    payload = json.dumps(dict(evidence=evidence, nonce=nonce, public_key=public_key, allowed_mrtd=allowed_mrtd))
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "SYSTEMROOT", "WINDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
    with tempfile.TemporaryDirectory(prefix="pycoder-attestation-") as directory:
        from pathlib import Path
        progress_path = Path(directory) / "attestation-progress.json"
        env.update(HOME=directory, TMPDIR=directory, TEMP=directory, TMP=directory,
                   PYCODER_ATTESTATION_PROGRESS=str(progress_path))
        def last_progress():
            try:
                result = json.loads(progress_path.read_text(encoding="utf-8"))
                return result if isinstance(result, dict) else {}
            except (OSError, ValueError):
                return {}
        sandbox = None
        from .e2ee_setup import python_executable
        args = [python_executable(), "-I", "-c", source]
        if proxy_config is not None and proxy_config.proxy_enabled:
            from dataclasses import replace
            from pathlib import Path
            from .sandbox import Sandbox
            from .proxy import attestation_route
            sandbox = Sandbox(Path(directory), replace(proxy_config, sandbox_network="all"),
                              proxy_route=lambda host: attestation_route(proxy_config, host), verifier_socketpair=True)
            try:
                args, sandbox_env = sandbox.prepare(args, directory)
                env.update(sandbox_env)
            except Exception:
                sandbox.close()
                raise EncryptionError("Не удалось изолировать аттестацию: прямое подключение запрещено") from None
        stderr_file = tempfile.TemporaryFile()
        def process_details():
            from .attestation import safe_detail
            size = os.fstat(stderr_file.fileno()).st_size
            offset = max(0, size - 16000)
            if hasattr(os, "pread"):
                data = os.pread(stderr_file.fileno(), 16000, offset)
            else:
                stderr_file.seek(offset)
                data = stderr_file.read(16000)
            secrets = [nonce, public_key, evidence.get("quote", "")]
            if proxy_config is not None:
                secrets += [proxy_config.proxy_password, proxy_config.proxy_second_password]
            return safe_detail(data.decode("utf-8", errors="replace"), secrets=secrets)
        try:
            process = subprocess.Popen(args, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=stderr_file,
                                       text=True, cwd=directory, env=env)
        except BaseException:
            stderr_file.close()
            if sandbox:
                sandbox.close()
            raise
        deadline = time.monotonic() + 90
        try:
            while True:
                check_cancelled()
                if time.monotonic() >= deadline:
                    result = last_progress()
                    result.update(error="TimeoutError", reason="Истекло время проверки Intel/NVIDIA (90 с)")
                    result["stderr"] = process_details()
                    raise EncryptionError(verification_failure(result))
                try:
                    output, _ = process.communicate(payload, timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    payload = None
            try:
                result = json.loads(output)
            except ValueError:
                result = last_progress()
                result.update(error="VerifierProcessError", reason=f"Проверяющий процесс не вернул JSON; код завершения {process.returncode}")
                result["stderr"] = process_details()
                raise EncryptionError(verification_failure(result)) from None
            if not isinstance(result, dict):
                raise EncryptionError("Проверяющий процесс вернул результат неверного формата")
            if process.returncode or result.get("verified") is not True:
                result["stderr"] = process_details()
                raise EncryptionError(verification_failure(result))
            return result
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()
            stderr_file.close()
            if sandbox:
                sandbox.close()


@dataclass
class Envelope:
    body: bytes
    response_key: bytes
    headers: dict
    instance_id: str


class ChutesEncryption:
    def __init__(self, fetch, check_cancelled, allowed_mrtd=(), proxy_config=None):
        self.proxy_config = proxy_config
        self.fetch, self.check_cancelled = fetch, check_cancelled
        self.allowed_mrtd = tuple(allowed_mrtd)
        self.models = []
        self.models_until = 0
        self.pool = []
        self.pool_scope = None
        self.pool_until = 0
        self.verified = {}
        self.background = None
        self.servers = None
        self.retry_instance = None
        self.retry_excluded = set()
        self.retry_attempts = {}

    def discover(self, model):
        self.check_cancelled()
        now = time.monotonic()
        if now >= self.models_until:
            data = self.fetch("llm.chutes.ai", "/v1/models")
            self.models = data.get("data", [])
            self.models_until = now + 300
        matches = [m for m in self.models if isinstance(m, dict) and m.get("id") == model]
        if len(matches) != 1 or matches[0].get("confidential_compute") is not True:
            raise EncryptionError("Каталог Chutes не подтверждает confidential_compute=true для этой модели")
        chute = matches[0].get("chute_id")
        if not isinstance(chute, str) or not chute:
            raise EncryptionError("Каталог Chutes не содержит chute_id")
        cached = self.servers.cached(chute) if self.servers else None
        if cached:
            self.pool, self.pool_until = cached
            self.pool_scope = chute
        if self.pool_scope != chute or time.monotonic() >= self.pool_until or not any(i.get("nonces") for i in self.pool):
            started = time.monotonic()
            data = self.fetch("api.chutes.ai", "/e2e/instances/" + quote(chute, safe=""))
            self.pool = data.get("instances", [])
            self.pool_scope = chute
            ttl = data.get("nonce_expires_in", 0)
            if type(ttl) not in (int, float) or ttl <= 5 or not isinstance(self.pool, list):
                raise EncryptionError("Chutes вернул некорректный срок действия nonce или список серверов")
            self.pool_until = started + min(ttl, 60) - 5
            if self.servers:
                self.servers.publish(chute, self.pool, self.pool_until)
        return chute

    def scope(self, instance):
        identifier, public_key = instance.get("instance_id"), instance.get("e2e_pubkey")
        if not isinstance(identifier, str) or not isinstance(public_key, str):
            raise EncryptionError("Chutes вернул некорректные данные сервера")
        if len(base64.b64decode(public_key, validate=True)) != 1184:
            raise EncryptionError("Неверный размер ключа ML-KEM-768")
        return identifier, public_key, self.allowed_mrtd

    def attest(self, scope, *, direct_attestation=False):
        identifier, public_key, pins = scope
        nonce = secrets.token_hex(32)
        evidence = self.fetch("api.chutes.ai", "/instances/" + quote(identifier, safe="") + "/evidence?nonce=" + nonce)
        if evidence.get("instance_id") not in (None, identifier):
            raise EncryptionError("Аттестация относится к другому серверу")
        verifier_config = self.proxy_config
        if direct_attestation and verifier_config is not None:
            from dataclasses import replace
            verifier_config = replace(verifier_config, proxy_intel=False, proxy_nvidia=False)
        verify_hardware(evidence, nonce, public_key, pins, self.check_cancelled,
                        **({"proxy_config": verifier_config} if verifier_config is not None and verifier_config.proxy_enabled else {}))
        self.verified[scope] = time.monotonic() + 300

    def prepare(self, model, payload, _refreshes=0, *, verify_attestation=True, direct_attestation=False):
        if verify_attestation and self.background and not direct_attestation:
            self.background.require(None, self.check_cancelled)
        chute = self.discover(model)
        impl = crypto()
        instances = [i for i in self.pool if isinstance(i, dict) and i.get("nonces")]
        if verify_attestation and self.background:
            # Prefer an available, already verified key over a new unverified server.
            instances.sort(key=lambda item: not self.background.valid(self.scope(item)))
        if self.servers:
            instances = self.servers.order(instances, self.retry_excluded, self.retry_attempts)
            if self.retry_instance:
                preferred = [i for i in instances if i.get("instance_id") == self.retry_instance]
                if preferred:
                    instances = preferred
        for instance in instances:
            if not isinstance(instance, dict) or not instance.get("nonces"):
                continue
            scope = self.scope(instance)
            identifier, public_key, _ = scope
            if verify_attestation:
                if self.background:
                    if direct_attestation and _refreshes == 0:
                        self.background.refresh(scope, direct=True)
                    self.background.require(scope, self.check_cancelled)
                elif (direct_attestation and _refreshes == 0) or self.verified.get(scope, 0) <= time.monotonic():
                    self.attest(scope, direct_attestation=direct_attestation)
            if time.monotonic() >= self.pool_until:
                self.pool = []
                # Attestation can take longer than a nonce lives. Refresh once
                # while retaining the verified key; a new key is always reverified.
                if _refreshes == 0:
                    return self.prepare(model, payload, _refreshes=1, verify_attestation=verify_attestation, direct_attestation=direct_attestation)
                raise EncryptionError("Nonce истёк во время аттестации. Повторите проверку для получения нового nonce")
            nonce = self.servers.nonce(instance) if self.servers else instance["nonces"].pop(0)
            if not isinstance(nonce, str) or not nonce:
                raise EncryptionError("Некорректный одноразовый nonce")
            self.check_cancelled()
            encrypted = impl.build_e2ee_request(public_key, payload)
            return Envelope(encrypted.blob, encrypted.response_sk, {
                "X-Chute-Id": chute, "X-Instance-Id": identifier, "X-E2E-Nonce": nonce,
                "X-E2E-Stream": str(bool(payload.get("stream"))).lower(),
                "X-E2E-Path": "/v1/chat/completions", "Content-Type": "application/octet-stream"}, identifier)
        raise EncryptionError("Нет доступных серверов Chutes с E2EE и одноразовыми nonce")


class DecryptedStream:
    """Expose authenticated SSE as readline() to the existing completion parser."""
    def __init__(self, response, response_key, check_cancelled):
        self.response, self.response_key, self.check_cancelled = response, response_key, check_cancelled
        self.key = None
        self.lines = deque()
        self.nonces = set()
        self.impl = crypto()

    def readline(self):
        while not self.lines:
            self.check_cancelled()
            line = self.response.readline(4 * 1024 * 1024 + 1)
            if len(line) > 4 * 1024 * 1024:
                raise EncryptionError("Слишком большой фрагмент E2EE")
            if not line:
                return b""
            if not line.startswith(b"data:"):
                continue
            raw = line[5:].strip()
            if raw == b"[DONE]":
                return b"data: [DONE]\n\n"
            if not raw:
                continue
            try:
                event = json.loads(raw)
                if "e2e_init" in event:
                    if self.key is not None:
                        raise EncryptionError("Повторная инициализация E2EE-потока")
                    self.key = self.impl.decrypt_stream_init(self.response_key, event["e2e_init"])
                elif "e2e" in event:
                    if self.key is None:
                        raise EncryptionError("Фрагмент E2EE до обмена ключами")
                    raw_chunk = base64.b64decode(event["e2e"], validate=True)
                    nonce = raw_chunk[:12]
                    if nonce in self.nonces:
                        raise EncryptionError("Повторный фрагмент E2EE")
                    self.nonces.add(nonce)
                    text = self.impl.decrypt_stream_chunk(event["e2e"], self.key)
                    self.lines.extend(io.BytesIO((text.rstrip("\r\n") + "\n\n").encode()).readlines())
                elif "usage" in event and not event.get("choices"):
                    # Chutes sends unauthenticated billing metadata. It must
                    # never supply assistant text, tools or finish_reason.
                    self.lines.extend([b"data: " + json.dumps({"usage": event["usage"]}).encode() + b"\n", b"\n"])
                elif "e2e_error" in event:
                    raise EncryptionError("Сервер сообщил об ошибке E2EE")
                else:
                    raise EncryptionError("Незашифрованные или неизвестные данные в E2EE-потоке")
            except EncryptionError:
                raise
            except Exception:
                raise EncryptionError("Не удалось проверить и расшифровать фрагмент ответа") from None
        return self.lines.popleft()


def decrypt_body(response, key):
    try:
        return json.dumps(crypto().decrypt_response(response.read(), key)).encode()
    except EncryptionError:
        raise
    except Exception:
        raise EncryptionError("Не удалось проверить и расшифровать ответ") from None


def fallback_question(choose, reason, *, allow_direct=False):
    options = [
        ("stop", "Остановить — ничего не отправлять"),
        ("retry", "Повторить проверку E2EE"),
        ("plain_once", "Отправить этот запрос без E2EE")]
    if allow_direct:
        options.append(("attestation_direct_once", "Повторить аттестацию без прокси (один раз)"))
    return choose("Сквозное шифрование недоступно", options, default="stop",
        detail=reason + ("\nПрямой повтор: GET каталога, ключей и evidence Chutes, проверка Intel/NVIDIA — напрямую; запрос модели сохраняет выбранный маршрут." if allow_direct else "") + "\nБез E2EE останется HTTPS, но провайдер сможет прочитать запрос и ответ. "
        "Разрешение действует только на один запрос, не сохраняется и не передаётся субагентам.")


def background_question(request, reason, recovered, stopped, *, allow_direct=False):
    def choose(title, options, **kwargs):
        options = [(key, "Продолжать ждать фоновую аттестацию" if key == "retry" else label)
                   for key, label in options]
        return request("Фоновая аттестация требует внимания", options,
                       auto_resolve=recovered, cancelled=stopped, **kwargs)
    return fallback_question(choose, reason, allow_direct=allow_direct)
