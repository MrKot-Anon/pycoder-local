# Instruction provenance

Source: OpenAI Codex, Apache-2.0, local revision 18344a972d.

- `system.md`: full `codex-rs/models-manager/prompt.md` baseline, adapted (not a summary). Retains personality, AGENTS.md scope, preambles, planning with examples, task execution, validation, ambition/precision, progress updates, final answer rules, and tool guidelines.
- `apply_patch.lark`: exact copy of `codex-rs/core/assets/tools/apply_patch.lark`.
- `apply_patch.md`: explanatory guide derived from that grammar and `apply-patch/src/parser.rs`, with runnable examples and Python implementation constraints. This guide is newly written, not an upstream verbatim file.
- `compact.md`, `summary_prefix.md`: exact copies from `codex-rs/prompts/templates/compact/`.
- Tool schema descriptions in `tools.py`: `codex-rs/core/src/tools/handlers/{shell_spec,plan_spec,request_user_input_spec,view_image_spec,get_context_remaining_spec,new_context_window_spec}.rs`, adapted to supported parameters and return values. Local read_file/list_files/search/list_processes descriptions document the Python implementation.

Intentional runtime adaptations:
- PyCoder identity and configured model instead of OpenAI/Codex identity; public progress instead of exposed private thinking.
- apply_patch uses a JSON `patch` string, not Responses FREEFORM or an external shell command. Its grammar is supplied as documentation; the provider is not assumed to enforce Lark during generation.
- AGENTS.md is supplied by the Python tools; macOS/Linux commands use a local OS sandbox and explicit user-approved escalation. Windows keeps exact-command approvals. Host allowlisting uses a local HTTP CONNECT proxy; no remote OpenAI services are introduced.
- Required testing proceeds through existing permissions, not an additional conversational approval.
- Nested plan children are retained. Plan updates are required at each step transition (replacing upstream permission to batch independent completions); the runtime performs one bounded final reconciliation request for open plans. Questions work in ordinary mode; free text is supplied by the UI.
- Local process timing defaults, image limits, context estimation and deferred compaction match the Python code. Unsupported tools, parameters and modes are not advertised.
- Markdown list spacing and file-reference claims match the Textual frontend.

No upstream checkout or SDK is needed at runtime. Linux sandboxing requires system Bubblewrap; macOS uses sandbox-exec. `seatbelt*.sbpl` comes from codex-rs/sandboxing/src. These resources are included in the standalone zipapp and Python package. Existing sessions get the new system instructions on restart.
