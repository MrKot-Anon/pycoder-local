"""Thread-safe E2EE instance selection, nonce pool and per-model request counters."""
import json
import threading
import time

from .util import private_json


class ServerSelectionError(RuntimeError):
    pass


class Servers:
    def __init__(self, path=None):
        self.lock = threading.RLock()
        self.path = path
        self.rows = {}
        self.pool = []
        self.until = 0
        self.chute = None
        self.selected = None
        self.auto_switch = True
        self.exhausted_action = "stop"
        self.partial_action = "stop_switch"
        self.retries_per_server = 1
        self.total_retries = 10
        self.refreshing = False
        self.refresh_error = ""
        self.storage_error = ""
        self.updated = None
        if path and path.exists():
            try:
                data = json.loads(path.read_text())
                for identifier, row in data.get("rows", {}).items():
                    if not isinstance(identifier, str) or not isinstance(row, dict):
                        continue
                    self.rows[identifier] = {key: value for key, value in row.items()
                        if key in {"success", "errors", "interrupted", "cancelled", "attempts"}
                        and type(value) is int and value >= 0}
                self.auto_switch = data.get("auto_switch", True) is not False
                if data.get("exhausted_action") in {"stop", "cycle"}:
                    self.exhausted_action = data["exhausted_action"]
                if data.get("partial_action") in {"stop", "stop_switch", "switch_retry"}:
                    self.partial_action = data["partial_action"]
                for name in ("retries_per_server", "total_retries"):
                    value = data.get(name)
                    if type(value) is int and 0 <= value <= 100:
                        setattr(self, name, value)
            except (OSError, ValueError, AttributeError, TypeError):
                self.storage_error = "Не удалось прочитать статистику серверов"

    def _save(self):
        if self.path:
            try:
                private_json(self.path, {"auto_switch": self.auto_switch,
                    "exhausted_action": self.exhausted_action,
                    "partial_action": self.partial_action,
                    "retries_per_server": self.retries_per_server, "total_retries": self.total_retries,
                    "rows": {identifier: {k: row.get(k, 0) for k in
                        ("success", "errors", "interrupted", "cancelled", "attempts")}
                        for identifier, row in self.rows.items()}})
                self.storage_error = ""
            except OSError:
                self.storage_error = "Не удалось сохранить статистику серверов"

    def cached(self, chute):
        with self.lock:
            if self.chute == chute and time.monotonic() < self.until and any(i.get("nonces") for i in self.pool):
                return self.pool, self.until
            return None

    def publish(self, chute, pool, until):
        with self.lock:
            self.chute, self.pool, self.until = chute, pool, until
            for item in pool:
                identifier = item.get("instance_id")
                if isinstance(identifier, str):
                    self.rows.setdefault(identifier, {})
            self.updated = time.strftime("%H:%M:%S")
            self.refresh_error = ""

    def choose(self, identifier):
        with self.lock:
            if identifier is not None and identifier not in {i.get("instance_id") for i in self.pool}:
                raise ServerSelectionError("Сервер уже отсутствует в списке. Обновите список.")
            self.selected = identifier
            if identifier is not None:
                self.rows.setdefault(identifier, {})["failed_until"] = 0

    def set_option(self, name, value):
        if name == "auto_switch":
            if type(value) is not bool:
                raise ValueError("Нужно логическое значение")
        elif name == "exhausted_action":
            if value not in {"stop", "cycle"}:
                raise ValueError("Неизвестное действие при исчерпании серверов")
        elif name == "partial_action":
            if value not in {"stop", "stop_switch", "switch_retry"}:
                raise ValueError("Неизвестное действие")
        elif name in {"retries_per_server", "total_retries"}:
            if type(value) is not int or not 0 <= value <= 100:
                raise ValueError("Число повторов должно быть от 0 до 100")
        else:
            raise ValueError("Unknown server option")
        with self.lock:
            setattr(self, name, value)
            self._save()

    def exclude(self, identifier):
        with self.lock:
            self.rows.setdefault(identifier, {})["failed_until"] = time.monotonic() + 60
            if self.selected == identifier:
                self.selected = None

    def order(self, instances, excluded=None, attempts=None):
        with self.lock:
            now = time.monotonic()
            candidates = [i for i in instances if i.get("nonces")]
            if self.selected:
                candidates = [i for i in candidates if i.get("instance_id") == self.selected]
                if not candidates:
                    raise ServerSelectionError("Выбранный сервер отсутствует в текущем списке. Обновите список или выберите «Автоматически».")
            available = candidates
            candidates = [i for i in available
                if i.get("instance_id") not in (excluded or ())
                and self.rows.get(i.get("instance_id"), {}).get("failed_until", 0) <= now]
            if not candidates and available and self.exhausted_action == "cycle":
                # Only a genuinely exhausted round can reset exclusions. Keep
                # the transport's total retry budget and ordinary backoff intact.
                if excluded is not None:
                    excluded.clear()
                if attempts is not None:
                    attempts.clear()
                for item in available:
                    self.rows.setdefault(item["instance_id"], {})["failed_until"] = 0
                candidates = available
            if not candidates:
                raise ServerSelectionError("Нет доступного сервера без недавних ошибок. Обновите список или выберите сервер вручную.")
            return candidates

    def nonce(self, instance):
        with self.lock:
            if not instance.get("nonces"):
                raise ServerSelectionError("Одноразовые nonce сервера закончились. Повторите запрос.")
            return instance["nonces"].pop(0)

    def begin(self, identifier):
        with self.lock:
            row = self.rows.setdefault(identifier, {})
            row["attempts"] = row.get("attempts", 0) + 1
            row["active"] = row.get("active", 0) + 1
            self._save()

    def finish(self, identifier, outcome, partial=False):
        with self.lock:
            row = self.rows.setdefault(identifier, {})
            row["active"] = max(0, row.get("active", 0) - 1)
            row[outcome] = row.get(outcome, 0) + 1
            if outcome == "errors":
                if partial:
                    row["interrupted"] = row.get("interrupted", 0) + 1
            self._save()

    def snapshot(self):
        with self.lock:
            available = {i.get("instance_id") for i in self.pool}
            return {"rows": [{"id": identifier, **row, "available": identifier in available}
                            for identifier, row in self.rows.items()],
                    "selected": self.selected, "auto_switch": self.auto_switch,
                    "exhausted_action": self.exhausted_action,
                    "partial_action": self.partial_action,
                    "retries_per_server": self.retries_per_server, "total_retries": self.total_retries, "refreshing": self.refreshing,
                    "error": self.refresh_error or self.storage_error, "updated": self.updated,
                    "expired": time.monotonic() >= self.until}
