"""Preference and evidence contracts, not a test of a model's natural dialogue."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import test_lesson_context as fixtures
import learning_tools as lt

PLUGIN=Path(__file__).resolve().parents[1]

class LessonPresentation(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.LessonContext();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.u=self.f.f

    def prefs(self,**kw):return lt.preferences(self.u.f.args(**kw))
    def settings(self,values=(),scope='profile',reset=False):
        return lt.settings(self.u.f.args(scope=scope,set=list(values),reset=reset,onboarding=None))
    def onboarding(self,value):
        p=self.u.f.f.directory/'presentation-onboarding.json'
        p.write_text(json.dumps(dict(answers=dict(check_frequency=value))))
        return lt.settings(self.u.f.args(scope='profile',set=[],reset=False,onboarding=None,onboarding_file=p))
    def files(self):
        root=self.u.args().course
        return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}

    def test_adaptive_default_without_writing_or_starting_onboarding(self):
        before=self.files();state=self.prefs()
        self.assertEqual(state['effective']['checks'],'adaptive')
        self.assertEqual(state['checks_source'],'default')
        self.assertFalse(state['checks_unresolved'])
        self.assertEqual(state['onboarding'],'not_asked')
        self.assertEqual(self.files(),before)

    def test_canonical_onboarding_choices_are_used_without_rewriting_values(self):
        for mode in ('adaptive','each_block','end','on_request'):
            with self.subTest(mode=mode):
                state=self.onboarding(mode)
                self.assertEqual(state['effective']['checks'],mode)
                self.assertEqual(state['checks_source'],'onboarding')
                self.assertFalse(state['checks_unresolved'])
                self.assertEqual(state['layers']['profile']['values'],{})
                self.assertEqual(state['onboarding_answers']['check_frequency']['value'],mode)

    def test_explicit_profile_and_course_override_onboarding_and_reset_reveals_it(self):
        self.onboarding('end');self.settings(['checks=each_block'])
        state=self.prefs();self.assertEqual(state['effective']['checks'],'each_block')
        self.assertEqual(state['checks_source'],'profile')
        self.settings(['checks=on_request'],scope='course')
        state=self.onboarding('adaptive')
        self.assertEqual(state['effective']['checks'],'on_request');self.assertEqual(state['checks_source'],'course')
        self.settings(scope='course',reset=True)
        self.assertEqual(self.prefs()['effective']['checks'],'each_block')
        self.settings(reset=True)
        self.assertEqual(self.prefs()['effective']['checks'],'adaptive')
        self.assertEqual(self.prefs()['checks_source'],'onboarding')

    def test_legacy_stored_each_block_is_preserved_byte_for_byte(self):
        path=lt.pref_file(self.u.args(),'profile');path.parent.mkdir(parents=True,exist_ok=True)
        raw=json.dumps(dict(revision=3,values=dict(checks='each_block',detail='detailed'),onboarding='complete'))
        path.write_text(raw);before=path.read_bytes()
        self.assertEqual(self.prefs(session='new-chat')['effective']['checks'],'each_block')
        self.assertEqual(self.prefs()['checks_source'],'profile')
        self.assertEqual(path.read_bytes(),before)

    def test_free_mixed_scheduled_and_pending_answers_are_not_reinterpreted(self):
        for answer in (dict(text='Проверяй после сложных мест'),['each_block','end'],'scheduled',
                       dict(value='each_block',text='Ещё думаю',pending=True)):
            with self.subTest(answer=answer):
                state=self.onboarding(answer);before=self.files();saved=state['onboarding_answers']['check_frequency']
                self.assertEqual(state['effective']['checks'],'adaptive')
                self.assertEqual(state['checks_source'],'default');self.assertTrue(state['checks_unresolved'])
                self.assertEqual(self.prefs()['onboarding_answers']['check_frequency'],saved)
                self.assertEqual(self.files(),before)
        self.settings(['checks=end'])
        self.assertFalse(self.prefs()['checks_unresolved'])
        self.assertEqual(self.prefs()['effective']['checks'],'end')

    def test_single_selected_option_is_supported_and_skipped_is_only_default(self):
        self.assertEqual(self.onboarding(['on_request'])['effective']['checks'],'on_request')
        state=lt.settings(self.u.f.args(scope='profile',set=[],reset=False,onboarding=None,onboarding_skip=['check_frequency']))
        self.assertEqual(state['effective']['checks'],'adaptive')
        self.assertFalse(state['checks_unresolved'])

    def test_adaptive_can_be_explicitly_saved_and_restored_in_another_process(self):
        self.settings(['checks=adaptive','detail=detailed'])
        before=self.files()
        process=subprocess.run([sys.executable,'-B',str(PLUGIN/'scripts/course.py'),'settings','--course',str(self.u.args().course),
            '--profile','default'],capture_output=True,text=True,timeout=20,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(process.returncode,0,process.stderr)
        state=json.loads(process.stdout)
        self.assertEqual(state['effective']['checks'],'adaptive');self.assertEqual(state['checks_source'],'profile')
        self.assertEqual(state['effective']['detail'],'detailed');self.assertEqual(self.files(),before)

    def test_all_five_lesson_contexts_supply_saved_goal_plan_and_preferences(self):
        self.settings(['checks=end'])
        for kind in ('new_topic','continuation','review','practice','gap_work'):
            with self.subTest(kind=kind):
                created=self.f.create(self.f.value(kind));identity=self.f.context(created)['value']['id']
                before=self.files();state=self.u.read(lesson_id=identity,session='new-chat')
                self.assertEqual(state['learning']['lesson_context']['event']['value']['kind'],kind)
                self.assertEqual(state['learning']['lesson_context']['event']['value']['goal'],self.f.value()['goal'])
                self.assertEqual(state['learning']['lesson_context']['event']['value']['plan'],self.f.value()['plan'])
                self.assertEqual(state['preferences']['effective']['checks'],'end')
                self.assertEqual(state['learning']['attempt_count'],0)
                self.assertEqual(self.files(),before)

    def test_acknowledgments_checkpoint_and_explanation_do_not_confirm_knowledge(self):
        identity=self.f.context(self.f.create())['value']['id']
        self.u.save(self.u.event(stage='explained',question=None,answer=None,criteria=[],
            details='Explained replacement of a duplicate map key'),lesson_id=identity)
        for ack in ('Понятно','Ясно','Окей, дальше','Пропусти знакомую часть','Перейдём к практике'):
            state=self.u.save(dict(recap='Explained key replacement. Learner: '+ack,
                next_step='Continue with the requested part',pending_question=None),checkpoint=True,lesson_id=identity)
            self.assertEqual(state['learning']['attempt_count'],0)
            self.assertEqual(state['learning']['criteria'],{'1':'not_assessed'})
            self.assertFalse(state['learning']['all_criteria_demonstrated'])
            self.assertEqual(state['learning_progress']['demonstrated_units'],[])
            self.assertEqual(state['learning']['lesson_state']['status'],'active')

if __name__=='__main__':unittest.main()
