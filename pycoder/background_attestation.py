"""Session-owned attestation worker, renewal deadlines and recoverable prompts."""

import threading
import time

from .e2ee import ChutesEncryption, EncryptionError
from .events import Cancelled


class AttestationDecision(EncryptionError):
    def __init__(self, action, reason):
        super().__init__(reason)
        self.action = action


class BackgroundAttestation:
    RENEW_AFTER = 300
    TRUST_FOR = 480
    INITIAL_WAIT = 180
    RETRY_AFTER = 15

    def __init__(self, model, transport, ask, status=lambda **data: None, *, proactive=False, clock=None):
        self.now = clock or time.monotonic
        self.model, self.transport = model, transport
        self.ask, self.status = ask, status
        self.proactive = proactive
        self.stopped = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.RLock()
        self.question_lock = threading.Lock()
        self.engine = ChutesEncryption(transport._encryption_metadata, self.check,
            transport.config.e2ee_mrtd, proxy_config=transport.config)
        self.current = None
        self.target_requested = False
        self.started = self.now()
        self.successes = {}
        self.error = "Первичная аттестация ещё не завершена"
        self.next_attempt = 0
        self.snooze_until = 0
        self.direct_once = False
        self.decision = None
        self.suppressed = False
        self.worker = threading.Thread(target=self._run, name="pycoder-attestation", daemon=True)
        self.monitor = threading.Thread(target=self._monitor, name="pycoder-attestation-watch", daemon=True)

    def start(self):
        self.status(mode="checking", detail="Первичная аттестация Intel/NVIDIA выполняется в фоне")
        self.worker.start()
        if self.proactive:
            self.monitor.start()

    def check(self):
        if self.stopped.is_set():
            raise Cancelled()

    def valid(self, scope):
        with self.lock:
            stamp = self.successes.get(scope)
            return stamp is not None and self.now() - stamp < self.TRUST_FOR

    def _warning_due(self):
        stamp = self.successes.get(self.current)
        deadline = stamp + self.TRUST_FOR if stamp is not None else self.started + self.INITIAL_WAIT
        return self.now() >= max(deadline, self.snooze_until)

    def _run(self):
        while not self.stopped.is_set():
            self.wake.clear()
            with self.lock:
                delay = self.next_attempt - self.now()
                scope, direct = self.current, self.direct_once
                requested = self.target_requested
                if delay <= 0:
                    self.direct_once = False
            if delay > 0:
                self.wake.wait(min(delay, 1))
                continue
            try:
                # This transport is private to the worker: overrides cannot leak
                # into concurrent generation or future attestation attempts.
                self.transport._direct_attestation = direct
                if scope is None or not requested:
                    self.engine.discover(self.model)
                    candidates = [i for i in self.engine.pool if isinstance(i, dict) and i.get("nonces")]
                    if not candidates:
                        raise EncryptionError("Нет доступных серверов Chutes для аттестации")
                    available = [self.engine.scope(item) for item in candidates]
                    selected = scope if scope in available else available[0]
                    with self.lock:
                        if self.current == scope:
                            if selected != scope:
                                self.started = self.now()
                                self.snooze_until = 0
                            self.current = selected
                    scope = selected
                self.engine.attest(scope, direct_attestation=direct)
                self.check()
                with self.lock:
                    now = self.now()
                    self.successes = {key: stamp for key, stamp in self.successes.items() if now - stamp < self.TRUST_FOR}
                    self.successes[scope] = now
                    current_success = self.current == scope
                    if current_success:
                        self.target_requested = False
                        self.error = ""
                        self.next_attempt = 0 if self.direct_once else now + self.RENEW_AFTER
                        self.snooze_until = 0
                if current_success:
                    self.status(mode="encrypted", detail="Фоновая аттестация успешна · Intel TDX и NVIDIA проверены")
            except Cancelled:
                break
            except Exception as error:
                with self.lock:
                    if self.current in (None, scope):
                        self.error = str(error)
                        retry = getattr(error, "retry_after", None)
                        self.next_attempt = self.now() + max(self.RETRY_AFTER, retry or 0)
            finally:
                self.transport._direct_attestation = False

    def _monitor(self):
        while not self.stopped.wait(0.1):
            self._prompt_if_due()

    def _prompt_if_due(self):
        with self.lock:
            if self.suppressed or not self._warning_due():
                return
        if not self.question_lock.acquire(blocking=False):
            return
        try:
            with self.lock:
                scope = self.current
                if self.suppressed or not self._warning_due():
                    return
                stamp = self.successes.get(scope)
                reason = ("С последней успешной аттестации прошло более 8 минут." if stamp is not None else
                          "Первичная аттестация не завершилась за 3 минуты.")
                reason += "\nФоновая проверка продолжается. Запросы ждут; при успехе вопрос закроется автоматически.\n" + self.error
            def recovered():
                with self.lock:
                    return self.stopped.is_set() or self.current != scope or self.valid(scope)
            self.status(mode="blocked", detail=reason)
            action = self.ask(reason, recovered, self.stopped)
            with self.lock:
                if action == "recovered" or self.stopped.is_set():
                    return
                if action in {"retry", "attestation_direct_once"}:
                    self.direct_once = action == "attestation_direct_once"
                    self.next_attempt = 0
                    self.snooze_until = self.now() + self.INITIAL_WAIT
                    self.wake.set()
                else:
                    self.decision = (scope, action, reason)
                    self.suppressed = True
        except Cancelled:
            # Cancelling a turn must not kill the session's renewal worker.
            with self.lock:
                self.suppressed = True
        finally:
            self.question_lock.release()

    def require(self, scope, check_cancelled):
        follow_current = scope is None
        with self.lock:
            if scope is None:
                scope = self.current
            if scope != self.current:
                self.current = scope
                self.target_requested = True
                self.started = self.now()
                self.next_attempt = 0
                self.snooze_until = 0
                self.suppressed = False
                self.wake.set()
            elif self.decision is None:
                self.suppressed = False
        while True:
            check_cancelled()
            self.check()
            with self.lock:
                if self.decision:
                    _, action, reason = self.decision
                    self.decision = None
                    raise AttestationDecision(action, reason)
                if follow_current:
                    scope = self.current
                if self.valid(scope) and not self.question_lock.locked():
                    return
            self._prompt_if_due()
            self.stopped.wait(0.1)

    def refresh(self, scope, *, direct=False):
        with self.lock:
            self.current = scope
            self.target_requested = True
            self.successes.pop(scope, None)
            self.started = self.now()
            self.snooze_until = 0
            self.direct_once = direct
            self.next_attempt = 0
            self.wake.set()

    def close(self):
        self.stopped.set()
        self.wake.set()
        self.transport.cancel()
        for thread in (self.worker, self.monitor):
            if thread.ident is not None and thread is not threading.current_thread():
                thread.join(timeout=2)
