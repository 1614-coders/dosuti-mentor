"""Regressions for explicit learner selection and course-local survey restarts."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import test_shared_learner
import connection
import learning_start
import learning_tools as lt
from learner_preferences import path_for


class ProfileIsolation(unittest.TestCase):
    setUp = test_shared_learner.SharedLearner.setUp
    make_course = test_shared_learner.SharedLearner.make_course
    save = test_shared_learner.SharedLearner.save
    complete = test_shared_learner.SharedLearner.complete
    def files(self):
        return {str(p.relative_to(self.base)): p.read_bytes()
                for p in self.base.rglob('*') if p.is_file()}

    def project_binding(self, name='project', legacy=False):
        project = self.base / name; project.mkdir()
        if legacy:
            (project / '.lecture-tutor.json').write_text(json.dumps({
                'course_path': str(self.a.course), 'profile': 'alice'}))
        else:
            connection.dispatch(NS(project=project, action='connect', scope='project', chat=None,
                expected_revision=0, course=self.a.course, profile='alice'))
        return project

    def local_course(self, selected='on_request', explicit_checks=True):
        self.complete()
        project = self.base / 'new-project'; project.mkdir()
        menu = learning_start.start(project, 'chat-a', self.home)
        chosen = learning_start.start(project, 'chat-a', self.home, choice='new',
                                      expected_revision=menu['revision'])
        args = NS(**(vars(self.b) | {'course': Path(chosen['binding']['course_path'])}))
        self.save(args, dict(outcome=['topics'], subject='History', self_assessment='unknown',
                            constraints={'selected': ['free_pace']}))
        self.save(args, course_settings='change', setting_question=['check_frequency'])
        self.save(args, {'check_frequency': selected},
                  set=(['checks='+selected] if explicit_checks else []) + ['detail=concise'])
        return args

    def test_project_and_legacy_binding_reject_other_learner_without_writes(self):
        for legacy in (False, True):
            project = self.project_binding('project-'+str(legacy), legacy)
            for chat in (None, 'new-chat'):
                with self.subTest(legacy=legacy, chat=chat), patch.dict('os.environ', {}, clear=True):
                    before = self.files()
                    with self.assertRaisesRegex(ValueError, 'learner'):
                        learning_start.start(project, chat, self.home, profile='bob')
                    self.assertEqual(self.files(), before)

    def test_existing_profile_and_unavailable_course_cannot_bypass_identity_check(self):
        project = self.project_binding()
        self.save(NS(**(vars(self.b) | {'profile': 'bob'})), set=['detail=concise'])
        for unavailable in (False, True):
            if unavailable: (self.a.course / 'course.json').unlink()
            with self.subTest(unavailable=unavailable):
                before = self.files()
                with self.assertRaisesRegex(ValueError, 'learner'):
                    learning_start.start(project, 'chat-b', self.home, profile='bob')
                self.assertEqual(self.files(), before)

    def test_same_legacy_learner_has_continue_action(self):
        project = self.project_binding(legacy=True)
        result = learning_start.start(project, 'new-chat', self.home, profile='alice')
        self.assertEqual(result['binding']['profile'], 'alice')
        self.assertEqual(result['start_action'], 'continue')

    def test_explicit_new_binding_can_select_bob_without_altering_project_alice(self):
        project = self.project_binding()
        result = learning_start.start(project, 'bob-chat', self.home, profile='bob',
                                      choice='new', expected_revision=connection.status(project)['revision'])
        self.assertEqual(result['binding']['profile'], 'bob')
        self.assertEqual(connection.status(project)['binding']['profile'], 'alice')
        self.assertEqual(connection.status(project, 'bob-chat')['binding']['profile'], 'bob')
        self.assertEqual(learning_start.start(project, 'bob-chat', self.home,
                                              profile='bob')['start_action'], 'continue')

    def test_restart_retains_local_values_history_and_shared_revision(self):
        args = self.local_course()
        progress = lt.learner(args) / 'cursor.json'; progress.write_bytes(b'untouched QA progress')
        shared = path_for(args); before = shared.read_bytes()
        state = self.save(args, restart_onboarding=True)
        self.assertEqual(state['effective']['checks'], 'on_request')
        self.assertEqual(state['effective']['detail'], 'concise')
        self.assertIsNotNone(state['course_setup'])
        self.assertEqual(len(state['onboarding_pending']), 8)
        self.save(args, set=['checks=end'])
        self.assertEqual(lt.preferences(self.a)['effective']['checks'], 'each_block')
        self.assertEqual(shared.read_bytes(), before)
        self.assertEqual(progress.read_bytes(), b'untouched QA progress')

    def test_eight_repeated_answers_stay_local_and_recover_in_new_process(self):
        args = self.local_course(); shared = path_for(args); before = shared.read_bytes()
        self.save(args, restart_onboarding=True)
        answers = dict(outcome=['topics'], subject='History', self_assessment='unknown',
            learning_format='step_by_step', assistant_role='partner', check_frequency='end',
            explanation_detail='concise', constraints={'selected': ['free_pace']})
        for index, (key, value) in enumerate(answers.items(), 1):
            state = lt.preferences(args)
            self.assertIn('Вопрос '+str(index)+' из 8', state['onboarding_message'])
            self.save(args, {key: value}, set=['checks=end'] if key=='check_frequency' else [])
        self.assertEqual(lt.preferences(args)['onboarding_mode'], 'ready')
        self.assertEqual(shared.read_bytes(), before)
        run = subprocess.run([sys.executable, '-B', str(Path(lt.__file__).with_name('course.py')),
            'settings', '--course', str(args.course), '--compact'], capture_output=True, text=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['effective']['checks'], 'end')
        self.assertEqual(lt.preferences(self.a)['effective']['checks'], 'each_block')

    def test_skip_restart_and_explicit_global_change_keep_separate_scopes(self):
        args = self.local_course(); shared = path_for(args); before = shared.read_bytes()
        self.save(args, restart_onboarding=True)
        self.save(args, onboarding='skipped')
        self.assertEqual(lt.preferences(args)['onboarding_mode'], 'ready')
        self.save(args, set=['checks=end'])
        self.assertEqual(shared.read_bytes(), before)
        self.save(args, scope='learner', set=['detail=detailed'])
        self.assertEqual(lt.preferences(self.a)['effective']['detail'], 'detailed')
        self.assertEqual(lt.preferences(args)['effective']['detail'], 'concise')

    def test_restart_preserves_answer_based_checks_until_learner_changes_them(self):
        args = self.local_course(explicit_checks=False)
        self.assertEqual(lt.preferences(args)['effective']['checks'], 'on_request')
        shared = path_for(args); before = shared.read_bytes()
        self.assertEqual(self.save(args, restart_onboarding=True)['effective']['checks'], 'on_request')
        self.assertEqual(self.save(args, {'check_frequency': 'end'})['effective']['checks'], 'end')
        self.assertEqual(shared.read_bytes(), before)

    def test_restart_unfinished_course_still_asks_exactly_eight_questions(self):
        self.complete()
        project = self.base / 'unfinished'; project.mkdir()
        menu = learning_start.start(project, 'chat', self.home)
        chosen = learning_start.start(project, 'chat', self.home, choice='new', expected_revision=menu['revision'])
        args = NS(**(vars(self.b) | {'course': Path(chosen['binding']['course_path'])}))
        self.save(args, restart_onboarding=True)
        state = self.save(args, onboarding='skipped')
        self.assertEqual(state['onboarding_pending'], [])
        self.assertEqual(state['onboarding_mode'], 'ready')

    def test_restart_cas_conflicts_and_concurrent_local_changes(self):
        args = self.local_course(); shared = path_for(args); before = shared.read_bytes()
        revision = lt.preferences(args)['layers']['profile']['revision']
        self.save(args, restart_onboarding=True, expected_revision=revision)
        local = lt.pref_file(args, 'profile').read_bytes()
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.save(args, set=['checks=end'], expected_revision=revision)
        self.assertEqual(lt.pref_file(args, 'profile').read_bytes(), local)
        with ThreadPoolExecutor(max_workers=2) as pool:
            runs = list(pool.map(lambda setting: subprocess.run([sys.executable, '-B',
                str(Path(lt.__file__).with_name('course.py')), 'settings', '--course', str(args.course),
                '--set', setting, '--compact'], capture_output=True, text=True, timeout=20),
                ['checks=end', 'detail=detailed']))
        for run in runs: self.assertEqual(run.returncode, 0, run.stderr)
        state = lt.preferences(args)['effective']
        self.assertEqual((state['checks'], state['detail']), ('end', 'detailed'))
        self.assertEqual(shared.read_bytes(), before)


if __name__ == '__main__': unittest.main()
