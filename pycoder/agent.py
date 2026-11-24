"""Coding turn loop and client-side compaction adapted from Codex (Apache-2.0).

Source: core/src/session/turn.rs and core/src/compact.rs, revision 18344a972d.
Modified: Chat Completions, sequential tools, model-independent budget estimates,
adaptive retained-user budget, explicit no-progress and step limits.
"""

import json
import re
import threading
import uuid

from .history import groups, repair_pending
from .prompts import COMPACT, SUMMARY_PREFIX, SYSTEM
from .tools import TOOL_SPECS
from .transport import ModelError, OutputLimit, ProtocolError
from .util import tokens, truncate
from .events import Cancelled
from .plans import has_open_steps, FINAL_PLAN_CHECK
from .scheduler import execute_batches
from .platform_support import environment_description

FINAL_MARKER = "[[PYCODER_DONE]]"
INVOKE_START = re.compile(r"<invoke\s+name\s*=\s*(['\"])([^'\"]+)\1\s*>")
PARAMETER_START = re.compile(r"<parameter\s+name\s*=\s*(['\"])([^'\"]+)\1\s*>")


def recover_text_tool_call(message):
    if message.get("tool_calls"):
        return message
    content = message.get("content") or ""
    if content.rstrip().endswith(FINAL_MARKER):
        return message
    closing = "</minimax:tool_call>"
    opener = "<minimax:tool_call>"
    invokes = list(INVOKE_START.finditer(content))
    if not invokes and opener not in content and closing not in content:
        return message
    start = content.rfind(opener)
    if start >= 0:
        prefix = content[:start].rstrip()
        start += len(opener)
    else:
        start = invokes[0].start() if invokes else -1
        if start >= 0 and content[:start] and not content[:start].endswith("\n"):
            return message
        if start >= 0 and content[:start].count("```") % 2:
            return message
        prefix = content[:start].rstrip() if start >= 0 else ""
    if start < 0:
        raise ProtocolError("Model emitted a Minimax tool-call marker without an invoke block")
    block = content[start:].strip()
    if block.endswith(closing):
        block = block[:-len(closing)].rstrip()
    if not block.endswith("</invoke>"):
        raise ProtocolError("Model emitted an incomplete textual Minimax tool call")
    calls = []
    position = 0
    while position < len(block):
        invoke = INVOKE_START.match(block, position)
        if invoke is None:
            raise ProtocolError("Model emitted a malformed textual Minimax tool call")
        name = invoke.group(2)
        position = invoke.end()
        spec = next((item["function"] for item in TOOL_SPECS if item["function"]["name"] == name), None)
        if spec is None:
            raise ProtocolError(f"Model emitted an unknown textual Minimax tool call: {name}")
        arguments = {}
        while True:
            while position < len(block) and block[position].isspace():
                position += 1
            if block.startswith("</invoke>", position):
                position += len("</invoke>")
                break
            parameter = PARAMETER_START.match(block, position)
            if parameter is None:
                raise ProtocolError("Model emitted a malformed textual Minimax tool call")
            key = parameter.group(2)
            position = parameter.end()
            end = block.find("</parameter>", position)
            if end < 0:
                end = block.find("</invoke>", position)
                if end < 0:
                    raise ProtocolError("Model emitted invalid textual Minimax tool parameters")
                next_position = end
            else:
                next_position = end + len("</parameter>")
            if key in arguments or key not in spec["parameters"]["properties"]:
                raise ProtocolError("Model emitted invalid textual Minimax tool parameters")
            value = block[position:end]
            position = next_position
            if spec["parameters"]["properties"][key]["type"] != "string":
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    raise ProtocolError("Model emitted invalid JSON in a textual Minimax tool parameter") from None
            arguments[key] = value
        if set(spec["parameters"]["required"]) - set(arguments):
            raise ProtocolError("Model omitted a required textual Minimax tool parameter")
        calls.append({"id": "text_" + uuid.uuid4().hex, "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}})
        while position < len(block) and block[position].isspace():
            position += 1
    if not calls:
        raise ProtocolError("Model emitted a Minimax tool-call marker without an invoke block")
    return {**message, "content": prefix or None, "tool_calls": calls}


class Agent:
    def __init__(self, config, client, history, tools, emit, on_text, on_event=None, cancelled=None):
        self.config, self.client, self.history, self.tools = config, client, history, tools
        self.emit, self.on_text = emit, on_text
        self.event = on_event or (lambda kind, **data: None)
        self.cancelled = cancelled or threading.Event()
        tools.cancelled = self.cancelled
        tools.processes.cancelled = self.cancelled
        instructions = tools.instructions(tools.root)
        self.system = SYSTEM + f"\nWorkspace: {tools.root}\n" + environment_description() + "\n" + instructions
        self.on_output_limit = lambda error: None
        self.on_checkpoint = lambda: None
        self.incoming = lambda: []
        self.before_finish = lambda: []
        self.specs = TOOL_SPECS
        self.observed_tokens = 0
        self.observed_estimate = 0
        self.tools.context_remaining = lambda: max(0, self.threshold() - self.estimate())

    def threshold(self):
        cfg = self.config
        return min(cfg.context_window * cfg.auto_compact_percent // 100,
                   cfg.context_window * 95 // 100 - min(cfg.max_output_tokens, cfg.context_window // 4))

    def estimate(self):
        local = tokens(self.effective_system()) + tokens(self.specs) + self.history.estimate() + 64
        # Account for provider usage plus the tool results added since its last reply.
        observed = self.observed_tokens + max(0, local - self.observed_estimate)
        return max(local, observed)

    def messages(self):
        return [{"role": "system", "content": self.effective_system()}] + self.history.messages

    def effective_system(self, *, finalization=True):
        from .sandbox import supported
        cfg = self.tools.sandbox_config()
        sandboxed = cfg.sandbox_enabled and supported()
        state = {"enabled": sandboxed, "network": cfg.sandbox_network if sandboxed else "unrestricted",
                 "full_access": cfg.full_access, "approval": cfg.approval}
        if sandboxed:
            state.update(hosts=cfg.sandbox_hosts, direct_hosts=cfg.sandbox_direct_hosts,
                         proxy_enabled=cfg.proxy_enabled, allow_localhost=cfg.sandbox_localhost,
                         allow_lan=cfg.sandbox_lan)
        prompt = self.system + "\nCurrent shell sandbox settings (for new processes): " + json.dumps(state, ensure_ascii=False)
        if finalization and self.config.require_explicit_finish:
            prompt += ("\nWhen the user's task is actually complete, end your final answer with " + FINAL_MARKER + ". "
                       "Do not use this marker in progress messages or while any requested work remains. "
                       "For tool use, issue a real tool call. If your provider emits MiniMax XML, include the complete "
                       "<minimax:tool_call>...</minimax:tool_call> block.")
        if cfg.full_access:
            prompt += ("\nThe user explicitly enabled FULL ACCESS. This overrides ordinary command/escalation/network approval instructions above. "
                       "Commands run without an OS sandbox or approval prompts, with the user's OS permissions, including outside the workspace. "
                       "Shell network access is unrestricted by PyCoder, including localhost and LAN; the model proxy setting affects model requests only. "
                       "Do not ask for command or network approval; make routine implementation decisions autonomously. "
                       "read_file, list_files, search and view_image can access paths outside the workspace. "
                       "For changes outside the workspace use shell commands; apply_patch remains workspace-scoped. "
                       "Stay within the user's task and never claim permissions beyond the OS account.")
        return prompt

    def compact(self):
        self.check_cancelled()
        if not self.history.messages:
            return
        self.emit("Сжатие контекста той же моделью…")
        self.event("compacting")
        before = self.estimate()
        source = groups(self.history.messages)
        summary_budget = min(self.config.max_output_tokens, max(512, self.threshold() // 4))
        request_limit = self.config.context_window * 95 // 100 - summary_budget
        base = [{"role": "system", "content": self.effective_system(finalization=False)}]
        suffix = [{"role": "user", "content": COMPACT}]
        # Trim only a working copy, never the durable original history on failure.
        while True:
            material = [m for group in source for m in group]
            request = base + material + suffix
            if tokens(request) > request_limit and len(source) > 1:
                source.pop(0)
                continue
            if tokens(request) > request_limit:
                raise ModelError("One history item exceeds compaction capacity; increase context_window")
            try:
                completion = self.client.complete(request, max_tokens=summary_budget)
            except OutputLimit as error:
                error.operation = "compaction"
                self.event("model_waiting")
                choice = self.on_output_limit(error)
                self.check_cancelled()
                if not choice or choice[0] != "increase":
                    raise
                value = choice[1]
                if type(value) is not int or not error.limit < value <= error.capacity:
                    raise ValueError("Invalid compaction output limit")
                summary_budget = value
                request_limit = self.config.context_window * 95 // 100 - summary_budget
                self.event("compacting")
                continue
            except ModelError as error:
                if error.context_overflow and len(source) > 1:
                    source.pop(0)
                    continue
                raise
            break
        self.check_cancelled()
        summary = completion.message.get("content") or ""
        if not summary.strip() or completion.message.get("tool_calls") or completion.finish_reason != "stop":
            raise ModelError("Compaction did not produce a complete summary; original history preserved")
        # Codex retains recent real user messages up to 20K tokens. Scale this cap
        # down for smaller local-model contexts, with room for tools and the summary.
        available = self.threshold() - tokens(self.system) - tokens(self.specs) - tokens(summary) - tokens(SUMMARY_PREFIX) - 512
        if available < 256:
            raise ModelError("Context too small for instructions, tools and summary; increase context_window")
        remaining = min(20000, available, max(256, self.threshold() // 3))
        selected = []
        for message in reversed(self.history.messages):
            text = message.get("content") or ""
            if not isinstance(text, str):
                # Images may be summarized, but never copied as base64 into a text checkpoint.
                text = "\n".join(item.get("text", "[local image]") for item in text if isinstance(item, dict))
            if message["role"] != "user" or text.startswith(SUMMARY_PREFIX):
                continue
            if remaining <= 0:
                break
            selected.append({"role": "user", "content": truncate(text, remaining)})
            remaining -= tokens(text) + 16
        replacement = list(reversed(selected)) + [{"role": "user", "content": SUMMARY_PREFIX + "\n" + summary}]
        if self.history.plan:
            replacement[-1]["content"] += "\nCurrent plan:\n" + json.dumps(self.history.plan, ensure_ascii=False)
        # Active process IDs are local state, not something the summary should guess.
        if self.tools.processes.sessions:
            replacement[-1]["content"] += "\nRunning process session IDs: " + str(list(self.tools.processes.sessions))
        after = tokens(self.system) + tokens(self.specs) + tokens(replacement) + 64
        if after >= self.threshold() or after >= before:
            raise ModelError("Compaction failed to free context; original history preserved")
        self.history.replace(replacement, "model summary")
        self.observed_tokens = self.observed_estimate = 0
        # Instruction contents embedded in old tool results may now be summarized.
        self.tools.instructions_seen.clear()
        self.emit(f"Контекст: примерно {before} → {after} токенов. Полный журнал сохранён.")
        self.event("context", used=after, window=self.config.context_window)

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise Cancelled("Операция прервана")
        self.on_checkpoint()

    def receive(self, messages):
        for message in messages:
            self.history.append({"role": "user", "content": message})
        return bool(messages)

    def execute_call(self, call):
        if self.cancelled.is_set():
            raise Cancelled()
        function = call["function"]
        display_id = uuid.uuid4().hex
        self.event("tool_start", id=display_id, name=function["name"], arguments=function["arguments"])
        self.tools.current_call_id = display_id
        try:
            output = self.tools.execute(function["name"], function["arguments"])
        finally:
            self.tools.current_call_id = None
        self.event("tool_end", id=display_id, name=function["name"], output=output)
        return output

    def run(self, text=None):
        if text:
            self.history.append({"role": "user", "content": text})
        if not self.history.messages:
            raise ValueError("No task to continue")
        repeated, previous = 0, None
        failed_repeats, previous_failure = 0, None
        final_plan_checked = False
        overflow_recovered = False
        protocol_failures = 0
        unfinished_replies = 0
        try:
            for step in range(self.config.max_steps):
                self.check_cancelled()
                self.receive(self.incoming())
                if self.estimate() >= self.threshold():
                    self.compact()
                self.receive(self.incoming())
                try:
                    self.event("model_start", step=step + 1)
                    result = self.client.complete(self.messages(), self.specs, self.on_text)
                except OutputLimit as error:
                    self.event("model_waiting")
                    choice = self.on_output_limit(error)
                    self.check_cancelled()
                    if not choice:
                        raise
                    action, value = choice
                    if action == "increase":
                        if type(value) is not int or value <= error.limit or value > error.capacity:
                            raise ValueError("New output limit must exceed the previous request limit and fit remaining context")
                        self.config.max_output_tokens = value
                    elif action == "fragment":
                        partial = error.message
                        if not partial.get("tool_calls") and partial.get("content"):
                            self.history.append({"role": "assistant", "content": partial["content"]})
                        self.history.append({"role": "user", "content":
                            "The previous response was truncated by the output limit. No tools from that response were executed. "
                            "Keep the current output limit. Break the remaining task into smaller steps and execute them in separate replies. "
                            "Use small, complete patches (one coherent section at a time), and concise progress text. "
                            "Do not repeat actions already completed in the tool history. Continue any partial prose without repeating it."})
                    else:
                        raise
                    self.event("model_restart")
                    continue
                except ProtocolError as error:
                    protocol_failures += 1
                    if protocol_failures >= 3:
                        raise ModelError(f"Model response violated the tool protocol three times: {error}. Session saved; use /continue.") from error
                    self.history.append({"role": "user", "content":
                        "Your previous response had invalid tool-call syntax. No tool from that response was executed. "
                        f"Error: {error}. Reissue the intended action as a complete, valid tool call. "
                        "For MiniMax XML include both opening and closing minimax:tool_call tags. "
                        "Do not repeat actions already recorded as completed in tool results."})
                    self.event("model_restart")
                    continue
                except ModelError as error:
                    if error.context_overflow and not overflow_recovered:
                        self.compact()
                        overflow_recovered = True
                        continue
                    raise
                overflow_recovered = False
                self.check_cancelled()
                self.event("model_end")
                try:
                    message = recover_text_tool_call(result.message)
                except ProtocolError as error:
                    protocol_failures += 1
                    if protocol_failures >= 3:
                        raise ModelError(f"Model response violated the tool protocol three times: {error}. Session saved; use /continue.") from error
                    self.history.append(result.message)
                    self.history.append({"role": "user", "content":
                        "Your previous response had invalid tool-call syntax. No tool from that response was executed. "
                        f"Error: {error}. Reissue the intended action as a complete, valid tool call. "
                        "For MiniMax XML include both opening and closing minimax:tool_call tags. "
                        "Do not repeat actions already recorded as completed in tool results."})
                    self.event("model_restart")
                    continue
                protocol_failures = 0
                self.history.append(message)
                self.observed_estimate = tokens(self.system) + tokens(self.specs) + self.history.estimate() + 64
                self.observed_tokens = int(result.usage.get("total_tokens") or (
                    (result.usage.get("prompt_tokens") or 0) + (result.usage.get("completion_tokens") or 0)))
                calls = message.get("tool_calls", [])
                self.event("context", used=self.estimate(), window=self.config.context_window)
                if not calls:
                    if self.receive(self.before_finish()):
                        continue
                    content = message.get("content") or ""
                    if self.config.require_explicit_finish and not content.rstrip().endswith(FINAL_MARKER):
                        unfinished_replies += 1
                        if unfinished_replies >= 3:
                            raise ModelError("Model stopped three times without an explicit final marker. Session saved; use /continue.")
                        self.history.append({"role": "user", "content":
                            "Your previous message did not explicitly finish the task. Continue the pending work with tool calls. "
                            "If the task is truly complete, give the final answer and end it with " + FINAL_MARKER + ". "
                            "Do not treat a running command or a promise to wait as completion."})
                        self.event("model_restart")
                        continue
                    if has_open_steps(self.history.plan):
                        if not final_plan_checked:
                            final_plan_checked = True
                            self.emit("В плане остались открытые пункты. Проверяю их статус перед завершением…")
                            self.history.append({"role": "user", "content": FINAL_PLAN_CHECK +
                                                 json.dumps(self.history.plan, ensure_ascii=False)})
                            continue
                        # One reconciliation request per user turn. A blocked task
                        # must remain visibly unfinished, not loop until fake success.
                        self.event("plan_unfinished")
                        self.emit("В плане остались незавершённые пункты; автоматические отметки не выставлены.")
                        if self.config.require_explicit_finish:
                            raise ModelError("Model claimed completion while plan steps remain open. Session saved; use /continue.")
                    return content.rstrip().removesuffix(FINAL_MARKER).rstrip() if self.config.require_explicit_finish else content
                unfinished_replies = 0
                outcomes = []
                for call, output in execute_batches(calls, self.execute_call, self.config.parallel_tools):
                    self.history.append({"role": "tool", "tool_call_id": call["id"], "content": output})
                    outcomes.append((call["function"], output))
                if self.tools.pending_images:
                    self.history.append({"role": "user", "content": self.tools.pending_images[:]})
                    self.tools.pending_images.clear()
                if self.tools.compaction_requested:
                    self.tools.compaction_requested = False
                    self.compact()
                signature = json.dumps(outcomes, sort_keys=True)
                failures = []
                for function, output in outcomes:
                    try:
                        value = json.loads(output)
                    except (ValueError, TypeError):
                        break
                    if not isinstance(value, dict) or not isinstance(value.get("error"), str):
                        break
                    failures.append((function["name"], value["error"]))
                failure = sorted(set(failures)) if failures and len(failures) == len(outcomes) else None
                failed_repeats = failed_repeats + 1 if failure and failure == previous_failure else (1 if failure else 0)
                previous_failure = failure
                if failed_repeats >= 3:
                    raise ModelError("Инструменты вернули одну и ту же ошибку три раза подряд, несмотря на изменения аргументов. "
                                     "Цикл остановлен; история сохранена. Исправьте причину и используйте /continue.")
                if failed_repeats == 2:
                    self.history.append({"role": "user", "content":
                        "The same tool error occurred twice. Read the error and tool schema before retrying. "
                        "Do not repeat the failed operation with cosmetic argument changes. For patch errors, "
                        "read the relevant file and prepare a small valid Update File patch. "
                        "A third consecutive identical tool error will stop this turn."})
                repeated = repeated + 1 if signature == previous else 0
                previous = signature
                if repeated >= 2:
                    raise ModelError("Agent repeated identical actions/results three times; stopped. Clarify the task or use /continue.")
            raise ModelError(f"Reached {self.config.max_steps} model steps; session saved. Use /continue to proceed.")
        except BaseException:
            self.tools.pending_images.clear()
            repaired = repair_pending(self.history.messages)
            if repaired != self.history.messages:
                self.history.replace(repaired, "interrupted turn")
            raise
