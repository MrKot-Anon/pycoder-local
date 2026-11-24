"""Explicit user decisions for server-declared output truncation."""


def resolve_output_limit(error, choose, input_fn):
    compacting = getattr(error, "operation", "") == "compaction"
    options = [] if compacting else [("fragment", "Оставить лимит и выполнять задачу по фрагментам")]
    if error.capacity > error.limit:
        options.append(("increase", "Увеличить лимит и повторить запрос"))
    options.append(("stop", "Остановиться и сохранить историю"))
    action = choose("Сводка контекста оборвана по лимиту" if compacting else "Модель оборвала ответ по лимиту. Что делать?", options, default="stop" if compacting else "fragment",
                    detail=f"В запросе: {error.limit} токенов. Свободно под ответ примерно: {error.capacity}. "
                    "Ни один инструмент из этого ответа не выполнен. Сервер может отклонить лимит выше заявленного в каталоге.")
    if action != "increase":
        return (action, None) if action == "fragment" else None
    suggested = min(error.limit * 2, error.capacity)
    while True:
        text = input_fn(f"Новый лимит: больше {error.limit}, не более {error.capacity} [{suggested}]: ").strip()
        if not text:
            text = str(suggested)
        if text.isdecimal() and error.limit < int(text) <= error.capacity:
            return "increase", int(text)
