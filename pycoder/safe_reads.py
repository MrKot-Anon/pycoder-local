"""Conservative POSIX read-command recognition, not a general shell sandbox.

Only a small grammar and known system executables qualify. Reconstruct commands
from quoted argv so shell syntax can never slip through the classifier.
"""

import os
from pathlib import Path
import re
import shlex


def _segments(command):
    if any(c in command for c in "\n\r\x00$`<>(){}*?[]~"):
        raise ValueError("Shell expansion or unsupported syntax")
    quote, escaped, start, result = None, False, 0, []
    i = 0
    while i < len(command):
        c = command[i]
        if escaped:
            escaped = False
        elif c == "\\" and quote != "'":
            escaped = True
        elif quote:
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c in ";&|":
            separator = "&&" if command[i:i + 2] == "&&" else c
            if separator not in {";", "&&"}:
                raise ValueError("Unsupported operator")
            result.append((shlex.split(command[start:i]), separator))
            i += len(separator) - 1
            start = i + 1
        i += 1
    if quote or escaped:
        raise ValueError("Unclosed quote")
    result.append((shlex.split(command[start:]), ""))
    if not all(args for args, _ in result):
        raise ValueError("Empty command")
    return result


def _checked_argv(args, root, cwd):
    name = Path(args[0]).name
    if name not in {"head", "tail", "cat", "wc", "ls", "grep", "sed", "echo"}:
        raise ValueError("Unknown reader")
    candidates = [Path("/usr/bin") / name, Path("/bin") / name]
    executable = next((p for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)
    if executable is None or (args[0] != name and Path(args[0]) not in candidates):
        raise ValueError("Not a system executable")
    operands = args[1:]
    if name == "echo":
        if any(a.startswith("-") or "\\" in a for a in operands):
            raise ValueError("Only literal echo is supported")
        return [str(executable), *operands]
    flags, files = [], []
    if name == "sed":
        if len(operands) < 3 or operands[0] != "-n" or not re.fullmatch(r"[1-9]\d*(?:,[1-9]\d*)?p", operands[1]):
            raise ValueError("Only sed -n N,Mp FILE is supported")
        flags, operands = operands[:2], operands[2:]
    pattern = None
    options = True
    i = 0
    allowed = {"head": "qv", "tail": "qv", "cat": "nbEsTv", "wc": "lwmcL",
               "ls": "lahdF1trSUnisgp", "grep": "nivEFwxclLHhsqa", "sed": ""}
    while i < len(operands):
        arg = operands[i]
        if options and arg == "--":
            options = False
        elif options and arg.startswith("-"):
            if name in {"head", "tail"} and re.fullmatch(r"-\d+", arg):
                flags.append(arg)
            elif name in {"head", "tail"} and arg in {"-n", "-c"}:
                i += 1
                if i >= len(operands) or not re.fullmatch(r"[+-]?\d+", operands[i]):
                    raise ValueError("Invalid count")
                flags.extend([arg, operands[i]])
            elif len(arg) > 1 and not arg.startswith("--") and all(c in allowed[name] for c in arg[1:]):
                flags.append(arg)
            else:
                raise ValueError("Unsupported option")
        elif name == "grep" and pattern is None:
            pattern = arg
        else:
            files.append(arg)
        i += 1
    if name == "grep":
        if pattern is None:
            raise ValueError("Missing pattern")
        flags.extend(["-e", pattern])
    if not files and name == "ls":
        files = ["."]
    if not files:
        raise ValueError("Explicit files required")
    checked = []
    for file in files:
        if file == "-":
            raise ValueError("stdin is not an explicit file")
        path = (cwd / file).resolve(strict=True)
        if not path.is_relative_to(root) or not (path.is_file() or name == "ls" and path.is_dir()):
            raise ValueError("Not a regular workspace file")
        checked.append(str(path))
    return [str(executable), *flags, *checked]


def trusted_read(command, root, cwd, shell, argv=None):
    """Return a normalized command/argv, or None to use normal approval policy."""
    if os.name != "posix" or (argv is None and shell != "/bin/sh"):
        return None
    try:
        root, cwd = root.resolve(), cwd.resolve()
        if not cwd.is_relative_to(root):
            return None
        if argv is not None:
            return _checked_argv(argv, root, cwd)
        return " ".join(shlex.join(_checked_argv(args, root, cwd)) + (" " + separator if separator else "")
                        for args, separator in _segments(command))
    except (ValueError, OSError, RuntimeError):
        return None
