"""Known learner bindings form a read-only catalogue, not a global course scanner."""
import importlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import test_shared_learner as fixtures
import test_unit_learning as units
import adaptive_learning
import connection
import learning_intent
import learning_start


class CourseCatalog(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.SharedLearner();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def bind(self,root=None,profile='default',location=None,chat='known'):
        location=location or self.f.home
        return connection.dispatch(NS(project=location,action='connect',scope='chat',chat=chat,
            expected_revision=connection.status(location)['revision'],course=root or self.f.a.course,profile=profile))
    def read(self,**kw):
        args=dict(home=self.f.home,profile='default')|kw
        return importlib.import_module('course_catalog').list_courses(**args)
    def files(self):return {str(p):p.read_bytes() for p in self.f.base.rglob('*') if p.is_file()}
    def unit_course(self):
        u=units.UnitLearning();u.setUp();self.addCleanup(u.tearDown)
        self.bind(u.args().course);return u

    def test_list_shows_title_goal_and_verified_topic_without_writes(self):
        u=self.unit_course()
        learning_intent.commit(u.args(expected_revision=0,reason='Learner goal'),dict(goal=dict(description='Build a Java cache')))
        u.save(dict(recap='Traced keys',next_step='Practice replacement',pending_question='Why size one?'),checkpoint=True)
        before=self.files();course_before=u.path.read_bytes();value=self.read();row=value['courses'][0]
        self.assertTrue(value['valid']);self.assertEqual(row['title'],'Context')
        self.assertEqual(row['goal'],'Build a Java cache');self.assertEqual(row['goal_source'],'learning_intent')
        self.assertEqual(row['current_topic'],'Maps');self.assertEqual(row['learning_state'],'in_progress')
        self.assertTrue(row['study_next_required']);self.assertEqual(self.files(),before)
        self.assertEqual(u.path.read_bytes(),course_before)

    def test_saved_goal_answer_is_used_without_repeating_survey(self):
        self.f.save(self.f.a,dict(outcome=dict(value=['skill'],text='Learn to trace Java collections')))
        self.bind();before=self.files();row=self.read()['courses'][0]
        self.assertEqual(row['goal'],'Learn to trace Java collections')
        self.assertEqual(row['goal_source'],'onboarding_answer');self.assertIsNone(row['current_topic'])
        self.assertEqual(self.files(),before)

    def test_only_requested_learner_is_read_even_with_other_project_binding(self):
        self.bind(profile='alice',chat='alice');self.bind(self.f.b.course,profile='bob',chat='bob')
        project=self.f.base/'alice-project';project.mkdir()
        self.bind(profile='alice',location=project)
        original=adaptive_learning.study_next
        with patch.object(adaptive_learning,'study_next',wraps=original) as reader:
            before=self.files();value=self.read(profile='bob',project=project)
        self.assertEqual([c['title'] for c in value['courses']],['b'])
        self.assertTrue(all(call.args[0].profile=='bob' for call in reader.call_args_list))
        self.assertEqual(connection.status(project,'known')['binding']['profile'],'alice')
        self.assertEqual(self.files(),before)

    def test_unknown_identity_does_not_fall_back_to_another_learner(self):
        self.bind(profile='alice');before=self.files();value=self.read(profile=None)
        self.assertFalse(value['valid']);self.assertTrue(value['needs_profile'])
        self.assertEqual(value['courses'],[]);self.assertEqual(self.files(),before)

    def test_empty_and_missing_home_do_not_create_courses_or_marker(self):
        before=self.files();self.assertEqual(self.read()['courses'],[])
        missing=self.f.base/'missing';value=self.read(home=missing)
        self.assertTrue(value['valid']);self.assertEqual(value['courses'],[])
        self.assertEqual(value['choices'],learning_start.CHOICES)
        self.assertFalse(missing.exists());self.assertEqual(self.files(),before)

    def test_unavailable_and_damaged_metadata_stay_visible_with_good_course(self):
        self.bind(chat='a');self.bind(self.f.b.course,chat='b')
        path=self.f.a.course/'course.json'
        for raw in (None,b'[]',b'{bad'):
            if raw is None:path.unlink()
            else:path.write_bytes(raw)
            before=self.files();value=self.read()
            bad=next(r for r in value['courses'] if r['course_path']==str(self.f.a.course))
            self.assertFalse(bad['available']);self.assertTrue(bad['reason'])
            self.assertTrue(next(r for r in value['courses'] if r['title']=='b')['available'])
            self.assertEqual(self.files(),before)

    def metadata_changes_after_validation(self,raw):
        """A writer changes the file after course_ready, before the catalog reads it."""
        self.bind(chat='a');self.bind(self.f.b.course,chat='b')
        self.bind(self.f.b.course,profile='other',chat='other-learner')
        path=self.f.a.course/'course.json';original=path.read_bytes()
        ready=connection.course_ready;written=None
        def change(binding):
            nonlocal written
            ready(binding)
            if Path(binding['course_path'])==self.f.a.course:
                if raw is None:path.unlink()
                else:path.write_bytes(raw)
                written=self.files()
        try:
            with patch.object(connection,'course_ready',side_effect=change):value=self.read()
            self.assertEqual(self.files(),written)  # Only the simulated writer changed data.
            self.assertEqual(len(value['courses']),2)
            self.assertTrue(all(r['profile']=='default' for r in value['courses']))
            bad=next(r for r in value['courses'] if r['course_path']==str(self.f.a.course))
            good=next(r for r in value['courses'] if r['course_path']==str(self.f.b.course))
            self.assertFalse(value['valid']);self.assertFalse(bad['available']);self.assertFalse(bad['summary_valid'])
            self.assertTrue(bad['reason'])
            for key in ('title','course_id','goal','current_topic'):self.assertIsNone(bad[key],key)
            self.assertTrue(good['available']);self.assertTrue(good['summary_valid']);self.assertEqual(good['title'],'b')
        finally:path.write_bytes(original)

    def test_concurrent_metadata_replacement_has_no_invented_fields(self):
        for raw in (b'[]',b'null',b'"text"',b'{"title":"Unverified"}',
                    b'{"schema_version":1,"id":"","title":"Unverified"}',
                    b'{"schema_version":2,"id":"different","title":"Unverified"}'):
            with self.subTest(raw=raw):self.metadata_changes_after_validation(raw)

    def test_concurrent_metadata_deletion_keeps_other_course_and_read_only(self):
        self.metadata_changes_after_validation(None)

    def test_concurrent_metadata_corruption_keeps_other_course_and_read_only(self):
        self.metadata_changes_after_validation(b'{broken')

    def late_metadata_change(self,raw):
        """A separate writer acts after discovery, before study-next's actual read."""
        self.bind(chat='a');self.bind(self.f.b.course,chat='b')
        self.bind(self.f.b.course,profile='other',chat='other-learner')
        path=self.f.a.course/'course.json';original=path.read_bytes()
        next_step=adaptive_learning.study_next;written=None
        def change(args):
            nonlocal written
            if args.course==self.f.a.course:
                writer=subprocess.run([sys.executable,'-B','-c',
                    "from pathlib import Path;import sys;p=Path(sys.argv[1]);p.unlink() if sys.argv[2]=='delete' else p.write_bytes(bytes.fromhex(sys.argv[2]))",
                    str(path),'delete' if raw is None else raw.hex()],capture_output=True,text=True,timeout=20)
                self.assertEqual(writer.returncode,0,writer.stderr);written=self.files()
            return next_step(args)
        try:
            with patch.object(adaptive_learning,'study_next',side_effect=change):value=self.read()
            self.assertEqual(self.files(),written)  # The catalog never writes or repairs.
            self.assertEqual(len(value['courses']),2);self.assertFalse(value['valid'])
            self.assertTrue(all(row['profile']=='default' for row in value['courses']))
            bad=next(row for row in value['courses'] if row['course_path']==str(self.f.a.course))
            good=next(row for row in value['courses'] if row['course_path']==str(self.f.b.course))
            self.assertFalse(bad['summary_valid']);self.assertTrue(bad['reason'])
            self.assertEqual(bad['learning_state'],'incomplete')
            for key in ('goal','current_topic'):self.assertIsNone(bad[key])
            self.assertNotIn('Other learner',json.dumps(value))
            self.assertTrue(good['summary_valid']);self.assertEqual(good['title'],'b')
        finally:path.write_bytes(original)

    def test_late_metadata_list_does_not_abort_catalog(self):
        self.late_metadata_change(b'[]')

    def test_late_metadata_null_and_invalid_types_do_not_leak_data(self):
        for raw in (b'null',b'"text"',b'{"title":"Other learner"}',
                    b'{"schema_version":true,"id":"other","title":"Other learner"}',
                    b'{"schema_version":2,"id":"other","title":"Other learner"}',
                    b'{"schema_version":1,"id":"","title":"Other learner"}',
                    b'{"schema_version":1,"id":"other","title":[]}',
                    b'{"schema_version":1,"id":"other","title":" "}',
                    b'{"schema_version":1,"id":"other","title":"Other learner"}'):
            with self.subTest(raw=raw):self.late_metadata_change(raw)

    def test_late_metadata_deletion_does_not_hide_other_course(self):
        self.late_metadata_change(None)

    def test_late_metadata_invalid_json_does_not_hide_other_course(self):
        self.late_metadata_change(b'{broken')

    def test_study_next_rejects_invalid_metadata_before_loading_learning_data(self):
        self.bind();path=self.f.a.course/'course.json';path.write_bytes(b'[]');before=self.files()
        with patch('learning_tools.learner',side_effect=AssertionError('No progress reads for invalid metadata')):
            with self.assertRaisesRegex(ValueError,'Course metadata'):
                adaptive_learning.study_next(NS(course=self.f.a.course,profile='default'))
        self.assertEqual(self.files(),before)
        process=subprocess.run([sys.executable,'-B',str(Path(learning_start.__file__)),
            '--list-courses','--home',str(self.f.home),'--profile','default'],capture_output=True,text=True,timeout=20)
        self.assertEqual(process.returncode,0,process.stderr);self.assertNotIn('Traceback',process.stderr)
        listed=json.loads(process.stdout);self.assertFalse(listed['valid'])
        self.assertFalse(listed['courses'][0]['summary_valid']);self.assertEqual(self.files(),before)

    def test_valid_late_title_change_uses_fresh_metadata_without_writes(self):
        self.bind();path=self.f.a.course/'course.json';next_step=adaptive_learning.study_next;written=None
        def change(args):
            nonlocal written
            data=json.loads(path.read_text());data['title']='Updated during summary'
            path.write_text(json.dumps(data));written=self.files()
            return next_step(args)
        with patch.object(adaptive_learning,'study_next',side_effect=change):value=self.read()
        self.assertTrue(value['valid']);self.assertEqual(value['courses'][0]['title'],'Updated during summary')
        self.assertEqual(self.files(),written)

    def test_valid_concurrent_title_change_uses_current_verified_metadata(self):
        self.bind();path=self.f.a.course/'course.json';ready=connection.course_ready;written=None
        def rename(binding):
            nonlocal written
            ready(binding);data=json.loads(path.read_text());data['title']='Updated course'
            path.write_text(json.dumps(data));written=self.files()
        with patch.object(connection,'course_ready',side_effect=rename):value=self.read()
        self.assertTrue(value['valid']);self.assertEqual(value['courses'][0]['title'],'Updated course')
        self.assertEqual(self.files(),written)

    def test_bad_binding_source_does_not_hide_independent_project_course(self):
        self.bind();project=self.f.base/'project';project.mkdir()
        self.bind(self.f.b.course,location=project)
        (self.f.home/'.dosuti-mentor.json').write_text('{bad')
        before=self.files();value=self.read(project=project)
        self.assertFalse(value['valid']);self.assertTrue(value['errors'])
        self.assertEqual([c['title'] for c in value['courses']],['b']);self.assertEqual(self.files(),before)

    def test_progress_read_error_is_not_a_claim_of_completion(self):
        u=self.unit_course();u.save();u.path.write_text('{bad')
        before=self.files();row=self.read()['courses'][0]
        self.assertTrue(row['available']);self.assertFalse(row['summary_valid'])
        self.assertEqual(row['learning_state'],'incomplete');self.assertIsNone(row['current_topic'])
        self.assertEqual(self.files(),before)

    def test_one_unresolvable_binding_does_not_hide_another_course(self):
        self.bind(chat='a');self.bind(self.f.b.course,chat='b')
        original=connection.resolve
        def resolve(location,value):
            if value['course_path']==str(self.f.a.course):raise OSError('QA path resolution failed')
            return original(location,value)
        before=self.files()
        with patch.object(connection,'resolve',side_effect=resolve):value=self.read()
        self.assertTrue(next(r for r in value['courses'] if r['title']=='b')['summary_valid'])
        self.assertTrue(any(not r['available'] for r in value['courses']))
        self.assertEqual(self.files(),before)

    def test_replaced_course_identity_is_not_combined_with_old_title(self):
        self.bind();original=adaptive_learning.study_next;written=None
        def replace(args):
            nonlocal written
            path=self.f.a.course/'course.json';data=json.loads(path.read_text())
            data.update(id='replacement-course',title='Different course');path.write_text(json.dumps(data))
            written=self.files()
            return original(args)
        with patch.object(adaptive_learning,'study_next',side_effect=replace):value=self.read()
        row=value['courses'][0]
        self.assertFalse(row['summary_valid']);self.assertIsNone(row['current_topic'])
        self.assertIsNone(row['goal']);self.assertIn('identity changed',row['reason'])
        self.assertEqual(self.files(),written)

    def test_multiple_continuations_do_not_choose_last_lesson(self):
        u=self.unit_course()
        for session in ('one','two'):
            u.save(dict(recap=session,next_step='Practice',pending_question=None),checkpoint=True,session=session)
        row=self.read()['courses'][0]
        self.assertTrue(row['continuation_choice_required']);self.assertIsNone(row['current_topic'])
        self.assertEqual(row['continuation_count'],2)

    def test_paging_only_loads_progress_for_visible_courses(self):
        self.bind(chat='a');self.bind(self.f.b.course,chat='b')
        original=adaptive_learning.study_next
        with patch.object(adaptive_learning,'study_next',wraps=original) as reader:
            value=self.read(limit=1,offset=1)
        self.assertEqual(value['total'],2);self.assertEqual([r['title'] for r in value['courses']],['b'])
        self.assertEqual(reader.call_count,1)
        for options in (dict(limit=0),dict(limit=21),dict(offset=-1)):
            with self.subTest(options=options),self.assertRaises(ValueError):self.read(**options)

    def test_listing_never_initializes_seeds_or_changes_a_binding(self):
        self.bind();before=self.files()
        with (patch.object(learning_start,'start',side_effect=AssertionError('No start mutation')),
              patch('learner_preferences.seed',side_effect=AssertionError('No seed mutation'))):
            self.assertTrue(self.read()['valid'])
        self.assertEqual(self.files(),before)

    def test_legacy_binding_is_read_without_rewrite(self):
        project=self.f.base/'legacy';project.mkdir()
        path=project/'.lecture-tutor.json';path.write_text(json.dumps(dict(course_path=str(self.f.a.course))))
        before=self.files();value=self.read(project=project)
        self.assertEqual([r['title'] for r in value['courses']],['a']);self.assertEqual(self.files(),before)

    def test_same_binding_is_deduplicated_but_different_settings_are_not_merged(self):
        self.bind();project=self.f.base/'aliases';project.mkdir();self.bind(location=project)
        self.assertEqual(self.read(project=project)['total'],1)
        connection.dispatch(NS(project=project,action='connect',scope='chat',chat='alternative',
            expected_revision=connection.status(project)['revision'],course=self.f.a.course,
            profile='default',runtime=self.f.home/'other-runtime'))
        before=self.files();value=self.read(project=project)
        self.assertEqual(value['total'],2)
        self.assertTrue(all(row['requires_binding_choice'] for row in value['courses']))
        self.assertEqual(self.files(),before)

    def test_catalog_does_not_weaken_existing_connection_cas(self):
        self.f.complete();self.bind();project=self.f.base/'concurrent';project.mkdir()
        menu=learning_start.start(project,'reader',self.f.home)
        self.read(project=project)
        self.bind(self.f.b.course,chat='other-writer')
        # The existing writer lock is persistent infrastructure, not course data.
        from storage_lock import file_lock
        with file_lock(self.f.home/'.learning-start.lock'):pass
        before=self.files()
        with self.assertRaisesRegex(ValueError,'Connections changed'):
            learning_start.start(project,'reader',self.f.home,choice='existing',course=self.f.a.course,
                expected_revision=menu['revision'])
        self.assertEqual(self.files(),before)

    def test_explicit_choices_reuse_existing_start_and_recommend_study_next(self):
        self.f.complete();self.bind();project=self.f.base/'choices';project.mkdir()
        state=learning_start.start(project,'new-chat',self.f.home)
        selected=learning_start.start(project,'new-chat',self.f.home,choice='existing',course=self.f.a.course,
            expected_revision=state['revision'])
        self.assertEqual(selected['next_action'],'study_next')
        self.assertEqual(selected['binding']['course_path'],str(self.f.a.course))
        state=learning_start.start(project,'new-chat',self.f.home)
        new=learning_start.start(project,'new-chat',self.f.home,choice='new',expected_revision=state['revision'])
        self.assertEqual(new['start_action'],'new_course')
        none=learning_start.start(project,'new-chat',self.f.home,choice='none',expected_revision=new['revision'])
        self.assertEqual(none['start_action'],'without_course');self.assertIsNone(none['binding'])

    def test_cli_list_is_read_only_and_rejects_mixed_mutation(self):
        self.bind();script=Path(learning_start.__file__)
        command=[sys.executable,'-B',str(script),'--home',str(self.f.home),'--profile','default','--list-courses']
        before=self.files();r=subprocess.run(command,capture_output=True,text=True,timeout=20)
        self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(json.loads(r.stdout)['total'],1)
        r=subprocess.run(command+['--choice','new'],capture_output=True,text=True,timeout=20)
        self.assertNotEqual(r.returncode,0);self.assertEqual(self.files(),before)
