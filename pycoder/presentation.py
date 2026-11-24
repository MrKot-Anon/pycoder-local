"""Pure presentation helpers: bounded tool labels and prose-only streaming."""

import json
import re
import shlex
from .platform_support import display_argv


def shorten(text, limit=88):
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[:limit - 22] + " … " + text[-19:]


def tool_label(name, arguments):
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(args, dict):
            return name
        if name == "shell":
            detail = display_argv(args.get("command", []))
        elif name == "apply_patch":
            paths = re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", args.get("patch", ""), re.M)
            detail = ", ".join(paths)
        elif name == "search":
            detail = f"{args.get('pattern', '')} · {args.get('path', '.')}"
        elif name == "write_stdin":
            detail = f"процесс {args.get('session_id', '?')}"
        elif name == "spawn_agent":
            detail = args.get("task_name", "")
        elif name in {"send_message", "followup_task", "interrupt_agent", "get_agent_result", "get_agent_log"}:
            detail = args.get("target", "")
        else:
            detail = args.get("cmd") or args.get("path") or ""
        return shorten(f"{name} · {detail}" if detail else name)
    except (ValueError, TypeError):
        return name


def user_prose(text, *, final=False):
    """Hide fenced/indented code, tool-call dumps and explicit reasoning tags.

    Keep prose streaming, withholding only incomplete fence/tag prefixes. This
    is a UI filter; full content remains in the model history and copy action.
    """
    text = re.sub(r"<(think|analysis)\b[^>]*>.*?(?:</\1>|$)", "", text, flags=re.S | re.I)
    # Withhold a partial opening tag split between SSE chunks.
    if not final:
        text = re.sub(r"<(?:t(?:h(?:i(?:n(?:k)?)?)?)?|a(?:n(?:a(?:l(?:y(?:s(?:i(?:s)?)?)?)?)?)?)?)?$", "", text, flags=re.I)
    result = []
    fence = None
    for line in text.splitlines(keepends=True):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence:
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not match[2].strip():
                fence = None
            continue
        if match:
            fence = match[1]
            continue
        if line.startswith(("    ", "\t")):
            continue
        if not final and not line.endswith(("\n", "\r")) and re.fullmatch(r" {0,3}[`~]{1,2}", line):
            continue
        if line.startswith(("*** Begin Patch", "*** Add File:", "*** Update File:")):
            break
        result.append(line)
    return "".join(result).strip()
