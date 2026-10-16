"""Research review dependency must survive copies, withdraws and unrelated additions."""
import contextlib
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import topic_research as tr
import unit_learning
import test_study_units

class ResearchSource(unittest.TestCase):
    def setUp(self):
        self.f=test_study_units.StudyUnits();self.f.setUp();self.root=self.f.f.path
        self.text=self.f.f.directory/'research.txt';self.text.write_text('Keys and values\nLookup by key\n')
        self.request=dict(action='request',topic='Maps',goal='Understand lookup',level='Java basics',technology_version='Java 17',gaps=['Lookup'],materials=[],budget=dict(queries=3,pages=4,characters=1000,per_source=200))
        self.capture=dict(action='capture',id='doc',url='https://docs.oracle.com/map',origin='user',query_id=None,status='captured',notes='Synthetic source, not a fetched Oracle page',text_file=str(self.text))
        self.review=dict(action='review',source_id='doc',verdict='accepted',version_fit='compatible',observed_version='Java 17',notes='Synthetic manual review')
        self.source=dict(kind='research',role='external',notes='Read selected lines; synthetic test',research_id='lookup',source_id='doc',start_line=1,end_line=2)
    def tearDown(self):self.f.tearDown()
    def event(self,value):
        args=self.f.args(research_id='lookup');rev=tr.research(args)['revision'];p=self.f.f.directory/'event.json';p.write_text(json.dumps(value))
        return tr.research(self.f.args(research_id='lookup',event_file=p,expected_revision=rev))
    def ready(self):self.event(self.request);self.event(self.capture);return self.event(self.review)
    def save(self,source=None):return self.f.save(self.f.card(sources=[source or self.source]))
    def get(self):return self.f.get()
    def test_missing_journal_does_not_create_or_publish(self):
        with self.assertRaises(ValueError):self.save()
        self.assertFalse((self.root/'research').exists());self.assertFalse(self.f.path.exists())
    def test_unreviewed_and_excluded_are_rejected(self):
        self.event(self.request);self.event(self.capture)
        with self.assertRaises(ValueError):self.save()
        self.event(self.review|dict(verdict='excluded',version_fit='conflict'))
        with self.assertRaises(ValueError):self.save()
    def test_context_contains_evidence_and_version(self):
        self.ready();self.save();ctx=self.get()['source_contexts'][0]
        self.assertEqual(ctx['text'],self.text.read_text().strip());self.assertEqual(ctx['capture']['url'],self.capture['url'])
        self.assertEqual(ctx['request']['technology_version'],'Java 17');self.assertEqual(ctx['review']['notes'],self.review['notes'])
    def test_review_withdrawal_invalidates_descendants(self):
        self.ready();self.save();self.f.save(self.f.card('child',prerequisites=['maps']))
        self.event(self.review|dict(verdict='excluded',version_fit='unknown'))
        self.assertTrue(self.get()['stale']);self.assertEqual(self.get()['source_contexts'],[]);self.assertTrue(self.f.get('child')['stale'])
    def test_review_content_change_requires_unit_resync(self):
        self.ready();self.save();self.event(self.review|dict(notes='Different interpretation'))
        self.assertTrue(self.get()['stale']);self.save();self.assertFalse(self.get()['stale'])
    def test_same_review_at_new_revision_not_stale(self):
        self.ready();self.save();self.event(self.review);self.assertFalse(self.get()['stale'])
    def test_unrelated_queries_and_sources_not_stale(self):
        self.ready();self.save();self.event(dict(action='query',id='q',query='Unrelated',provider='QA'))
        self.event(self.capture|dict(id='other'));self.event(self.review|dict(source_id='other',verdict='excluded',version_fit='unknown'))
        self.assertFalse(self.get()['stale'])
    def test_unrelated_snapshot_damage_does_not_invalidate_selected_source(self):
        self.ready();self.save();self.text.write_text('Other source\n')
        rows=self.event(self.capture|dict(id='other'))['sources'];p=self.root/'research/lookup'/rows[1]['snapshot'];p.chmod(0o600);p.write_text('Bad')
        self.assertFalse(self.get()['stale'])
    def test_revocation_keeps_history_and_rejects_learning_write(self):
        self.ready();self.save();args=self.f.args(unit_id='maps',history_limit=100);state=unit_learning.learn_unit(args)
        p=self.f.f.directory/'answer.json';p.write_text(json.dumps(dict(stage='solved_independently',details='QA',question='QA?',answer='QA answer',hints=0,criteria=[1])))
        unit_learning.learn_unit(self.f.args(unit_id='maps',event_file=p,expected_revision=0,expected_unit_sha256=state['unit']['content_sha256']))
        journal=self.root/'learners/default/unit-learning.json';before=journal.read_bytes()
        self.event(self.review|dict(verdict='excluded',version_fit='conflict'));state=unit_learning.learn_unit(args)
        self.assertEqual(state['learning']['outdated_attempt_count'],1);self.assertFalse(state['learning']['all_criteria_demonstrated'])
        with self.assertRaises(ValueError):unit_learning.learn_unit(self.f.args(unit_id='maps',event_file=p,expected_revision=1,expected_unit_sha256=state['unit']['content_sha256']))
        self.assertEqual(journal.read_bytes(),before)
        self.event(self.review);self.assertTrue(unit_learning.learn_unit(args)['learning']['all_criteria_demonstrated'])
    def test_snapshot_tampering_marks_stale(self):
        rows=self.ready()['sources'];self.save();p=self.root/'research/lookup'/rows[0]['snapshot'];p.chmod(0o600);p.write_text('Bad')
        self.assertTrue(self.get()['stale']);self.assertEqual(self.get()['source_contexts'],[])
    def test_original_file_change_does_not_change_capture(self):
        self.ready();self.save();self.text.write_text('Original changed');self.assertFalse(self.get()['stale'])
    def test_tampered_journal_marks_stale_without_rewriting_unit(self):
        self.ready();self.save();before=self.f.path.read_bytes();p=self.root/'research/lookup/research.json';data=json.loads(p.read_text());data['events'][0]['value']['technology_version']='21';p.write_text(json.dumps(data))
        self.assertTrue(self.get()['stale']);self.assertEqual(self.f.path.read_bytes(),before)
    def test_move_preserves_research_binding(self):
        self.ready();self.save();moved=self.root.with_name('moved');shutil.move(self.root,moved);self.f.f.path=moved;self.f.f.args.course=moved
        self.assertFalse(self.get()['stale']);self.assertIn('keys',self.get()['source_contexts'][0]['text'].lower())
    def test_invalid_ids_fields_and_ranges_do_not_publish(self):
        self.ready()
        for change in [dict(research_id='../lookup'),dict(source_id='missing'),dict(end_line=99),dict(start_line=True),dict(lesson='not-required'),dict(evidence_sha256='0'*64)]:
            with self.subTest(change=change),self.assertRaises(ValueError):self.save(self.source|change)
            self.assertFalse(self.f.path.exists())
    def test_research_writes_use_same_unit_lock_before_research_lock(self):
        names=[];real=tr.file_lock
        @contextlib.contextmanager
        def observed(path):
            names.append(Path(path).name)
            with real(path):yield
        with patch.object(tr,'file_lock',observed):self.event(self.request)
        self.assertEqual(names,['.study-units.lock','.topic-research.lock'])
