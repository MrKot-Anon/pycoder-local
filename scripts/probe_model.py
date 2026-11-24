"""Explicit live diagnostics using synthetic input; never prints keys or response text."""

import argparse
from dataclasses import replace
import http.client
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pycoder.config import config_dir, get_token, load
from pycoder.transport import Client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--mode", choices=["text", "tool"], default="text")
    parser.add_argument("--tokens", type=int, default=8192)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--generation-timeout", type=int, default=600)
    parser.add_argument("--functions", type=int, default=80)
    args = parser.parse_args()
    config = replace(load(config_dir()), retries=0, timeout_seconds=args.timeout, generation_timeout_seconds=args.generation_timeout, max_output_tokens=args.tokens)
    key = get_token(config, config_dir())
    start = time.monotonic()
    def report(kind, **data):
        print(json.dumps({"seconds": round(time.monotonic() - start, 3), "event": kind, **data}), flush=True)
    original_connect = http.client.HTTPSConnection.connect
    original_response = http.client.HTTPSConnection.getresponse
    def connect(connection):
        report("connect_start")
        original_connect(connection)
        report("connected")
    def getresponse(connection):
        report("waiting_headers")
        response = original_response(connection)
        report("headers", status=response.status, content_type=response.getheader("Content-Type"), transfer=response.getheader("Transfer-Encoding"))
        return response
    http.client.HTTPSConnection.connect = connect
    http.client.HTTPSConnection.getresponse = getresponse
    class ProbeClient(Client):
        def _stream(self, response, on_text):
            original_read = response.readline
            counts = {"lines": 0, "bytes": 0, "content": 0, "reasoning": 0, "tool_arguments": 0}
            last, emitted = time.monotonic(), 0
            def read(*a, **kw):
                nonlocal last, emitted
                raw = original_read(*a, **kw)
                now = time.monotonic()
                gap = now - last
                last = now
                counts["lines"] += 1
                counts["bytes"] += len(raw)
                if raw.startswith(b"data:"):
                    try:
                        event = json.loads(raw[5:])
                    except ValueError:
                        event = {}
                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        counts["content"] += len(delta.get("content") or "")
                        counts["reasoning"] += len(delta.get("reasoning_content") or "")
                        for call in delta.get("tool_calls") or []:
                            counts["tool_arguments"] += len((call.get("function") or {}).get("arguments") or "")
                if counts["lines"] == 1 or now - emitted > 15 or gap > 5:
                    report("stream", gap=round(gap, 3), **counts)
                    emitted = now
                return raw
            response.readline = read
            try:
                return super()._stream(response, on_text)
            finally:
                report("stream_end", **counts)
    client = ProbeClient(config, key, lambda text: report("status", detail=text.replace(key, "[REDACTED]") if key else text))
    tools = None
    prompt = f"Generate {args.functions} numbered Python functions, increment_0001 through increment_{args.functions:04d}. Each has a docstring, validates an integer input, returns x plus its function number. Output all functions directly, no analysis, no summaries or placeholders. This is a synthetic long-output transport test."
    if args.mode == "tool":
        tools = [{"type": "function", "function": {"name": "apply_patch", "description": "Return the requested file patch. It is not executed in this diagnostic.", "parameters": {"type": "object", "properties": {"patch": {"type": "string"}}, "required": ["patch"]}}}]
        prompt += " Send a short progress sentence, then call apply_patch to add generated.py using *** Begin Patch / *** Add File format."
    report("start", model=config.model, mode=args.mode, output_limit=args.tokens, timeout=args.timeout, generation_timeout=args.generation_timeout)
    try:
        result = client.complete([{"role": "user", "content": prompt}], tools)
        for call in result.message.get("tool_calls", []):
            arguments = call["function"]["arguments"]
            parsed = json.loads(arguments)
            patch = parsed.get("patch", "")
            report("tool_validated", name=call["function"]["name"], arguments_chars=len(arguments),
                   patch_complete=patch.startswith("*** Begin Patch") and patch.rstrip().endswith("*** End Patch"))
        report("complete", finish=result.finish_reason, usage=result.usage,
               content_chars=len(result.message.get("content") or ""), tools=len(result.message.get("tool_calls", [])))
        return 0
    except Exception as error:
        report("failure", type=type(error).__name__, detail=str(error).replace(key, "[REDACTED]") if key else str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
