"""Course scoping must be explicit, isolated, revision checked and non-destructive."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import connection as conn
import course
import test_learning_tools


class Connections(unittest.TestCase):
    def setUp(self):
        self.f=test_learning_tools.LearningTools();self.f.setUp()
        self.project=self.f.base/'project';self.project.mkdir()
        self.settings=self.project/'.dosuti-mentor.json'
    def tearDown(self):self.f.tearDown()
    def args(self,**kw):return self.f.args(project=self.project,**kw)
    def read(self,chat=None):return conn.dispatch(self.args(action='status',chat=chat))
    def update(self,action='connect',scope='project',chat=None,revision=None,**kw):
        if revision is None:revision=self.read()['revision']
        return conn.dispatch(self.args(action=action,scope=scope,chat=chat,expected_revision=revision,**kw))

    def test_initial_read_does_not_create_settings(self):
        self.assertFalse(self.read()['enabled']);self.assertFalse(self.settings.exists())

    def test_renamed_plugin_reads_and_updates_old_connection(self):
        self.update(scope='chat',chat='old',profile='saved')
        data=json.loads(self.settings.read_text())
        data[conn.LEGACY_KEY]=data.pop(conn.KEY)
        previous=self.project/'.lecture-tutor.json'
        previous.write_text(json.dumps(data));self.settings.unlink()
        before=previous.read_bytes()
        self.assertEqual(self.read('old')['binding']['profile'],'saved')
        self.assertEqual(before,previous.read_bytes())
        self.update(scope='chat',chat='new',profile='second')
        self.assertFalse(self.settings.exists())
        self.assertEqual(self.read('old')['binding']['profile'],'saved')
        self.assertEqual(self.read('new')['binding']['profile'],'second')
        self.assertIn(conn.LEGACY_KEY,json.loads(previous.read_text()))

    def test_new_connection_file_takes_precedence(self):
        self.update()
        (self.project/'.lecture-tutor.json').write_text('invalid old file')
        self.assertTrue(self.read()['enabled'])

    def test_project_default_and_chat_override(self):
        self.update();self.update(scope='chat',chat='a',profile='friend')
        self.assertEqual(self.read('a')['binding']['profile'],'friend')
        self.assertEqual(self.read('b')['binding']['profile'],'default')
        self.assertEqual(self.read('a')['scope'],'chat')

    def test_chat_only_does_not_connect_other_chats(self):
        self.update(scope='chat',chat='a')
        self.assertTrue(self.read('a')['enabled']);self.assertFalse(self.read('b')['enabled'])

    def test_chat_disconnect_blocks_fallback(self):
        self.update();self.update(action='disconnect',scope='chat',chat='a')
        x=self.read('a');self.assertFalse(x['enabled']);self.assertTrue(x['explicitly_disabled'])
        self.assertTrue(self.read('b')['enabled'])

    def test_project_disconnect_preserves_explicit_chat(self):
        self.update();self.update(scope='chat',chat='a')
        self.update(action='disconnect');self.assertFalse(self.read('b')['enabled'])
        self.assertTrue(self.read('a')['enabled']);self.assertNotIn('course_path',json.loads(self.settings.read_text()))

    def test_projects_do_not_inherit_each_other(self):
        self.update();other=self.f.base/'other';other.mkdir()
        self.assertFalse(conn.dispatch(self.f.args(project=other,action='status'))['enabled'])

    def test_legacy_relative_paths_resolve_from_project(self):
        self.settings.write_text(json.dumps(dict(course_path='../course',runtime='../runtime',custom='keep')))
        self.assertEqual(self.read()['binding']['course_path'],str(self.f.a.course))
        self.update(scope='chat',chat='a')
        self.assertEqual(self.read('b')['binding']['profile'],'default')
        self.assertEqual(json.loads(self.settings.read_text())['custom'],'keep')

    def test_missing_course_does_not_fallback_to_project(self):
        self.update();self.update(scope='chat',chat='a')
        data=json.loads(self.settings.read_text());data[conn.KEY]['chats']['a']['course_path']='missing'
        self.settings.write_text(json.dumps(data));x=self.read('a')
        self.assertEqual(x['course_status'],'unavailable');self.assertEqual(x['scope'],'chat')

    def test_invalid_connection_does_not_change_settings(self):
        self.update();before=self.settings.read_bytes()
        for kw in [dict(course=self.f.base/'missing'),dict(profile='../escape'),dict(scope='chat'),dict(revision=-1),dict(revision=0)]:
            with self.subTest(kw=kw),self.assertRaises(ValueError):self.update(**kw)
            self.assertEqual(self.settings.read_bytes(),before)

    def test_corrupt_settings_not_overwritten(self):
        for raw in ['{','[]',json.dumps({conn.KEY:dict(schema='future')})]:
            self.settings.write_text(raw);before=self.settings.read_bytes()
            with self.assertRaises(ValueError):self.update(revision=0)
            self.assertEqual(self.settings.read_bytes(),before)

    def test_linked_settings_and_lock_rejected(self):
        target=self.f.base/'target';target.write_text('{}');self.settings.symlink_to(target)
        with self.assertRaises(ValueError):self.update(revision=0)
        self.settings.unlink();(self.project/'.dosuti-mentor.lock').symlink_to(target)
        with self.assertRaises(ValueError):self.update(revision=0)
        self.assertEqual(target.read_text(),'{}')

    def test_disconnect_keeps_course_intent_and_progress(self):
        import learning_intent
        learning_intent.intent(self.f.args(update_file=self.f.write('goal.json',{'goal':{'description':'Java'}}),expected_revision=0,reason='QA'))
        with contextlib.redirect_stdout(io.StringIO()):course.progress(self.f.args(topic='QA',stage='explained',details='QA'))
        intent=(self.f.a.course/'learners/default/learning-intent.json').read_bytes()
        self.update();self.update(action='disconnect')
        self.assertEqual((self.f.a.course/'learners/default/learning-intent.json').read_bytes(),intent)
        with course.db(self.f.a) as db:self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],1)

    def test_parallel_connection_updates_cas(self):
        def update(_):
            try:self.update(revision=0);return True
            except ValueError:return False
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(update,range(2)))
        self.assertEqual(sorted(results),[False,True])

    def test_check_reports_unknown_search_and_current_interpreter(self):
        self.update(runtime=self.f.base/'other-runtime')
        with patch('maintenance.doctor',return_value=dict(read_only=True,network_used=False,checks=[])):
            x=conn.dispatch(self.args(action='check'))['environment']
        self.assertEqual(x['web_search']['status'],'unknown');self.assertEqual(x['interpreter'],sys.executable)
        self.assertEqual(x['configured_runtime'],str(self.f.base/'other-runtime'))

    def test_check_without_binding_does_not_install_anything(self):
        with patch('maintenance.doctor',side_effect=AssertionError('No course')):
            self.assertFalse(conn.dispatch(self.args(action='check'))['environment']['checked'])
        self.assertFalse(self.settings.exists())

    def test_relative_settings_survive_project_and_course_move(self):
        self.settings.write_text(json.dumps(dict(course_path='../course',profile='default')))
        new=self.f.base/'moved';new.mkdir();shutil.move(self.project,new/'project');shutil.move(self.f.a.course,new/'course')
        self.project=new/'project';self.assertEqual(self.read()['course_status'],'available')

    def test_runtime_only_legacy_settings_preserved_on_chat_connect(self):
        self.settings.write_text(json.dumps(dict(runtime='../runtime',custom=1)))
        self.update(scope='chat',chat='a');self.assertTrue(self.read('a')['enabled']);self.assertFalse(self.read()['enabled'])

    def test_arbitrary_json_folder_is_not_a_course(self):
        other=self.f.base/'not-course';other.mkdir();(other/'course.json').write_text('{}')
        with self.assertRaises(ValueError):self.update(course=other)
        self.assertFalse(self.settings.exists())
