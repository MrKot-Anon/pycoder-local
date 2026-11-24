"""Live local-model coding test in a disposable workspace; validates actual tool results."""

import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pycoder.cli import main as run_agent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True,
                        help="Send synthetic prompts and run commands in a temporary project")
    parser.add_argument("--address", default="127.0.0.1:8080")
    parser.add_argument("--model")
    parser.add_argument("--no-stream", action="store_true")
    args = parser.parse_args()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="pycoder-local-probe-") as directory:
        root = Path(directory)
        (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        command = "python3 -c \"from calc import add; assert add(2, 3) == 5; print('MINIMAX_LIVE_PASS')\""
        prompt = ("Read calc.py using read_file. Fix add using apply_patch so it adds a and b. "
                  "Then run exec_command with this exact command: " + command + ". "
                  "If it returns session_id, poll it with write_stdin until it exits. "
                  "Do not change anything else, create a plan or delegate. Report the result briefly.")
        argv = ["--local", args.address, "--plain", "--new", "--max-steps", "8",
                "--max-output-tokens", "4096", "--config-dir", str(root / "config"),
                "--state-dir", str(root / "state"), "-C", str(root)]
        if args.model:
            argv += ["--model", args.model]
        if args.no_stream:
            argv += ["--no-stream"]
        code = run_agent(argv + [prompt])
        calls, results = {}, {}
        for path in (root / "state" / "sessions").glob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if record["type"] != "message":
                    continue
                message = record["data"]
                for call in message.get("tool_calls", []):
                    calls[call["id"]] = call["function"]["name"]
                if message["role"] == "tool":
                    results[message["tool_call_id"]] = json.loads(message["content"])
        used = set(calls.values())
        command_passed = any(calls.get(key) in {"exec_command", "write_stdin"}
                             and value.get("exit_code") == 0
                             and "MINIMAX_LIVE_PASS" in value.get("output", "")
                             for key, value in results.items())
        patch_passed = any(calls.get(key) == "apply_patch" and "calc.py" in value.get("changed", [])
                           for key, value in results.items())
        passed = (code == 0 and {"read_file", "apply_patch", "exec_command"} <= used
                  and command_passed and patch_passed)
        print(json.dumps({"passed": passed, "agent_exit_code": code, "tools": sorted(used),
                          "command_passed": command_passed, "stream": not args.no_stream,
                          "seconds": round(time.monotonic() - started, 2)}, ensure_ascii=False))
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
