"""Isolated Intel/NVIDIA verifier; stdin contains evidence, never prompts or API keys."""

import asyncio
import base64
import hashlib
import hmac
import json
import time
import re
import os
from pathlib import Path
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

STEP = "Запуск проверяющего процесса"
HTTP_EVENTS = []
CHECK_FAILURES = []
SECRETS = []


class PipeEventLoop(asyncio.SelectorEventLoop):
    """Use a pipe for asyncio wakeups: the Linux sandbox forbids socketpair."""

    def _make_self_pipe(self):
        self._wake_read, self._wake_write = os.pipe()
        os.set_blocking(self._wake_read, False)
        os.set_blocking(self._wake_write, False)
        self._internal_fds += 1
        self._add_reader(self._wake_read, self._read_from_self)

    def _read_from_self(self):
        while True:
            try:
                data = os.read(self._wake_read, 4096)
                if not data:
                    break
                self._process_self_data(data)
            except InterruptedError:
                continue
            except BlockingIOError:
                break

    def _write_to_self(self):
        fd = self._wake_write
        if fd is None:
            return
        try:
            os.write(fd, b'\0')
        except OSError:
            pass  # A full pipe already wakes the loop; closing may race a thread.

    def _close_self_pipe(self):
        self._remove_reader(self._wake_read)
        os.close(self._wake_read)
        os.close(self._wake_write)
        self._wake_read = self._wake_write = None
        self._internal_fds -= 1


def run_verification(data):
    import sys
    set_step("Инициализация асинхронного процесса проверки")
    factory = PipeEventLoop if sys.platform.startswith("linux") else None
    with asyncio.Runner(loop_factory=factory) as runner:
        return runner.run(verify(data))


def write_progress():
    path = os.environ.get("PYCODER_ATTESTATION_PROGRESS")
    if path:
        try:
            Path(path).write_text(json.dumps({"stage": STAGE, "step": STEP,
                "requests": HTTP_EVENTS[-16:], "checks": [safe_detail(v) for v in CHECK_FAILURES[-16:]]}), encoding="utf-8")
        except OSError:
            pass


def set_step(step, stage=None):
    global STEP, STAGE
    STEP = step
    if stage is not None:
        STAGE = stage
    write_progress()


def safe_url(value):
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        if parts.port:
            host += ":" + str(parts.port)
        public = {"ca", "encoding", "fmspc", "update", "tcbevaluationdatanumber"}
        query = urlencode([(k, v if k.lower() in public else "[REDACTED]")
                           for k, v in parse_qsl(parts.query, keep_blank_values=True)])
        return urlunsplit((parts.scheme, host, parts.path, query, ""))
    except ValueError:
        return "[некорректный URL]"


def safe_detail(value, secrets=()):
    text = str(value)
    for secret in sorted((s for s in (*SECRETS, *secrets) if s), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"https?://[^\s<>\"']+", lambda m: safe_url(m.group()), text)
    text = re.sub(r"(?i)(Bearer\s+)[^\s,;]+", r"\1[REDACTED]", text)
    text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[JWT скрыт]", text)
    text = re.sub(r"[A-Za-z0-9+/=_-]{256,}", "[большой блок данных скрыт]", text)
    return ''.join(c if c in '\n\t' or ord(c) >= 32 and not 127 <= ord(c) < 160 else '?' for c in text)[:4000]


def exception_detail(error):
    parts, seen = [], set()
    while error is not None and id(error) not in seen and len(parts) < 5:
        seen.add(id(error))
        parts.append(f"{type(error).__name__}: {safe_detail(error)}")
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return ' → '.join(parts)[:5000]


def record_http(method, url, status=None, error=None):
    event = {"method": str(method), "url": safe_url(url), "status": status}
    if error is not None:
        event["error"] = exception_detail(error)
    HTTP_EVENTS.append(event)
    del HTTP_EVENTS[:-16]
    write_progress()
    return event


@contextmanager
def observe_http():
    import requests
    original = requests.sessions.Session.send
    def send(session, request, **kwargs):
        event = record_http(request.method, request.url)
        try:
            response = original(session, request, **kwargs)
        except Exception as error:
            event["error"] = exception_detail(error)
            write_progress()
            raise
        event["status"] = response.status_code
        write_progress()
        original_json = response.json
        def decode_json(*args, **kwargs):
            try:
                return original_json(*args, **kwargs)
            except Exception as error:
                CHECK_FAILURES.append(f"Ответ {request.method} {safe_url(request.url)} не удалось разобрать как JSON: " + exception_detail(error))
                raise
        response.json = decode_json
        return response
    requests.sessions.Session.send = send
    try:
        yield
    finally:
        requests.sessions.Session.send = original


def claim_mismatches(token, rules, prefix=""):
    result = []
    for key, expected in rules.items():
        name = prefix + key
        if key not in token:
            result.append(f"{name}: поле отсутствует; ожидалось {expected!r}")
        elif isinstance(expected, dict) and isinstance(token[key], dict):
            result.extend(claim_mismatches(token[key], expected, name + '.'))
        elif token[key] != expected:
            result.append(f"{name}: ожидалось {expected!r}; получено {token[key]!r}")
    return result


@contextmanager
def observe_nvidia():
    from nv_attestation_sdk.utils import claim_utils, nras_utils
    claims, decode, matching = claim_utils.validate_claims, nras_utils.decode_jwt_token, nras_utils.get_matching_key
    def validate(token, rules):
        result = claims(token, rules)
        if not result:
            CHECK_FAILURES.extend(claim_mismatches(token, rules))
        return result
    def decode_token(token, cert):
        try:
            result = decode(token, cert)
        except Exception as error:
            CHECK_FAILURES.append("Подпись/срок JWT NVIDIA: " + exception_detail(error))
            raise
        if result.get("x-nvidia-overall-att-result") is False:
            CHECK_FAILURES.append("x-nvidia-overall-att-result: ожидалось True; получено False")
        return result
    def find_key(data, kid):
        result = matching(data, kid)
        if result is None or not result.get("x5c"):
            CHECK_FAILURES.append(f"JWKS NVIDIA: нет ключа с сертификатом x5c для kid={kid!r}")
        return result
    claim_utils.validate_claims, nras_utils.decode_jwt_token, nras_utils.get_matching_key = validate, decode_token, find_key
    try:
        yield
    finally:
        claim_utils.validate_claims, nras_utils.decode_jwt_token, nras_utils.get_matching_key = claims, decode, matching


def failure_result(error):
    reason = exception_detail(error)
    requests = list(HTTP_EVENTS)
    # Intel's Rust HTTP client does not use requests. It reports the failing
    # GET URL/status in its exception chain; retain these instead of losing it.
    if STEP == "Загрузка проверочных данных Intel PCS":
        for url in re.findall(r'https?://[^\s<>"\')]+', str(error)):
            match = re.search(r"(?:HTTP(?: status)?|status(?: code)?)[^0-9]{0,12}([45][0-9]{2})\b", str(error), re.IGNORECASE)
            requests.append({"method": "GET", "url": safe_url(url.rstrip(',:;')),
                             "status": int(match.group(1)) if match else None})
    return {"verified": False, "error": type(error).__name__, "stage": STAGE,
            "step": STEP, "reason": reason, "requests": requests[-16:],
            "checks": [safe_detail(v) for v in CHECK_FAILURES[-16:]]}

STAGE = "Intel TDX"


def hex_field(report, field, size):
    value = report.get(field)
    if not isinstance(value, str):
        raise ValueError(f"TDX {field}: ожидалась hex-строка; получено {type(value).__name__}")
    try:
        decoded = bytes.fromhex(value)
    except ValueError:
        raise ValueError(f"TDX {field}: некорректная hex-строка") from None
    if len(decoded) != size:
        raise ValueError(f"TDX {field}: ожидалось {size} байт; получено {len(decoded)}")
    return decoded


def check_report(report, nonce, public_key, allowed_mrtd=()):
    if report.get("status") != "UpToDate":
        raise ValueError(f"TDX status: ожидалось UpToDate; получено {report.get('status')!r}")
    td = report.get("report", {}).get("TD10")
    if not isinstance(td, dict):
        raise ValueError("TDX: отсутствует объект report.TD10")
    attributes = hex_field(td, "td_attributes", 8)
    if int.from_bytes(attributes, "little") & 1:
        raise ValueError(f"TDX td_attributes.DEBUG: ожидалось 0; получено 1 (td_attributes={attributes.hex()})")
    binding = hashlib.sha256((nonce + public_key).encode()).digest()
    data = hex_field(td, "report_data", 64)
    if not hmac.compare_digest(data[:32], binding):
        raise ValueError(f"TDX report_data[:32]: привязка nonce + публичного ключа не совпадает; ожидалось SHA256={binding.hex()}; получено {data[:32].hex()}")
    mrtd = td.get("mr_td", "")
    hex_field(td, "mr_td", 48)
    if allowed_mrtd and mrtd.lower() not in allowed_mrtd:
        raise ValueError(f"TDX MRTD: получено {mrtd}; разрешены {list(allowed_mrtd)!r}")
    return binding.hex(), mrtd


async def verify(data):
    global STAGE, STEP
    set_step("Загрузка библиотеки dcap-qvl", "Intel TDX")
    from dcap_qvl import get_collateral_from_pcs, parse_quote, verify as verify_quote
    set_step("Разбор evidence.quote (base64 / TDX)")
    evidence = data["evidence"]
    raw = base64.b64decode(evidence["quote"], validate=True)
    if len(raw) < 632:
        raise ValueError(f"quote: ожидалось не менее 632 байт; получено {len(raw)}")
    quote_type = parse_quote(raw).quote_type()
    if quote_type != "TDX":
        raise ValueError(f"quote_type: ожидалось TDX; получено {quote_type!r}")
    set_step("Загрузка проверочных данных Intel PCS")
    collateral = await get_collateral_from_pcs(raw)
    set_step("Проверка подписи TDX, цепочки сертификатов и сроков действия")
    verified = verify_quote(raw, collateral, int(time.time()))
    report = json.loads(verified.to_json())
    set_step("Проверка полей TDX: status, DEBUG, report_data, MRTD")
    binding, mrtd = check_report(report, data["nonce"], data["public_key"], data.get("allowed_mrtd", []))
    set_step("Проверка наличия gpu_evidence", "NVIDIA GPU")
    gpu = evidence.get("gpu_evidence")
    if not isinstance(gpu, list) or not gpu:
        raise ValueError("GPU: отсутствуют доказательства аттестации")
    set_step("Загрузка nv-attestation-sdk")
    from nv_attestation_sdk.attestation import Attestation, Devices, Environment
    client = Attestation()
    client.add_verifier(Devices.GPU, Environment.REMOTE, "", "")
    client.set_nonce(binding)
    set_step("Запрос NVIDIA NRAS и проверка подписи результата")
    with observe_nvidia():
        if client.attest(gpu) is not True:
            raise ValueError("NVIDIA attest вернул False; подробности HTTP/JWT приведены ниже")
    policy = json.dumps({"authorization-rules": {
        "type": "JWT", "overall-claims": {"x-nvidia-overall-att-result": True, "eat_nonce": binding},
        "detached-claims": {"eat_nonce": binding, "secboot": True, "dbgstat": "disabled",
                            "x-nvidia-gpu-attestation-report-nonce-match": True}}})
    set_step("Проверка политики NVIDIA: nonce, secure boot, debug, подпись JWT")
    with observe_nvidia():
        if client.validate_token(policy) is not True:
            raise ValueError("NVIDIA validate_token вернул False; несовпадения полей приведены ниже")
    return {"verified": True, "mrtd": mrtd}


if __name__ == "__main__":
    import contextlib
    import logging
    import os
    import sys
    # NVIDIA SDK uses global state and may create log files; the caller supplies
    # a disposable private working directory and a hard timeout for this process.
    logging.disable(logging.CRITICAL)
    try:
        data = json.load(sys.stdin)
        SECRETS = [data.get("nonce", ""), data.get("public_key", ""), data.get("evidence", {}).get("quote", "")]
        with contextlib.redirect_stdout(sys.stderr):
            with observe_http():
                result = run_verification(data)
    except Exception as error:
        result = failure_result(error)
    print(json.dumps(result))
