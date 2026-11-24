import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from pycoder.agent import Agent
from pycoder.config import Config
from pycoder.events import Cancelled
from pycoder.events import EventBus
from pycoder.history import History
from pycoder.team import Team
from pycoder.tools import Tools, COLLAB_NAMES
from pycoder.transport import Client, Completion
from pycoder.usage import TurnUsage
from tests.support import server, reply, call, json_response


class TeamTests(unittest.TestCase):
    def test_parallel_questions_are_serialized_and_individually_cancellable(self):
        bus = EventBus()
        cancel = threading.Event()
        answers = []
        def ask(title, cancelled=None):
            try:
                answers.append(bus.request(title, [("yes", "Yes")], cancelled=cancelled))
            except Cancelled:
                answers.append("cancelled")
        first = threading.Thread(target=ask, args=("first", cancel))
        second = threading.Thread(target=ask, args=("second",))
        first.start()
        question = bus.queue.get(timeout=2)
        second.start()
        time.sleep(0.15)
        self.assertTrue(bus.queue.empty())
        cancel.set()
        first.join(2)
        self.assertTrue(question.data["reply"].cancelled())
        next_question = bus.queue.get(timeout=2)
        self.assertEqual(next_question.data["title"], "second")
        next_question.data["reply"].set_result("yes")
        second.join(2)
        self.assertCountEqual(answers, ["cancelled", "yes"])
        self.assertFalse(bus.cancelled.is_set())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.cfg = Config(model="test", context_window=65536, max_output_tokens=1024, approval="auto", retries=0)
        self.history = History(self.root / "state", self.root)
        self.tools = Tools(self.root, self.cfg, self.history, lambda *a: False, lambda q: "yes", lambda t: None)
        self.team = None

    def tearDown(self):
        if self.team:
            self.team.close()
        self.tools.close()
        self.history.close()
        self.tmp.cleanup()

    def agent(self, client):
        return Agent(self.cfg, client, self.history, self.tools, lambda t: None, lambda t: None)

    def test_real_http_subagents_overlap_patch_separate_files_and_parent_integrates(self):
        barrier = threading.Barrier(2)
        errors = []
        def responder(requests):
            body = requests[-1]["body"]
            messages = body["messages"]
            system = messages[0]["content"]
            child = next((name for name in ("alpha", "beta") if f"You are subagent {name}." in system), None)
            if child:
                self.assertFalse(COLLAB_NAMES & {spec["function"]["name"] for spec in body["tools"]})
                if not any(m["role"] == "tool" for m in messages):
                    try:
                        barrier.wait(timeout=3)
                    except threading.BrokenBarrierError:
                        errors.append("subagents did not overlap")
                    patch = f"*** Begin Patch\n*** Add File: {child}.txt\n+{child}\n*** End Patch"
                    return json_response(reply(None, [call("apply_patch", {"patch": patch})]))
                return json_response(reply(f"{child} done"))
            if not any(m["role"] == "assistant" for m in messages):
                return json_response(reply(None, [call("spawn_agent", {"task_name": name, "message": f"Create {name}.txt only"}, name) for name in ("alpha", "beta")]))
            if any("Subagent alpha-" in str(m.get("content")) for m in messages):
                return json_response(reply("Integrated both results"))
            return json_response(reply("Waiting for integration"))
        with server(responder) as (url, requests):
            self.cfg.base_url = url
            client = Client(self.cfg, "test-key")
            usage = TurnUsage()
            client.on_usage = usage.add
            parent = self.agent(client)
            self.team = Team(parent, self.root / "state", on_usage=usage.add)
            answer = parent.run("Create alpha.txt and beta.txt independently")
            self.team.join()
            self.assertEqual(answer, "Integrated both results")
            self.assertFalse(errors)
            for name in ("alpha", "beta"):
                self.assertEqual((self.root / f"{name}.txt").read_text(), name + "\n")
            self.assertTrue(all(m["status"] == "completed" for m in self.team.snapshot()))
            self.assertEqual(usage.requests, len(requests))
            self.assertTrue(all(r["path"] == "/v1/chat/completions" and r["headers"]["Authorization"] == "Bearer test-key" for r in requests))
            paths = [m["history"] for m in self.team.snapshot()]
            self.assertEqual(len(set(paths)), 2)
            self.assertTrue(all(Path(path).exists() for path in paths))

    def test_cap_interrupt_followup_and_mailbox(self):
        entered = threading.Event()
        class BlockingClient:
            def __init__(self, cfg, token, status):
                self.token = token
                self.cancelled = threading.Event()
                self.count = 0
            def complete(self, messages, *args, **kwargs):
                self.count += 1
                if self.count == 1:
                    entered.set()
                    self.cancelled.wait(5)
                    raise Cancelled()
                return Completion({"role": "assistant", "content": "Follow-up complete"}, {}, "stop")
            def cancel(self):
                self.cancelled.set()
        self.cfg.max_subagents = 1
        parent = self.agent(Client(self.cfg, ""))
        self.team = Team(parent, self.root / "state", client_factory=BlockingClient)
        member_id = self.team.spawn_agent("worker", "Do work")["agent_id"]
        self.assertTrue(entered.wait(2))
        self.team.update(member_id, "text", text="Проверяю исходники")
        self.team.update(member_id, "tool_start", name="read_file", arguments='{"path":"app.py"}')
        self.team.update(member_id, "tool_output", tail="live command output")
        self.team.update(member_id, "plan", plan=[{"step": "Inspect", "status": "in_progress"}])
        # Go through the public tool dispatcher while the child request is blocked.
        log = json.loads(self.tools.execute("get_agent_log", json.dumps({"target": "worker"})))
        self.assertEqual(log["status"], "running")
        self.assertIn("Do work", log["journal"]["text"])
        self.assertIn("app.py", log["phase"])
        self.assertEqual(log["live_text"], "Проверяю исходники")
        self.assertEqual(log["tool_output"], "live command output")
        self.assertEqual(log["plan"][0]["status"], "in_progress")
        self.assertLess(log["seconds_since_activity"], 2)
        self.assertNotIn("history", self.team.list_agents()["agents"][0])
        self.assertEqual(self.team.get_agent_log(member_id, log["journal"]["next_offset"])["journal"]["text"], "")
        with self.assertRaisesRegex(ValueError, "Unknown agent"):
            self.team.get_agent_log(str(self.history.path))
        with self.assertRaisesRegex(ValueError, "limit"):
            self.team.spawn_agent("second", "Too many")
        self.team.interrupt_agent(member_id)
        self.team.join()
        self.assertFalse(parent.cancelled.is_set())
        self.assertEqual(self.team.snapshot()[0]["status"], "interrupted")
        self.team.send_message(member_id, "Preserve the API")
        self.team.followup_task(member_id, "Try again")
        self.team.join()
        self.assertEqual(self.team.get_agent_result(member_id)["text"], "Follow-up complete")
        self.assertIn("Follow-up complete", self.team.get_agent_log(member_id)["journal"]["text"])
        self.assertIn("Preserve the API", str(self.team.members[member_id].history.messages))
        self.assertEqual(len(self.team.drain()), 2)

    def test_journal_pagination_is_utf8_safe_and_preserves_writer(self):
        self.history.append({"role": "user", "content": "русский 蟑螂 🦀 " * 100})
        expected = self.history.path.read_text(encoding="utf-8")
        chunks, offset = [], 0
        while True:
            page = self.history.read_log(offset, 257)
            chunks.append(page["text"])
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
            if not page["has_more"]:
                break
        self.assertEqual("".join(chunks), expected)
        tail = self.history.read_log(limit=257)
        self.assertTrue(expected.endswith(tail["text"]))
        self.assertLessEqual(len(tail["text"].encode("utf-8")), 257)
        self.history.append({"role": "assistant", "content": "new data"})
        self.assertIn("new data", self.history.read_log(offset)["text"])
        for offset, limit in [(-1, 6000), (0, 0), (0, 24001), (True, 6000)]:
            with self.assertRaises(ValueError):
                self.history.read_log(offset, limit)

    def test_fork_repairs_pending_parent_calls_without_mutating_parent(self):
        received = []
        class ChildClient:
            def __init__(self, cfg, token, status):
                self.token = token
                self.cancelled = threading.Event()
            def complete(self, messages, *args, **kwargs):
                received.extend(messages)
                return Completion({"role": "assistant", "content": "Child complete"}, {}, "stop")
            def cancel(self):
                self.cancelled.set()
        self.history.append({"role": "user", "content": "Preserve public API"})
        self.history.append({"role": "assistant", "content": None, "tool_calls": [call("spawn_agent", {"task_name": "child", "message": "Review"}, "pending")]})
        before = list(self.history.messages)
        parent = self.agent(Client(self.cfg, ""))
        self.team = Team(parent, self.root / "state", client_factory=ChildClient)
        self.team.spawn_agent("child", "Review only", "all")
        self.team.join()
        self.assertEqual(self.history.messages, before)
        self.assertTrue(any(m["role"] == "tool" and m["tool_call_id"] == "pending" for m in received))
        self.assertEqual(received[-1]["content"], "Review only")

    def test_parallel_reads_with_mutation_barrier_and_ordered_durable_results(self):
        (self.root / "a").write_text("old\n")
        (self.root / "b").write_text("b\n")
        barrier = threading.Barrier(2)
        read = self.tools.read_file
        count = 0
        lock = threading.Lock()
        def coordinated(path):
            nonlocal count
            with lock:
                count += 1
                initial = count <= 2
            if initial:
                barrier.wait(timeout=3)
            return read(path)
        self.tools.read_file = coordinated
        self.tools.output_directory = self.root / "output"
        calls = [call("read_file", {"path": "a"}, "read-a"), call("read_file", {"path": "b"}, "read-b"),
                 call("apply_patch", {"patch": "*** Begin Patch\n*** Update File: a\n@@\n-old\n+new\n*** End Patch"}, "patch"),
                 call("read_file", {"path": "a"}, "read-after")]
        client = Mock()
        client.complete.side_effect = [Completion({"role": "assistant", "content": None, "tool_calls": calls}, {}, "tool_calls"),
                                       Completion({"role": "assistant", "content": "done"}, {}, "stop")]
        self.agent(client).run("Read both then edit")
        results = [m for m in self.history.messages if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in results], [c["id"] for c in calls])
        self.assertIn("old", results[0]["content"])
        self.assertIn("new", results[-1]["content"])
        events = [json.loads(line) for line in self.history.path.read_text().splitlines()]
        journals = [e["data"]["path"] for e in events if e["type"] == "tool_output"]
        self.assertEqual(len(set(journals)), 4)
