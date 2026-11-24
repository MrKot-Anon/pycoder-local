"""Per-turn accounting, including retries and compaction requests."""

import time
import threading


class TurnUsage:
    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.input_tokens = self.output_tokens = self.requests = self.unknown = 0
        self.exact = True
        self.total_tokens = 0
        self.exact_total = True

    def add(self, *, input_tokens, output_tokens, exact, unknown=False, total_tokens=None):
        with self.lock:
            self.requests += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.exact &= exact
            self.total_tokens += total_tokens if total_tokens is not None else input_tokens + output_tokens
            self.exact_total &= total_tokens is not None or exact
            self.unknown += bool(unknown)

    def result(self, outcome):
        return dict(elapsed=time.monotonic() - self.started, input_tokens=self.input_tokens,
                    output_tokens=self.output_tokens, requests=self.requests, exact=self.exact,
                    total_tokens=self.total_tokens, exact_total=self.exact_total,
                    unknown=self.unknown, outcome=outcome)


def summary_text(data):
    seconds = int(data["elapsed"])
    duration = f"{seconds // 60:02d}:{seconds % 60:02d}"
    prefix = "" if data["exact"] else "≈"
    total = data.get("total_tokens", data["input_tokens"] + data["output_tokens"])
    total_prefix = "" if data.get("exact_total", data["exact"]) else "≈"
    title = {"done": "Обработка завершена", "error": "Обработка остановлена", "cancelled": "Прервано"}[data["outcome"]]
    return (f"{title} · всего {duration} · {total_prefix}{total:,} токенов "
            f"(вход {prefix}{data['input_tokens']:,}, выход {prefix}{data['output_tokens']:,}) · запросов {data['requests']}"
            + (" · расход при сбоях известен не полностью" if data["unknown"] else ""))
