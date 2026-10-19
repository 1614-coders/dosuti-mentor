"""Profile isolation, concurrent cursors, grounded navigation and source drift."""
from concurrent.futures import ThreadPoolExecutor
import contextlib,io,json,shutil,sys
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course,learning_tools as lt
import test_context

class LearningTools(unittest.TestCase):
    def setUp(self):
        self.fixture=test_context.ContextRegression();self.fixture.setUp();self.a=self.fixture.args
        self.base=self.fixture.directory;self.fragment=self.fixture.add('a',0,60,[(5,15,'HashMap stores keys and values'),(20,30,'Objects and interfaces')])
    def tearDown(self):self.fixture.tearDown()
    def args(self,**kw):return NS(**(vars(self.a)|kw))
    def settings(self,**kw):return lt.settings(self.args(scope='profile',set=[],reset=False,onboarding=None,**kw))
    def write(self,name,value):
        p=self.base/name;p.write_text(json.dumps(value));return p
    def prefs(self,profile='default',scope='profile',values=(),**kw):
        return lt.settings(self.args(profile=profile,scope=scope,set=list(values),reset=False,onboarding=None,**kw))
    def state(self):return dict(lesson='lesson-01',time=10,topic='Maps',recap='Discussed keys',next_step='Ask about duplicate keys',pending_question='Can a key repeat?',misconceptions=['Confuses key with value'])
    def save_cursor(self,revision=0,profile='default',value=None):
        return lt.resume(self.args(profile=profile,state_file=self.write('cursor-input.json',value or self.state()),expected_revision=revision))
    def card(self):return dict(id='maps',title='HashMap',summary='Assistant explanation of maps',keywords=['словарь','хешмапа'],start=5,end=15,code_files=[])
    def add_topic(self,value=None):return lt.topic(self.args(topic_file=self.write('topic-input.json',value or self.card())))
    def test_defaults_offer_onboarding(self):
        r=lt.preferences(self.a);self.assertEqual(r['onboarding'],'not_asked');self.assertEqual(r['effective']['format'],'steps')
    def test_settings_profile_isolation(self):
        self.prefs(values=['style=partner']);self.assertEqual(lt.preferences(self.a)['effective']['style'],'partner');self.assertEqual(lt.preferences(self.args(profile='bob'))['effective']['style'],'teacher')
    def test_course_override_and_reset(self):
        self.prefs(values=['detail=detailed']);self.prefs(scope='course',values=['detail=concise']);self.assertEqual(lt.preferences(self.a)['effective']['detail'],'concise')
        lt.settings(self.args(scope='course',set=[],reset=True,onboarding=None));self.assertEqual(lt.preferences(self.a)['effective']['detail'],'detailed')
    def test_shared_preferences_across_courses(self):
        shared=self.base/'profiles';self.prefs(values=['format=file'],profiles_dir=shared)
        other=self.base/'second';other.mkdir();(other/'course.json').write_text('{}')
        self.assertEqual(lt.preferences(self.args(course=other,profiles_dir=shared))['effective']['format'],'file')
    def test_skipped_onboarding_persists(self):
        lt.settings(self.args(scope='profile',set=[],reset=False,onboarding='skipped'));self.assertEqual(lt.preferences(self.a)['onboarding'],'skipped')
    def test_onboarding_pause_and_resume_in_new_chat(self):
        lt.settings(self.args(scope='profile',set=['format=blocks'],reset=False,onboarding='in_progress'))
        result=lt.preferences(self.args(session='another-chat'))
        self.assertEqual(result['onboarding'],'in_progress');self.assertEqual(result['effective']['format'],'blocks')
        self.prefs(values=['detail=concise'])
        self.assertEqual(lt.preferences(self.a)['onboarding'],'in_progress')
        self.onboarding(self.all_onboarding_answers())
        self.assertEqual(lt.preferences(self.args(session='third-chat'))['onboarding'],'complete')
        self.assertNotIn('session',lt.read_json(lt.pref_file(self.a,'profile')))
    def test_legacy_onboarding_profiles_read_without_rewriting(self):
        p=lt.pref_file(self.a,'profile');p.parent.mkdir(parents=True)
        for state in (None,'not_asked','complete','skipped'):
            with self.subTest(state=state):
                value=dict(values=dict(style='partner'),revision=2)
                if state is not None:value['onboarding']=state
                p.write_text(json.dumps(value));before=p.read_bytes()
                result=lt.preferences(self.a)
                self.assertEqual(result['onboarding'],state or 'not_asked')
                if state in ('complete','skipped'):
                    self.assertEqual(result['onboarding_pending'],[])
                    self.assertIsNone(result['onboarding_next'])
                else:self.assertEqual(result['onboarding_next'],'outcome')
                self.assertEqual(p.read_bytes(),before)
    def test_direct_lecture_does_not_require_or_start_onboarding(self):
        p=lt.pref_file(self.a,'profile')
        for state in ('not_asked','in_progress','complete','skipped'):
            with self.subTest(state=state):
                if state=='complete':self.onboarding(self.all_onboarding_answers())
                elif state!='not_asked':lt.settings(self.args(scope='profile',set=[],reset=False,onboarding=state))
                before=p.read_bytes() if p.exists() else None
                output=io.StringIO()
                with contextlib.redirect_stdout(output):course.context(self.args(time=10,window=30))
                result=json.loads(output.getvalue())
                self.assertEqual(result['preferences']['onboarding'],state)
                self.assertTrue(result['speech'])
                self.assertEqual(p.read_bytes() if p.exists() else None,before)
                self.assertFalse(list(lt.learner(self.a).glob('*intent*')))
    def test_cli_accepts_in_progress_onboarding(self):
        output=io.StringIO()
        with patch.object(sys,'argv',['course.py','settings','--course',str(self.a.course),'--onboarding','in_progress']),contextlib.redirect_stdout(output):
            course.main()
        self.assertEqual(json.loads(output.getvalue())['onboarding'],'in_progress')
    def test_invalid_onboarding_not_written(self):
        self.prefs(values=['style=partner']);p=lt.pref_file(self.a,'profile');before=p.read_bytes()
        with self.assertRaisesRegex(ValueError,'Invalid onboarding'):
            lt.settings(self.args(scope='profile',set=[],reset=False,onboarding='paused'))
        self.assertEqual(p.read_bytes(),before)
    def test_onboarding_belongs_to_profile(self):
        with self.assertRaisesRegex(ValueError,'belongs to the profile'):
            lt.settings(self.args(scope='course',set=[],reset=False,onboarding='in_progress'))
    def onboarding(self,answers=None,skip=(),**kw):
        return self.settings(onboarding_file=self.write('onboarding-input.json',dict(answers=answers or {})),
                             onboarding_skip=list(skip),**kw)
    def all_onboarding_answers(self):
        return dict(outcome=['topics','skill'],subject='Музыка',self_assessment='basics',
                    learning_format='step_by_step',assistant_role='mentor',check_frequency='adaptive',
                    explanation_detail='detailed',constraints=dict(selected=['study_time'],details='Два вечера'))
    def test_eight_answers_complete_without_assessments_or_preference_mapping(self):
        expected=self.all_onboarding_answers();result=self.onboarding(expected)
        self.assertEqual(result['onboarding'],'complete');self.assertIsNone(result['onboarding_next'])
        self.assertEqual({q:r['value'] for q,r in result['onboarding_answers'].items()},expected)
        self.assertEqual(result['effective'],lt.DEFAULTS)
        with course.db(self.a) as db:self.assertEqual(db.execute('SELECT count(*) FROM progress').fetchone()[0],0)
        self.assertFalse(list(lt.learner(self.a).glob('*intent*')))
        self.assertFalse(list(lt.learner(self.a).glob('*diagnostic*')))
    def test_individual_skip_resume_order_and_edit_in_new_chat(self):
        self.onboarding(dict(outcome=['course']),skip=['subject'])
        result=lt.preferences(self.args(session='new-chat'))
        self.assertEqual(result['onboarding_next'],'self_assessment')
        self.assertEqual(result['onboarding_answers']['subject'],dict(status='skipped'))
        self.assertEqual(result['onboarding'],'in_progress')
        self.prefs(values=['style=partner'])
        result=self.onboarding(dict(subject='История'))
        self.assertEqual(result['onboarding_answers']['subject']['value'],'История')
        self.assertEqual(result['onboarding_answers']['outcome']['value'],['course'])
        self.assertEqual(result['effective']['style'],'partner')
    def test_whole_skip_preserves_answers_and_can_change_later(self):
        self.onboarding(dict(subject='История'))
        result=lt.settings(self.args(scope='profile',set=[],reset=False,onboarding='skipped'))
        self.assertEqual(result['onboarding'],'skipped');self.assertEqual(result['onboarding_pending'],[])
        self.assertEqual(result['onboarding_answers']['subject']['value'],'История')
        self.assertEqual(result['onboarding_answers']['outcome'],dict(status='skipped'))
        result=self.onboarding(dict(assistant_role='partner'))
        self.assertEqual(result['onboarding_answers']['assistant_role']['value'],'partner')
    def test_onboarding_comment_and_profile_isolation(self):
        path=self.write('onboarding-input.json',dict(comment='Использовать примеры из музыки'))
        self.settings(onboarding_file=path)
        self.assertEqual(lt.preferences(self.args(session='new-chat'))['onboarding_comment'],'Использовать примеры из музыки')
        self.assertEqual(lt.preferences(self.args(profile='bob'))['onboarding_answers'],{})
    def test_onboarding_invalid_updates_preserve_saved_file(self):
        self.onboarding(dict(subject='История'));path=lt.pref_file(self.a,'profile');before=path.read_bytes()
        invalid=[dict(outcome='topics'),dict(outcome=['topics','topics']),dict(subject=' '),
                 dict(self_assessment=True),dict(learning_format='unknown'),dict(assistant_role='reviewer'),
                 dict(check_frequency='sometimes'),dict(explanation_detail=[]),dict(constraints=[]),
                 dict(constraints=dict(selected=['deadline'],details=42)),dict(progress='confirmed')]
        for answers in invalid:
            with self.subTest(answers=answers),self.assertRaises(ValueError):self.onboarding(answers)
            self.assertEqual(path.read_bytes(),before)
        for skip in (['unknown'],['subject','subject']):
            with self.subTest(skip=skip),self.assertRaises(ValueError):self.onboarding(skip=skip)
            self.assertEqual(path.read_bytes(),before)
        with self.assertRaises(ValueError):self.onboarding(dict(subject='Музыка'),skip=['subject'])
        with self.assertRaises(ValueError):lt.settings(self.args(scope='course',set=[],reset=False,onboarding=None,onboarding_skip=['subject']))
        self.assertEqual(path.read_bytes(),before)
    def test_corrupt_onboarding_records_are_not_overwritten(self):
        p=lt.pref_file(self.a,'profile');p.parent.mkdir(parents=True)
        for value in ({'unknown':dict(status='skipped')},{'subject':dict(status='answered')},
                      {'subject':dict(status='skipped',value='invented')}):
            p.write_text(json.dumps(dict(values={},onboarding_answers=value)));before=p.read_bytes()
            with self.assertRaises(ValueError):self.prefs(values=['style=partner'])
            self.assertEqual(p.read_bytes(),before)
    def test_direct_task_preserves_partial_answer_records(self):
        self.onboarding(dict(outcome=['course']),skip=['subject'])
        p=lt.pref_file(self.a,'profile');before=p.read_bytes()
        output=io.StringIO()
        with contextlib.redirect_stdout(output):course.context(self.args(time=10,window=30))
        result=json.loads(output.getvalue())
        self.assertTrue(result['speech']);self.assertEqual(p.read_bytes(),before)
        self.assertEqual(result['preferences']['onboarding_next'],'self_assessment')
    def test_shared_onboarding_and_individual_skip_completion(self):
        shared=self.base/'shared-onboarding'
        self.onboarding(dict(subject='Музыка'),profiles_dir=shared)
        result=self.settings(profiles_dir=shared,onboarding_skip=[q for q in lt.ONBOARDING_QUESTIONS if q!='subject'])
        self.assertEqual(result['onboarding'],'complete')
        other=self.base/'other-onboarding-course';other.mkdir();(other/'course.json').write_text('{}')
        self.assertEqual(lt.preferences(self.args(course=other,profiles_dir=shared))['onboarding_answers'],result['onboarding_answers'])
    def test_empty_new_onboarding_cannot_complete(self):
        p=lt.pref_file(self.a,'profile')
        with self.assertRaisesRegex(ValueError,'Cannot complete'):
            lt.settings(self.args(scope='profile',set=[],reset=False,onboarding='complete'))
        self.assertFalse(p.exists())
        self.assertEqual(lt.preferences(self.a)['onboarding_next'],'outcome')
    def test_legacy_complete_settings_edit_does_not_restart_onboarding(self):
        p=lt.pref_file(self.a,'profile');p.parent.mkdir(parents=True)
        p.write_text(json.dumps(dict(values={},onboarding='complete')))
        result=self.prefs(values=['style=partner'])
        self.assertEqual(result['onboarding_pending'],[]);self.assertIsNone(result['onboarding_next'])
        self.assertEqual(result['onboarding'],'complete')
    def test_premature_complete_and_invalid_update_metadata_rejected(self):
        self.onboarding(dict(subject='Музыка'));p=lt.pref_file(self.a,'profile');before=p.read_bytes()
        with self.assertRaises(ValueError):
            lt.settings(self.args(scope='profile',set=[],reset=False,onboarding='complete'))
        for update in (dict(comment=3),dict(answers=[]),dict(progress='confirmed'),dict(answers=dict(constraints=dict(details='')))):
            with self.subTest(update=update),self.assertRaises(ValueError):
                self.settings(onboarding_file=self.write('invalid-onboarding.json',update))
            self.assertEqual(p.read_bytes(),before)
    def test_cli_records_answer_and_individual_skip(self):
        path=self.write('onboarding-input.json',dict(answers=dict(outcome=['skill'])))
        output=io.StringIO()
        with patch.object(sys,'argv',['course.py','settings','--course',str(self.a.course),
                          '--onboarding-file',str(path),'--onboarding-skip','subject']),contextlib.redirect_stdout(output):
            course.main()
        self.assertEqual(json.loads(output.getvalue())['onboarding_next'],'self_assessment')
    def test_cannot_disable_evidence_checks(self):
        with self.assertRaises(ValueError):self.prefs(values=['verify_sources=false'])
        with self.assertRaises(ValueError):self.prefs(values=['style=anything'])
    def test_concurrent_setting_updates_preserved(self):
        with ThreadPoolExecutor(2) as pool:list(pool.map(lambda v:self.prefs(values=[v]),['style=partner','detail=detailed']))
        r=lt.preferences(self.a);self.assertEqual(r['effective']['style'],'partner');self.assertEqual(r['effective']['detail'],'detailed')
    def test_profile_traversal_rejected(self):
        with self.assertRaises(ValueError):lt.preferences(self.args(profile='../bob'))
    def test_corrupt_preferences_not_overwritten(self):
        p=lt.pref_file(self.a,'profile');p.parent.mkdir(parents=True);p.write_text('{"values":{},"revision":"bad"}')
        before=p.read_bytes()
        with self.assertRaises(ValueError):self.prefs(values=['style=partner'])
        self.assertEqual(p.read_bytes(),before)
    def test_symlink_profile_rejected(self):
        p=self.a.course/'learners';p.mkdir();(p/'bob').mkdir();(p/'default').symlink_to(p/'bob',target_is_directory=True)
        with self.assertRaises(ValueError):self.prefs(values=['style=partner'])
    def test_cursor_saves_question_without_progress(self):
        r=self.save_cursor();self.assertTrue(r['valid']);self.assertEqual(r['state']['pending_question'],'Can a key repeat?')
        with course.db(self.a) as db:self.assertEqual(db.execute('SELECT count(*) FROM progress').fetchone()[0],0)
        self.assertIsNone(lt.cursor(self.args(profile='bob'))['state'])
    def test_cursor_stale_revision_rejected(self):
        self.save_cursor()
        with self.assertRaises(ValueError):self.save_cursor()
        self.assertEqual(self.save_cursor(1)['revision'],2)
    def test_two_chats_cannot_overwrite_same_revision(self):
        path=self.write('cursor-input.json',self.state())
        def save(_):
            try:
                lt.resume(self.args(state_file=path,expected_revision=0));return True
            except ValueError:return False
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(save,range(2)))
        self.assertEqual(sorted(results),[False,True]);self.assertEqual(lt.cursor(self.a)['revision'],1)
    def test_preferences_survive_move(self):
        self.prefs(values=['style=reviewer']);new=self.base/'moved';shutil.move(self.a.course,new);self.a.course=new
        self.assertEqual(lt.preferences(self.a)['effective']['style'],'reviewer')
    def test_topic_survives_disjoint_new_preparation(self):
        self.add_topic();self.fixture.add('b',60,90,[(65,70,'New segment')])
        result=lt.topic(self.args(topic_file=None));self.assertFalse(result['topics'][0]['stale'])
    def test_cursor_rejects_unprepared_time(self):
        value=self.state();value['time']=80
        with self.assertRaises(ValueError):self.save_cursor(value=value)
    def test_cursor_rejects_nan(self):
        value=self.state();value['time']=float('nan')
        with self.assertRaises(ValueError):self.save_cursor(value=value)
    def test_cursor_invalidated_on_source_change(self):
        self.save_cursor();self.fixture.video.write_bytes(b'changed');self.assertFalse(lt.cursor(self.a)['valid'])
    def test_cursor_survives_course_move(self):
        self.save_cursor();new=self.base/'moved';shutil.move(self.a.course,new);self.a.course=new
        self.assertTrue(lt.cursor(self.a)['valid'])
    def test_topic_requires_full_coverage(self):
        v=self.card();v['end']=80
        with self.assertRaises(ValueError):self.add_topic(v)
    def test_topic_and_speech_search(self):
        self.add_topic();r=lt.search(self.args(query='словарь',limit=10));self.assertEqual(r['results'][0]['kind'],'topic')
        r=lt.search(self.args(query='keys',limit=10));self.assertEqual(r['results'][0]['start'],5)
    def test_search_empty_and_limit_rejected(self):
        for q,n in [('',10),('keys',0),('keys',51)]:
            with self.assertRaises(ValueError):lt.search(self.args(query=q,limit=n))
    def test_search_corrupt_packet_not_used(self):
        (self.fragment/'tutor_packet.json').write_text('{}');r=lt.search(self.args(query='keys',limit=10));self.assertFalse(r['results']);self.assertTrue(r['warnings'])
    def test_search_changed_source_not_used(self):
        self.fixture.video.write_bytes(b'changed');r=lt.search(self.args(query='keys',limit=10));self.assertFalse(r['results']);self.assertTrue(r['warnings'])
    def test_topic_unknown_code_file_rejected(self):
        v=self.card();v['code_files']=['Missing.java']
        with self.assertRaises(ValueError):self.add_topic(v)
    def attach_code(self,lesson,files):
        path=self.base/('code-'+lesson);path.mkdir(exist_ok=True)
        for name,content in files.items():(path/name).write_text(content)
        with contextlib.redirect_stdout(io.StringIO()):course.attach(self.args(lesson=lesson,kind='code',source=str(path),none=False,ref=None))
    def test_project_map_diffs_and_candidate_edges(self):
        self.attach_code('lesson-01',{'A.java':'class A { B b; }','B.java':'class B {}'})
        with contextlib.redirect_stdout(io.StringIO()):course.add_lesson(self.args(lesson='second',title='Second',order=2))
        self.attach_code('second',{'A.java':'class A {}','C.java':'class C {}'})
        r=lt.project_map(self.a);self.assertEqual(r['lessons'][0]['dependencies'][0]['to_file'],'B.java')
        self.assertEqual({v['status'] for v in r['lessons'][1]['changes']},{'added','removed','modified'})
        self.assertEqual(r['lessons'][1]['lecture_match'],'unverified')
    def test_project_map_tampered_snapshot_rejected(self):
        self.attach_code('lesson-01',{'A.java':'class A {}'});d=course.read(self.fixture.lesson/'lesson.json');f=Path(d['code']['snapshot'])/'A.java';f.chmod(0o600);f.write_text('bad')
        with self.assertRaises(ValueError):lt.project_map(self.a)
    def test_code_replacement_marks_topic_stale(self):
        self.attach_code('lesson-01',{'A.java':'class A {}'});v=self.card();v['code_files']=['A.java'];self.add_topic(v)
        self.attach_code('lesson-01',{'A.java':'class A { int x; }'})
        result=lt.topic(self.args(topic_file=None));self.assertTrue(result['topics'][0]['stale'])
