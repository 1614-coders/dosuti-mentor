"""Research budgets, provenance, immutable captures and CAS without network calls."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import shutil
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import topic_research as tr
import test_study_units

class Research(unittest.TestCase):
    def setUp(self):
        self.f=test_study_units.StudyUnits();self.f.setUp();self.root=self.f.f.path
        self.path=self.root/'research/maps/research.json';self.input=self.f.f.directory/'event.json'
        self.text=self.f.f.directory/'source.txt';self.text.write_text('Map stores keys and values.\n')
    def tearDown(self):self.f.tearDown()
    def args(self,**kw):return self.f.args(research_id='maps',**kw)
    def read(self):return tr.research(self.args())
    def save(self,value,revision=None):
        self.input.write_text(json.dumps(value));rev=self.read()['revision'] if revision is None else revision
        return tr.research(self.args(event_file=self.input,expected_revision=rev))
    def request(self,**budget):
        return dict(action='request',topic='Java maps',goal='Understand key lookup',level='Java basics',technology_version='Java 17',gaps=['HashMap collisions'],materials=[],budget=dict(queries=2,pages=3,characters=100,per_source=60)|budget)
    def capture(self,**kw):
        return dict(action='capture',id='one',url='https://docs.oracle.com/map',query_id=None,origin='user',status='captured',notes='Local QA text, not fetched page',text_file=str(self.text))|kw
    def review(self,**kw):
        return dict(action='review',source_id='one',verdict='accepted',version_fit='compatible',observed_version='17',notes='Manual QA interpretation')|kw
    def test_old_course_read_creates_no_files(self):
        self.assertEqual(self.read()['revision'],0);self.assertFalse(self.path.parent.exists())
    def test_capture_requires_review_and_retains_provenance(self):
        self.save(self.request());x=self.save(self.capture());self.assertFalse(x['sources'][0]['eligible']);self.assertIsNone(x['sources'][0]['text'])
        x=self.save(self.review());self.assertEqual(x['sources'][0]['text'],self.text.read_text());self.assertEqual(x['sources'][0]['origin'],'user')
        self.assertFalse('text_file' in json.loads(self.path.read_text())['events'][1]['value'])
    def test_local_user_material_needs_no_invented_url(self):
        self.save(self.request());x=self.save(self.capture(url=None));self.assertIsNone(x['sources'][0]['url'])
    def test_original_changes_do_not_rewrite_snapshot(self):
        self.save(self.request());self.save(self.capture());self.text.write_text('New');self.save(self.review())
        self.assertIn('keys',self.read()['sources'][0]['text'])
    def test_query_requires_reservation_and_enforces_budget(self):
        self.save(self.request(queries=1))
        with self.assertRaises(ValueError):self.save(self.capture(origin='search',query_id='q'))
        self.save(dict(action='query',id='q',query='Java 17 map docs',provider='web tool'))
        self.save(self.capture(origin='search',query_id='q'))
        with self.assertRaisesRegex(ValueError,'budget'):self.save(dict(action='query',id='q2',query='more',provider='web tool'))
    def test_unavailable_counts_against_page_budget(self):
        self.save(self.request(pages=1));x=self.save(self.capture(status='unavailable',text_file=None));self.assertEqual(x['used']['pages'],1)
        with self.assertRaises(ValueError):self.save(self.review())
        with self.assertRaisesRegex(ValueError,'budget'):self.save(self.capture(id='two'))
    def test_per_source_budget_rejects_before_snapshot(self):
        self.save(self.request(per_source=5))
        with self.assertRaisesRegex(ValueError,'budget'):self.save(self.capture())
        self.assertFalse((self.path.parent/'snapshots').exists());self.assertEqual(self.read()['revision'],1)
    def test_total_budget_and_duplicate_source_reject(self):
        self.save(self.request(characters=40,per_source=40));self.save(self.capture())
        for source in [self.capture(),self.capture(id='two')]:
            with self.assertRaises(ValueError):self.save(source)
        self.assertEqual(self.read()['used']['pages'],1)
    def test_conflict_and_unknown_cannot_be_accepted(self):
        self.save(self.request());self.save(self.capture())
        for fit in ['unknown','conflict']:
            with self.assertRaises(ValueError):self.save(self.review(version_fit=fit))
        x=self.save(self.review(verdict='excluded',version_fit='conflict'));self.assertFalse(x['sources'][0]['eligible'])
        with self.assertRaises(ValueError):self.save(self.review(observed_version=None))
    def test_review_revision_can_withdraw_acceptance(self):
        self.save(self.request());self.save(self.capture());self.save(self.review())
        x=self.save(self.review(verdict='excluded',version_fit='conflict'));self.assertFalse(x['sources'][0]['eligible']);self.assertEqual(len(json.loads(self.path.read_text())['events']),4)
    def test_tampered_snapshot_not_eligible(self):
        self.save(self.request());self.save(self.capture());x=self.save(self.review());p=Path(x['sources'][0]['text_file']);p.chmod(0o600);p.write_text('Bad')
        row=self.read()['sources'][0];self.assertFalse(row['eligible']);self.assertEqual(row['integrity'],'stale');self.assertIsNone(row['text'])
    def test_review_of_damaged_snapshot_rejected(self):
        self.save(self.request());self.save(self.capture());p=self.path.parent/self.read()['sources'][0]['snapshot'];p.chmod(0o600);p.write_text('Bad');before=self.path.read_bytes()
        with self.assertRaises(ValueError):self.save(self.review())
        self.assertEqual(self.path.read_bytes(),before)
    def test_tampered_journal_cannot_be_overwritten(self):
        self.save(self.request());d=json.loads(self.path.read_text());d['events'][0]['value']['technology_version']='Java 21';self.path.write_text(json.dumps(d));before=self.path.read_bytes()
        with self.assertRaises(ValueError):self.read()
        self.input.write_text(json.dumps(self.capture()))
        with self.assertRaises(ValueError):tr.research(self.args(event_file=self.input,expected_revision=1))
        self.assertEqual(self.path.read_bytes(),before)
    def test_cas_and_concurrent_query_writes(self):
        self.save(self.request());self.input.write_text(json.dumps(dict(action='query',id='q',query='docs',provider='QA')))
        def write(_):
            try:tr.research(self.args(event_file=self.input,expected_revision=1));return True
            except ValueError:return False
        with ThreadPoolExecutor(2) as pool:result=list(pool.map(write,range(2)))
        self.assertEqual(sorted(result),[False,True]);self.assertEqual(self.read()['revision'],2)
    def test_request_is_immutable(self):
        self.save(self.request())
        with self.assertRaises(ValueError):self.save(self.request())
    def test_invalid_inputs_do_not_publish(self):
        for patch in [dict(technology_version=None),dict(budget=dict(queries=True,pages=2,characters=100,per_source=10)),dict(materials='url')]:
            with self.subTest(patch=patch),self.assertRaises(ValueError):self.save(self.request()|patch)
            self.assertFalse(self.path.exists())
    def test_urls_and_linked_storage_rejected(self):
        self.save(self.request())
        for bad in ['http://example.org','https://user:pass@example.org','https://example.org/#fragment','https://example.org/ bad']:
            with self.assertRaises(ValueError):self.save(self.capture(url=bad))
        folder=self.path.parent;shutil.move(folder,folder.with_name('real'));folder.symlink_to(folder.with_name('real'),target_is_directory=True)
        with self.assertRaises(ValueError):self.read()
    def test_move_preserves_accepted_snapshot(self):
        self.save(self.request());self.save(self.capture());self.save(self.review());moved=self.root.with_name('moved');shutil.move(self.root,moved);self.f.f.path=moved;self.f.f.args.course=moved
        self.assertTrue(self.read()['sources'][0]['eligible']);self.assertTrue(Path(self.read()['sources'][0]['text_file']).is_relative_to(moved))
