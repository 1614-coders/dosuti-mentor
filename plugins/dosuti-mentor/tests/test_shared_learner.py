"""Cross-project preferences are not subject knowledge or project progress."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import course
import learning_start
import learning_tools as lt
import managed_storage


class SharedLearner(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = managed_storage.initialize(self.base / 'home')
        self.a = self.make_course('a'); self.b = self.make_course('b')

    def make_course(self, name):
        root = self.home / 'courses' / name
        with contextlib.redirect_stdout(io.StringIO()): course.init(NS(course=root, title=name))
        return NS(course=root, profile='default', scope='profile', set=[], reset=False, onboarding=None)

    def save(self, args, answers=None, **options):
        args = NS(**(vars(args) | options))
        if answers is not None:
            p = self.base / 'answers.json'; p.write_text(json.dumps({'answers': answers}))
            args.onboarding_file = p
        return lt.settings(args)

    def complete(self):
        return self.save(self.a, dict(outcome=['profession'], subject='Java Backend',
            self_assessment='confident', learning_format='step_by_step', assistant_role='teacher',
            check_frequency='each_block', explanation_detail='detailed',
            constraints={'selected': ['deadline'], 'details': 'Only project A'}))

    def test_first_start_uses_unchanged_eight_questions(self):
        r = lt.preferences(self.a)
        self.assertEqual(r['onboarding_next'], 'outcome')
        self.assertIn('Вопрос 1 из 8', r['onboarding_message'])
        self.assertEqual(len(r['onboarding_pending']), 8)

    def test_second_course_reuses_only_general_answers(self):
        self.complete()
        r = lt.preferences(self.b)
        self.assertEqual(r['onboarding_mode'], 'project')
        self.assertNotIn('Вопрос', r['onboarding_message'])
        for name in ('outcome', 'subject', 'self_assessment', 'constraints'):
            self.assertNotIn(name, r['onboarding_answers'])
        self.assertEqual(r['effective']['checks'], 'each_block')
        self.assertEqual(r['onboarding_pending'], ['subject', 'outcome'])
        r = self.save(self.b, {'outcome': ['topics'], 'subject': 'History'})
        self.assertIsNone(r['onboarding_next'])
        self.assertIsNone(r['onboarding_message'])
        self.assertNotIn('self_assessment', r['onboarding_answers'])
        self.assertEqual(lt.preferences(self.a)['onboarding_answers']['subject']['value'], 'Java Backend')
        from diagnostic_learning import starting_guidance
        self.assertTrue(starting_guidance(self.b)['calibration'])

    def test_changes_and_language_are_shared_but_level_is_local(self):
        self.complete()
        self.save(self.a, set=['detail=concise', 'communication_language=Русский', 'level=advanced'])
        self.assertEqual(lt.preferences(self.b)['effective']['detail'], 'concise')
        self.assertEqual(lt.preferences(self.b)['effective']['communication_language'], 'Русский')
        self.assertEqual(lt.preferences(self.b)['effective']['level'], 'beginner')
        self.save(self.b, set=['detail=balanced', 'communication_language=English'])
        self.assertEqual(lt.preferences(self.a)['effective']['detail'], 'balanced')
        self.assertEqual(lt.preferences(self.a)['effective']['communication_language'], 'English')

    def test_restart_is_explicit_and_does_not_delete_history(self):
        self.complete()
        folder = lt.learner(self.a); marker = folder / 'unit-learning.json'
        marker.write_bytes(b'preserve old progress')
        r = self.save(self.a, restart_onboarding=True)
        self.assertEqual(len(r['onboarding_pending']), 8)
        self.assertIn('Вопрос 1 из 8', r['onboarding_message'])
        self.assertEqual(marker.read_bytes(), b'preserve old progress')
        self.assertEqual(lt.preferences(self.b)['onboarding_mode'], 'project')

    def test_no_shared_profile_and_other_identity_do_not_inherit(self):
        self.assertEqual(lt.preferences(self.b)['onboarding'], 'not_asked')
        self.complete()
        other = NS(**(vars(self.b) | {'profile': 'other-person'}))
        self.assertEqual(lt.preferences(other)['onboarding'], 'not_asked')

    def test_read_is_pure_and_new_process_recovers_profile(self):
        self.complete()
        before = {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        r = subprocess.run([sys.executable, '-B', str(Path(lt.__file__).with_name('course.py')),
            'settings', '--course', str(self.b.course), '--compact'], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['onboarding_mode'], 'project')
        after = {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        self.assertEqual({k: after[k] for k in before}, before)
        # Existing CLI storage sessions may initialize their lock, never learner/progress data.
        self.assertLessEqual(set(after) - set(before), {str(self.home / '.storage.lock')})

    def test_new_projects_get_distinct_managed_courses(self):
        p = self.base / 'project-a'; p.mkdir()
        q = self.base / 'project-b'; q.mkdir()
        first = learning_start.start(p, 'chat-a', self.home)
        menu = learning_start.start(q, 'chat-b', self.home)
        self.assertEqual(menu['start_action'], 'choose_course')
        second = learning_start.start(q, 'chat-b', self.home, choice='new', expected_revision=menu['revision'])
        self.assertNotEqual(first['binding']['course_path'], second['binding']['course_path'])
        again = learning_start.start(p, 'another-chat', self.home)
        self.assertEqual(again['start_action'], 'choose_course')
        resumed = learning_start.start(p, 'another-chat', self.home, choice='existing', course=Path(first['binding']['course_path']), expected_revision=again['revision'])
        self.assertEqual(first['binding'], resumed['binding'])
        self.assertEqual(list(p.iterdir()), [])
        self.assertEqual(list(q.iterdir()), [])
        standalone = learning_start.start(None, 'standalone', self.home)
        self.assertEqual(standalone['start_action'], 'choose_course')

    def test_project_overrides_remain_local_and_global_reset_does_not_delete_answers(self):
        self.complete()
        self.save(self.a, set=['detail=detailed'])
        self.save(self.b, scope='course', set=['detail=concise'])
        self.assertEqual(lt.preferences(self.a)['effective']['detail'], 'detailed')
        self.assertEqual(lt.preferences(self.b)['effective']['detail'], 'concise')
        self.save(self.a, scope='learner', reset=True)
        self.assertEqual(lt.preferences(self.b)['onboarding_mode'], 'project')
        self.assertEqual(lt.preferences(self.a)['effective']['detail'], 'balanced')

    def test_unmanaged_course_does_not_borrow_an_arbitrary_home(self):
        root = self.base / 'outside'
        with contextlib.redirect_stdout(io.StringIO()): course.init(NS(course=root, title='outside'))
        args = NS(**(vars(self.a) | {'course': root}))
        self.complete()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(lt.preferences(args)['onboarding'], 'not_asked')
            with self.assertRaisesRegex(ValueError, 'No managed learner home'):
                self.save(args, scope='learner', set=['style=partner'])
        args.learner_home = self.home
        self.assertEqual(lt.preferences(args)['onboarding_mode'], 'project')

    def test_selected_legacy_profile_is_adopted_without_modifying_original(self):
        path = lt.pref_file(self.a, 'profile'); path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'revision': 2, 'values': {'style': 'partner', 'level': 'advanced'},
                                   'onboarding': 'complete', 'onboarding_answers': {
                                       'subject': {'status': 'answered', 'value': 'Java'}}}))
        before = path.read_bytes()
        import connection
        project = self.base / 'legacy-project'; project.mkdir()
        connection.dispatch(NS(project=project, action='connect', scope='project', chat=None,
            expected_revision=0, course=self.a.course, profile='default'))
        state = learning_start.start(project, 'new-chat', self.home)
        self.assertEqual(state['start_action'], 'choose_course')
        self.assertEqual(path.read_bytes(), before)
        r = lt.preferences(self.b)
        self.assertEqual(r['effective']['style'], 'partner')
        self.assertNotIn('subject', r['onboarding_answers'])
        self.assertEqual(r['effective']['level'], 'beginner')

    def test_shared_cas_and_concurrent_independent_changes(self):
        from concurrent.futures import ThreadPoolExecutor
        self.complete()
        rev = lt.preferences(self.a)['layers']['learner']['revision']
        self.save(self.a, scope='learner', set=['detail=concise'], expected_revision=rev)
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.save(self.b, scope='learner', set=['style=partner'], expected_revision=rev)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda x: self.save(self.a, scope='learner', set=[x]),
                          ['style=partner', 'format=blocks']))
        prefs = lt.preferences(self.b)['effective']
        self.assertEqual((prefs['style'], prefs['format'], prefs['detail']), ('partner', 'blocks', 'concise'))
        with self.assertRaises(ValueError): self.save(self.a, scope='learner', set=['level=advanced'])

    def test_corrupt_or_linked_shared_file_is_preserved(self):
        from learner_preferences import path_for
        self.complete(); path = path_for(self.a)
        original = path.read_bytes()
        path.write_text('{invalid')
        with self.assertRaises(ValueError): self.save(self.b, set=['detail=concise'])
        self.assertEqual(path.read_text(), '{invalid')
        path.write_bytes(original)
        external = self.base / 'private'; external.write_bytes(original)
        path.unlink()
        try: os.link(external, path)
        except OSError as error: self.skipTest('Hard links unavailable: ' + str(error))
        with self.assertRaises(ValueError): lt.preferences(self.b)
        self.assertEqual(external.read_bytes(), original)

    def test_managed_first_survey_keeps_all_eight_approved_messages(self):
        expected = json.loads(Path(__file__).with_name('fixtures').joinpath('onboarding-approved.json').read_text())
        values = dict(outcome=['profession'], subject='Java Backend', self_assessment='confident',
            learning_format='step_by_step', assistant_role='teacher', check_frequency='each_block',
            explanation_detail='detailed', constraints={'selected': ['free_pace'], 'details': ''})
        self.save(self.a, onboarding='in_progress')
        for index, (key, value) in enumerate(values.items()):
            self.assertEqual(lt.preferences(self.a)['onboarding_message'], expected['questions'][index])
            self.save(self.a, {key: value})
        self.assertEqual(lt.preferences(self.a)['onboarding_mode'], 'ready')


if __name__ == '__main__': unittest.main()
