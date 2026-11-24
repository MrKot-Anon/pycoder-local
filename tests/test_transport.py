import io
import json
import os
import unittest
from unittest.mock import patch

from pycoder.events import Cancelled
from pycoder.config import Config, endpoint
from pycoder.transport import Client, ModelError
from tests.support import call, json_response, reply, server


class TransportTests(unittest.TestCase):
    def test_model_output_arrives_before_stream_finishes_and_survives_failure(self):
        events = [
            {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
            {"choices": [{"delta": {"content": "answer"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {
                "name": "read_file", "arguments": '{"path":'}}]}}]},
        ]
        data = "".join("data: " + json.dumps(e) + "\n\n" for e in events).encode()
        response = io.BytesIO(data)
        client = Client(Config(model="test"), "")
        fragments = []

        def observe(**event):
            fragments.append(event)
            if event["channel"] == "reasoning":
                self.assertLess(response.tell(), len(data))

        client.on_output = observe
        with self.assertRaisesRegex(ModelError, "Incomplete response"):
            client._stream(response, lambda _: None)
        self.assertEqual(fragments, [
            {"channel": "reasoning", "text": "thinking"},
            {"channel": "content", "text": "answer"},
            {"channel": "tool:0:name", "text": "read_file"},
            {"channel": "tool:0:arguments", "text": '{"path":'},
        ])

    def test_nonstream_output_has_attempt_boundaries_and_reasoning(self):
        message = reply("answer")
        message["choices"][0]["message"]["reasoning_content"] = "thinking"
        with server(lambda _: json_response(message)) as (url, _):
            client = Client(Config(model="test", base_url=url, stream=False), "")
            fragments = []
            client.on_output = lambda **event: fragments.append(event)
            client.complete([])
        self.assertEqual([e["channel"] for e in fragments], ["request", "reasoning", "content", "end"])
        self.assertIn("без streaming", fragments[0]["text"])
        self.assertEqual(fragments[1]["text"], "thinking")

    def test_speed_uses_provider_tokens_or_marks_estimate(self):
        for usage, exact in [({"completion_tokens": 123}, True), ({"total_tokens": 200}, False)]:
            with self.subTest(usage=usage), server(lambda r: json_response(reply("Hello", usage=usage))) as (url, _):
                client = Client(Config(model="test", base_url=url), "")
                metrics = []
                client.on_metrics = lambda **data: metrics.append(data)
                client.complete([])
                self.assertEqual(metrics[-1]["exact"], exact)
                self.assertGreater(metrics[-1]["elapsed"], 0)
                self.assertEqual(metrics[-1]["tokens"], 123 if exact else 2)

    def test_nullable_stream_fields_and_cyrillic_text_after_tool(self):
        events = [
            {"choices": None, "usage": None},
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": None,
                                                  "reasoning_content": None, "tool_calls": None}}]},
            {"choices": [{"index": 0, "delta": {"content": "Р", "tool_calls": None}}]},
            {"choices": [{"index": 0, "delta": {"content": "аботаю", "tool_calls": None}}]},
            {"choices": [{"index": 0, "delta": None, "finish_reason": "stop"}]},
            {"choices": None, "usage": {"total_tokens": 42}},
        ]
        response = io.BytesIO(("".join("data: " + json.dumps(e) + "\n\n" for e in events)
                               + "data: [DONE]\n\n").encode())
        output = []
        completion = Client(Config(model="test"), "")._stream(response, output.append)
        self.assertEqual(completion.message, {"role": "assistant", "content": "Работаю"})
        self.assertEqual("".join(output), "Работаю")
        self.assertEqual(completion.usage, {"total_tokens": 42})

    def test_nullable_tool_function_fragments(self):
        events = [
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": None}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": None, "function": {
                "name": "list_files", "arguments": None}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {
                "name": None, "arguments": "{}"}}]}}]},
            {"choices": [{"delta": None, "finish_reason": "tool_calls"}]},
        ]
        response = io.BytesIO("".join("data: " + json.dumps(e) + "\n\n" for e in events).encode())
        completion = Client(Config(model="test"), "")._stream(response, lambda _: None)
        self.assertEqual(completion.message["tool_calls"], [call("list_files", {})])

    def test_wrong_stream_types_report_field_without_payload(self):
        for field, value in [("tool_calls", {}), ("content", ["private response"]),
                             ("reasoning_content", 123)]:
            with self.subTest(field=field):
                response = io.BytesIO(("data: " + json.dumps({"choices": [{"delta": {field: value}}]}) + "\n\n").encode())
                with self.assertRaisesRegex(ModelError, "delta." + field) as error:
                    Client(Config(model="test"), "")._stream(response, lambda _: None)
                self.assertNotIn("private response", str(error.exception))

    def test_full_message_null_tools_and_malformed_tool_objects(self):
        self.assertEqual(Client._message({"content": "done", "tool_calls": None, "reasoning_content": None}),
                         {"role": "assistant", "content": "done"})
        for raw in ({"content": "x", "tool_calls": {}},
                    {"tool_calls": [{"id": "x", "function": None}]},
                    {"tool_calls": [None]}):
            with self.subTest(raw=raw), self.assertRaises(ModelError):
                Client._validate(Client._message(raw), "tool_calls")

    def test_tool_calls_finish_requires_a_call_in_stream_and_json(self):
        for stream in (True, False):
            with self.subTest(stream=stream):
                if stream:
                    events = [{"choices": [{"delta": {"content": "I will run a command"}}]},
                              {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}]
                    response = io.BytesIO("".join("data: " + json.dumps(event) + "\n\n" for event in events).encode())
                    with self.assertRaisesRegex(ModelError, "tool_calls without tool_calls"):
                        Client(Config(model="test"), "")._stream(response, lambda _: None)
                else:
                    with server(lambda _: json_response(reply("I will run a command", finish="tool_calls"))) as (url, _):
                        with self.assertRaisesRegex(ModelError, "tool_calls without tool_calls"):
                            Client(Config(model="test", base_url=url, stream=False, retries=0), "").complete([])

    def test_textual_minimax_call_with_tool_finish_reaches_agent(self):
        raw = ('<minimax:tool_call><invoke name="read_file">'
               '<parameter name="path">a.py</parameter></invoke></minimax:tool_call>')
        for stream in (True, False):
            with self.subTest(stream=stream):
                if stream:
                    events = [{"choices": [{"delta": {"content": raw}}]},
                              {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}]
                    response = io.BytesIO("".join("data: " + json.dumps(event) + "\n\n" for event in events).encode())
                    message = Client(Config(model="test"), "")._stream(response, lambda _: None).message
                else:
                    with server(lambda _: json_response(reply(raw, finish="tool_calls"))) as (url, _):
                        message = Client(Config(model="test", base_url=url, stream=False), "").complete([]).message
                self.assertEqual(message["content"], raw)

    def test_only_configured_endpoint_no_proxy(self):
        with server(lambda r: json_response(reply("Привет"))) as (url, requests):
            with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1"}):
                result = Client(Config(base_url=url, model="test", retries=0), "secret").complete([{"role": "user", "content": "hi"}])
            self.assertEqual(result.message["content"], "Привет")
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["path"], "/v1/chat/completions")
            self.assertEqual(requests[0]["headers"]["Authorization"], "Bearer secret")

    def test_redirect_never_followed(self):
        with server(lambda r: json_response(reply("should not reach"))) as (other, other_requests):
            with server(lambda r: (307, "text/plain", "redirect", {"Location": other})) as (url, requests):
                with self.assertRaisesRegex(ModelError, "307"):
                    Client(Config(base_url=url, model="test", retries=0), "secret").complete([])
                self.assertEqual(len(requests), 1)
                self.assertFalse(other_requests)

    def test_fragmented_sse_tools_and_reasoning(self):
        events = [
            {"choices": [{"delta": {"reasoning_content": "think"}, "index": 0}]},
            {"choices": [{"delta": {"content": "Сейчас "}, "index": 0}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "x", "type": "function", "function": {"name": "read_", "arguments": '{"pa'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "file", "arguments": 'th":"a"}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 42}},
        ]
        sse = ": heartbeat\r\n\r\n" + "".join("data: " + json.dumps(e, ensure_ascii=False) + "\r\n\r\n" for e in events) + "data: [DONE]\n\n"
        with server(lambda r: (200, "text/event-stream", sse, {})) as (url, _):
            output = []
            result = Client(Config(base_url=url, model="test"), "").complete([], on_text=output.append)
            self.assertEqual(result.message["tool_calls"], [{"id": "x", "type": "function",
                             "function": {"name": "read_file", "arguments": '{"path":"a"}'}}])
            self.assertEqual(result.message["reasoning_content"], "think")
            self.assertEqual("".join(output), "Сейчас ")
            self.assertEqual(result.usage["total_tokens"], 42)

    def test_incomplete_stream_does_not_execute_call(self):
        sse = 'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"x","function":{"name":"exec_command","arguments":"{}"}}]}}]}\n\n'
        with server(lambda r: (200, "text/event-stream", sse, {})) as (url, _):
            client = Client(Config(base_url=url, model="test", retries=0), "")
            client.on_retry = lambda **data: client.cancel()
            with self.assertRaises(Cancelled):
                client.complete([])

    def test_retry_before_output(self):
        with server(lambda r: (429, "text/plain", "busy", {}) if len(r) == 1 else json_response(reply())) as (url, requests):
            with patch("pycoder.transport.time.sleep"):
                Client(Config(base_url=url, model="test", retries=1), "").complete([])
            self.assertEqual(len(requests), 2)

    def test_incomplete_stream_retries_from_same_history_after_visible_text(self):
        for content, expected_requests in [("", 2), ("partial", 2)]:
            with self.subTest(content=content):
                sse = "data: " + json.dumps({"choices": [{"delta": {"content": content}}]}) + "\n\n"
                def responder(requests):
                    return (200, "text/event-stream", sse, {}) if len(requests) == 1 else json_response(reply())
                with server(responder) as (url, requests), patch("pycoder.transport.time.sleep"):
                    client = Client(Config(base_url=url, model="test", retries=1), "")
                    restarts = []
                    client.on_retry = lambda **data: restarts.append(data)
                    self.assertEqual(client.complete([]).message["content"], "done")
                    self.assertEqual(len(restarts), 1)
                    self.assertEqual(restarts[0]["attempt"], 1)
                    self.assertIsNone(restarts[0]["limit"])
                    self.assertEqual(restarts[0]["partial"], bool(content))
                    self.assertEqual(restarts[0]["delay"], 1)
                    self.assertEqual(requests[0]["body"], requests[1]["body"])
                    self.assertEqual(len(requests), expected_requests)

    def test_auth_error_not_retried_or_leaked(self):
        with server(lambda r: (401, "text/plain", "bad secret", {})) as (url, requests):
            with self.assertRaises(ModelError) as captured:
                Client(Config(base_url=url, model="test", retries=3), "secret").complete([])
            self.assertNotIn("secret", str(captured.exception))
            self.assertEqual(len(requests), 1)

    def test_partial_timeout_retry_is_unlimited_and_cancellable(self):
        from pycoder.events import Cancelled
        client = Client(Config(base_url="https://example.com/v1", model="test", retries=2), "")
        def timeout(payload, emit):
            emit("partial")
            raise ModelError("Connection failed: TimeoutError; phase=SSE stream", retryable=True)
        client.on_retry = lambda **data: client.cancel() if data["attempt"] == 9 else None
        with patch.object(client, "_request", side_effect=timeout) as request, patch.object(client, "_delay") as delay:
            with self.assertRaises(Cancelled):
                client.complete([])
            self.assertEqual(request.call_count, 9)
            self.assertEqual([c.args[0] for c in delay.call_args_list], [1, 2, 4, 8, 10, 10, 10, 10, 10])

    def test_capacity_details_and_unlimited_backoff_without_status_spam(self):
        def responder(requests):
            if len(requests) <= 7:
                return 429, "application/json", {"detail": "Instance is at maximum capacity, try again later secret"}, {}
            return json_response(reply("Recovered"))
        with server(responder) as (url, requests):
            statuses, retries = [], []
            client = Client(Config(base_url=url, model="test-model", retries=0), "secret", statuses.append)
            client.on_retry = lambda **data: retries.append(data)
            with patch.object(client, "_delay") as delay:
                self.assertEqual(client.complete([]).message["content"], "Recovered")
            self.assertEqual([c.args[0] for c in delay.call_args_list], [1, 2, 4, 8, 10, 10, 10])
            self.assertEqual([r["attempt"] for r in retries], list(range(1, 8)))
            self.assertEqual(statuses, [])
            self.assertIn("перегружен", retries[0]["error"])
            self.assertIn("HTTP 429", retries[0]["error"])
            self.assertIn("127.0.0.1", retries[0]["error"])
            self.assertIn("test-model", retries[0]["error"])
            self.assertNotIn("secret", retries[0]["error"])
            self.assertEqual(len(requests), 8)

    def test_context_overflow(self):
        with server(lambda r: (400, "application/json", {"error": "maximum context length exceeded"}, {})) as (url, _):
            with self.assertRaises(ModelError) as captured:
                Client(Config(base_url=url, model="test"), "").complete([])
            self.assertTrue(captured.exception.context_overflow)

    def test_incomplete_function_arguments_rejected(self):
        with server(lambda r: json_response(reply(None, [call("apply_patch", {})], finish="length"))) as (url, _):
            with self.assertRaisesRegex(ModelError, "truncated"):
                Client(Config(base_url=url, model="test"), "").complete([])

    def test_url_forms(self):
        self.assertEqual(endpoint("http://localhost:8000"), "http://localhost:8000/v1/chat/completions")
        self.assertEqual(endpoint("https://example.org/custom/v1/"), "https://example.org/custom/v1/chat/completions")
        for url in ("file:///tmp/foo", "https://user:secret@example.org", "https://a/v1?token=x"):
            with self.assertRaises(ValueError):
                endpoint(url)
