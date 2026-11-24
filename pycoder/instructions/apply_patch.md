The apply_patch tool edits files using the Codex patch format: a simple, line-oriented format designed to be easy to parse and safe to apply.

Transport adaptation: this is a JSON function tool, not Codex's FREEFORM tool. Call it with {"patch": "*** Begin Patch\n...\n*** End Patch"}. The decoded patch value is plain patch text, without Markdown fences or a second JSON encoding. Use actual newlines in that string (JSON encodes them as \n). Do not pass a command array or a standard unified diff.

Begin with *** Begin Patch and finish with *** End Patch. Between these markers provide one or more file operations:

*** Add File: path — create a new file. EVERY content line MUST start with +, including the first line, docstrings, comments, and blank lines. A blank added line is a line containing just +. Do not use Add File to overwrite an existing file.
*** Delete File: path — remove an existing file; no content lines follow.
*** Update File: path — patch an existing file in place. Optionally follow immediately with *** Move to: new/path to rename it.

For updates, prefix unchanged context with one space, removed lines with -, and added lines with +. Do not include displayed read_file line numbers. Use surrounding unchanged lines to identify the exact location (typically three lines before and after). If needed, @@ followed by an actual class/function/context line advances to that line; it is not a unified-diff line-number header such as @@ -1,3 +1,4 @@. A bare @@ separates chunks. *** End of File anchors a chunk to the end of the file. Context must match the current file: read the relevant region when a match fails.

Example, creating a file with a Unicode docstring and blank line:
*** Begin Patch
*** Add File: hello.py
+"""Привет, мир."""
+
+print("Hello, world!")
*** End Patch

Example, updating and moving an existing file and deleting another:
*** Begin Patch
*** Update File: hello.py
*** Move to: app/hello.py
@@
 """Привет, мир."""

-print("Hello, world!")
+print("Hello from the app!")
*** Delete File: obsolete.txt
*** End Patch

Prefer one Update File block per file with several @@ chunks. Repeated Update File blocks without moves are also supported: each block matches the contents resulting from preceding blocks. Do not combine Add/Delete/Move with another operation on the same path. To replace an existing file's contents, use Update File instead of Delete plus Add.

PyCoder constraints: paths must resolve inside the workspace; use relative paths. Targets must be regular UTF-8 text files at most 8 MiB. The entire patch is checked before writing; a preparation error changes no files. If a file changes after preparation, read it again and retry. A missing + is a format error, not an encoding problem. Successful results list changed paths. Scoped AGENTS.md instructions may be returned first: follow them and resubmit the patch. Writes need no extra permission inside a trusted project; read-only mode denies mutations.
