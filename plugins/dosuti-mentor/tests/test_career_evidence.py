"""Career samples use reviewed evidence, never learner grades or market percentages."""
import copy,json,shutil,sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import career_evidence as ce
import test_topic_research

class Career(unittest.TestCase):
    def setUp(self):
        self.f=test_topic_research.Research();self.f.setUp();self.root=self.f.root
        self.f.text.write_text('QA Java position; active at observation\nRequired: Java and algorithms\nPreferred: SQL\n')
        self.f.save(self.f.request(pages=10,characters=3000,per_source=500));self.f.save(self.f.capture(url='https://jobs.example/java/123'));self.f.save(self.f.review())
        self.path=self.root/'career/java.json';self.input=self.f.f.f.directory/'career.json'
    def tearDown(self):self.f.tearDown()
    def args(self,**kw):return self.f.f.args(sample_id='java',**kw)
    def posting(self,identity='one',**kw):
        return dict(id=identity,employer='QA Employer',employer_job_id='123',role='Java backend',region='QA region',level='intern',kind='vacancy',status='active',inclusion='included',inclusion_reason='Explicit QA assumption, not real vacancy',observed_on='2026-01-01',published_on=None,source=dict(kind='research',role='external',notes='Synthetic test source',research_id='maps',source_id='one',start_line=1,end_line=3))|kw
    def card(self,**kw):
        return dict(id='java',title='QA sample',scope=dict(role='Java backend',region='QA region',level='intern',goal='Technical testing only',basis='qa_assumption'),as_of='2026-01-01',selection='Synthetic convenience sample',limitations=['No real market inference'],postings=[self.posting()],requirements=[dict(id='java',title='Java',interpretation='Manual QA requirement grouping',evidence=[dict(posting='one',classification='required',start_line=2,end_line=2,explanation='Explicit synthetic required line')])])|kw
    def read(self):return ce.career(self.args())
    def save(self,card=None,revision=None):
        self.input.write_text(json.dumps(card or self.card()));return ce.career(self.args(sample_file=self.input,expected_revision=self.read()['revision'] if revision is None else revision))
    def source(self,identity,url,text=None):
        if text:self.f.text.write_text(text)
        self.f.save(self.f.capture(id=identity,url=url));self.f.save(self.f.review(source_id=identity))
    def test_read_is_non_creating_for_old_course(self):self.assertIsNone(self.read()['sample']);self.assertFalse(self.path.parent.exists())
    def test_current_requirement_has_exact_evidence_and_date(self):
        x=self.save();self.assertTrue(x['valid']);self.assertEqual(x['current_vacancy_count'],1);link=x['requirements'][0]['evidence'][0]
        self.assertEqual(link['fragment'],'Required: Java and algorithms');self.assertEqual(link['observed_on'],'2026-01-01');self.assertEqual(x['requirements'][0]['current_counts']['required'],1)
    def test_archive_unknown_program_excluded_do_not_count(self):
        for change in [dict(status='archived'),dict(status='unknown'),dict(kind='program'),dict(inclusion='excluded')]:
            with self.subTest(change=change):self.assertEqual(self.save(self.card(postings=[self.posting(**change)]))['current_vacancy_count'],0)
    def test_duplicate_card_not_double_counted(self):
        x=self.save(self.card(postings=[self.posting(),self.posting('mirror')]))
        self.assertEqual(x['current_vacancy_count'],1);self.assertEqual(len(x['duplicates']),1)
    def test_canonical_tracking_url_and_content_duplicate(self):
        self.source('two','https://jobs.example/java/123/?utm_source=feed','QA alternate description\nRequired: Java\nPreferred: SQL\n')
        p=self.posting('two',employer_job_id=None);p['source']['source_id']='two'
        x=self.save(self.card(postings=[self.posting(employer_job_id=None),p]));self.assertEqual(x['current_vacancy_count'],1)
    def test_status_conflict_does_not_establish_current_demand(self):
        x=self.save(self.card(postings=[self.posting(),self.posting('mirror',status='archived')]))
        self.assertEqual(x['current_vacancy_count'],0);self.assertTrue(x['duplicates'][0]['status_conflict'])
    def test_withdrawn_source_blocks_aggregate_without_destroying_history(self):
        self.save();before=self.path.read_bytes();self.f.save(self.f.review(verdict='excluded',version_fit='unknown'))
        x=self.read();self.assertFalse(x['valid']);self.assertIsNone(x['current_vacancy_count']);self.assertIsNone(x['requirements'][0]['current_counts']);self.assertIsNone(x['requirements'][0]['evidence'][0]['fragment'])
        with self.assertRaises(ValueError):self.save()
        self.assertEqual(before,self.path.read_bytes())
    def test_changed_review_invalidates_semantic_binding(self):
        self.save();self.f.save(self.f.review(notes='New meaning must be reconsidered'));self.assertFalse(self.read()['valid'])
    def test_source_corruption_is_detected(self):
        self.save();source=self.f.read()['sources'][0];p=Path(source['text_file']);p.chmod(0o600);p.write_text('Bad');self.assertFalse(self.read()['valid'])
    def test_requirement_cannot_escape_excerpt_or_unknown_posting(self):
        for change in [dict(start_line=0),dict(end_line=4),dict(posting='missing'),dict(classification='must'),dict(start_line=True)]:
            card=self.card();card['requirements'][0]['evidence'][0].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):self.save(card)
        self.assertFalse(self.path.exists())
    def test_invalid_dates_and_scope_rejected(self):
        for date in ['2026-1-1','2026-13-01',None,42]:
            with self.subTest(date=date),self.assertRaises(ValueError):self.save(self.card(as_of=date))
        card=self.card();card['postings'][0]['observed_on']='2026-01-02'
        with self.assertRaises(ValueError):self.save(card)
        with patch('course.now',return_value='2025-01-01T00:00:00Z'),self.assertRaises(ValueError):self.save()
    def test_noop_and_real_revision_preserve_history(self):
        self.save();before=self.path.read_bytes();self.assertEqual(self.save()['revision'],1);self.assertEqual(before,self.path.read_bytes())
        x=self.save(self.card(title='Revised scope'));self.assertEqual(x['revision'],2);self.assertEqual(len(x['history']),2)
    def test_cas_conflict(self):
        self.save();before=self.path.read_bytes()
        with self.assertRaises(ValueError):self.save(self.card(title='Wrong revision'),revision=0)
        self.assertEqual(before,self.path.read_bytes())
    def test_relocation(self):
        self.save();new=self.root.with_name('moved');shutil.move(self.root,new);self.root=new;self.f.f.f.args.course=new
        self.assertTrue(self.read()['valid'])
    def test_tampered_history_rejected(self):
        self.save();d=json.loads(self.path.read_text());d['records'][0]['scope']['region']='Other';self.path.write_text(json.dumps(d))
        with self.assertRaises(ValueError):self.read()
    def test_no_learning_journal_or_goal_is_created(self):
        self.save();self.assertFalse((self.root/'learners').exists());self.assertFalse((self.root/'study-units.json').exists())

    def test_excluded_duplicate_does_not_make_count_order_dependent(self):
        a=self.posting(inclusion='excluded');b=self.posting('included')
        for rows in [[a,b],[b,a]]:self.assertEqual(self.save(self.card(postings=rows))['current_vacancy_count'],1)
    def test_program_and_vacancy_conflict_not_counted(self):
        x=self.save(self.card(postings=[self.posting(),self.posting('program',kind='program')]))
        self.assertEqual(x['current_vacancy_count'],0)
