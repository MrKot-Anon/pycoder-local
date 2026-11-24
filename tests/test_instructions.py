import json
from pathlib import Path
import tempfile
import unittest

from pycoder.patch import PreparedPatch, parse
from pycoder.prompts import APPLY_PATCH, SYSTEM, instruction
from pycoder.tools import TOOL_SPECS


class InstructionsTests(unittest.TestCase):
    def test_documented_patch_examples_execute(self):
        # The actual examples seen by the model must match the actual parser.
        guide = instruction('apply_patch.md')
        examples = []
        remaining = guide[guide.index('Example, creating'):]
        while '*** Begin Patch' in remaining:
            start = remaining.index('*** Begin Patch')
            end = remaining.index('*** End Patch', start) + len('*** End Patch')
            examples.append(remaining[start:end])
            remaining = remaining[end:]
        self.assertEqual(len(examples), 2)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'obsolete.txt').write_text('old')
            for example in examples:
                args = json.loads(json.dumps({'patch': example}))
                PreparedPatch(root, args['patch']).apply()
            self.assertFalse((root / 'hello.py').exists())
            self.assertFalse((root / 'obsolete.txt').exists())
            self.assertIn('Привет, мир.', (root / 'app/hello.py').read_text())
            self.assertIn('Hello from the app!', (root / 'app/hello.py').read_text())

    def test_missing_plus_on_docstring_or_blank_has_actionable_error(self):
        for content, line in [('"""Русский текст"""', 3), ('+ok\n\n+more', 4)]:
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, rf"line {line}.*every content line must start with '\+'"):
                parse('*** Begin Patch\n*** Add File: a.py\n' + content + '\n*** End Patch')

    def test_transported_description_contains_grammar_and_correct_envelope(self):
        spec = next(s['function'] for s in TOOL_SPECS if s['function']['name'] == 'apply_patch')
        self.assertEqual(spec['description'], APPLY_PATCH)
        self.assertIn(instruction('apply_patch.lark'), spec['description'])
        self.assertEqual(spec['parameters']['required'], ['patch'])
        self.assertIn('JSON object with one `patch` string', SYSTEM)
        self.assertNotIn('"command":["apply_patch"', SYSTEM)
        self.assertNotIn('streaming thinking', SYSTEM)
