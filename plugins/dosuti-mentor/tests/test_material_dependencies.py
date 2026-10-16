"""Material changes invalidate opted-in checkpoints, with portable, bounded evidence."""
import contextlib
import copy
import io
import shutil
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import course
import learning_tools as lt
import material_sources as ms
import term_notes
import code_stages
import test_learning_tools


class MaterialDependencies(unittest.TestCase):
    def setUp(self):
        self.f = test_learning_tools.LearningTools(); self.f.setUp(); self.f.add_topic()
        self.path = self.f.fixture.lesson / 'lesson.json'

    def tearDown(self): self.f.tearDown()
    def data(self): return course.read(self.path)
    def edit(self, action):
        data = self.data(); action(data); course.save(self.path, data)
    def save(self, chosen, revision=0, topics=True):
        value = self.f.state() | {'dependency_materials': chosen}
        if topics: value['dependency_topics'] = ['maps']
        return self.f.save_cursor(revision=revision, value=value)
    def valid(self): return lt.cursor(self.f.a)['valid']
    def attach(self, kind, text):
        p = self.f.base / (kind + '.txt'); p.write_text(text)
        with contextlib.redirect_stdout(io.StringIO()):
            course.attach(self.f.args(kind=kind, source=str(p), none=False, ref=None, resource_type='article'))
        return self.data()[kind]
    def resource(self, reviewed=True):
        item = self.attach('resources', 'Documentation text')['items'][-1]
        result = ms.resource(self.f.args(action='extract', resource_id=item['id']))['items'][-1]
        if reviewed:
            ms.resource(self.f.args(action='review', resource_id=item['id'], expected_revision=result['revision'], notes='Read documentation'))
        return self.data()['resources']['items'][-1]
    def term(self, name='hashmap'):
        value = dict(id=name, start=5, end=15, asr_text='HashMap', term='HashMap', basis='hypothesis', notes='Tentative term', frames=[], code=None)
        term_notes.term_note(self.f.args(note_file=self.f.write('term.json', value)))
        return name
    def stage(self, name='stage'):
        if not self.data()['code'].get('snapshot'):
            self.f.attach_code('lesson-01', {'A.java':'class A {}'})
            self.extra = self.f.fixture.add('extra', 60, 90, [(65,70,'Example')], [(65,b'frame')])
        value = dict(id=name, title='Stage', start=60, end=90, code_sha256=self.data()['code']['content_sha256'],
                     file='A.java', screen_file='A.java', match='partial', notes='Manual observation',
                     frames=[dict(time=65, sha256=course.file_hash(self.extra/'frames/0.jpg'))])
        code_stages.code_stage(self.f.args(stage_file=self.f.write('stage.json',value)))
        return name

    def test_homework_changes_invalidate_without_losing_event_history(self):
        self.attach('homework', 'Solve A')
        with contextlib.redirect_stdout(io.StringIO()):
            course.progress(self.f.args(topic='QA', stage='explained', details='Synthetic QA event'))
        self.save({'homework':True}); self.attach('homework', 'Solve B'); self.assertFalse(self.valid())
        with course.db(self.f.a) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM progress').fetchone()[0],1)
        self.attach('homework', 'Solve A'); self.assertTrue(self.valid())

    def test_homework_tampering_and_missing_reject(self):
        with self.assertRaises(ValueError): self.save({'homework':True})
        hw = self.attach('homework','A'); self.save({'homework':True})
        p = Path(hw['file']); p.chmod(0o600); p.write_text('B'); self.assertFalse(self.valid())

    def test_materials_can_combine_with_legacy_packet_dependencies(self):
        self.attach('homework','A'); self.assertTrue(self.save({'homework':True},topics=False)['valid'])
        self.attach('homework','B'); self.assertFalse(self.valid())

    def test_unselected_homework_does_not_invalidate_old_checkpoint(self):
        self.f.save_cursor(); self.attach('homework','A'); self.assertTrue(self.valid())

    def test_resource_requires_review(self):
        item=self.resource(False)
        with self.assertRaisesRegex(ValueError,'review'): self.save({'resources':[item['id']]})

    def test_resource_review_and_selected_text_changes_invalidate(self):
        item=self.resource(); self.save({'resources':[item['id']]})
        self.edit(lambda d:d['resources']['items'][0]['review'].update(notes='Changed conclusion'))
        self.assertFalse(self.valid())
        self.edit(lambda d:d['resources']['items'][0]['review'].update(notes='Read documentation'))
        self.assertTrue(self.valid())
        p=Path(item['extracted_file']); p.chmod(0o600); p.write_text('Changed text'); self.assertFalse(self.valid())

    def test_unselected_resource_update_preserves(self):
        item=self.resource(); self.save({'resources':[item['id']]})
        self.attach('resources','Unrelated'); self.assertTrue(self.valid())

    def test_term_change_and_removal_invalidate(self):
        name=self.term(); self.save({'term_notes':[name]})
        self.edit(lambda d:d['term_notes'][name].update(notes='Different interpretation')); self.assertFalse(self.valid())
        self.edit(lambda d:d['term_notes'].pop(name)); self.assertFalse(self.valid())

    def test_unselected_term_and_timestamps_preserve(self):
        name=self.term(); self.save({'term_notes':[name]}); self.term('other')
        self.edit(lambda d:d['term_notes'][name].update(updated='later')); self.assertTrue(self.valid())

    def test_stage_edit_and_new_overlap_invalidate(self):
        name=self.stage(); self.save({'code_stages':[name]})
        self.stage('overlap'); self.assertFalse(self.valid())
        self.edit(lambda d:d['code_stages'].pop('overlap')); self.assertTrue(self.valid())
        self.edit(lambda d:d['code_stages'][name].update(match='differs')); self.assertFalse(self.valid())

    def test_stage_historical_file_damage_invalidates(self):
        name=self.stage(); self.f.attach_code('lesson-01',{'A.java':'class B {}'})
        self.save({'code_stages':[name]})
        p=Path(self.data()['code']['history'][0]['snapshot'])/'A.java'; p.chmod(0o600); p.write_text('bad')
        self.assertFalse(self.valid())

    def test_visual_notes_edit_and_corruption_invalidate(self):
        self.save({'visual_notes':['a']}); p=self.f.fragment/'visual_notes.json'; original=p.read_bytes()
        p.write_text('{'); self.assertFalse(self.valid()); p.write_bytes(original); self.assertTrue(self.valid())
        notes=course.read(p); notes['uncertainties'].append(dict(text='New uncertainty')); course.save(p,notes)
        self.assertFalse(self.valid())

    def test_stale_attached_code_note_rejects_new_checkpoint(self):
        self.f.attach_code('lesson-01',{'A.java':'class A {}'}); code=self.data()['code']
        course.save(self.f.fragment/'visual_notes.json',dict(observations=[dict(source_kind='attached_code',source_sha256=code['content_sha256'])],uncertainties=[]))
        self.save({'visual_notes':['a']}); self.f.attach_code('lesson-01',{'A.java':'class B {}'})
        self.assertFalse(self.valid())
        with self.assertRaises(ValueError): self.save({'visual_notes':['a']}, revision=1)

    def test_invalid_selections_preserve_cursor_bytes(self):
        self.attach('homework','A'); self.save({'homework':True}); p=lt.learner(self.f.a)/'cursor.json'; before=p.read_bytes()
        for chosen in [{},[],None,{'unknown':True},{'homework':False},{'homework':1},{'resources':[]},
                       {'resources':['x','x']},{'resources':['../x']},{'term_notes':['missing']},{'visual_notes':['missing']}]:
            with self.subTest(chosen=chosen), self.assertRaises(ValueError): self.save(chosen, revision=1)
            self.assertEqual(p.read_bytes(),before)

    def test_malformed_stored_material_evidence_is_preserved(self):
        self.attach('homework','A'); self.save({'homework':True}); p=lt.learner(self.f.a)/'cursor.json'; good=course.read(p)
        for mutate in [lambda s:s.pop('material_evidence'),lambda s:s.pop('dependency_materials'),
                       lambda s:s['material_evidence'].update(schema='future'),
                       lambda s:s['material_evidence']['materials'].update(homework={'task':'bad'})]:
            bad=copy.deepcopy(good); mutate(bad['state']); course.save(p,bad); before=p.read_bytes()
            self.assertEqual(lt.cursor(self.f.a)['reason'],'invalid_checkpoint')
            with self.assertRaises(ValueError): self.save({'homework':True},revision=1)
            self.assertEqual(p.read_bytes(),before)

    def test_relocation_preserves_all_material_fingerprints(self):
        self.attach('homework','A'); item=self.resource(); term=self.term(); stage=self.stage()
        self.save(dict(homework=True, resources=[item['id']], term_notes=[term], code_stages=[stage],visual_notes=['a']))
        moved=self.f.base/'moved'; shutil.move(self.f.a.course,moved); self.f.a.course=moved
        self.assertTrue(self.valid())

    def test_cas_prevents_overwriting_concurrent_cursor(self):
        self.attach('homework','A'); self.save({'homework':True})
        with self.assertRaisesRegex(ValueError,'another chat'): self.save({'homework':True})

    def test_no_network_needed_for_cached_resource_check(self):
        from unittest.mock import patch
        item=self.resource(); self.save({'resources':[item['id']]})
        with patch.object(ms,'acquire',side_effect=AssertionError('No fetch')): self.assertTrue(self.valid())
