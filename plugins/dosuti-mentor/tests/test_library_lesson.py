"""r10 annotations reach existing units without becoming truth or learner evidence."""
import copy
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import source_library as lib
import library_lesson as bridge
import test_source_library as fixtures
import test_study_units as units
import unit_learning

class LibraryLesson(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.SourceLibrary();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.selection=self.f.find()
        self.request=dict(goal='Select columns from a table',gap='Need one theory example and an original task',
            learner_materials_sufficient=False,knowledge=dict(confirmed=['Rows and columns'],reported=[],uncertain=[]),
            reviews=[self.review()])
    def review(self,entry='E10-001',**kw):
        return dict(entry_id=entry,verdict='suitable',prerequisites='confirmed',basis='Rows and columns demonstrated in prior work',
            theory_plan='One SELECT/FROM example; omit familiar table basics',practice_plan='Author a new two-column table task, no solution upfront')|kw
    def brief(self,request=None,selection=None):return bridge.lesson_brief(self.selection if selection is None else selection,request or self.request)
    def test_reviewed_theory_and_practice_are_plans_not_mastery_or_truth(self):
        r=self.brief();self.assertEqual(r['next_action'],'prepare_lesson');self.assertEqual(len(r['adaptation']['reviews']),1)
        self.assertIn('omit familiar',r['adaptation']['reviews'][0]['theory_plan'])
        self.assertFalse(r['fact_verified']);self.assertFalse(r['mastery_assessed']);self.assertFalse(r['ai_content_import_allowed'])
    def test_no_fit_review_does_not_automatically_select_a_source(self):
        r=self.brief(self.request|dict(reviews=[]))
        self.assertEqual(r['next_action'],'review_fit');self.assertEqual(r['reference_markdown'],'')
    def test_unknown_prerequisites_require_check_and_missing_cannot_be_suitable(self):
        req=self.request|dict(knowledge=dict(confirmed=[],reported=['I use SQL'],uncertain=['Tables']),
            reviews=[self.review(verdict='conditional',prerequisites='needs_check',basis='Self-report has not been tested')])
        r=self.brief(req);self.assertEqual(r['next_action'],'check_prerequisites')
        self.assertFalse(r['ready_for_main_content'])
        with self.assertRaises(ValueError):self.brief(req|dict(reviews=[self.review(prerequisites='missing')]))
    def test_wrong_topic_marked_unsuitable_is_not_forced_into_lesson(self):
        req=self.request|dict(reviews=[self.review(verdict='unsuitable',basis='Goal is transactions; SELECT sample does not cover it')])
        r=self.brief(req);self.assertEqual(r['reference_markdown'],'');self.assertEqual(r['next_action'],'find_material_for_gap')
    def test_learner_materials_sufficient_skip_library_even_when_path_is_bad(self):
        req=self.request|dict(learner_materials_sufficient=True)
        r=bridge.lesson_brief(None,req);self.assertEqual(r['status'],'learner_materials_sufficient');self.assertEqual(r['reference_markdown'],'')
        p=Path(self.f.tmp.name)/'request.json';p.write_text(json.dumps(req))
        result=subprocess.run([sys.executable,'-B',lib.__file__,'--library','does-not-exist.zip','--lesson-request',str(p)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(json.loads(result.stdout)['status'],'learner_materials_sufficient')
    def test_optional_library_absent_does_not_block_preparation(self):
        r=self.brief(selection=dict(status='not_configured'))
        self.assertEqual(r['next_action'],'use_existing_research');self.assertEqual(r['reference_markdown'],'')
    def test_rights_provenance_dates_and_outside_yaml_caveats_are_preserved(self):
        text=self.brief()['reference_markdown']
        for token in ('SQL-01','E10-001','https://example.org/lesson/1','2026-10-30','No AI ingestion permission','Важная оговорка','reference_link','r10',self.selection['archive_sha256']):
            self.assertIn(token,text)
    def test_only_two_reference_sources_without_truncating_licenses(self):
        r=self.brief(self.request|dict(reviews=[self.review(f'E10-00{i}') for i in range(1,4)]))
        self.assertEqual(r['selected_entry_ids'],['E10-001','E10-002']);self.assertEqual(r['omitted_count'],1)
        self.assertNotIn('E10-003',r['reference_markdown']);self.assertEqual(r['reference_markdown'].count('No AI ingestion permission'),2)
    def test_combined_cli_reads_only_reviewed_entries_once(self):
        request=self.request|dict(reviews=[self.review('E10-001'),self.review('E10-002')])
        path=Path(self.f.tmp.name)/'request.json';path.write_text(json.dumps(request))
        output=io.StringIO()
        with patch.object(sys,'argv',['source_library.py','--lesson-request',str(path),'--direction','programming','--topic','S10-01','--level','basic','--language','EN']),patch.object(lib,'search',return_value=self.selection) as search,contextlib.redirect_stdout(output):
            lib.main()
        search.assert_called_once();self.assertEqual(search.call_args.kwargs['entries'],['E10-001','E10-002'])
        result=json.loads(output.getvalue());self.assertEqual(result['selected_entry_ids'],['E10-001','E10-002'])
        self.assertNotIn('candidates',result)
    def test_reference_limit_does_not_fetch_the_third_reviewed_card(self):
        request=self.request|dict(reviews=[self.review(f'E10-00{i}') for i in range(1,4)])
        path=Path(self.f.tmp.name)/'request.json';path.write_text(json.dumps(request))
        selected=copy.deepcopy(self.selection);selected['candidates']=selected['candidates'][:2]
        output=io.StringIO()
        with patch.object(sys,'argv',['source_library.py','--lesson-request',str(path)]),patch.object(lib,'search',return_value=selected) as search,contextlib.redirect_stdout(output):
            lib.main()
        self.assertEqual(search.call_args.kwargs['entries'],['E10-001','E10-002'])
        self.assertEqual(json.loads(output.getvalue())['omitted_count'],1)
    def test_cli_does_not_fetch_candidates_already_marked_unsuitable(self):
        request=self.request|dict(reviews=[self.review('E10-001'),self.review('E10-002',verdict='unsuitable')])
        path=Path(self.f.tmp.name)/'request.json';path.write_text(json.dumps(request))
        with patch.object(sys,'argv',['source_library.py','--lesson-request',str(path)]),patch.object(lib,'search',return_value=self.selection) as search,contextlib.redirect_stdout(io.StringIO()):
            lib.main()
        self.assertEqual(search.call_args.kwargs['entries'],['E10-001'])
    def test_all_rejected_reviews_need_no_archive(self):
        req=self.request|dict(reviews=[self.review(verdict='unsuitable')])
        path=Path(self.f.tmp.name)/'request.json';path.write_text(json.dumps(req))
        output=io.StringIO()
        with patch.object(sys,'argv',['source_library.py','--library','missing.zip','--lesson-request',str(path)]),patch.object(lib,'search',side_effect=AssertionError('Nothing will be used')),contextlib.redirect_stdout(output):
            lib.main()
        self.assertEqual(json.loads(output.getvalue())['next_action'],'find_material_for_gap')
    def test_existing_selection_is_projected_without_any_archive_read(self):
        with patch.object(lib,'search',side_effect=AssertionError('Must reuse query result')),patch.object(lib,'ZipFile',side_effect=AssertionError('No new reads')):
            self.brief()
    def test_cannot_override_human_only_or_unknown_entry_by_fit_review(self):
        altered=copy.deepcopy(self.selection);altered['candidates'][0]['card']['ai_use_mode']='human_link_only'
        with self.assertRaises(ValueError):self.brief(selection=altered)
        with self.assertRaises(ValueError):self.brief(self.request|dict(reviews=[self.review('E10-999')]))
    def test_limits_reject_whole_reference_instead_of_truncating_rights(self):
        with patch.object(bridge,'MAX_REFERENCE_BYTES',100),self.assertRaisesRegex(ValueError,'budget'):self.brief()
    def test_no_fit_result_also_obeys_total_context_budget(self):
        with patch.object(bridge,'MAX_BRIEF_BYTES',100),self.assertRaisesRegex(ValueError,'budget'):
            self.brief(self.request|dict(reviews=[]))
    def test_unsupported_request_or_large_input_is_rejected(self):
        with self.assertRaises(ValueError):self.brief(self.request|dict(unknown='field'))
        path=Path(self.f.tmp.name)/'large-request.json';path.write_bytes(b' '*17000)
        with self.assertRaisesRegex(ValueError,'16 KiB'):bridge.read_request(path)
    def test_citation_url_cannot_be_a_script_or_local_file(self):
        for url in ('file:///private/file','https://:password@example.org/'):
            selection=copy.deepcopy(self.selection);selection['candidates'][0]['entry']['url']=url
            with self.subTest(url=url),self.assertRaises(ValueError):self.brief(selection=selection)
    def test_private_learner_context_is_not_in_shared_reference_snapshot(self):
        req=copy.deepcopy(self.request);req['knowledge']['uncertain']=['PRIVATE_LEARNER_MARKER'];req['reviews'][0]['basis']='PRIVATE_LEARNER_MARKER'
        brief=self.brief(req);self.assertNotIn('PRIVATE_LEARNER_MARKER',brief['reference_markdown'])
        f=units.StudyUnits();f.setUp();self.addCleanup(f.tearDown)
        source=f.f.directory/'r10-notes.md';source.write_text(brief['reference_markdown'],encoding='utf-8')
        ref=dict(kind='text',role='assistant',notes='Own bibliographic annotation, not a copy of the linked lesson',path=str(source),start_line=1,end_line=len(brief['reference_markdown'].splitlines()))
        own=f.card()['sources'][0]|dict(role='assistant',notes='Original lesson and practice')
        f.text.write_text('SELECT name FROM students;\nOriginal task: choose two columns from a new table.\n')
        f.save(f.card('sql-select',title='SELECT columns',objectives=['Choose columns'],criteria=['Solve a new SELECT task independently'],sources=[own,ref]));source.unlink()
        context=f.f.directory/'context-input.json'
        plan=[r['theory_plan'] for r in brief['adaptation']['reviews']]+[r['practice_plan'] for r in brief['adaptation']['reviews']]
        context.write_text(json.dumps(dict(kind='new_topic',topic='SELECT columns',goal='PRIVATE_GOAL',plan=plan)))
        created=unit_learning.learn_unit(f.args(unit_id='sql-select',session='first',lesson_context_file=context,
            expected_revision=0,expected_unit_sha256=f.get('sql-select')['content_sha256']))
        identity=created['learning']['lesson_context']['event']['value']['id']
        journal=f.f.path/'learners/default/unit-learning.json';before=journal.read_bytes()
        with patch.object(lib,'search',side_effect=AssertionError('Saved unit must not query r10')),patch.object(lib,'ZipFile',side_effect=AssertionError('No archive on resume')):
            a=unit_learning.learn_unit(f.args(unit_id='sql-select',session='first',lesson_id=identity,compact=True))
            b=unit_learning.learn_unit(f.args(unit_id='sql-select',session='other-chat',lesson_id=identity,compact=True))
        self.assertEqual(b['learning']['lesson_context']['event']['value']['goal'],'PRIVATE_GOAL')
        self.assertEqual(b['learning']['lesson_context']['event']['value']['plan'],plan)
        self.assertEqual(journal.read_bytes(),before)
        self.assertNotIn('PRIVATE_GOAL',b['unit']['source_contexts'][1]['text'])
        self.assertEqual(a['unit']['content_sha256'],b['unit']['content_sha256']);self.assertFalse(b['unit']['stale'])
        text=b['unit']['source_contexts'][1]['text'];self.assertIn('No AI ingestion permission',text);self.assertNotIn('PRIVATE_LEARNER_MARKER',text)
        self.assertFalse(b['unit']['fact_assurance']['fact_verified']);self.assertFalse(b['learning']['all_criteria_demonstrated'])

if __name__=='__main__':unittest.main()
