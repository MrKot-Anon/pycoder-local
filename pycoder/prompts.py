"""Codex-derived packaged instructions; see instructions/SOURCES.md (Apache-2.0)."""

from importlib.resources import files


def instruction(name):
    return files("pycoder").joinpath("instructions", name).read_text(encoding="utf-8")


SYSTEM = instruction("system.md")
COMPACT = instruction("compact.md")
SUMMARY_PREFIX = instruction("summary_prefix.md")
APPLY_PATCH = instruction("apply_patch.md") + "\nFormal grammar for the decoded patch string:\n```lark\n" + instruction("apply_patch.lark") + "```"
