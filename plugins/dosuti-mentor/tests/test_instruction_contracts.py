"""Check objective documentation contracts; this does not grade teaching semantics."""
import argparse
from pathlib import Path
import re
import shlex
import sys
import unittest
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / 'scripts'))
import cli_output
import course
import diagnostic_learning
import unit_learning


def instructions():
    return sorted(PLUGIN.glob('references/*.md')) + sorted(PLUGIN.glob('skills/*/SKILL.md'))


def command_examples():
    for path in instructions():
        content = path.read_text(encoding='utf-8')
        for language, body in re.findall(r'^```([^\n]*)\n(.*?)^```', content, re.M | re.S):
            if language.strip() not in ('sh', 'bash', 'shell', 'powershell', ''):
                continue
            for line in body.replace('\\\n', ' ').splitlines():
                if 'course.py' not in line:
                    continue
                tokens = shlex.split(line, comments=True)
                index = next((i for i, token in enumerate(tokens) if token.endswith('course.py')), None)
                if index is not None and index + 1 < len(tokens):
                    yield path, tokens[index + 1:]


def course_commands():
    class ParserCaptured(BaseException):
        pass
    captured = []
    def capture(parser, *args, **kwargs):
        captured.append(parser)
        raise ParserCaptured()
    # Use the real CLI parser without executing any command or accessing a profile.
    with patch.object(argparse.ArgumentParser, 'parse_args', capture):
        try:
            course.main()
        except ParserCaptured:
            pass
    return next(action.choices for action in captured[0]._actions
                if isinstance(action, argparse._SubParsersAction))


class InstructionContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.commands = course_commands()

    def test_all_local_markdown_links_resolve(self):
        for path in instructions():
            for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', path.read_text(encoding='utf-8')):
                if re.match(r'^[a-zA-Z][\w+.-]*:|^#', target):
                    continue
                relative = target.split('#', 1)[0]
                with self.subTest(file=path.name, target=target):
                    self.assertTrue((path.parent / relative).is_file())

    def test_fenced_course_examples_use_existing_commands_and_options(self):
        count = 0
        for path, tokens in command_examples():
            count += 1
            with self.subTest(file=str(path.relative_to(PLUGIN)), command=tokens[0]):
                self.assertIn(tokens[0], self.commands)
                options = self.commands[tokens[0]]._option_string_actions
                for token in tokens[1:]:
                    if token.startswith('--'):
                        self.assertIn(token.split('=', 1)[0], options)
        self.assertGreater(count, 0)

    def test_compact_contract_matches_real_cli_support(self):
        content = (PLUGIN / 'references/cli-output.md').read_text(encoding='utf-8')
        supported = content.split('Поддержаны ', 1)[1].split('. Это ', 1)[0]
        self.assertEqual(set(re.findall(r'`([^`]+)`', supported)), set(cli_output.COMMANDS))
        actual = {name for name, parser in self.commands.items() if '--compact' in parser._option_string_actions}
        self.assertEqual(actual, set(cli_output.COMMANDS))

    def test_documented_lesson_types_match_existing_enum(self):
        content = (PLUGIN / 'references/lesson-presentation.md').read_text(encoding='utf-8')
        self.assertEqual(set(re.findall(r'^\| ([a-z_]+) \|', content, re.M)), unit_learning.LESSON_KINDS)

    def test_diagnostic_budget_and_policy_are_consistent(self):
        for relative in ('references/start-learning.md', 'references/adaptive-learning.md', 'skills/start-learning/SKILL.md'):
            content = (PLUGIN / relative).read_text(encoding='utf-8')
            with self.subTest(file=relative):
                self.assertIn(diagnostic_learning.QUESTION_POLICY, content)
                self.assertIn('adaptive_v2', content)
        self.assertIn(f'Вопрос N из {diagnostic_learning.QUESTION_LIMIT}',
                      (PLUGIN / 'references/start-learning.md').read_text(encoding='utf-8'))

    def test_shared_contracts_remain_reachable_from_calling_skills(self):
        for name in ('start-learning', 'teach-unit', 'teach-lecture', 'teach-project', 'review-homework', 'review-learning'):
            self.assertIn('../../references/cli-output.md', (PLUGIN / f'skills/{name}/SKILL.md').read_text(encoding='utf-8'))
        for name in ('research-topic', 'prepare-lecture', 'teach-unit', 'teach-lecture', 'teach-project'):
            self.assertIn('../../references/material-lifecycle.md', (PLUGIN / f'skills/{name}/SKILL.md').read_text(encoding='utf-8'))

    def test_removed_pilot_has_no_dangling_instruction(self):
        # Exact retired instruction, not a semantic/keyword classifier for QA prose.
        for path in instructions():
            with self.subTest(file=str(path.relative_to(PLUGIN))):
                self.assertFalse('Если работа идёт в явно выбранном тестовом пилоте, используй\nСохраняй'
                                 in path.read_text(encoding='utf-8'))
        self.assertNotIn('pilot', self.commands)
        self.assertFalse((PLUGIN / 'skills/test-learning').exists())


if __name__ == '__main__':
    unittest.main()
