"""First learning start is explicit; default data must not enter the user's project."""
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import connection
import managed_storage
import learning_tools

PLUGIN = Path(__file__).resolve().parents[1]


class InstallationPackage(unittest.TestCase):
    def test_setup_is_separate_from_educational_start(self):
        manifest = json.loads((PLUGIN / 'plugin.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['extensions']['com.openai']['onboardingSkill'], './skills/setup-dosuti/SKILL.md')
        self.assertTrue((PLUGIN / 'skills/setup-dosuti/SKILL.md').is_file())

    def test_windows_bootstrap_is_inside_installed_package(self):
        for name in ('Install-DoSuti.ps1', 'Windows-Runtime.ps1', 'bootstrap.py'):
            self.assertTrue((PLUGIN / 'scripts' / name).is_file(), name)


class ManagedStart(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = managed_storage.initialize(self.base / 'данные')
        self.project = self.base / 'чужой проект'; self.project.mkdir()
        (self.project / 'important.txt').write_text('user data')

    def start(self, project=None, chat='chat-a'):
        import learning_start
        return learning_start.start(project or self.project, chat, self.home)

    def files(self, folder):
        return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob('*') if p.is_file()}

    def test_clean_start_creates_managed_course_and_profile_not_project_files(self):
        before = self.files(self.project)
        state = self.start()
        binding = state['binding']
        root = Path(binding['course_path'])
        self.assertTrue(root.is_relative_to(self.home / 'courses'))
        self.assertTrue((root / 'course.json').is_file())
        self.assertTrue((root / 'learners/default').is_dir())
        self.assertEqual(binding['profile'], 'default')
        self.assertEqual(self.files(self.project), before)
        self.assertFalse((root / 'learners/default/diagnostic-learning.json').exists())
        self.assertFalse((root / 'learners/default/learning-intent.json').exists())

    def test_repeat_and_other_project_reuse_saved_answers_without_overwrite(self):
        first = self.start(); root = Path(first['binding']['course_path'])
        learning_tools.settings(NS(course=root, profile='default', scope='profile',
            set=['detail=detailed'], reset=False, onboarding='in_progress'))
        payload = self.base / 'answer.json'; payload.write_text(json.dumps({'answers': {'subject': 'Java'}}))
        learning_tools.settings(NS(course=root, profile='default', scope='profile',
            set=[], reset=False, onboarding=None, onboarding_file=payload))
        before = self.files(root)
        other = self.base / 'другой проект'; other.mkdir()
        self.assertEqual(self.start(chat='chat-b')['binding'], first['binding'])
        self.assertEqual(self.start(other, 'chat-c')['binding'], first['binding'])
        self.assertEqual(self.files(root), before)
        self.assertEqual(list(other.iterdir()), [])

    def test_actual_cli_new_chat_reads_saved_profile_and_does_not_touch_project(self):
        scripts = PLUGIN / 'scripts'
        def cli(script, *args):
            result = subprocess.run([sys.executable, '-B', str(scripts / script), *map(str, args)],
                capture_output=True, encoding='utf-8', timeout=20, env=dict(os.environ, PYTHONUTF8='1'))
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        first = cli('learning_start.py', '--project', self.project, '--chat', 'chat-a', '--home', self.home)
        root = first['binding']['course_path']
        cli('course.py', 'settings', '--course', root, '--profile', 'default', '--onboarding', 'in_progress')
        before = self.files(self.home); project_before = self.files(self.project)
        other = cli('learning_start.py', '--project', self.project, '--chat', 'chat-b', '--home', self.home)
        prefs = cli('course.py', 'settings', '--course', other['binding']['course_path'], '--profile', 'default')
        self.assertEqual(prefs['onboarding'], 'in_progress')
        self.assertEqual(other['binding'], first['binding'])
        self.assertEqual(self.files(self.home), before)
        self.assertEqual(self.files(self.project), project_before)

    def test_existing_connection_takes_precedence_and_home_is_untouched(self):
        first = self.start(); root = Path(first['binding']['course_path'])
        connection.dispatch(NS(project=self.project, action='connect', scope='project',
            chat=None, expected_revision=0, course=root, profile='existing'))
        before = self.files(self.home); project_before = self.files(self.project)
        self.assertEqual(self.start()['binding']['profile'], 'existing')
        self.assertEqual(self.files(self.home), before)
        self.assertEqual(self.files(self.project), project_before)

    def test_disconnected_project_or_chat_does_not_fall_back_to_default(self):
        for scope, chat in (('project', None), ('chat', 'chat-a')):
            with self.subTest(scope=scope):
                state = connection.status(self.project, chat)
                connection.dispatch(NS(project=self.project, action='disconnect', scope=scope,
                    chat=chat, expected_revision=state['revision']))
                before = self.files(self.home)
                self.assertFalse(self.start()['enabled'])
                self.assertEqual(self.files(self.home), before)

    def test_unavailable_existing_connection_is_not_replaced(self):
        state = self.start(); root = Path(state['binding']['course_path'])
        connection.dispatch(NS(project=self.project, action='connect', scope='project',
            chat=None, expected_revision=0, course=root, profile='default'))
        (root / 'course.json').unlink()  # Isolated QA damage, not user data.
        before = self.files(self.home)
        self.assertEqual(self.start()['course_status'], 'unavailable')
        self.assertEqual(self.files(self.home), before)

    def test_unknown_course_folder_is_not_adopted(self):
        folder = self.home / 'courses/default'; folder.mkdir(parents=True)
        (folder / 'keep.txt').write_text('external')
        before = self.files(self.home)
        with self.assertRaises(ValueError): self.start()
        self.assertEqual((folder / 'keep.txt').read_text(), 'external')
        self.assertFalse((folder / 'course.json').exists())

    def test_permission_denied_does_not_claim_success(self):
        with patch('learning_start.file_lock', side_effect=PermissionError('Denied')):
            with self.assertRaises(PermissionError): self.start()
        self.assertFalse((self.home / 'courses').exists())

    def test_default_location_and_projectless_start(self):
        import learning_start
        with patch('learning_start.default_home', return_value=self.home):
            state = learning_start.start(None, 'chat-a')
        self.assertTrue(Path(state['binding']['course_path']).is_relative_to(self.home))

    def test_two_concurrent_starts_share_one_course(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(lambda chat: self.start(chat=chat), ['chat-a', 'chat-b']))
        self.assertEqual(rows[0]['binding'], rows[1]['binding'])
        self.assertEqual(len(list((self.home / 'courses').iterdir())), 1)

    def test_connection_changed_during_creation_is_not_overwritten(self):
        import course
        original = course.init
        def create_then_connect(args):
            original(args)
            connection.dispatch(NS(project=self.home, action='connect', scope='project',
                chat=None, expected_revision=0, course=args.course, profile='other'))
        with patch('course.init', side_effect=create_then_connect):
            with self.assertRaisesRegex(ValueError, 'Connections changed'):
                self.start()
        self.assertEqual(connection.status(self.home)['binding']['profile'], 'other')


if __name__ == '__main__': unittest.main()
