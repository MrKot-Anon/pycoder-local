"""Textual frontend. Agent state is owned exclusively by Runtime's worker."""

import json
import asyncio
import time
import sys
from dataclasses import replace
from concurrent.futures import InvalidStateError
from queue import Empty

from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.content import Content
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Collapsible, Footer, Input, OptionList, Static, TextArea, TabbedContent, TabPane, RichLog, Checkbox, Select
from textual.widgets.option_list import Option

from .runtime import Runtime
from .util import terminal_text, truncate
from .presentation import tool_label, user_prose, shorten
from .clipboard import write_clipboard, read_clipboard
from .plans import plan_lines
from .tool_output import read_page, format_output
from .usage import summary_text
from .config import Config
from .settings import LIMITS, SANDBOX_FIELDS, SECURITY_FIELDS, encryption_label
from .platform_support import shortcut_help


class SelectableStatic(Static):
    """Convert rich renderables into styled text, preserving real selection offsets."""
    def render(self):
        content = self.content
        if isinstance(content, str):
            return Content(content)
        if isinstance(content, Text):
            return Content.from_rich_text(content, console=self.app.console)
        width = max(1, self.content_size.width or self.app.size.width - 6)
        text = Text()
        for segment in self.app.console.render(content, self.app.console.options.update(width=width)):
            if not segment.control:
                text.append(segment.text, segment.style)
        text.rstrip()
        return Content.from_rich_text(text, console=self.app.console)


class ContextMouse:
    def _forward_event(self, event):
        # Intercept before Textual starts a new selection for the right button.
        # Otherwise the existing selection disappears before the menu can copy it.
        if isinstance(event, events.MouseDown) and event.button == 3:
            widget, _ = self.get_widget_at(event.x, event.y)
            self.app.action_copy_menu(widget)
            return
        if isinstance(event, events.MouseUp) and event.button == 3:
            return
        super()._forward_event(event)


class TranscriptScreen(ContextMouse, Screen):
    pass


class OutputScreen(ContextMouse, ModalScreen):
    """Paged transcript: arbitrary output size without an unbounded UI widget."""
    BINDINGS = [Binding("escape", "dismiss(None)", "Закрыть", priority=True)]
    DEFAULT_CSS = """
    OutputScreen { align: center middle; background: $background 80%; }
    OutputScreen > Vertical { width: 95%; height: 90%; border: round #79c0ff; padding: 1; }
    OutputScreen VerticalScroll { height: 1fr; }
    OutputScreen .output-position { height: auto; color: #8b949e; }
    OutputScreen Horizontal { height: 3; }
    OutputScreen Button { min-width: 12; margin-right: 1; }
    """

    def __init__(self, path):
        super().__init__()
        self.path, self.page_offset, self.page_end, self.file_size = path, 0, 0, 0
        self.previous = []
        self.follow = False

    def compose(self):
        with Vertical():
            yield Static(self.path, markup=False, classes="output-position")
            yield Static("", id="output-position", markup=False, classes="output-position")
            with VerticalScroll():
                yield SelectableStatic("", id="output-page", markup=False)
            with Horizontal():
                yield Button("Начало", id="output-first")
                yield Button("Назад", id="output-prev")
                yield Button("Далее", id="output-next")
                yield Button("Следить", id="output-tail")
                yield Button("Закрыть", id="output-close")

    async def on_mount(self):
        await self.load_page()
        self.set_interval(0.5, self.refresh_tail)

    async def load_page(self):
        try:
            text, self.page_offset, self.page_end, self.file_size = await asyncio.to_thread(read_page, self.path, self.page_offset, last=self.follow)
        except OSError as error:
            self.query_one("#output-position", Static).update(f"Не удалось прочитать вывод: {type(error).__name__}")
            return
        self.query_one("#output-page", Static).update(self.app.runtime.safe(text))
        self.query_one("#output-position", Static).update(
            f"Байты {self.page_offset}–{self.page_end} / {self.file_size} · " + ("слежение за хвостом" if self.follow else "полный вывод по страницам"))
        self.query_one("#output-next", Button).disabled = self.page_end >= self.file_size
        self.query_one("#output-prev", Button).disabled = not self.previous and not self.page_offset
        if self.follow:
            self.call_after_refresh(self.query_one(VerticalScroll).scroll_end, animate=False)

    async def refresh_tail(self):
        if self.follow:
            await self.load_page()

    async def on_button_pressed(self, event):
        event.stop()
        action = event.button.id
        if action == "output-close":
            self.dismiss(None)
            return
        self.follow = action == "output-tail"
        if action == "output-first":
            self.page_offset, self.previous = 0, []
        elif action == "output-next":
            self.previous.append(self.page_offset)
            self.page_offset = self.page_end
        elif action == "output-prev":
            self.page_offset = self.previous.pop() if self.previous else 0
        await self.load_page()
        if not self.follow:
            self.query_one(VerticalScroll).scroll_home(animate=False)


class ToolPreview(SelectableStatic):
    def render(self):
        width = max(1, self.content_size.width - 4)
        lines = Text(str(self.content) or "(no output)").wrap(self.app.console, width)[-5:]
        text = Text("\n".join(("  └ " if i == 0 else "    ") + line.plain for i, line in enumerate(lines)))
        return Content.from_rich_text(text, console=self.app.console)


class ToolCard(Collapsible):
    DEFAULT_CSS = """
    ToolCard { border: none; padding: 0; margin-bottom: 1; background: transparent; }
    ToolCard > CollapsibleTitle { padding: 0; }
    ToolCard > CollapsibleTitle { color: #dce3eb; }
    ToolCard > .tool-preview { display: none; height: auto; max-height: 5; color: #8b949e; }
    ToolCard.-collapsed > .tool-preview { display: block; }
    ToolCard .tool-full-scroll { height: auto; max-height: 20; }
    ToolCard .tool-detail { color: #8b949e; }
    """

    def __init__(self, label, detail, command=False):
        self.label, self.command = label, command
        self.path, self.tail = "", ""
        self.completed = False
        self.failed = False
        self.preview = ToolPreview("…", classes="tool-preview", markup=False)
        self.full = SelectableStatic("", markup=False)
        self.detail = SelectableStatic(detail, classes="tool-detail", markup=False)
        self.open_button = Button("Полный вывод / следить…", disabled=True)
        super().__init__(self.detail, VerticalScroll(self.full, classes="tool-full-scroll"), self.open_button,
                         title="", collapsed=True,
                         collapsed_symbol="•", expanded_symbol="•", classes="tool")
        self.title = ("Running " if command else "") + label

    def _watch_title(self, title):
        self._title.label = Content(str(title))

    def compose(self):
        yield from super().compose()
        yield self.preview

    def update_preview(self, tail, path="", error=""):
        self.tail = tail
        self.path = path or self.path
        self.preview.update(tail or ("(no output)" if self.completed else "…"))
        self.preview.copy_source = tail
        self.open_button.disabled = not bool(self.path)
        if error:
            self.detail.update(str(self.detail.content) + "\n" + error)
        if not self.collapsed and not self.path:
            self.full.update(tail or "(no output)")

    def finish(self, failed=False, running=False):
        self.failed |= failed
        self.completed |= not running
        self.title = (("Failed " if self.failed else "Ran " if self.completed else "Running ") + self.label
                      if self.command else ("Failed " if self.failed else "") + self.label)
        color = "#ff7b72" if self.failed else "#7ee787" if self.completed else "#d7ba7d"
        self._title.collapsed_symbol = self._title.expanded_symbol = Content.styled("•", color)
        self._title._update_label()
        self.set_class(self.failed, "tool-error")
        self.set_class(self.completed and not self.failed, "tool-ok")
        self.update_preview(self.tail)

    async def on_collapsible_expanded(self, event):
        if event.collapsible is not self:
            return
        self.full.update(self.tail or "(no output)")
        if self.path:
            try:
                text, _, end, size = await asyncio.to_thread(read_page, self.path)
                text = self.app.runtime.safe(text)
                if end < size:
                    text += "\n[Продолжение — кнопка «Полный вывод»]"
                self.full.update(text or "(no output)")
            except OSError as error:
                self.full.update(f"Не удалось прочитать вывод: {type(error).__name__}")

    def on_button_pressed(self, event):
        event.stop()
        if self.path:
            self.app.push_screen(OutputScreen(self.path))


class CopyMenu(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss(None)", "Закрыть")]
    DEFAULT_CSS = """
    CopyMenu { align: center middle; background: $background 50%; }
    CopyMenu > Vertical { width: 62; max-width: 95%; height: auto;
        border: round #79c0ff; background: #161b22; padding: 1 2; }
    CopyMenu OptionList { height: auto; }
    """

    def __init__(self, selected, message, last_error, paste_available=True):
        super().__init__()
        self.selected, self.message, self.last_error = selected, message, last_error
        self.paste_available = paste_available

    def compose(self):
        with Vertical():
            yield Static("Буфер обмена", markup=False)
            copy = Option("Копировать выделенное", id="selection", disabled=not bool(self.selected))
            paste = Option("Вставить", id="paste", disabled=not self.paste_available)
            yield OptionList(*( [copy, paste] if self.selected else [paste, copy]),
                             Option("Копировать сообщение целиком", id="message", disabled=not bool(self.message)),
                             Option("Копировать последнюю ошибку", id="error", disabled=not bool(self.last_error)),
                             Option("Открыть журнал", id="logs"))

    def on_mount(self):
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.dismiss(event.option.id)


class LogPane(Vertical):
    """Independent live view; closing it never sends cancellation to Runtime."""
    BINDINGS = [Binding("escape", "close", "Закрыть журнал", priority=True)]
    DEFAULT_CSS = """
    LogPane { display: none; dock: right; width: 50%; min-width: 30; height: 1fr;
        border: round #ff7b72; padding: 0 1; background: #161b22; }
    LogPane > Static { height: auto; }
    LogPane TabbedContent { height: 1fr; }
    LogPane ContentSwitcher { height: 1fr; }
    LogPane TabPane { height: 1fr; padding: 0; }
    LogPane #model-stream-status { height: auto; }
    LogPane VerticalScroll { height: 1fr; }
    LogPane #log-content, LogPane #model-stream-content { height: auto; }
    LogPane #log-actions { height: 3; }
    LogPane #log-actions Button { height: 3; width: 1fr; min-width: 0; padding: 0; }
    LogPane #clear-error-log, LogPane #external-logs { display: none; }
    """

    def compose(self):
        yield Static("Журнал · F3 / Esc — закрыть", markup=False)
        with TabbedContent(initial="model-stream-tab"):
            with TabPane("Поток модели", id="model-stream-tab"):
                yield Static("", id="model-stream-status", markup=False)
                with VerticalScroll(id="model-stream-scroll"):
                    yield SelectableStatic("", id="model-stream-content", markup=False)
            with TabPane("Ошибки", id="errors-tab"):
                yield Static("", id="log-path", markup=False)
                with VerticalScroll(id="log-scroll"):
                    yield SelectableStatic("", id="log-content", markup=False)
        with Horizontal(id="log-actions"):
            yield Button("Очистить поток", id="clear-model-stream")
            yield Button("Очистить ошибки", id="clear-error-log")
            yield Button("Терминал", id="external-logs", tooltip="Открыть отдельный терминал")
            yield Button("Закрыть журнал", id="close-logs")

    def on_tabbed_content_tab_activated(self, event):
        errors = event.tabbed_content.active == "errors-tab"
        self.query_one("#clear-model-stream").display = not errors
        self.query_one("#clear-error-log").display = errors
        self.query_one("#external-logs").display = errors

    def on_mount(self):
        self.last_text = None
        self.log_generation = 0
        self.model_text = ""
        self.model_channel = None
        self.model_received = 0
        self.model_last_chunk = None
        self.model_request_started = None
        self.model_active = False
        self.model_dirty = True
        self.model_trimmed = False
        self.set_interval(1, self.refresh_log)
        self.set_interval(0.2, self.refresh_model)

    def append_model(self, channel, text, received_at=None):
        now = received_at if received_at is not None else time.monotonic()
        if channel == "request":
            self.model_active = True
            self.model_received = 0
            self.model_last_chunk = None
            self.model_request_started = now
        elif channel == "end":
            self.model_active = False
        else:
            self.model_received += len(text)
            self.model_last_chunk = now
        if channel != self.model_channel or channel in {"request", "end"}:
            label = {"request": "Запрос", "end": "Завершение", "content": "Ответ",
                     "reasoning": "Reasoning от провайдера"}.get(channel, channel)
            self.model_text += f"\n\n[{label}]\n"
        self.model_text += text
        self.model_channel = channel
        if len(self.model_text) > 120000:
            self.model_text = self.model_text[-120000:]
            self.model_trimmed = True
        self.model_dirty = True

    def refresh_model(self):
        if not self.display or self.query_one(TabbedContent).active != "model-stream-tab":
            return
        state = "Получение ответа" if self.model_active else "Ожидание запроса" if self.model_request_started is None else "Запрос завершён"
        if self.model_last_chunk is not None:
            age = max(0, int(time.monotonic() - self.model_last_chunk))
            state += f" · {self.model_received} симв. · последний фрагмент {age} с назад"
        elif self.model_request_started is not None:
            age = max(0, int(time.monotonic() - self.model_request_started))
            state += f" · текст не получен" + (f" · ожидание {age} с" if self.model_active else "")
        self.query_one("#model-stream-status", Static).update(state)
        if self.model_dirty:
            scroll = self.query_one("#model-stream-scroll", VerticalScroll)
            follow = scroll.is_vertical_scroll_end
            text = self.model_text or "Здесь появятся ответ, reasoning и аргументы инструментов, переданные провайдером основному агенту. История вкладки хранится только до закрытия приложения."
            if self.model_trimmed:
                text = "[Показаны последние 120 000 символов]\n" + text
            self.query_one("#model-stream-content", Static).update(self.app.runtime.safe(text))
            self.model_dirty = False
            if follow:
                self.call_after_refresh(scroll.scroll_end, animate=False)

    async def refresh_log(self):
        if not self.display:
            return
        diagnostics = getattr(self.app.runtime, "diagnostics", None)
        if diagnostics is None:
            return
        generation = self.log_generation
        try:
            text = await asyncio.to_thread(diagnostics.tail)
        except OSError as error:
            text = f"Не удалось прочитать журнал: {type(error).__name__}"
        if generation != self.log_generation:
            return
        if text != self.last_text:
            scroll = self.query_one("#log-scroll", VerticalScroll)
            follow = scroll.is_vertical_scroll_end
            self.query_one("#log-content", Static).update(text)
            self.query_one("#log-path", Static).update(str(diagnostics.path))
            self.last_text = text
            if follow:
                self.call_after_refresh(scroll.scroll_end, animate=False)

    def action_close(self):
        self.display = False
        self.app.query_one(Composer).focus()

    async def on_button_pressed(self, event):
        event.stop()
        if event.button.id == "clear-model-stream":
            self.model_text = ""
            self.model_channel = None
            self.model_trimmed = False
            self.model_dirty = True
            self.refresh_model()
            return
        if event.button.id == "clear-error-log":
            diagnostics = getattr(self.app.runtime, "diagnostics", None)
            if diagnostics is not None:
                try:
                    self.log_generation += 1
                    await asyncio.to_thread(diagnostics.clear)
                    self.last_text = None
                    await self.refresh_log()
                except OSError as error:
                    self.app.notify(self.app.runtime.safe(f"Не удалось очистить журнал: {error}"), severity="error")
            return
        if event.button.id == "external-logs":
            from .logview import open_terminal
            runtime = self.app.runtime
            try:
                await asyncio.to_thread(open_terminal, runtime.args.config_dir, runtime.diagnostics.path.parent)
            except Exception as error:
                self.app.notify(runtime.safe(str(error)), severity="error")
            return
        self.action_close()


class E2EESetupScreen(ModalScreen):
    DEFAULT_CSS = """
    E2EESetupScreen { align: center middle; background: $background 80%; }
    E2EESetupScreen > Vertical { width: 90%; height: 85%; border: round #d7ba7d;
        background: #161b22; padding: 1 2; }
    E2EESetupScreen Static { height: auto; margin-bottom: 1; }
    E2EESetupScreen RichLog { height: 1fr; margin-bottom: 1; }
    """

    def compose(self):
        from textual.containers import Vertical
        with Vertical():
            yield Static("Подготовка E2EE", id="setup-title", markup=False)
            yield Static("При необходимости установка идёт НАПРЯМУЮ, БЕЗ ПРОКСИ.\nМаршруты запросов модели сохраняются.", markup=False)
            yield RichLog(id="setup-log", wrap=True, markup=False, highlight=False, max_lines=4000)
            yield Button("Отменить запуск", id="setup-close")

    def update_setup(self, state, text):
        self.query_one("#setup-log", RichLog).write(text)
        if state in {"checking", "installing"}:
            self.failed = False
            self.query_one("#setup-close", Button).label = "Отменить запуск"
            self.query_one("#setup-close", Button).disabled = False
        titles = {"checking": "Проверка библиотек E2EE", "installing": "Установка библиотек E2EE",
                  "failed": "Ошибка установки E2EE", "ready": "E2EE готово"}
        if state in titles:
            self.query_one("#setup-title", Static).update(titles[state])
        if state == "failed":
            self.query_one("#setup-close", Button).label = "Закрыть лог"
            self.failed = True

    def on_button_pressed(self, event):
        if event.button.id == "setup-close":
            if getattr(self, "failed", False):
                self.app.e2ee_setup_screen = None
                self.dismiss()
            else:
                self.query_one("#setup-close", Button).disabled = True
                self.app.runtime.stop()


class QuestionScreen(ContextMouse, ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Назад", priority=True)]
    DEFAULT_CSS = """
    QuestionScreen { align: center middle; background: $background 75%; }
    QuestionScreen > Vertical { width: 85%; max-width: 110; height: auto;
        max-height: 90%; border: round #d7ba7d; background: #161b22; padding: 1 2; }
    QuestionScreen .question-title { color: #d7ba7d; text-style: bold; margin-bottom: 1; height: auto; }
    QuestionScreen #question-detail { height: auto; max-height: 12; margin-bottom: 1; }
    QuestionScreen OptionList { height: auto; max-height: 16; background: #161b22; }
    QuestionScreen Input { margin-top: 1; }
    QuestionScreen .hint { color: #8b949e; height: auto; margin-top: 1; }
    """

    def __init__(self, data):
        super().__init__()
        self.data = data
        self.options = list(data.get("options", []))
        if data.get("allow_text") and self.options:
            self.options.append((None, "Свой ответ…"))

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(terminal_text(self.data["title"]), classes="question-title", markup=False)
            if self.data.get("detail"):
                with VerticalScroll(id="question-detail"):
                    yield SelectableStatic(terminal_text(self.data["detail"]), markup=False)
            if self.options:
                yield OptionList(*(Option(Text(terminal_text(label)), id=str(i)) for i, (_, label) in enumerate(self.options)), id="choices")
            if self.data.get("allow_text"):
                yield Input(value=self.data.get("value", ""), password=self.data.get("secret", False), placeholder="Введите ответ и нажмите Enter", id="answer")
            yield Static("↑↓ — выбрать · Enter — подтвердить · Tab — поле ввода · Esc — назад", classes="hint", markup=False)

    def on_mount(self):
        if self.options:
            choices = self.query_one(OptionList)
            default = self.data.get("default")
            choices.highlighted = next((i for i, (key, _) in enumerate(self.options) if default and key == default), 0)
            choices.focus()
        else:
            self.query_one(Input).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        value = self.options[event.option_index][0]
        if value is None:
            self.query_one(Input).focus()
        else:
            self.dismiss(value)

    def on_input_submitted(self, event: Input.Submitted):
        self.dismiss(event.value)

    def action_cancel(self):
        self.dismiss(None)


class MenuScreen(QuestionScreen):
    def on_mount(self):
        choices = self.query_one(OptionList)
        choices.clear_options()
        choices.add_options(Option(Text(label), id=key) for key, label in self.options)
        choices.highlighted = 0
        self.set_interval(0.2, self.refresh_menu)

    def refresh_menu(self):
        options = self.app.menu_options()
        if options == self.options:
            return
        choices = self.query_one(OptionList)
        index = choices.highlighted or 0
        key = self.options[index][0] if index < len(self.options) else None
        self.options = options
        choices.clear_options()
        choices.add_options(Option(Text(label), id=key) for key, label in options)
        choices.highlighted = next((i for i, (value, _) in enumerate(options) if value == key), 0)

    def on_option_list_option_selected(self, event):
        event.stop()
        event.prevent_default()
        self.app.open_action(event.option.id)


class SettingsScreen(QuestionScreen):
    def __init__(self, config):
        self.draft = Config(**config)
        self.original = replace(self.draft)
        super().__init__({"title": "Настройки", "options": self.items(),
                         "detail": "Применяются между шагами агента; отправленный запрос не изменяется.\n"
                         "Esc — назад без сохранения. Лимит шагов действует со следующего хода."})

    def items(self):
        return [(key, f"{label}: {getattr(self.draft, key)}") for key, label in LIMITS.items()] + [
            ("apply", "Применить к сессии"), ("save", "Применить и сохранить"),
            ("sandbox", "Песочница и сеть…"), ("proxy", "SOCKS-прокси и DNS…"),
            ("e2ee_verify_attestation", encryption_label(self.draft)),
            ("servers", "Серверы модели и повторы…"), ("back", "Назад")]

    def refresh_options(self, index=0):
        self.options = self.items()
        choices = self.query_one(OptionList)
        choices.clear_options()
        choices.add_options(Option(Text(label), id=str(i)) for i, (_, label) in enumerate(self.options))
        choices.highlighted = index
        choices.focus()

    def on_option_list_option_selected(self, event):
        event.stop()
        event.prevent_default()
        key = self.options[event.option_index][0]
        if key == "back":
            self.dismiss(None)
        elif key == "e2ee_verify_attestation":
            self.draft.e2ee_verify_attestation = not self.draft.e2ee_verify_attestation
            self.refresh_options(event.option_index)
        elif key == "servers":
            self.app.push_screen(ServersScreen())
        elif key == "proxy":
            self.app.push_screen(ProxyScreen(self.app.runtime.view_state["config"]))
        elif key == "sandbox":
            self.app.push_screen(SandboxScreen(self.app.runtime.view_state["config"]))
        elif key in {"apply", "save"}:
            values = {key: getattr(self.draft, key) for key in (*LIMITS, *SECURITY_FIELDS)}
            self.app.runtime.update_limits(values, save=key == "save")
            self.original = replace(self.draft)
            self.app.notify("Изменения переданы агенту; применятся на ближайшей границе шага.")
        else:
            index = event.option_index
            def answer(value):
                if value is None:
                    return
                try:
                    candidate = replace(self.draft, **{key: int(value)})
                    candidate.validate()
                except ValueError as error:
                    self.app.notify(str(error), severity="error")
                else:
                    self.draft = candidate
                    self.refresh_options(index)
            self.app.push_screen(QuestionScreen({"title": LIMITS[key], "allow_text": True,
                "value": str(getattr(self.draft, key))}), answer)


class ProxyScreen(SettingsScreen):
    def __init__(self, config):
        self.draft = Config(**config)
        QuestionScreen.__init__(self, {"title": "SOCKS-прокси", "options": self.items(),
            "detail": "Сохраняется для следующего запуска. Перезапустите PyCoder, чтобы не оставить старые прямые соединения.\n"
                      "Цепочка: PyCoder → первый SOCKS → второй SOCKS → сервис. Адреса обоих прокси — IP.\n"
                      "SOCKS4: USERID без пароля; DNS через прокси требует SOCKS4a.\n"
                      "Маршрут НАПРЯМУЮ — явное исключение для выбранного сервиса, включая его DNS. Неизвестные адреса и команды сохраняют общий прокси.\n"
                      "При локальном DNS запросы DNS идут напрямую. Команды без сетевой изоляции запрещены."})

    def items(self):
        options = [("proxy_enabled", f"Использовать прокси: {self.draft.proxy_enabled}"),
                   ("proxy_version", f"Первый прокси · версия: SOCKS{self.draft.proxy_version}"),
                   ("proxy_host", f"Первый прокси · IP: {self.draft.proxy_host}"),
                   ("proxy_port", f"Первый прокси · порт: {self.draft.proxy_port}"),
                   ("proxy_username", "Первый прокси · логин / USERID: " + ("задан" if self.draft.proxy_username else "нет")),
                   ("proxy_password", "Первый прокси · пароль: " + ("задан" if self.draft.proxy_password else "нет")),
                   ("proxy_chain_enabled", "Второй SOCKS через первый: " + ("включён" if self.draft.proxy_chain_enabled else "выключен"))]
        if self.draft.proxy_chain_enabled:
            options += [("proxy_second_version", f"Второй прокси · версия: SOCKS{self.draft.proxy_second_version}"),
                        ("proxy_second_host", f"Второй прокси · IP: {self.draft.proxy_second_host}"),
                        ("proxy_second_port", f"Второй прокси · порт: {self.draft.proxy_second_port}"),
                        ("proxy_second_username", "Второй прокси · логин / USERID: " + ("задан" if self.draft.proxy_second_username else "нет")),
                        ("proxy_second_password", "Второй прокси · пароль: " + ("задан" if self.draft.proxy_second_password else "нет"))]
        return options + [("proxy_remote_dns", f"DNS через последний прокси: {self.draft.proxy_remote_dns}"),
                          ("proxy_chutes", "Chutes / API модели: " + ("через прокси" if self.draft.proxy_chutes else "НАПРЯМУЮ")),
                          ("proxy_intel", "Intel: " + ("через прокси" if self.draft.proxy_intel else "НАПРЯМУЮ")),
                          ("proxy_nvidia", "NVIDIA: " + ("через прокси" if self.draft.proxy_nvidia else "НАПРЯМУЮ")),
                          ("save", "Сохранить для следующего запуска"), ("back", "Назад")]

    def on_option_list_option_selected(self, event):
        event.stop()
        event.prevent_default()
        key = self.options[event.option_index][0]
        if key == "back":
            self.dismiss(None)
        elif key == "save":
            from .proxy import save as save_proxy
            try:
                save_proxy(self.draft, self.app.runtime.args.config_dir)
            except (ValueError, OSError) as error:
                self.app.notify(str(error), severity="error")
                return
            self.app.notify("Сохранено. Перезапустите PyCoder для применения прокси.")
            self.dismiss(None)
        elif key in {"proxy_enabled", "proxy_remote_dns", "proxy_chutes", "proxy_intel", "proxy_nvidia", "proxy_chain_enabled"}:
            setattr(self.draft, key, not getattr(self.draft, key))
            self.refresh_options(event.option_index)
        elif key in {"proxy_version", "proxy_second_version"}:
            setattr(self.draft, key, 4 if getattr(self.draft, key) == 5 else 5)
            self.refresh_options(event.option_index)
        else:
            def answer(value):
                if value is None:
                    return
                try:
                    setattr(self.draft, key, int(value) if key in {"proxy_port", "proxy_second_port"} else value)
                    self.refresh_options(event.option_index)
                except ValueError:
                    self.app.notify("Порт должен быть числом", severity="error")
            self.app.push_screen(QuestionScreen({"title": dict(self.items())[key], "allow_text": True,
                "secret": key in {"proxy_password", "proxy_second_password"}, "value": "" if key in {"proxy_password", "proxy_second_password"} else str(getattr(self.draft, key))}), answer)


class SandboxScreen(SettingsScreen):
    def __init__(self, config):
        self.draft = Config(**config)
        self.draft.sandbox_hosts = list(self.draft.sandbox_hosts)
        self.draft.sandbox_direct_hosts = list(self.draft.sandbox_direct_hosts)
        QuestionScreen.__init__(self, {"title": "Песочница · macOS/Linux", "options": self.items(),
            "detail": "Windows работает без песочницы. Изменения — только для новых процессов, включая субагентов.\n"
                      "Только команды агента и субагентов. Маршруты модели и аттестации настраиваются отдельно.\n"
                      "В режимах без песочницы сеть команд не ограничивается этими настройками.\n"
                      "Прямой список разрешает точные host:port, включая localhost/LAN. Остальные адреса в режиме списков запрещены. Esc — назад."})

    def items(self):
        from .settings import network_modes, execution_mode, EXECUTION_MODES, visible_sandbox_fields
        labels = dict(network_modes(self.draft))
        options = [("enabled", "Режим: " + dict(EXECUTION_MODES)[execution_mode(self.draft)]),
                ("network", "Сеть песочницы: " + labels[self.draft.sandbox_network]),
                ("hosts", ("Через прокси: " if self.draft.proxy_enabled else "Разрешённые хосты: ") + (", ".join(self.draft.sandbox_hosts) or "список пуст")),
                ("direct_hosts", "Напрямую (включая локальные): " + (", ".join(self.draft.sandbox_direct_hosts) or "список пуст")),
                ("sandbox_localhost", f"Разрешать localhost через прокси: {self.draft.sandbox_localhost}"),
                ("sandbox_lan", f"Разрешать локальную сеть через прокси: {self.draft.sandbox_lan}")]
        field_names = {"enabled": "sandbox_enabled", "network": "sandbox_network",
                       "hosts": "sandbox_hosts", "direct_hosts": "sandbox_direct_hosts"}
        visible = visible_sandbox_fields(self.draft)
        options = [(key, label) for key, label in options if field_names.get(key, key) in visible]
        return options + [("apply", "Применить к сессии"), ("save", "Применить и сохранить"), ("back", "Назад")]

    def on_option_list_option_selected(self, event):
        event.stop()
        event.prevent_default()
        key = self.options[event.option_index][0]
        if key == "back":
            self.dismiss(None)
        elif key in {"apply", "save"}:
            self.draft.validate()
            self.app.runtime.update_limits({name: getattr(self.draft, name) for name in SANDBOX_FIELDS}, save=key == "save")
            self.app.notify("Настройки песочницы переданы агенту. Старые процессы сохраняют прежние ограничения.")
        elif key == "enabled":
            from .settings import EXECUTION_MODES, FULL_ACCESS_WARNING, execution_mode, set_execution_mode
            def selected(value):
                if value not in dict(EXECUTION_MODES):
                    return
                if value == "full":
                    def confirmed(answer):
                        if answer == "yes":
                            set_execution_mode(self.draft, value)
                            self.refresh_options()
                    self.app.push_screen(QuestionScreen({"title": "Включить полный доступ без подтверждений?",
                        "detail": FULL_ACCESS_WARNING,
                        "options": [("no", "Отмена"), ("yes", "Включить полный доступ")]}), confirmed)
                    return
                set_execution_mode(self.draft, value)
                self.refresh_options()
            self.app.push_screen(QuestionScreen({"title": "Режим выполнения команд", "options": EXECUTION_MODES,
                "default": execution_mode(self.draft)}), selected)
        elif key == "network":
            def network(value):
                if value is not None:
                    self.draft.sandbox_network = value
                    self.refresh_options(1)
            from .settings import network_modes
            self.app.push_screen(QuestionScreen({"title": "Сеть внутри песочницы", "options": network_modes(self.draft)}), network)
        elif key in {"hosts", "direct_hosts"}:
            self.edit_hosts("sandbox_direct_hosts" if key == "direct_hosts" else "sandbox_hosts")
        else:
            setattr(self.draft, key, not getattr(self.draft, key))
            self.refresh_options(event.option_index)

    def edit_hosts(self, field="sandbox_hosts"):
        row = 3 if field == "sandbox_direct_hosts" else 2
        def select(value):
            if value is None:
                return
            if value == "add":
                self.edit_host(None, field)
            else:
                index = int(value)
                def action(choice):
                    if choice == "edit":
                        self.edit_host(index, field)
                    elif choice == "delete":
                        getattr(self.draft, field).pop(index)
                        self.refresh_options(row)
                self.app.push_screen(QuestionScreen({"title": getattr(self.draft, field)[index],
                    "options": [("edit", "Изменить"), ("delete", "Удалить")]}), action)
        self.app.push_screen(QuestionScreen({"title": "Напрямую: host:port" if field == "sandbox_direct_hosts" else "Через прокси: host:port", "options": [
            ("add", "Добавить хост…"), *[(str(i), host) for i, host in enumerate(getattr(self.draft, field))]]}), select)

    def edit_host(self, index, field="sandbox_hosts"):
        row = 3 if field == "sandbox_direct_hosts" else 2
        def answer(value):
            if value is None:
                return
            hosts = list(getattr(self.draft, field))
            if index is None:
                hosts.append(value.strip())
            else:
                hosts[index] = value.strip()
            candidate = replace(self.draft, **{field: hosts})
            try:
                candidate.validate()
            except ValueError as error:
                self.app.notify(str(error), severity="error")
            else:
                self.draft = candidate
                self.refresh_options(row)
        self.app.push_screen(QuestionScreen({"title": "Точный host:port, например github.com:443",
            "allow_text": True, "value": "" if index is None else getattr(self.draft, field)[index]}), answer)


class ServersScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss(None)", "Назад", priority=True)]
    DEFAULT_CSS = """
    ServersScreen { align: center middle; background: $background 80%; }
    ServersScreen > Vertical { width: 94%; height: 92%; border: round #79c0ff; padding: 0 1; }
    ServersScreen Static { height: auto; }
    ServersScreen OptionList { height: 1fr; min-height: 3; }
    ServersScreen .server-actions { height: 3; }
    ServersScreen .server-actions Button { width: 1fr; min-width: 0; }
    ServersScreen .server-limits { height: 3; }
    ServersScreen .server-limits Static { width: 1fr; padding-top: 1; }
    ServersScreen .server-limits Input { width: 8; }
    """
    def compose(self):
        with Vertical():
            yield Static("Серверы модели · выбор применяется к следующей попытке", markup=False)
            yield Static("", id="server-status", markup=False)
            yield OptionList(id="server-list")
            yield Checkbox("Автоматически менять сервер при ошибках", id="server-auto")
            yield Static("Когда все доступные серверы дали ошибку:", markup=False)
            yield Select([("Остановиться", "stop"), ("Перебирать по кругу в пределах общего лимита повторов", "cycle")],
                         value="stop", allow_blank=False, id="server-exhausted")
            yield Static("Если ответ оборвался (смена в этом пункте действует независимо от галочки):", markup=False)
            yield Select([("Стоп", "stop"), ("Стоп и смена сервера", "stop_switch"),
                          ("Смена сервера и повтор", "switch_retry")],
                         value="stop_switch", allow_blank=False, id="server-partial")
            with Horizontal(classes="server-limits"):
                yield Static("Повторов на сервере:")
                yield Input("1", type="integer", id="server-per")
                yield Static("Всего повторов:")
                yield Input("10", type="integer", id="server-total")
            yield Static("Лимиты после первой попытки. Обрыв со сменой — сразу другой сервер. "
                         "Повтор отправляет весь запрос заново и расходует токены.", markup=False)
            with Horizontal(classes="server-actions"):
                yield Button("Обновить", id="server-refresh")
                yield Button("Сохранить настройки", id="server-save")
                yield Button("Закрыть", id="server-close")

    def on_mount(self):
        self.last_state = None
        state = self.state()
        if state:
            self.query_one("#server-auto", Checkbox).value = state["auto_switch"]
            self.query_one("#server-exhausted", Select).value = state["exhausted_action"]
            self.query_one("#server-partial", Select).value = state["partial_action"]
            self.query_one("#server-per", Input).value = str(state["retries_per_server"])
            self.query_one("#server-total", Input).value = str(state["total_retries"])
        self.refresh_view()
        self.set_interval(.5, self.refresh_view)
        if state and not state["updated"]:
            self.app.runtime.refresh_servers()

    def state(self):
        method = getattr(self.app.runtime, "server_state", None)
        return method() if method else None

    def refresh_view(self):
        state = self.state()
        available = state is not None
        for identifier in ("server-refresh", "server-save", "server-list", "server-auto",
                           "server-partial", "server-exhausted", "server-per", "server-total"):
            self.query_one("#" + identifier).disabled = not available
        if not available:
            self.query_one("#server-status", Static).update("Выбор экземпляров доступен после запуска чата с Chutes / E2EE.")
            return
        self.query_one("#server-refresh", Button).disabled = state["refreshing"]
        if state == self.last_state:
            return
        self.last_state = state
        status = "Обновление списка…" if state["refreshing"] else "Список обновлён: " + (state["updated"] or "ещё не загружен")
        if state["expired"] and state["updated"]:
            status += " · список мог измениться"
        status += "\nСчётчики сохранены для этой модели, включая субагентов. Загрузка серверов неизвестна."
        if state["error"]:
            status += "\n" + state["error"]
        self.query_one("#server-status", Static).update(status)
        choices = self.query_one("#server-list", OptionList)
        highlighted = choices.highlighted
        old_id = choices.get_option_at_index(highlighted).id if highlighted is not None and highlighted < choices.option_count else None
        choices.clear_options()
        choices.add_option(Option(Text(("▶ " if state["selected"] is None else "") + "Автоматически выбирать экземпляр", style="bold"), id="auto"))
        for row in sorted(state["rows"], key=lambda r: (not r["available"], r["id"])):
            identifier = row["id"]
            selected = identifier == state["selected"]
            label = ("▶ " if selected else "  ") + identifier
            label += " · " + ("есть в списке" if row["available"] else "нет в текущем списке")
            label += " · " + ("использован" if row.get("attempts", 0) else "ещё не использован")
            label += "\n    Успех: " + str(row.get("success", 0)) + " · Ошибки: " + str(row.get("errors", 0))
            label += " · Неполных: " + str(row.get("interrupted", 0)) + " · Отмен: " + str(row.get("cancelled", 0))
            if row.get("active"):
                label += " · Сейчас запросов: " + str(row["active"])
            style = "bold green" if selected else "cyan" if row["available"] and row.get("attempts") else "green" if row["available"] else "dim"
            choices.add_option(Option(Text(label, style=style), id="instance:" + identifier, disabled=not row["available"]))
        if old_id:
            for index in range(choices.option_count):
                if choices.get_option_at_index(index).id == old_id:
                    choices.highlighted = index
                    break

    def on_option_list_option_selected(self, event):
        event.stop()
        identifier = event.option.id
        try:
            self.app.runtime.select_server(None if identifier == "auto" else identifier.removeprefix("instance:"))
        except ValueError as error:
            self.app.notify(str(error), severity="error")
        except RuntimeError as error:
            self.app.notify(str(error), severity="error")
        self.refresh_view()

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == "server-close":
            self.dismiss(None)
        elif event.button.id == "server-refresh":
            self.app.runtime.refresh_servers()
            self.refresh_view()
        elif event.button.id == "server-save":
            try:
                per = int(self.query_one("#server-per", Input).value)
                total = int(self.query_one("#server-total", Input).value)
                if not 0 <= per <= 100 or not 0 <= total <= 100:
                    raise ValueError("Число повторов должно быть от 0 до 100")
                for name, value in {
                    "auto_switch": self.query_one("#server-auto", Checkbox).value,
                    "exhausted_action": self.query_one("#server-exhausted", Select).value,
                    "partial_action": self.query_one("#server-partial", Select).value,
                    "retries_per_server": per, "total_retries": total,
                }.items():
                    self.app.runtime.server_option(name, value)
            except ValueError as error:
                self.app.notify(str(error), severity="error")
            else:
                self.app.notify("Настройки серверов сохранены")
                self.refresh_view()


class PanelScreen(ContextMouse, ModalScreen):
    BINDINGS = [Binding("escape", "dismiss(None)", "Назад", priority=True)]
    DEFAULT_CSS = """
    PanelScreen { align: center middle; background: $background 80%; }
    PanelScreen > Vertical { width: 90%; height: 80%; border: round #79c0ff; padding: 1 2; }
    PanelScreen VerticalScroll { height: 1fr; }
    PanelScreen Static { height: auto; }
    PanelScreen Button { margin-top: 1; }
    """

    def __init__(self, section):
        super().__init__()
        self.section = section

    def compose(self):
        with Vertical():
            yield Static(ACTIONS[self.section], markup=False)
            with VerticalScroll():
                yield SelectableStatic("", id="panel-body", markup=False)
            if self.section == "permissions":
                yield Button("Удалить запомненные разрешения", id="clear-permissions")
            if self.section == "agents":
                yield Button("Остановить субагента…", id="interrupt-subagent")
            if self.section == "processes":
                yield Button("Остановить процесс…", id="stop-process")
            yield Button("Назад", id="panel-back")

    def on_mount(self):
        self.refresh_panel()
        self.set_interval(0.5, self.refresh_panel)

    def refresh_panel(self):
        state = getattr(self.app.runtime, "view_state", {})
        cfg = state.get("config", {})
        if self.section == "help":
            text = shortcut_help() + "\nНастройки и служебные окна доступны во время работы агента."
        elif self.section == "context":
            text = f"Контекст: ≈{state.get('used', 0)} / {cfg.get('context_window', '?')} токенов\nПорог сжатия: {state.get('threshold', '?')}\nЛимит ответа: {cfg.get('max_output_tokens', '?')}"
        elif self.section == "plan":
            text = "\n".join(plan_lines(state.get("plan", []))) or "План пока не создан."
        elif self.section == "session":
            text = f"Сессия: {state.get('session', '—')}\nПроект: {state.get('root', '—')}\nИстория: {state.get('path', '—')}"
        elif self.section == "processes":
            snapshot = self.app.background_processes()
            text = f"Работает: {snapshot['active_count']}/{snapshot['limit']} · основной агент и субагенты\n\n"
            text += "\n\n".join(f"#{p['id']} · {p['owner']} · PID {p['pid'] or 'запуск'} · {p['elapsed']} с\n"
                                  f"{p['command']}\ncwd: {p['cwd']}\nsession_id владельца: {p['session_id']}"
                                  for p in snapshot["running"]) or "Запущенных процессов нет."
        elif self.section == "permissions":
            text = "\n\n".join(f"cwd: {cwd}\nshell: {shell}\n{command}" for cwd, shell, command in state.get("permissions", [])) or "Запомненных разрешений нет."
        elif self.section == "agents":
            text = "\n\n".join(f"{m['task_name']} [{m['id']}] · {m['status']}\n{m['phase']}\n"
                              + "\n".join(plan_lines(m.get("plan", []))) + "\n"
                              + (m.get("result") or user_prose(m.get("preview", "")))
                              + f"\nЖурнал: {m['history']}" for m in self.app.agents) or "Субагентов пока нет."
        else:
            text = self.app.action_output or "Ожидание выполнения…"
        self.query_one("#panel-body", Static).update(self.app.runtime.safe(text))

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == "panel-back":
            self.dismiss(None)
        elif event.button.id == "stop-process":
            options = [(p["id"], f"#{p['id']} · {p['owner']} · {shorten(p['command'], 90)}")
                       for p in self.app.background_processes()["running"]]
            if not options:
                self.app.notify("Запущенных процессов нет.")
                return
            self.app.push_screen(QuestionScreen({"title": "Остановить процесс", "options": options}),
                                 lambda identifier: self.app.runtime.stop_background_process(identifier) if identifier is not None else None)
        elif event.button.id == "interrupt-subagent":
            options = [(m["id"], m["task_name"]) for m in self.app.agents if m["status"] == "running"]
            if not options:
                self.app.notify("Работающих субагентов нет.")
                return
            self.app.push_screen(QuestionScreen({"title": "Какого субагента остановить?", "options": options}),
                                 lambda target: self.app.runtime.interrupt_subagent(target) if target else None)
        else:
            self.app.push_screen(QuestionScreen({"title": "Удалить запомненные разрешения?",
                "options": [("no", "Назад"), ("yes", "Удалить")]}),
                lambda value: self.app.runtime.clear_permissions() if value == "yes" else None)


ACTIONS = {"settings": "Настройки", "context": "Контекст", "plan": "План", "processes": "Процессы", "agents": "Субагенты",
           "session": "Текущая сессия", "permissions": "Разрешения", "logs": "Журнал",
           "compact": "Сжать контекст", "continue": "Продолжить задачу", "help": "Справка", "exit": "Выход"}


class Composer(TextArea):
    class Submitted(Message):
        def __init__(self, text):
            super().__init__()
            self.text = text

    class CommandMenu(Message):
        pass

    def __init__(self):
        super().__init__(id="composer", soft_wrap=True, show_line_numbers=False)
        self.past = []
        self.index = 0
        self.draft = ""

    async def _on_key(self, event: events.Key):
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            text = self.text.strip()
            if text:
                self.past.append(text)
                self.index = len(self.past)
                self.draft = ""
                self.load_text("")
                self.post_message(self.Submitted(text))
            return
        if event.key in {"ctrl+j", "shift+enter", "alt+enter"}:
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if event.key == "tab":
            event.stop()
            event.prevent_default()
            self.post_message(self.CommandMenu())
            return
        if event.key in {"up", "down"} and "\n" not in self.text and self.past:
            event.stop()
            event.prevent_default()
            if self.index == len(self.past):
                self.draft = self.text
            self.index = max(0, min(len(self.past), self.index + (-1 if event.key == "up" else 1)))
            self.load_text(self.past[self.index] if self.index < len(self.past) else self.draft)
            self.move_cursor(self.document.end)
            return
        await super()._on_key(event)


class CoderApp(App):
    TITLE = "pycoder"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+q", "quit_agent", "Выход", priority=True),
                Binding("escape", "back", "Назад"),
                Binding("ctrl+c", "copy_selection", "Копировать", priority=True),
                Binding("ctrl+v,super+v,shift+insert", "paste_clipboard", "Вставить", priority=True),
                Binding("ctrl+g", "cancel_turn", "Остановить", priority=True),
                Binding("tab", "open_menu", "Меню", priority=True),
                Binding("f4", "servers", "Серверы", priority=True),
                Binding("ctrl+shift+c,super+c" if sys.platform == "darwin" else "ctrl+shift+c", "copy_selection", "Копировать", show=False, priority=True),
                Binding("f3", "logs", "Журнал", priority=True),
                Binding("f2,shift+f10", "copy_menu", "Меню", priority=True),
                Binding("pageup", "history_up", "Выше", show=False, priority=True),
                Binding("pagedown", "history_down", "Ниже", show=False, priority=True)]
    CSS = """
    Screen { background: #0d1117; color: #dce3eb; }
    #header { dock: top; height: auto; max-height: 6; padding: 0 2; background: #161b22; color: #79c0ff; }
    #transcript { height: 1fr; padding: 1 2; }
    .bubble { height: auto; margin: 0 0 1 0; padding: 0 1; }
    .user { border-left: thick #79c0ff; background: #161b22; }
    .assistant { border-left: thick #7ee787; }
    .status { color: #8b949e; }
    .error { color: #ff7b72; border-left: thick #ff7b72; }
    .tool { height: auto; margin-bottom: 1; background: #111820; }
    .tool Static { height: auto; }
    .tool-ok { color: #7ee787; }
    .tool-error { color: #ff7b72; }
    #plan { height: auto; max-height: 10; padding: 0 2; color: #d2a8ff; }
    #status { height: 1; padding: 0 2; color: #8b949e; }
    #background-processes { height: 1; min-height: 1; width: 100%; border: none; padding: 0 2; margin: 0; text-align: left; color: #79c0ff; background: #161b22; }
    #composer { height: 5; min-height: 3; max-height: 12; margin: 0 1; border: round #79c0ff; background: #161b22; }
    Footer { background: #161b22; }
    """

    def __init__(self, runtime):
        super().__init__()
        self.devtools = None  # The agent never connects to Textual's optional dev server.
        self.runtime = runtime
        self.ready = False
        self.busy = True
        self.closing = False
        self.pending_count = 0
        self.used = 0
        self.window = 1
        self.phase = "Настройка…"
        self.response = None
        self.response_text = ""
        self.dirty_response = False
        self.tool_cards = {}
        self.question = None
        self.pending_question = None
        self.quit_question = False
        self.action_output = ""
        self.agents = []
        self.fatal = False
        self.tick = 0
        self.last_status = None
        self.header_identity = None
        self.last_header = None
        self.model_security = {"mode": "unknown", "detail": "E2EE ещё не проверено"}
        self.last_process_count = None
        self.speed = "— ток/с"
        self.tool_labels = {}
        self.last_error = ""
        self.retry_bubble = None
        self.model_started = None
        self.model_elapsed = 0.0
        self.turn_failed = False
        self.plan_unfinished = False
        self._clipboard_lock = asyncio.Lock()

    def compose(self):
        from .proxy import label as proxy_label
        initial_proxy = proxy_label(getattr(self.runtime, "config", None) or Config(**self.runtime.view_state.get("config", {})))
        yield Static(Text("pycoder · запуск" + (" · " + initial_proxy if initial_proxy else ""),
                          style="bold yellow" if initial_proxy else ""), id="header", markup=False)
        yield LogPane(id="logs")
        yield VerticalScroll(id="transcript")
        yield Static("", id="plan", markup=False)
        yield Button("Фоновые процессы: 0/5 · открыть", id="background-processes")
        yield Static("Настройка…", id="status", markup=False)
        yield Composer()
        yield Footer()

    def get_default_screen(self):
        return TranscriptScreen(id="_default")

    def on_mount(self):
        self.query_one(Composer).disabled = True
        self.event_timer = self.set_interval(1 / 30, self.drain_events)
        self.runtime.start()

    def background_processes(self):
        if hasattr(self.runtime, "background_processes"):
            return self.runtime.background_processes()
        return {"active_count": 0, "limit": self.runtime.view_state.get("config", {}).get("max_background_processes", 5), "running": []}

    def on_button_pressed(self, event):
        if event.button.id == "background-processes":
            event.stop()
            self.push_screen(PanelScreen("processes"))

    async def add_bubble(self, content, kind="status"):
        transcript = self.query_one("#transcript", VerticalScroll)
        follow = transcript.is_vertical_scroll_end
        widget = SelectableStatic(content, classes=f"bubble {kind}", markup=False)
        await transcript.mount(widget)
        if follow:
            transcript.scroll_end(animate=False)
        return widget

    async def drain_events(self):
        if not self.query("#status"):
            return  # The default screen may already be unmounting.
        transcript = self.query_one("#transcript", VerticalScroll)
        follow = transcript.is_vertical_scroll_end
        content_changed = False
        for _ in range(200):
            try:
                event = self.runtime.bus.queue.get_nowait()
            except Empty:
                break
            await self.handle_event(event.kind, event.data)
            content_changed |= event.kind in {"ready", "text", "tool_start", "tool_output", "tool_end", "turn_summary", "patch", "status", "error"}
            if event.kind == "stopped":
                return
        if self.header_identity:
            from .sandbox import supported
            config = self.runtime.view_state.get("config", {})
            mode = "только чтение" if config.get("approval") == "never" else (
                "песочница · сеть " + {"none": "закрыта", "allowlist": "по спискам", "all": "открыта", "direct": "напрямую"}.get(config.get("sandbox_network"), "закрыта")
                if supported() and config.get("sandbox_enabled", True) else
                "ПОЛНЫЙ ДОСТУП · без подтверждений" if config.get("full_access") else
                "без песочницы · авторазрешение команд" if config.get("approval") == "auto" else "без песочницы · подтверждения команд")
            from .proxy import label as proxy_label
            proxy_status = proxy_label(Config(**config))
            if proxy_status:
                mode = proxy_status
            if not config.get("e2ee_verify_attestation", True):
                mode += " · E2EE БЕЗ АТТЕСТАЦИИ"
            model, root = self.header_identity
            security = self.model_security
            header_key = (model, root, mode, security["mode"], security["detail"])
            if header_key != self.last_header:
                color = {"encrypted": "bold green", "checking": "cyan", "blocked": "bold red",
                         "plain": "yellow", "unverified": "bold yellow", "unknown": "dim"}.get(security["mode"], "dim")
                header = Text("pycoder · ")
                if proxy_status:
                    header.append(" ЧЕРЕЗ ПРОКСИ ", style="bold black on yellow")
                if not config.get("e2ee_verify_attestation", True):
                    header.append(" СЕРВЕР НЕ ПРОВЕРЕН ", style="bold black on yellow")
                header.append(self.runtime.safe(model), style=color)
                header.append(self.runtime.safe(("\n" if proxy_status else " · ") + f"{mode}\n{security['detail']}\n{root}"))
                self.query_one("#header", Static).update(header)
                self.last_header = header_key
        if self.dirty_response and self.response:
            self.response.update(Markdown(user_prose(self.response_text), code_theme="monokai"))
            self.response.copy_source = self.response_text
            self.dirty_response = False
            transcript = self.query_one("#transcript", VerticalScroll)
            if transcript.is_vertical_scroll_end:
                transcript.scroll_end(animate=False)
        if self.question and self.question.data.get("reply") and self.question.data["reply"].cancelled():
            if self.screen is self.question:
                self.question.dismiss(None)
        if self.pending_question and self.pending_question["reply"].done():
            self.pending_question = None
            if self.phase == "Ожидается ответ · Tab":
                self.phase = "Работаю…" if self.busy else "Готов"
        self.tick += 1
        processes = self.background_processes()
        count = (processes["active_count"], processes["limit"])
        if count != self.last_process_count:
            self.query_one("#background-processes", Button).label = f"Фоновые процессы: {count[0]}/{count[1]} · открыть"
            self.last_process_count = count
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[(self.tick // 3) % 10] if self.busy and len(self.screen_stack) == 1 else "●"
        status = self.query("#status")
        if not status:
            return
        elapsed = self.model_elapsed + (time.monotonic() - self.model_started if self.model_started is not None else 0)
        duration = f"{int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}"
        status_width = status.first(Static).content_size.width
        phase_limit = 12 if status_width < 100 else 32
        status_text = (
            f"{spinner} {shorten(self.phase, phase_limit)} · контекст ≈{self.used * 100 // max(1, self.window)}% · модель {duration} · {self.speed}"
            + (f" · в очереди: {self.pending_count}" if self.pending_count else "")
            + (" · Enter ↵ · Ctrl+J: строка" if status_width >= 110 else ""))
        if status_text != self.last_status:
            status.first(Static).update(status_text)
            self.last_status = status_text
        if follow and content_changed:
            self.call_after_refresh(transcript.scroll_end, animate=False)

    async def handle_event(self, kind, data):
        safe = self.runtime.safe
        if kind == "e2ee_setup":
            screen = getattr(self, "e2ee_setup_screen", None)
            if screen is None:
                screen = self.e2ee_setup_screen = E2EESetupScreen()
                await self.push_screen(screen)
            screen.update_setup(data["state"], safe(data["text"]))
            if data["state"] == "ready":
                screen.dismiss()
                self.e2ee_setup_screen = None
        elif kind == "question":
            if data["reply"].done():
                return
            if self.closing:
                data["reply"].cancel()
                return
            data = dict(data)
            data["title"], data["detail"] = safe(data["title"]), safe(data.get("detail", ""))
            data["options"] = [(key, safe(label)) for key, label in data.get("options", [])]
            self.pending_question = data
            self.phase = "Ожидается ответ · Tab"
            if len(self.screen_stack) == 1:
                self.show_pending_question()
            else:
                self.notify("Агент ожидает ответа. Вернитесь в меню → Ответить на вопрос.")
        elif kind == "ready":
            self.ready = True
            self.header_identity = (data["model"], data["root"])
            composer = self.query_one(Composer)
            composer.disabled = False
            composer.focus()
            for message in data["messages"][-100:]:
                content = message.get("content")
                if content and message["role"] in {"user", "assistant"}:
                    if isinstance(content, list):
                        content = "\n".join(item.get("text", "[изображение]") for item in content if isinstance(item, dict))
                    text = safe(truncate(content, 8000))
                    bubble = await self.add_bubble(Markdown(user_prose(text, final=True) or "[Ответ с кодом — доступен через меню копирования]") if message["role"] == "assistant" else text, message["role"])
                    bubble.copy_source = text
                    if message["role"] == "user":
                        composer.past.append(text)
            composer.index = len(composer.past)
            await self.handle_event("plan", {"plan": data["plan"]})
            self.notify("Tab — меню действий; Esc — назад; Ctrl+G — остановить задачу.")
        elif kind == "busy":
            self.turn_failed = False
            self.plan_unfinished = False
            self.model_elapsed = 0.0
            self.busy = True
            self.pending_count = max(0, self.pending_count - 1)
            self.phase = "Работаю…"
            self.response = None
            self.response_text = ""
        elif kind == "idle":
            if self.retry_bubble is not None:
                await self.retry_bubble.remove()
                self.retry_bubble = None
            self.busy = False
            self.stop_model_clock()
            self.phase = "Ошибка · Tab: продолжить" if self.turn_failed else "План не завершён" if self.plan_unfinished else "Готов"
        elif kind == "model_start":
            if self.retry_bubble is not None:
                await self.retry_bubble.remove()
                self.retry_bubble = None
            self.stop_model_clock()
            self.model_started = time.monotonic()
            self.phase = "Модель отвечает…"
            self.response = None
            self.response_text = ""
            self.speed = "— ток/с"
        elif kind == "text":
            if self.response is None:
                self.response = await self.add_bubble("", "assistant")
            self.response_text += data["text"]
            self.dirty_response = True
        elif kind == "model_output":
            self.query_one(LogPane).append_model(**data)
            if self.retry_bubble is not None and data.get("channel") == "request":
                self.phase = "Повторный запрос отправлен · ожидаю модель"
                self.retry_bubble.update(self.retry_detail + "\nПовторный запрос отправлен; ожидаю ответ.")
        elif kind == "model_security":
            self.model_security = data
            if data["mode"] == "blocked":
                self.notify(safe(data["detail"]), severity="warning")
        elif kind == "model_waiting":
            self.stop_model_clock()
            self.phase = "Нужно решение пользователя"
        elif kind == "model_restart":
            automatic = data.get("automatic")
            note = ("Ответ оборван; инструменты из него не выполнялись. Автоматический повтор "
                    f"{data.get('attempt', 1)} с последней завершённой истории."
                    if automatic else
                    "Незавершённая попытка; инструмент не выполнен. Продолжение по решению пользователя.")
            if automatic:
                self.phase = f"Повтор {data['attempt']} · через {data.get('delay', 10)} с"
                detail = (f"{self.phase} · без ограничения числа попыток\n"
                          f"{data.get('error', 'Временная ошибка модели')}\n"
                          "Ctrl+G — остановить. История завершённых действий сохранена.")
                if data.get("streaming") is False:
                    detail += "\nПовтор без streaming."
                self.retry_detail = safe(detail)
                if self.retry_bubble is None:
                    self.retry_bubble = await self.add_bubble(self.retry_detail, "status")
                else:
                    self.retry_bubble.update(safe(detail))
            if self.response:
                self.response.update(Markdown(user_prose(self.response_text, final=True) + f"\n\n*{note}*"))
                self.response.copy_source = self.response_text
            self.response = None
            self.response_text = ""
            self.dirty_response = False
            self.speed = "— ток/с"
        elif kind == "model_end":
            if self.retry_bubble is not None:
                await self.retry_bubble.remove()
                self.retry_bubble = None
            self.stop_model_clock()
            if self.response:
                self.response.update(Markdown(user_prose(self.response_text, final=True) or "[Ответ с кодом — доступен через меню копирования]", code_theme="monokai"))
                self.response.copy_source = self.response_text
                self.dirty_response = False
        elif kind == "generation":
            rate = data["tokens"] / max(0.001, data["elapsed"])
            self.speed = f"{'' if data['exact'] else '≈'}{rate:.1f} ток/с"
        elif kind == "control_result":
            self.notify(data["text"], severity="error" if data.get("error") else "information")
        elif kind == "agents":
            self.agents = data["agents"]
        elif kind == "waiting_agents":
            self.stop_model_clock()
            self.phase = "Ожидаю субагентов · Tab"
        elif kind == "action_status":
            self.action_output = (self.action_output + "\n" + safe(data["text"]))[-32000:]
        elif kind == "turn_summary":
            await self.add_bubble(summary_text(data))
        elif kind == "tool_start":
            label = safe(tool_label(data["name"], data["arguments"]))
            self.phase = data["name"]
            self.tool_labels[data["id"]] = label
            try:
                args = json.loads(data["arguments"])
                detail = (args.get("cmd") or args.get("path") or json.dumps(args, ensure_ascii=False, indent=2)) if isinstance(args, dict) else data["arguments"]
            except (ValueError, TypeError):
                detail = data["arguments"]
            command = data["name"] in {"exec_command", "shell"}
            if command:
                label = label.split(" · ", 1)[-1]
            card = ToolCard(label, safe(truncate(str(detail), 3000)), command)
            card.detail.copy_source = safe(str(detail))
            self.tool_cards[data["id"]] = card
            await self.query_one("#transcript").mount(card)
        elif kind == "tool_output":
            card = self.tool_cards.get(data["id"])
            if card:
                tail = data["tail"].replace("\r\n", "\n").replace("\r", "\n")
                card.update_preview(safe(tail), data.get("path", ""), safe(data.get("error", "")))
                if data.get("completed"):
                    card.finish(failed=data.get("exit_code") not in (None, 0))
        elif kind == "tool_end":
            card = self.tool_cards.get(data["id"])
            if card:
                running = False
                try:
                    output = json.loads(data["output"])
                    failed = isinstance(output, dict) and bool(output.get("error") or output.get("denied") or output.get("timed_out") or output.get("exit_code") not in (None, 0))
                    running = isinstance(output, dict) and bool(output.get("running"))
                    formatted = format_output(output)
                except ValueError:
                    failed, formatted = False, data["output"]
                self.tool_labels.pop(data["id"], None)
                if not card.path:
                    card.update_preview(safe(formatted))
                card.finish(failed, running)
                if failed:
                    self.last_error = safe(formatted)
        elif kind == "patch":
            card = Collapsible(SelectableStatic(Syntax(safe(data["diff"]), "diff", theme="monokai", word_wrap=True)),
                               title=shorten("Изменения: " + ", ".join(safe(p) for p in data["changed"])), classes="tool", collapsed=True)
            await self.query_one("#transcript").mount(card)
        elif kind == "plan_unfinished":
            self.plan_unfinished = True
        elif kind == "plan":
            self.query_one("#plan", Static).update(safe("\n".join(plan_lines(data["plan"]))))
        elif kind == "context":
            if self.phase == "Сжатие контекста…":
                self.stop_model_clock()
            self.used, self.window = data["used"], data["window"]
        elif kind == "compacting":
            self.stop_model_clock()
            self.model_started = time.monotonic()
            self.phase = "Сжатие контекста…"
        elif kind in {"status", "error"}:
            if kind == "status" and data["text"].startswith("Инструмент:"):
                return
            await self.add_bubble(safe(data["text"]), "error" if kind == "error" else "status")
            if kind == "error":
                self.turn_failed = True
                self.stop_model_clock()
                self.last_error = safe(data["text"])
            self.fatal |= data.get("fatal", False)
        elif kind == "error_log":
            self.action_logs()
        elif kind == "stopped":
            self.event_timer.stop()
            self.busy = False
            self.ready = False
            self.phase = "Остановлен · Ctrl+Q — выход"
            if self.closing or not self.fatal:
                self.exit()

    async def on_composer_submitted(self, event: Composer.Submitted):
        text = event.text
        if text in {"/exit", "/quit"}:
            self.action_quit_agent()
            return
        if text.startswith("/"):
            action = text[1:]
            if action in ACTIONS:
                self.open_action(action)
            else:
                self.notify("Неизвестное действие. Tab — меню.")
            return
        if not self.ready or self.closing:
            return
        await self.add_bubble(self.runtime.safe(text), "user")
        self.pending_count += 1
        self.runtime.submit(text)

    def on_composer_command_menu(self):
        self.action_open_menu()

    def check_action(self, action, parameters):
        if action in {"open_menu", "history_up", "history_down"}:
            return len(self.screen_stack) == 1
        return True

    def menu_options(self):
        options = list(ACTIONS.items()) if self.ready else [("help", "Справка"), ("exit", "Выход")]
        if self.pending_question and not self.pending_question["reply"].done():
            options.insert(0, ("question", "Ответить на вопрос агента"))
        return options

    def action_servers(self):
        self.push_screen(ServersScreen())

    def action_open_menu(self):
        self.push_screen(MenuScreen({"title": "Меню", "options": self.menu_options()}))

    def show_pending_question(self):
        data = self.pending_question
        if not data or data["reply"].done() or self.question:
            return
        self.question = QuestionScreen(data)
        def answer(value):
            self.question = None
            if value is not None and not data["reply"].done():
                try:
                    data["reply"].set_result(value)
                except InvalidStateError:
                    pass  # Background attestation may have just cancelled this prompt.
                if self.pending_question is data:
                    self.pending_question = None
            elif not data["reply"].done():
                self.notify("Вопрос ожидает ответа: Tab → Ответить на вопрос агента.")
            if self.pending_question is not data and len(self.screen_stack) == 1:
                self.call_later(self.show_pending_question)
        self.push_screen(self.question, answer)

    def open_action(self, action):
        if action == "exit":
            self.action_quit_agent()
        elif action == "question":
            self.show_pending_question()
        elif action == "help":
            self.push_screen(PanelScreen(action))
        elif not self.ready or self.closing:
            self.notify("Сначала завершите настройку модели.")
        elif action == "settings":
            self.push_screen(SettingsScreen(self.runtime.view_state["config"]))
        elif action == "logs":
            if isinstance(self.screen, MenuScreen):
                self.screen.dismiss(None)
            self.action_logs()
        elif action in {"continue", "compact"}:
            if action == "compact":
                self.action_output = "Сжатие запрошено; ожидание текущей задачи…" if self.busy else "Сжатие запрошено…"
                self.push_screen(PanelScreen(action))
            elif isinstance(self.screen, MenuScreen):
                self.screen.dismiss(None)
            self.pending_count += 1
            self.runtime.submit_action(action)
        else:
            self.push_screen(PanelScreen(action))

    def action_back(self):
        if len(self.screen_stack) == 1 and self.query_one(LogPane).display:
            self.query_one(LogPane).action_close()

    def stop_model_clock(self):
        if self.model_started is not None:
            self.model_elapsed += time.monotonic() - self.model_started
            self.model_started = None

    def action_logs(self):
        pane = self.query_one(LogPane)
        pane.display = not pane.display
        if pane.display:
            self.run_worker(pane.refresh_log(), group="logs", exit_on_error=False)
            pane.refresh_model()
            pane.query_one("#close-logs", Button).focus()
        else:
            self.query_one(Composer).focus()

    def action_cancel_turn(self):
        if self.busy or self.pending_count:
            self.runtime.cancel()
            self.pending_count = 0
            self.phase = "Прерываю…"

    def selected_text(self):
        selected = self.screen.get_selected_text()
        if selected:
            return selected
        focused = self.screen.focused
        if isinstance(focused, (TextArea, Input)):
            if isinstance(focused, Input) and focused.password:
                return ""
            return focused.selected_text
        return ""

    def action_copy_selection(self):
        selected = self.selected_text()
        if selected:
            self.copy_to_clipboard(selected)
        else:
            self.notify("Сначала выделите текст мышью. F2 — меню копирования.")

    def paste_target(self, widget=None):
        for candidate in (widget, self.screen.focused):
            if isinstance(candidate, (Input, TextArea)) and not candidate.disabled:
                if not isinstance(candidate, TextArea) or not candidate.read_only:
                    return candidate
        if len(self.screen_stack) == 1:
            return self.query_one(Composer)
        return None

    def insert_clipboard(self, target, text):
        if target is None:
            self.notify("Выберите поле для вставки.")
        elif text is None:
            self.notify("Буфер ОС недоступен. Используйте вставку терминала Ctrl+Shift+V "
                        "(на Mac ⌘V). В Linux для Ctrl+V установите wl-clipboard "
                        "(Wayland) или xclip (X11).", timeout=10)
        elif text:
            target.focus()
            paste = events.Paste(text.replace("\r\n", "\n").replace("\r", "\n"))
            paste.stop()  # Already targeted: do not bubble to App and forward twice.
            target.post_message(paste)

    def action_paste_clipboard(self):
        target, screen = self.paste_target(), self.screen
        async def paste():
            async with self._clipboard_lock:
                text = await asyncio.to_thread(read_clipboard)
            if self.screen is screen:
                self.insert_clipboard(target, text)
        self.run_worker(paste(), group="clipboard", exit_on_error=False)

    def copy_to_clipboard(self, text):
        self._clipboard = text
        async def copy():
            async with self._clipboard_lock:
                native = await asyncio.to_thread(write_clipboard, text)
                if not native:
                    super(CoderApp, self).copy_to_clipboard(text)
                self.notify("Скопировано" if native else "Отправлено в буфер терминала (OSC 52)")
        self.run_worker(copy(), group="clipboard", exit_on_error=False)

    @staticmethod
    def message_text(widget):
        if widget is None:
            return ""
        if isinstance(widget, SelectableStatic):
            if hasattr(widget, "copy_source"):
                return widget.copy_source
            content = widget.content
            if isinstance(content, str):
                return content
            if isinstance(content, Text):
                return content.plain
            return getattr(content, "markup", getattr(content, "code", ""))
        if isinstance(widget, TextArea):
            return widget.text
        return ""

    def action_copy_menu(self, widget=None):
        if isinstance(self.screen, CopyMenu):
            return
        selected = self.selected_text()
        target, screen = self.paste_target(widget), self.screen
        if widget is None:
            widget = next(iter(self.screen.selections), None)
        if widget is None:
            bubbles = list(self.query(".bubble"))
            widget = bubbles[-1] if bubbles else None
        message = self.message_text(widget)
        def choose(action):
            if action == "logs":
                self.action_logs()
            elif action == "paste":
                self.insert_clipboard(target, clipboard_text)
            else:
                text = {"selection": selected, "message": message, "error": self.last_error}.get(action)
                if text:
                    self.copy_to_clipboard(text)
        async def open_menu():
            nonlocal clipboard_text
            async with self._clipboard_lock:
                clipboard_text = await asyncio.to_thread(read_clipboard)
            if self.screen is screen:
                self.push_screen(CopyMenu(selected, message, self.last_error,
                    paste_available=target is not None and clipboard_text != ""), choose)
        clipboard_text = None
        self.run_worker(open_menu(), group="clipboard-menu", exclusive=True, exit_on_error=False)

    def _handle_exception(self, error):
        self.fatal = True
        if hasattr(self.runtime, "report_error"):
            self.runtime.report_error(error, "ui")
        super()._handle_exception(error)

    def _fatal_error(self):
        # Textual's default includes locals, which may contain credentials.
        path = getattr(getattr(self.runtime, "diagnostics", None), "path", None)
        self._exit_renderables.append(Text(self.runtime.safe(f"Ошибка интерфейса: {self._exception}\nЖурнал: {path}")))
        self._close_messages_no_wait()

    def action_history_up(self):
        if len(self.screen_stack) == 1:
            self.query_one("#transcript", VerticalScroll).scroll_page_up()

    def action_history_down(self):
        if len(self.screen_stack) == 1:
            self.query_one("#transcript", VerticalScroll).scroll_page_down()

    def action_quit_agent(self):
        if self.closing or self.quit_question:
            return
        self.quit_question = True
        def confirm(value):
            self.quit_question = False
            if value != "yes":
                return
            self.closing = True
            self.phase = "Сохраняю историю…"
            self.runtime.stop()
            if self.runtime.thread is None or not self.runtime.thread.is_alive():
                self.exit()
        self.push_screen(QuestionScreen({"title": "Выйти из PyCoder?",
            "detail": "История сохранится. Текущая работа и дочерние процессы будут остановлены.",
            "options": [("no", "Остаться"), ("yes", "Выйти")]}), confirm)

    def on_unmount(self):
        self.event_timer.stop()
        self.runtime.stop()


def run_interactive(args, config, root, state_dir):
    runtime = Runtime(args, config, root, state_dir)
    app = CoderApp(runtime)
    try:
        app.run()
    finally:
        runtime.stop()
        if runtime.thread:
            # Do not release the workspace lock while the worker can still mutate it.
            runtime.thread.join()
    return 1 if app.fatal else 0
