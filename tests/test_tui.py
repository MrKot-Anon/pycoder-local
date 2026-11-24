import asyncio
from dataclasses import asdict
from concurrent.futures import Future
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from pycoder.cli import parser
from pycoder.config import Config
from pycoder.events import EventBus
from pycoder.runtime import Runtime
from pycoder.settings import LIMITS
from pycoder.tui import CoderApp, Composer, QuestionScreen, CopyMenu, SelectableStatic, ToolCard, OutputScreen, SettingsScreen, MenuScreen, PanelScreen, E2EESetupScreen
from textual.widgets import Input, OptionList, Collapsible, RichLog, Static, Button
from tests.support import server, reply, call, json_response


class FakeRuntime:
    def __init__(self):
        self.bus = EventBus()
        self.submitted = []
        self.token = ""
        self.thread = None
        self.view_state = {"config": asdict(Config(model="test"))}
        self.actions = []

    def submit_action(self, action):
        self.actions.append(action)

    def update_limits(self, values, save=False):
        self.view_state["config"].update(values)

    def safe(self, text):
        return str(text)

    def start(self):
        self.bus.emit("ready", model="test", root="/project", session="test", messages=[], plan=[], approval="ask")
        self.bus.emit("idle")

    def submit(self, text):
        self.submitted.append(text)

    def cancel(self):
        self.bus.cancelled.set()

    def stop(self):
        self.cancel()


class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_dialog_selection_refresh_and_retry_settings(self):
        from pycoder.servers import Servers
        from pycoder.tui import ServersScreen
        from textual.widgets import Checkbox, Select
        import time
        runtime = FakeRuntime()
        registry = Servers()
        registry.publish("chute", [{"instance_id": "a", "nonces": ["n"]},
                                   {"instance_id": "b", "nonces": ["n2"]}], time.monotonic() + 50)
        registry.begin("a")
        registry.finish("a", "success")
        runtime.server_state = registry.snapshot
        runtime.select_server = registry.choose
        runtime.server_option = registry.set_option
        refreshed = []
        def refresh():
            refreshed.append(True)
            registry.publish("chute", [{"instance_id": "b", "nonces": ["n3"]},
                                       {"instance_id": "c", "nonces": ["n4"]}], time.monotonic() + 50)
        runtime.refresh_servers = refresh
        app = CoderApp(runtime)
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause(.1)
            app.query_one(Composer).load_text("черновик")
            await pilot.press("f4")
            screen = app.screen
            self.assertIsInstance(screen, ServersScreen)
            choices = screen.query_one("#server-list", OptionList)
            self.assertIn("Успех: 1", str(choices.get_option_at_index(1).prompt))
            choices.highlighted = 2
            choices.focus()
            await pilot.press("enter")
            self.assertEqual(registry.selected, "b")
            screen.query_one("#server-auto", Checkbox).value = False
            screen.query_one("#server-exhausted", Select).value = "cycle"
            screen.query_one("#server-partial", Select).value = "switch_retry"
            screen.query_one("#server-per", Input).value = "2"
            screen.query_one("#server-total", Input).value = "7"
            await pilot.click("#server-save")
            self.assertFalse(registry.auto_switch)
            self.assertEqual(registry.exhausted_action, "cycle")
            self.assertEqual(registry.partial_action, "switch_retry")
            self.assertEqual(registry.retries_per_server, 2)
            self.assertEqual(registry.total_retries, 7)
            await pilot.click("#server-refresh")
            self.assertEqual(len(refreshed), 1)
            rows = {r["id"]: r for r in registry.snapshot()["rows"]}
            self.assertFalse(rows["a"]["available"])
            self.assertEqual(rows["a"]["success"], 1)
            self.assertTrue(rows["c"]["available"])
            await pilot.press("escape")
            self.assertEqual(app.query_one(Composer).text, "черновик")
            self.assertFalse(runtime.bus.cancelled.is_set())

    async def test_clear_stream_and_persisted_errors_without_cancelling_generation(self):
        from pycoder.diagnostics import Diagnostics
        from pycoder.tui import LogPane
        from textual.widgets import TabbedContent
        with tempfile.TemporaryDirectory() as directory:
            runtime = FakeRuntime()
            runtime.diagnostics = Diagnostics(Path(directory))
            runtime.diagnostics.record(ValueError('old failure'))
            archive = runtime.diagnostics.path.with_suffix('.log.1')
            archive.write_text('archived failure')
            app = CoderApp(runtime)
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause(.1)
                runtime.bus.emit('model_output', channel='request', text='started')
                runtime.bus.emit('model_output', channel='content', text='old stream')
                await pilot.press('f3')
                await pilot.pause(.2)
                pane = app.query_one(LogPane)
                await pilot.click('#clear-model-stream')
                self.assertEqual(pane.model_text, '')
                self.assertTrue(pane.model_active)
                runtime.bus.emit('model_output', channel='content', text='new stream')
                await pilot.pause(.3)
                self.assertIn('new stream', str(pane.query_one('#model-stream-content').content))
                self.assertNotIn('old stream', str(pane.query_one('#model-stream-content').content))
                await pilot.click('#--content-tab-errors-tab')
                await pilot.pause(.2)
                self.assertTrue(await pilot.click('#clear-error-log'),
                    (pane.display, pane.query_one(TabbedContent).active, pane.query_one('#clear-error-log').region))
                await pilot.pause(.2)
                self.assertNotIn('old failure', runtime.diagnostics.tail())
                self.assertFalse(archive.exists())
                self.assertNotIn('old failure', str(pane.query_one('#log-content').content))
                runtime.diagnostics.record(ValueError('new failure'))
                await pane.refresh_log()
                self.assertIn('new failure', str(pane.query_one('#log-content').content))
                await pilot.press('escape', 'f3')
                await pilot.pause(.2)
                self.assertNotIn('old stream', pane.model_text)
                self.assertNotIn('old failure', Diagnostics(Path(directory)).tail())
                self.assertFalse(runtime.bus.cancelled.is_set())

    async def test_e2ee_installation_log_and_failure_stay_visible(self):
        runtime = FakeRuntime()
        app = CoderApp(runtime)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(0.1)
            runtime.bus.emit("e2ee_setup", state="checking", text="Проверка библиотек")
            runtime.bus.emit("e2ee_setup", state="installing", text="Автоматическая установка БЕЗ ПРОКСИ")
            runtime.bus.emit("e2ee_setup", state="log", text="pip: downloading crypto")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, E2EESetupScreen)
            self.assertIn("pip: downloading crypto", '\n'.join(line.text for line in app.screen.query_one(RichLog).lines))
            runtime.bus.emit("e2ee_setup", state="failed", text="pip HTTP 403")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, E2EESetupScreen)
            self.assertIn("Ошибка", str(app.screen.query_one('#setup-title', Static).content))
            self.assertIn("pip HTTP 403", '\n'.join(line.text for line in app.screen.query_one(RichLog).lines))
            self.assertEqual(str(app.screen.query_one(Button).label), "Закрыть лог")

    async def test_e2ee_ready_closes_installer_and_cancel_stops_startup(self):
        runtime = FakeRuntime()
        app = CoderApp(runtime)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(0.1)
            runtime.bus.emit("e2ee_setup", state="checking", text="Проверка")
            await pilot.pause(0.2)
            runtime.bus.emit("e2ee_setup", state="ready", text="Готово")
            await pilot.pause(0.2)
            self.assertNotIsInstance(app.screen, E2EESetupScreen)
            self.assertFalse(runtime.bus.cancelled.is_set())
            runtime.bus.emit("e2ee_setup", state="installing", text="Установка")
            await pilot.pause(0.2)
            await pilot.click('#setup-close')
            self.assertTrue(runtime.bus.cancelled.is_set())

    async def test_attestation_recovery_closes_visible_question_without_answer(self):
        runtime = FakeRuntime()
        app = CoderApp(runtime)
        recovered = threading.Event()
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(0.1)
            waiting = Future()
            def ask():
                try:
                    waiting.set_result(runtime.bus.request("Аттестация просрочена",
                        [("stop", "Остановить")], auto_resolve=recovered.is_set))
                except Exception as error:
                    waiting.set_exception(error)
            thread = threading.Thread(target=ask, daemon=True)
            self.addCleanup(recovered.set)
            thread.start()
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, QuestionScreen)
            recovered.set()
            self.assertEqual(waiting.result(timeout=2), "recovered")
            thread.join(timeout=1)
            await pilot.pause(0.2)
            self.assertIsNone(app.question)
            self.assertIsNone(app.pending_question)
            self.assertNotEqual(app.phase, "Ожидается ответ · Tab")
            self.assertNotIsInstance(app.screen, QuestionScreen)
            self.assertFalse(runtime.bus.cancelled.is_set())

    async def test_second_proxy_settings_save_for_restart_and_hide_password(self):
        from types import SimpleNamespace
        from pycoder.tui import ProxyScreen
        from pycoder.config import load
        runtime = FakeRuntime()
        with tempfile.TemporaryDirectory() as directory:
            runtime.args = SimpleNamespace(config_dir=Path(directory))
            app = CoderApp(runtime)
            async with app.run_test(size=(150, 48)) as pilot:
                await pilot.pause(0.1)
                screen = ProxyScreen(runtime.view_state["config"])
                app.push_screen(screen)
                await pilot.pause()
                self.assertNotIn("proxy_second_host", dict(screen.items()))
                def select(key):
                    screen.query_one(OptionList).highlighted = next(i for i, item in enumerate(screen.options) if item[0] == key)
                select("proxy_chain_enabled")
                await pilot.press("enter")
                self.assertIn("proxy_second_host", dict(screen.items()))
                screen.draft.proxy_enabled = True
                screen.draft.proxy_second_username = "second-user"
                select("proxy_second_password")
                await pilot.press("enter")
                await pilot.pause()
                answer = app.screen.query_one(Input)
                self.assertTrue(answer.password)
                answer.value = "second-secret"
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(screen.draft.proxy_second_password, "second-secret")
                self.assertNotIn("second-secret", str(screen.items()))
                select("save")
                await pilot.press("enter")
                await pilot.pause()
                saved = load(Path(directory))
                self.assertTrue(saved.proxy_chain_enabled)
                self.assertEqual(saved.proxy_second_password, "second-secret")
                self.assertFalse(runtime.view_state["config"]["proxy_chain_enabled"])

    async def test_unverified_mode_and_independent_proxy_routes_are_visible(self):
        from textual.widgets import Static
        from pycoder.tui import ProxyScreen
        runtime = FakeRuntime()
        runtime.view_state["config"].update(proxy_enabled=True, proxy_intel=False)
        app = CoderApp(runtime)
        async with app.run_test(size=(150, 44)) as pilot:
            await pilot.pause(0.2)
            header = str(app.query_one("#header", Static).content)
            self.assertIn("Intel: напрямую", header)
            self.assertIn("Chutes/API: прокси", header)
            settings = SettingsScreen(runtime.view_state["config"])
            app.push_screen(settings)
            await pilot.pause()
            settings.query_one(OptionList).highlighted = next(i for i, item in enumerate(settings.options) if item[0] == "e2ee_verify_attestation")
            await pilot.press("enter")
            self.assertFalse(settings.draft.e2ee_verify_attestation)
            settings.query_one(OptionList).highlighted = next(i for i, item in enumerate(settings.options) if item[0] == "apply")
            await pilot.press("enter")
            await pilot.press("escape")
            await pilot.pause(0.2)
            self.assertFalse(runtime.view_state["config"]["e2ee_verify_attestation"])
            header = app.query_one("#header", Static).content
            self.assertIn("СЕРВЕР НЕ ПРОВЕРЕН", str(header))
            self.assertTrue(any(str(span.style) == "bold black on yellow" for span in header.spans))
            screen = ProxyScreen(runtime.view_state["config"])
            app.push_screen(screen)
            await pilot.pause()
            screen.query_one(OptionList).highlighted = next(i for i, item in enumerate(screen.options) if item[0] == "proxy_nvidia")
            await pilot.press("enter")
            self.assertFalse(screen.draft.proxy_nvidia)
            self.assertTrue(screen.draft.proxy_chutes)

    async def test_proxy_indicator_and_restart_only_settings(self):
        from types import SimpleNamespace
        from textual.widgets import Static
        from pycoder.tui import ProxyScreen
        from pycoder.config import load
        runtime = FakeRuntime()
        runtime.view_state["config"].update(proxy_enabled=True, proxy_username="private-user", proxy_password="private-pass")
        with tempfile.TemporaryDirectory() as directory:
            runtime.args = SimpleNamespace(config_dir=Path(directory))
            app = CoderApp(runtime)
            async with app.run_test(size=(140, 42)) as pilot:
                await pilot.pause(0.2)
                header = app.query_one("#header", Static).content
                self.assertIn("ЧЕРЕЗ ПРОКСИ", str(header))
                self.assertNotIn("private-", str(header))
                self.assertTrue(any(str(span.style) == "bold black on yellow" for span in header.spans))
                screen = ProxyScreen(runtime.view_state["config"])
                app.push_screen(screen)
                await pilot.pause()
                self.assertNotIn("private-", str(screen.items()))
                screen.draft.proxy_enabled = False
                index = next(i for i, (key, _) in enumerate(screen.options) if key == "save")
                screen.query_one(OptionList).highlighted = index
                await pilot.press("enter")
                await pilot.pause()
                self.assertFalse(load(Path(directory)).proxy_enabled)
                self.assertTrue(runtime.view_state["config"]["proxy_enabled"])


    async def test_model_name_color_reflects_verified_encryption_and_downgrade(self):
        from textual.widgets import Static
        runtime = FakeRuntime()
        app = CoderApp(runtime)
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause(0.1)
            for mode, color, detail in [
                    ("checking", "cyan", "Проверка TEE"),
                    ("encrypted", "bold green", "E2EE · Intel TDX и NVIDIA проверены"),
                    ("blocked", "bold red", "Аттестация не пройдена"),
                    ("plain", "yellow", "Без E2EE · разрешено пользователем")]:
                runtime.bus.emit("model_security", mode=mode, detail=detail)
                await pilot.pause(0.1)
                header = app.query_one("#header", Static).content
                self.assertIn(detail, str(header))
                self.assertTrue(any(str(span.style) == color for span in header.spans))
            self.assertFalse(runtime.bus.cancelled.is_set())

    async def test_live_model_tab_retains_hidden_stream_and_does_not_cancel(self):
        from pycoder.tui import LogPane
        from textual.widgets import TabbedContent
        runtime = FakeRuntime()
        runtime.safe = lambda text: str(text).replace("secret-token", "[REDACTED]")
        app = CoderApp(runtime)
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause(0.1)
            runtime.bus.emit("model_output", channel="request", text="Запрос начат")
            runtime.bus.emit("model_output", channel="reasoning", text="secret-")
            runtime.bus.emit("model_output", channel="reasoning", text="token размышление")
            await pilot.pause(0.1)
            await pilot.press("f3")
            await pilot.pause(0.3)
            pane = app.query_one(LogPane)
            self.assertTrue(pane.display)
            self.assertEqual(pane.query_one(TabbedContent).active, "model-stream-tab")
            content = str(pane.query_one("#model-stream-content").content)
            self.assertIn("[REDACTED] размышление", content)
            self.assertNotIn("secret-token", content)
            self.assertIn("последний фрагмент", str(pane.query_one("#model-stream-status").content))
            self.assertIsNone(app.response)  # Reasoning does not become the chat answer.
            pane.query_one(TabbedContent).active = "errors-tab"
            runtime.bus.emit("model_output", channel="tool:0:arguments", text='{"path":"a"}')
            runtime.bus.emit("model_output", channel="end", text="Ответ получен")
            await pilot.pause(0.1)
            pane.query_one(TabbedContent).active = "model-stream-tab"
            await pilot.pause(0.3)
            self.assertIn('{"path":"a"}', str(pane.query_one("#model-stream-content").content))
            self.assertIn("Запрос завершён", str(pane.query_one("#model-stream-status").content))
            await pilot.press("escape")
            self.assertFalse(pane.display)
            self.assertFalse(runtime.bus.cancelled.is_set())
            pane.append_model("content", "x" * 130000)
            self.assertEqual(len(pane.model_text), 120000)
            self.assertTrue(pane.model_trimmed)

    async def test_live_process_indicator_and_stop_from_panel(self):
        from textual.widgets import Button
        runtime = FakeRuntime()
        snapshot = {"active_count": 1, "limit": 5, "running": [
            {"id": 7, "owner": "worker", "command": "python server.py", "cwd": "/project", "session_id": 2, "pid": 123, "elapsed": 9}]}
        stopped = []
        runtime.background_processes = lambda: snapshot
        runtime.stop_background_process = lambda identifier: stopped.append(identifier)
        app = CoderApp(runtime)
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause(0.2)
            self.assertIn("1/5", str(app.query_one("#background-processes", Button).label))
            await pilot.click("#background-processes")
            self.assertIsInstance(app.screen, PanelScreen)
            self.assertIn("python server.py", str(app.screen.query_one("#panel-body").content))
            await pilot.click("#stop-process")
            await pilot.press("enter")
            self.assertEqual(stopped, [7])
            snapshot.update(active_count=0, running=[])
            await pilot.press("escape")
            await pilot.pause(0.2)
            self.assertIn("0/5", str(app.query_one("#background-processes", Button).label))
            self.assertFalse(runtime.bus.cancelled.is_set())

    async def test_sandbox_settings_use_windows_and_escape_preserves_chat(self):
        from pycoder.tui import SandboxScreen
        runtime = FakeRuntime()
        app = CoderApp(runtime)
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause(0.1)
            app.query_one(Composer).load_text("черновик")
            await pilot.press("tab", "enter")
            app.screen.query_one(OptionList).highlighted = len(LIMITS) + 2
            await pilot.press("enter")
            self.assertIsInstance(app.screen, SandboxScreen)
            # Select allowlist, then add an exact host in a nested text dialog.
            app.screen.query_one(OptionList).highlighted = 1
            await pilot.press("enter", "down", "enter")
            app.screen.query_one(OptionList).highlighted = 2
            await pilot.press("enter", "enter")
            app.screen.query_one(Input).value = "pypi.org:443"
            await pilot.press("enter")
            self.assertEqual(app.screen.draft.sandbox_hosts, ["pypi.org:443"])
            app.screen.query_one(OptionList).highlighted = 3
            await pilot.press("enter", "enter")
            app.screen.query_one(Input).value = "localhost:8000"
            await pilot.press("enter")
            self.assertEqual(app.screen.draft.sandbox_direct_hosts, ["localhost:8000"])
            app.screen.query_one(OptionList).highlighted = next(i for i, item in enumerate(app.screen.options) if item[0] == "apply")
            await pilot.press("enter")
            self.assertEqual(runtime.view_state["config"]["sandbox_network"], "allowlist")
            self.assertEqual(runtime.view_state["config"]["sandbox_hosts"], ["pypi.org:443"])
            self.assertEqual(runtime.view_state["config"]["sandbox_direct_hosts"], ["localhost:8000"])
            await pilot.press("escape", "escape")
            self.assertEqual(app.query_one(Composer).text, "черновик")
            self.assertEqual(runtime.submitted, [])
            self.assertFalse(runtime.bus.cancelled.is_set())

    async def test_menu_navigation_preserves_draft_and_escape_never_cancels(self):
        runtime = FakeRuntime()
        app = CoderApp(runtime)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.1)
            app.busy = True
            app.query_one(Composer).load_text("Мой черновик")
            await pilot.press("escape", "tab", "enter")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, SettingsScreen)
            await pilot.press("enter")
            await pilot.pause(0.1)
            app.screen.query_one(Input).value = "12345"
            await pilot.press("escape")
            self.assertIsInstance(app.screen, SettingsScreen)
            self.assertEqual(app.screen.draft.max_output_tokens, 4096)
            await pilot.press("escape")
            self.assertIsInstance(app.screen, MenuScreen)
            await pilot.press("down", "enter")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, PanelScreen)
            self.assertEqual(app.screen.section, "context")
            await pilot.press("escape", "escape", "escape")
            self.assertEqual(app.query_one(Composer).text, "Мой черновик")
            self.assertFalse(runtime.submitted)
            self.assertFalse(list(app.query(".user")))
            self.assertFalse(runtime.bus.cancelled.is_set())

    async def test_agent_question_waits_behind_settings_and_appears_in_existing_menu(self):
        app = CoderApp(FakeRuntime())
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.1)
            await pilot.press("tab", "enter")
            self.assertIsInstance(app.screen, SettingsScreen)
            answer = Future()
            app.runtime.bus.emit("question", title="Новый вопрос", options=[("ok", "Ответить")], reply=answer)
            await pilot.pause(0.3)
            self.assertIsInstance(app.screen, SettingsScreen)
            await pilot.press("escape")
            self.assertIsInstance(app.screen, MenuScreen)
            self.assertEqual(app.screen.options[0][0], "question")
            app.screen.query_one(OptionList).highlighted = 0
            await pilot.press("enter")
            self.assertEqual(app.screen.data["title"], "Новый вопрос")
            await pilot.press("enter")
            self.assertEqual(answer.result(timeout=0.2), "ok")

    async def test_escape_suspends_question_and_menu_can_restore_it(self):
        runtime = FakeRuntime()
        app = CoderApp(runtime)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(0.1)
            answer = Future()
            runtime.bus.emit("question", title="Разрешить?", options=[("yes", "Да"), ("no", "Нет")], reply=answer)
            await pilot.pause(0.1)
            await pilot.press("escape")
            self.assertFalse(answer.done())
            self.assertFalse(runtime.bus.cancelled.is_set())
            await pilot.press("tab", "enter")
            await pilot.pause(0.1)
            self.assertEqual(app.screen.data["title"], "Разрешить?")
            await pilot.press("down", "enter")
            self.assertEqual(answer.result(timeout=0.2), "no")

    async def test_quit_requires_explicit_confirmation(self):
        runtime = FakeRuntime()
        app = CoderApp(runtime)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(0.1)
            app.busy = True
            await pilot.press("ctrl+q")
            self.assertFalse(runtime.bus.cancelled.is_set())
            await pilot.press("escape")
            self.assertFalse(app.closing)
            await pilot.press("ctrl+q", "enter")
            self.assertFalse(app.closing)
            await pilot.press("ctrl+q", "down", "enter")
            self.assertTrue(app.closing)
            self.assertTrue(runtime.bus.cancelled.is_set())

    async def test_five_rolling_rows_expand_full_output_and_close_without_cancelling(self):
        runtime = FakeRuntime()
        app = CoderApp(runtime)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.log"
            content = "\n".join(f"line {i}" for i in range(20))
            path.write_text(content)
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause(0.1)
                runtime.bus.emit("tool_start", id="x", name="exec_command", arguments='{"cmd":"python3 build.py"}')
                runtime.bus.emit("tool_output", id="x", tail=content, path=str(path), completed=False)
                await pilot.pause(0.1)
                card = app.query_one(ToolCard)
                self.assertEqual(card.title, "Running python3 build.py")
                self.assertTrue(card.preview.display)
                lines = card.preview.render().plain.splitlines()
                self.assertEqual(len(lines), 5)
                self.assertIn("line 15", lines[0])
                self.assertIn("line 19", lines[-1])
                self.assertLessEqual(card.preview.size.height, 5)
                # Even a single long line is capped after wrapping to screen rows.
                card.update_preview("я" * 1200)
                await pilot.pause(0.1)
                self.assertEqual(len(card.preview.render().plain.splitlines()), 5)
                runtime.bus.emit("tool_output", id="x", tail=content, path=str(path), completed=True, exit_code=0)
                runtime.bus.emit("tool_end", id="x", name="exec_command", output='{"exit_code":0,"output":"[limited for model]"}')
                await pilot.pause(0.1)
                self.assertEqual(card.title, "Ran python3 build.py")
                card.collapsed = False
                await pilot.pause(0.2)
                self.assertIn("line 0\n", card.full.content)
                self.assertNotIn("limited for model", card.full.content)
                await pilot.click(card.open_button)
                await pilot.pause(0.1)
                self.assertIsInstance(app.screen, OutputScreen)
                self.assertEqual(app.screen.query_one("#output-page").content, content)
                await pilot.press("escape")
                await pilot.pause(0.1)
                self.assertFalse(runtime.bus.cancelled.is_set())

    async def test_settings_in_existing_dialogue_and_final_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with server(lambda r: json_response(reply("Готово"))) as (url, requests):
                args = parser().parse_args(["--new", "--config-dir", str(root / "config"), "--no-auth"])
                runtime = Runtime(args, Config(model="test", base_url=url, require_token=False), root, root / "state")
                app = CoderApp(runtime)
                try:
                    async with app.run_test(size=(100, 40)) as pilot:
                        await self.wait_until(pilot, lambda: isinstance(app.screen, QuestionScreen))
                        await pilot.press("enter")
                        await self.wait_until(pilot, lambda: app.ready)
                        session = runtime.history.session_id
                        await pilot.press("tab", "enter")
                        await self.wait_until(pilot, lambda: isinstance(app.screen, QuestionScreen))
                        await pilot.press("enter")
                        await self.wait_until(pilot, lambda: isinstance(app.screen, QuestionScreen) and not app.screen.options)
                        app.screen.query_one(Input).value = "8192"
                        await pilot.press("enter")
                        await self.wait_until(pilot, lambda: isinstance(app.screen, QuestionScreen) and bool(app.screen.options))
                        await pilot.press(*(["down"] * len(LIMITS)), "enter")
                        await self.wait_until(pilot, lambda: runtime.config.max_output_tokens == 8192)
                        await pilot.press("escape", "escape")
                        self.assertEqual(runtime.config.max_output_tokens, 8192)
                        self.assertEqual(runtime.history.session_id, session)
                        self.assertFalse((root / "config/config.json").exists())
                        app.query_one(Composer).load_text("Скажи готово")
                        await pilot.press("enter")
                        await self.wait_until(pilot, lambda: bool(requests) and not app.busy)
                        self.assertEqual(requests[0]["body"]["max_tokens"], 8192)
                        self.assertTrue(any("30 токенов" in str(w.content) and "всего" in str(w.content)
                                            for w in app.query(".status")))
                finally:
                    runtime.stop()
                    await asyncio.to_thread(runtime.thread.join, 3)

    async def test_mouse_selection_native_copy_and_right_click_menu(self):
        runtime = FakeRuntime()
        app = CoderApp(runtime)
        with patch("pycoder.tui.write_clipboard", return_value=True) as copy, patch("pycoder.tui.read_clipboard", return_value="Вставка"):
            async with app.run_test(size=(100, 35)) as pilot:
                await pilot.pause(0.1)
                runtime.bus.emit("error", text="Ошибка соединения")
                await pilot.pause(0.1)
                bubble = list(app.query(".error"))[-1]
                x = bubble.content_region.x - bubble.region.x
                y = bubble.content_region.y - bubble.region.y
                await pilot.mouse_down(bubble, offset=(x, y))
                await pilot.hover(bubble, offset=(x + 5, y))
                await pilot.mouse_up(bubble, offset=(x + 5, y))
                await pilot.pause(0.1)
                self.assertEqual(app.selected_text(), "Ошибка")
                app.busy = True
                await pilot.press("ctrl+c")
                await pilot.pause(0.2)
                copy.assert_called_with("Ошибка")
                self.assertFalse(runtime.bus.cancelled.is_set())
                await pilot.mouse_down(bubble, offset=(x + 2, y), button=3)
                await pilot.pause(0.1)
                self.assertIsInstance(app.screen, CopyMenu)
                self.assertEqual(app.screen.selected, "Ошибка")
                self.assertEqual(app.screen.message, "Ошибка соединения")
                await pilot.press("down", "down", "enter")
                await pilot.pause(0.2)
                copy.assert_called_with("Ошибка соединения")

    async def test_native_paste_shortcut_menu_order_and_copy_never_cancels(self):
        from textual.document._document import Selection
        runtime = FakeRuntime()
        app = CoderApp(runtime)
        with patch("pycoder.tui.read_clipboard", return_value="первая\r\nвторая") as read:
            async with app.run_test(size=(100, 35)) as pilot:
                await pilot.pause(0.1)
                composer = app.query_one(Composer)
                composer.focus()
                app.busy = True
                await pilot.press("ctrl+c", "ctrl+v")
                await pilot.pause(0.2)
                self.assertEqual(composer.text, "первая\nвторая")
                self.assertFalse(runtime.bus.cancelled.is_set())
                self.assertFalse(runtime.submitted)
                composer.load_text("")
                app.action_copy_menu(composer)
                await pilot.pause(0.2)
                self.assertEqual(app.screen.query_one(OptionList).get_option_at_index(0).id, "paste")
                await pilot.press("enter")
                await pilot.pause(0.2)
                self.assertEqual(composer.text, "первая\nвторая")
                composer.selection = Selection((0, 0), (0, 6))
                read.return_value = "замена"
                app.action_copy_menu(composer)
                await pilot.pause(0.2)
                self.assertEqual(app.screen.query_one(OptionList).get_option_at_index(0).id, "selection")
                await pilot.press("down", "enter")
                await pilot.pause(0.2)
                self.assertEqual(composer.text, "замена\nвторая")
                read.return_value = ""
                app.action_copy_menu(composer)
                await pilot.pause(0.2)
                self.assertTrue(app.screen.query_one(OptionList).get_option("paste").disabled)
                await pilot.press("escape", "ctrl+g")
                self.assertTrue(runtime.bus.cancelled.is_set())
                app.push_screen(QuestionScreen({"title": "API-ключ", "allow_text": True, "secret": True}))
                await pilot.pause(0.1)
                read.return_value = "тестовый-ключ"
                await pilot.press("ctrl+v")
                await pilot.pause(0.2)
                self.assertEqual(app.screen.query_one(Input).value, "тестовый-ключ")
                self.assertEqual(composer.text, "замена\nвторая")
                read.return_value = None
                await pilot.press("ctrl+v")
                await pilot.pause(0.2)
                self.assertEqual(app.screen.query_one(Input).value, "тестовый-ключ")

    async def test_streamed_markdown_is_selectable_prose_with_speed_and_labels(self):
        runtime = FakeRuntime()
        app = CoderApp(runtime)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(0.1)
            runtime.bus.emit("model_start")
            runtime.bus.emit("text", text="**Привет** мир\n```py\nsecret_code()")
            runtime.bus.emit("generation", tokens=40, elapsed=2, exact=False, finished=False)
            await pilot.pause(0.1)
            self.assertEqual(app.response.content.markup, "**Привет** мир")
            self.assertIn("≈20.0 ток/с", app.last_status)
            bubble = app.response
            x, y = bubble.content_region.x - bubble.region.x, bubble.content_region.y - bubble.region.y
            await pilot.mouse_down(bubble, offset=(x, y))
            await pilot.hover(bubble, offset=(x + 5, y))
            await pilot.mouse_up(bubble, offset=(x + 5, y))
            self.assertEqual(app.selected_text(), "Привет")
            runtime.bus.emit("text", text="\n```\nГотово")
            runtime.bus.emit("model_end")
            runtime.bus.emit("tool_start", id="file", name="read_file", arguments='{"path":"main.py"}')
            runtime.bus.emit("tool_end", id="file", name="read_file", output='{"content":"x"}')
            await pilot.pause(0.1)
            self.assertIn("Готово", app.response.content.markup)
            self.assertNotIn("secret_code", app.response.content.markup)
            self.assertIn("main.py", app.query_one(Collapsible).title)

    async def test_automatic_retry_separates_partial_and_recovered_answers(self):
        app = CoderApp(FakeRuntime())
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(0.1)
            await app.handle_event("model_start", {})
            await app.handle_event("text", {"text": "Оборванный ответ"})
            old = app.response
            await app.handle_event("model_restart", {"automatic": True, "attempt": 1, "limit": 3})
            self.assertIn("Автоматический повтор 1", old.content.markup)
            self.assertIsNone(app.response)
            await app.handle_event("text", {"text": "Новый ответ"})
            await app.handle_event("model_end", {})
            self.assertIsNot(app.response, old)
            self.assertEqual(app.response_text, "Новый ответ")
            self.assertNotIn("Оборванный", app.response.content.markup)

    async def test_retry_updates_one_bubble_and_clears_on_success_or_cancel(self):
        app = CoderApp(FakeRuntime())
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(0.1)
            await app.handle_event("model_start", {})
            before = len(app.query(".bubble"))
            bubble = None
            for attempt in range(1, 9):
                await app.handle_event("model_restart", {"automatic": True, "attempt": attempt,
                                       "limit": None, "delay": 10, "error": "HTTP 429 capacity"})
                if bubble is None:
                    bubble = app.retry_bubble
                self.assertIs(app.retry_bubble, bubble)
                self.assertEqual(len(app.query(".bubble")), before + 1)
                self.assertIn(f"Повтор {attempt}", str(bubble.content))
            await app.handle_event("text", {"text": "Готово"})
            await app.handle_event("model_end", {})
            self.assertIsNone(app.retry_bubble)
            self.assertEqual(len(app.query(".bubble")), before + 1)
            await app.handle_event("model_restart", {"automatic": True, "attempt": 1})
            await app.handle_event("idle", {})
            self.assertIsNone(app.retry_bubble)

    async def test_arrow_choice_custom_answer_and_no_automatic_approval(self):
        app = CoderApp(FakeRuntime())
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(0.1)
            answer = Future()
            app.runtime.bus.emit("question", title="Permit?", options=[("once", "Once"), ("remember", "Remember"), ("deny", "Deny")], reply=answer)
            await pilot.pause(0.1)
            self.assertFalse(answer.done())
            await pilot.press("down", "enter")
            await pilot.pause(0.1)
            self.assertEqual(answer.result(timeout=0.2), "remember")
            answer = Future()
            app.runtime.bus.emit("question", title="Choose", options=[("a", "A")], allow_text=True, reply=answer)
            await pilot.pause(0.1)
            await pilot.press("down", "enter", "x", "enter")
            await pilot.pause(0.1)
            self.assertEqual(answer.result(timeout=0.2), "x")

    async def test_stream_tools_diff_composer_and_command_menu(self):
        runtime = FakeRuntime()
        app = CoderApp(runtime)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.1)
            await pilot.press("h", "i", "ctrl+j", "x", "enter")
            await pilot.pause(0.1)
            self.assertEqual(runtime.submitted, ["hi\nx"])
            runtime.bus.emit("model_start")
            runtime.bus.emit("text", text="**Готово**\n\n```python\nprint('hi')\n```")
            runtime.bus.emit("model_end")
            runtime.bus.emit("tool_start", id="1", name="exec_command", arguments='{"cmd":"echo hi"}')
            runtime.bus.emit("tool_end", id="1", name="exec_command", output='{"exit_code":0,"output":"hi"}')
            runtime.bus.emit("patch", diff="--- a\n+++ b\n-old\n+new", changed=["a"])
            runtime.bus.emit("context", used=100, window=1000)
            for _ in range(40):
                if app.used == 100:
                    break
                await pilot.pause(0.05)
            self.assertEqual(len(app.query(Collapsible)), 2)
            self.assertIn("Готово", app.response_text)
            await pilot.press("tab")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, QuestionScreen)
            await pilot.press("enter")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, SettingsScreen)
            self.assertEqual(app.query_one(Composer).text, "")

    async def test_runtime_real_tools_permission_question_and_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            responses = [reply(None, [call("apply_patch", {"patch": "*** Begin Patch\n*** Add File: hello.py\n+print('hello')\n*** End Patch"}, "patch")]),
                         reply(None, [call("shell", {"command": [__import__("sys").executable, "-c", "print('PASS')"]}, "command")]),
                         reply(None, [call("request_user_input", {"questions": [{"id": "next", "question": "Next?", "options": [{"label": "Done", "description": "Finish"}]}]}, "question")]),
                         reply("**Готово.**")]
            with server(lambda r: json_response(responses[len(r) - 1])) as (url, requests):
                args = parser().parse_args(["--new", "--config-dir", str(root / "config"), "--no-auth"])
                runtime = Runtime(args, Config(model="test", base_url=url, require_token=False, sandbox_enabled=False), root, root / "state")
                app = CoderApp(runtime)
                async with app.run_test(size=(100, 40)) as pilot:
                    await self.wait_until(pilot, lambda: isinstance(app.screen, QuestionScreen))
                    await pilot.press("enter")
                    await self.wait_until(pilot, lambda: app.ready)
                    app.query_one(Composer).load_text("Make hello")
                    await pilot.press("enter")
                    await self.wait_until(pilot, lambda: isinstance(app.screen, QuestionScreen))
                    self.assertIn("Разрешить", app.screen.data["title"])
                    self.assertTrue((root / "hello.py").exists())
                    await pilot.press("down", "down", "enter")
                    await self.wait_until(pilot, lambda: isinstance(app.screen, QuestionScreen) and app.screen.data["title"] == "Next?")
                    await pilot.press("enter")
                    await self.wait_until(pilot, lambda: len(requests) == 4 and not app.busy)
                    self.assertTrue(runtime.policy.saved)
                    self.assertIn("Готово", runtime.history.messages[-1]["content"])
                runtime.stop()
                await asyncio.to_thread(runtime.thread.join, 3)
                self.assertFalse(runtime.thread.is_alive())

    async def wait_until(self, pilot, condition):
        for _ in range(100):
            if condition():
                return
            await pilot.pause(0.05)
        self.fail("UI condition did not become true")
