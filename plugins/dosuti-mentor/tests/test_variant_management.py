import contextlib,io,json,shutil,sys,unittest
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course,test_context,code_stages,fragment_selection as fs,learning_tools as lt

class Variants(unittest.TestCase):
 def setUp(self):
  self.f=test_context.ContextRegression();self.f.setUp();self.a=self.f.add('a',0,60,[(1,5,'old words')],[(10,b'one')]);self.b=self.f.add('b',0,60,[(1,5,'new words')],[(10,b'two')])
 def tearDown(self):self.f.tearDown()
 def args(self,**kw):return NS(**(vars(self.f.args)|kw))
 def data(self):return course.read(self.f.lesson/'lesson.json')
 def select(self,names,revision=None):return fs.fragments(self.args(keep=names,expected_revision=self.data().get('revision',0) if revision is None else revision))
 def attach(self,name,content):
  p=self.f.directory/'A.java';p.write_text(content)
  with contextlib.redirect_stdout(io.StringIO()):course.attach(self.args(kind='code',none=False,source=str(p),repository=name,ref=None))
  return self.data()
 def test_switch_and_restore_preserves_files(self):
  self.select(['b']);x=self.f.context(0,20);self.assertEqual([s['text'] for s in x['speech']],['new words']);self.assertFalse(x['speech_overlaps']);self.assertTrue(self.a.exists())
  self.select(['a']);self.assertEqual(self.f.context(0,20)['speech'][0]['text'],'old words')
 def test_partial_selection_cannot_drop_coverage(self):
  self.f.add('later',60,90,[(65,70,'tail')]);before=(self.f.lesson/'lesson.json').read_bytes()
  with self.assertRaisesRegex(ValueError,'unprepared'):self.select(['b'])
  self.assertEqual((self.f.lesson/'lesson.json').read_bytes(),before)
 def test_cas_rejects_stale_selection(self):
  self.select(['b'])
  with self.assertRaisesRegex(ValueError,'revision'):self.select(['a'],revision=0)
 def test_unknown_and_duplicate_ids_rejected(self):
  for names in [['missing'],['a','a']]:
   with self.assertRaises(ValueError):self.select(names)
 def test_damaged_choice_rejected_without_mutation(self):
  (self.b/'tutor_packet.json').write_text('{}');before=(self.f.lesson/'lesson.json').read_bytes()
  with self.assertRaises(ValueError):self.select(['b'])
  self.assertEqual((self.f.lesson/'lesson.json').read_bytes(),before)
 def test_selection_invalidates_cursor_but_does_not_create_progress(self):
  p=self.f.directory/'cursor.json';p.write_text(json.dumps(dict(lesson='lesson-01',time=10,topic='QA',recap='QA',next_step='Ask',pending_question='Why?',misconceptions=[])))
  lt.resume(self.args(state_file=p,expected_revision=0));self.select(['b']);self.assertFalse(lt.cursor(self.f.args)['valid'])
  with course.db(self.f.args) as db:self.assertEqual(db.execute('SELECT count(*) FROM progress').fetchone()[0],0)
 def test_two_repositories_do_not_replace_primary(self):
  primary=self.attach(None,'class A {}')['code'];self.attach('streams','class StreamExample {}');x=self.f.context(0,20)
  self.assertEqual(x['teacher_code']['content_sha256'],primary['content_sha256']);self.assertEqual(set(x['teacher_repositories']),{'default','streams'})
 def test_named_history_is_independent(self):
  self.attach(None,'class A {}');self.attach('streams','class B {}');d=self.attach('streams','class C {}')
  self.assertEqual(len(d['code_repositories']['streams']['history']),1);self.assertFalse(d['code']['history'])
 def test_secondary_stage_current_then_historical(self):
  self.select(['a']);d=self.attach('streams','class A {}');code=d['code_repositories']['streams']
  v=dict(id='stage',title='QA',start=0,end=20,code_sha256=code['content_sha256'],file='A.java',screen_file='A.java',match='partial',notes='Only a frame',frames=[dict(time=10,sha256=course.file_hash(self.a/'frames/0.jpg'))]);p=self.f.directory/'stage.json';p.write_text(json.dumps(v))
  row=code_stages.code_stage(self.args(stage_file=p))['stages'][0];self.assertFalse(row['historical']);self.assertFalse(row['stale'])
  self.attach('streams','class B {}');row=self.f.context(0,20)['code_stages'][0];self.assertTrue(row['historical']);self.assertFalse(row['stale'])
 def test_move_preserves_secondary_and_retired_paths(self):
  self.attach('streams','class A {}');self.attach('streams','class B {}');self.select(['b']);new=self.f.directory/'moved';shutil.move(self.f.path,new);self.f.path=new;self.f.args.course=new;self.f.lesson=new/'lessons/lesson-01'
  x=self.f.context(0,20);repo=x['teacher_repositories']['streams'];self.assertTrue(Path(repo['snapshot']).is_relative_to(new));self.assertTrue(Path(repo['history'][0]['snapshot']).is_relative_to(new));self.select(['a'])
 def test_secondary_corruption_not_exposed_as_valid(self):
  d=self.attach('streams','class A {}');p=Path(d['code_repositories']['streams']['snapshot'])/'A.java';p.chmod(0o600);p.write_text('BAD')
  with self.assertRaises(ValueError):self.f.context(0,20)
 def test_reserved_name_rejected(self):
  with self.assertRaises(ValueError):self.attach('default','class A {}')
