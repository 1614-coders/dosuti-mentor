"""Persisted choices use existing connections, never inferred chat identities."""
import json
import hashlib
import os
import subprocess
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import test_shared_learner
import learning_start
import learning_tools as lt


class StartChoices(unittest.TestCase):
    setUp = test_shared_learner.SharedLearner.setUp
    make_course = test_shared_learner.SharedLearner.make_course
    save = test_shared_learner.SharedLearner.save
    complete = test_shared_learner.SharedLearner.complete

    def test_legacy_project_override_without_chat_is_not_a_chat_choice(self):
        import connection
        self.complete()
        project = self.base / 'legacy-project'; project.mkdir()
        key = 'project-' + hashlib.sha256(os.path.normcase(str(project)).encode('utf-8')).hexdigest()[:32]
        connection.dispatch(NS(project=self.home, action='connect', scope='chat', chat=key,
            expected_revision=0, course=self.a.course, profile='default'))
        with patch.dict(os.environ):
            os.environ.pop('CODEX_THREAD_ID', None)  # Preserve HOME/USERPROFILE and other runtime variables.
            self.assertNotIn('CODEX_THREAD_ID', os.environ)
            state = learning_start.start(project, None, self.home)
        self.assertEqual(state['start_action'], 'choose_course')
        self.assertTrue(state['needs_chat_id'])
        self.assertIsNone(state['binding'])

    def test_bound_chat_cannot_silently_switch_learner_identity(self):
        self.complete()
        project = self.base / 'identity'; project.mkdir()
        menu = learning_start.start(project, 'chat', self.home)
        learning_start.start(project, 'chat', self.home, choice='existing', course=self.a.course,
                             expected_revision=menu['revision'])
        with self.assertRaisesRegex(ValueError, 'learner'):
            learning_start.start(project, 'chat', self.home, profile='other-person')
        self.assertEqual(learning_start.start(project, 'chat', self.home)['binding']['profile'], 'default')

    def test_course_local_settings_are_never_adopted_as_general_profile(self):
        from learner_preferences import path_for
        self.complete()
        project = self.base / 'local-course'; project.mkdir()
        menu = learning_start.start(project, 'chat', self.home)
        chosen = learning_start.start(project, 'chat', self.home, choice='new', expected_revision=menu['revision'])
        args = NS(**(vars(self.b) | {'course':Path(chosen['binding']['course_path'])}))
        self.save(args, dict(outcome=['topics'], subject='History', self_assessment='unknown',
            constraints={'selected':['free_pace']}, learning_format='step_by_step', assistant_role='partner',
            check_frequency='on_request', explanation_detail='concise'), set=['style=partner'])
        common = path_for(args); common.unlink()  # Isolated simulated loss; preserve course settings.
        learning_start.start(project, 'chat', self.home)
        self.assertFalse(common.exists())

    def test_legacy_adoption_preserves_newer_general_preferences(self):
        from learner_preferences import path_for, seed
        self.complete()
        local = lt.pref_file(self.a, 'profile')
        data = json.loads(local.read_text()); data['values']['detail'] = 'detailed'
        local.write_text(json.dumps(data))
        common = path_for(self.a)
        data = json.loads(common.read_text()); data['onboarding'] = 'not_asked'; data['values']['detail'] = 'concise'
        common.write_text(json.dumps(data))
        seed(self.a)
        self.assertEqual(json.loads(common.read_text())['values']['detail'], 'concise')

    def test_concurrent_cli_choices_have_one_cas_winner_and_preserve_progress(self):
        self.complete()
        project = self.base / 'parallel'; project.mkdir()
        menu = learning_start.start(project, 'chat', self.home)
        before = {str(p): p.read_bytes() for c in (self.a.course, self.b.course)
                  for p in c.rglob('*') if p.is_file()}
        command = [sys.executable, '-B', learning_start.__file__, '--project', str(project),
                   '--chat', 'chat', '--home', str(self.home), '--choice', 'existing',
                   '--expected-revision', str(menu['revision']), '--course']
        workers = [subprocess.Popen(command + [str(c)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True) for c in (self.a.course, self.b.course)]
        try:
            outputs = [p.communicate(timeout=30) for p in workers]
            self.assertEqual(sorted(p.returncode for p in workers), [0, 1], outputs)
            rejected = next(err for p, (_, err) in zip(workers, outputs) if p.returncode)
            self.assertIn('changed', rejected)
            self.assertEqual(before, {str(p): p.read_bytes() for c in (self.a.course, self.b.course)
                                     for p in c.rglob('*') if p.is_file()})
        finally:
            for p in workers:
                if p.poll() is None: p.kill(); p.communicate(timeout=10)

    def test_missing_bound_course_is_not_reported_as_ready_to_continue(self):
        self.complete()
        project = self.base / 'broken-course'; project.mkdir()
        menu = learning_start.start(project, 'chat', self.home)
        learning_start.start(project, 'chat', self.home, choice='existing', course=self.a.course,
                             expected_revision=menu['revision'])
        (self.a.course / 'course.json').unlink()
        state = learning_start.start(project, 'chat', self.home)
        self.assertEqual(state['start_action'], 'connection_unavailable')
        self.assertEqual(state['course_status'], 'unavailable')

    def test_choice_before_new_course_or_binding(self):
        self.complete()
        project = self.base / 'new-project'; project.mkdir()
        before = {str(p):p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        state = learning_start.start(project, 'chat-a', self.home)
        self.assertEqual(state['start_action'], 'choose_course')
        self.assertEqual(state['start_message'], 'Этот проект (чат) будет продолжением существующего курса или начнём новый?')
        self.assertEqual(len(state['choices']), 3)
        self.assertEqual(before, {str(p):p.read_bytes() for p in self.home.rglob('*') if p.is_file()})
        self.assertEqual(list(project.iterdir()), [])

    def test_new_course_questions_then_keep_settings(self):
        self.complete()
        p = self.base / 'project'; p.mkdir()
        prompt = learning_start.start(p, 'chat-a', self.home)
        result = learning_start.start(p, 'chat-a', self.home, choice='new', expected_revision=prompt['revision'])
        args = NS(**(vars(self.b) | {'course':Path(result['binding']['course_path'])}))
        expected = json.loads(Path(__file__).with_name('fixtures').joinpath('onboarding-approved.json').read_text())
        for number, key, value in [(1,'outcome',['topics']),(2,'subject','History'),
                                  (3,'self_assessment','unknown'),(8,'constraints',{'selected':['free_pace']})]:
            state = lt.preferences(args)
            self.assertEqual(state['onboarding_message'], expected['questions'][number-1])
            self.save(args, {key:value})
        state = lt.preferences(args)
        self.assertEqual(state['onboarding_mode'], 'course_preferences')
        self.assertEqual(state['onboarding_message'], 'Оставим твои прежние настройки обучения или хочешь их изменить для нового курса?')
        self.save(args, course_settings='keep')
        self.assertEqual(lt.preferences(args)['onboarding_mode'], 'ready')
        self.assertEqual(lt.preferences(args)['effective']['checks'], 'each_block')

    def test_change_only_selected_course_setting_does_not_publish_globally(self):
        self.complete()
        p = self.base / 'project'; p.mkdir()
        menu = learning_start.start(p, 'chat-a', self.home)
        r = learning_start.start(p, 'chat-a', self.home, choice='new', expected_revision=menu['revision'])
        a = NS(**(vars(self.b) | {'course':Path(r['binding']['course_path'])}))
        self.save(a, dict(outcome=['topics'],subject='History',self_assessment='unknown',constraints={'selected':['free_pace']}))
        self.save(a, course_settings='change', setting_question=['check_frequency'])
        message = lt.preferences(a)['onboarding_message']
        self.assertIn('Вопрос 6 из 8', message)
        self.save(a, {'check_frequency':'on_request'}, set=['checks=on_request', 'detail=concise'])
        self.assertEqual(lt.preferences(a)['onboarding_mode'], 'ready')
        self.assertEqual(lt.preferences(a)['effective']['checks'], 'on_request')
        self.assertEqual(lt.preferences(self.a)['effective']['checks'], 'each_block')
        self.assertEqual(lt.preferences(self.a)['effective']['detail'], 'balanced')

    def test_existing_course_can_continue_from_two_projects_and_change_chat_binding(self):
        self.complete()
        progress = lt.learner(self.a) / 'cursor.json'; progress.write_bytes(b'preserve checkpoint')
        for name in ('one','two'):
            p = self.base / name; p.mkdir()
            menu = learning_start.start(p, 'chat-a', self.home)
            select = learning_start.start(p, 'chat-a', self.home, choice='existing')
            self.assertEqual(select['start_action'], 'select_course')
            result = learning_start.start(p, 'chat-a', self.home, choice='existing', course=self.a.course,
                                         expected_revision=menu['revision'])
            self.assertEqual(result['binding']['course_path'], str(self.a.course))
            self.assertEqual(result['start_message'], 'Что именно хочешь изучать или делать в рамках этого курса в новом проекте?')
            self.assertEqual(learning_start.start(p, 'chat-a', self.home)['binding'], result['binding'])
            other = learning_start.start(p, 'chat-b', self.home)
            self.assertEqual(other['start_action'], 'choose_course')
            changed = learning_start.start(p, 'chat-a', self.home, choice='none', expected_revision=other['revision'])
            self.assertFalse(changed['enabled'])
        self.assertEqual(progress.read_bytes(), b'preserve checkpoint')

    def test_without_course_is_persisted_without_touching_learning_data(self):
        self.complete()
        p = self.base / 'free-chat'; p.mkdir()
        before = {str(x):x.read_bytes() for x in self.a.course.rglob('*') if x.is_file()}
        menu = learning_start.start(p, 'free', self.home)
        r = learning_start.start(p, 'free', self.home, choice='none', expected_revision=menu['revision'])
        self.assertFalse(r['enabled']); self.assertIsNone(r['binding'])
        self.assertEqual(learning_start.start(p, 'free', self.home)['start_action'], 'without_course')
        self.assertEqual(before, {str(x):x.read_bytes() for x in self.a.course.rglob('*') if x.is_file()})

    def test_missing_chat_id_is_not_replaced_with_fake_id(self):
        self.complete()
        with patch.dict(os.environ):
            os.environ.pop('CODEX_THREAD_ID', None)
            self.assertNotIn('CODEX_THREAD_ID', os.environ)
            r = learning_start.start(None, None, self.home)
            self.assertTrue(r['needs_chat_id'])
            with self.assertRaisesRegex(ValueError, 'chat ID'):
                learning_start.start(None, None, self.home, choice='new', expected_revision=r['revision'])

    def test_known_thread_environment_and_cas(self):
        self.complete()
        with patch.dict(os.environ, {'CODEX_THREAD_ID':'real-thread-fixture'}):
            menu = learning_start.start(None, None, self.home)
            learning_start.start(None, None, self.home, choice='none', expected_revision=menu['revision'])
            with self.assertRaisesRegex(ValueError, 'changed'):
                learning_start.start(None, None, self.home, choice='new', expected_revision=menu['revision'])

    def test_known_answers_and_subject_clarification_are_not_repeated(self):
        self.complete()
        p = self.base / 'project'; p.mkdir()
        menu = learning_start.start(p, 'chat-a', self.home)
        r = learning_start.start(p, 'chat-a', self.home, choice='new', expected_revision=menu['revision'])
        a = NS(**(vars(self.b) | {'course':Path(r['binding']['course_path'])}))
        self.save(a, dict(outcome=['topics'], subject={'value':'Программирование и разработка.',
            'area':'Программирование и разработка.', 'pending':True}))
        state = lt.preferences(a)
        self.assertEqual(state['onboarding_next'], 'subject')
        self.assertIn('Backend', state['onboarding_message'])
        self.save(a, dict(subject={'value':'Java Backend', 'area':'Программирование и разработка.',
            'direction':'Backend', 'focus':'Java'}, self_assessment='basics', constraints={'selected':['free_pace']}))
        self.assertEqual(lt.preferences(a)['onboarding_mode'], 'course_preferences')
        self.save(a, course_settings='keep')
        self.assertIsNone(lt.preferences(a)['onboarding_message'])

    def test_catalog_filters_identity_and_preserves_selected_binding_options(self):
        import connection
        self.complete()
        connection.dispatch(NS(project=self.home, action='connect', scope='chat', chat='known',
            expected_revision=0, course=self.a.course, profile='default', runtime=self.home/'old-runtime',
            model_path=self.home/'models/old', profiles_dir=None))
        connection.dispatch(NS(project=self.home, action='connect', scope='chat', chat='other-learner',
            expected_revision=1, course=self.b.course, profile='someone-else'))
        p = self.base / 'project'; p.mkdir()
        catalog = learning_start.start(p, 'chat-a', self.home, choice='existing')
        self.assertEqual([c['course_path'] for c in catalog['courses']], [str(self.a.course)])
        result = learning_start.start(p, 'chat-a', self.home, choice='existing', course=self.a.course,
                                     expected_revision=catalog['revision'])
        self.assertEqual(result['binding']['runtime'], str(self.home/'old-runtime'))
        self.assertEqual(result['binding']['model_path'], str(self.home/'models/old'))

    def test_cli_persists_choice_across_processes_and_rejects_invalid_course_settings(self):
        self.complete()
        p = self.base / 'проект с пробелом'; p.mkdir()
        def cli(*flags):
            r = subprocess.run([sys.executable, '-B', learning_start.__file__, '--project', str(p),
                '--chat', 'real-chat-fixture', '--home', str(self.home), *map(str, flags)],
                capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout)
        menu = cli()
        created = cli('--choice', 'new', '--expected-revision', menu['revision'])
        again = cli()
        self.assertEqual(again['binding'], created['binding'])
        a = NS(**(vars(self.b) | {'course':Path(created['binding']['course_path'])}))
        self.save(a, dict(outcome=['topics'], subject='History', self_assessment='unknown', constraints={'selected':['free_pace']}))
        path = lt.pref_file(a, 'profile'); before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'Invalid course setting'):
            self.save(a, course_settings='change', setting_question=['subject'])
        self.assertEqual(path.read_bytes(), before)
        free = cli('--choice', 'none', '--expected-revision', again['revision'])
        self.assertEqual(free['start_action'], 'without_course')
        self.assertEqual(cli()['start_action'], 'without_course')
