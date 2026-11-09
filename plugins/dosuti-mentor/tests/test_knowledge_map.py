"""On-request map explains saved evidence without introducing grades or mutations."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import test_unit_learning as fixtures
import test_diagnostic_map as diagnostics
import test_study_next_snapshot as snapshots
import adaptive_learning as adaptive
import course
import learning_intent
import study_units
import unit_learning


class KnowledgeMap(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.UnitLearning();self.f.setUp();self.addCleanup(self.f.tearDown)

    def args(self,**kw):return self.f.args(**(dict(session='map-reader',knowledge_map=True,knowledge_offset=0,limit=5)|kw))
    def read(self,**kw):return adaptive.study_next(self.args(**kw))['knowledge_map']
    def files(self):return {str(p):p.read_bytes() for p in self.f.f.f.path.rglob('*') if p.is_file()}
    def items(self,value,section):return value['sections'][section]['items']

    def test_map_is_opt_in_and_read_only_with_no_progress(self):
        before=self.files();self.assertNotIn('knowledge_map',adaptive.study_next(self.f.args()))
        value=self.read();self.assertTrue(value['valid'])
        self.assertEqual(self.items(value,'confirmed'),[])
        row=self.items(value,'unverified')[0]
        self.assertEqual(row['evidence'],[]);self.assertEqual(row['unit'],'maps')
        self.assertEqual(self.files(),before)

    def test_four_sections_keep_independent_assisted_error_and_unknown_separate(self):
        self.f.f.save(self.f.f.card(criteria=['Trace','Implement','Explain','Transfer']))
        self.f.save(self.f.event(criteria=[1]))
        self.f.save(self.f.event(stage='practice_with_help',criteria=[2],hints=2))
        self.f.save(self.f.event(stage='needs_review',criteria=[3],error_tags=['concept']))
        before=self.files();value=self.read()
        for section,index in [('confirmed',1),('partial',2),('gaps',3),('unverified',4)]:
            row=self.items(value,section)[0];self.assertEqual(row['criterion'],index)
            self.assertEqual(row['scope'],'unit_criterion_only')
            self.assertTrue(row['recommendation']['reason'])
        evidence=self.items(value,'confirmed')[0]['evidence'][0]
        self.assertFalse(self.items(value,'confirmed')[0]['fact_assurance']['fact_verified'])
        self.assertEqual(evidence['stage'],'solved_independently');self.assertEqual(evidence['hints'],0)
        self.assertEqual(evidence['answer_excerpt'],'Keys and values');self.assertEqual(evidence['revision'],1)
        self.assertEqual(self.items(value,'partial')[0]['evidence'][0]['hints'],2)
        self.assertEqual(self.items(value,'gaps')[0]['recommendation']['action'],'review_error')
        self.assertEqual(self.files(),before)

    def test_explained_and_legacy_understood_do_not_confirm_skills(self):
        self.f.save(self.f.event(stage='explained',question=None,answer=None,criteria=[]))
        with contextlib.redirect_stdout(io.StringIO()):
            course.progress(self.f.args(topic='Old maps',stage='understood',details='Legacy claim'))
        value=self.read();self.assertEqual(self.items(value,'confirmed'),[])
        self.assertTrue(self.items(value,'unverified'));self.assertTrue(self.items(value,'legacy'))
        self.assertEqual(self.items(value,'legacy')[0]['verification'],'legacy_recorded_claim')

    def test_long_evidence_is_explicitly_excerpted_and_addressable(self):
        answer='Keys and values '+('long explanation '*100)
        self.f.save(self.f.event(answer=answer))
        evidence=self.items(self.read(),'confirmed')[0]['evidence'][0]
        self.assertLessEqual(len(evidence['answer_excerpt']),240)
        self.assertIn('answer',evidence['truncated_fields']);self.assertEqual(evidence['revision'],1)
        # Original evidence remains in the journal, independent of presentation.
        self.assertEqual(json.loads(self.f.path.read_text())['events'][0]['value']['answer'],answer)

    def test_pagination_counts_and_offsets_do_not_hide_unverified_criteria(self):
        self.f.f.save(self.f.f.card(criteria=['Trace','Implement','Explain']))
        first=self.read(limit=1);second=self.read(limit=1,knowledge_offset=1)
        section=first['sections']['unverified']
        self.assertEqual(section['total'],3);self.assertEqual(section['next_offset'],1)
        self.assertEqual(section['items'][0]['criterion'],1)
        self.assertEqual(self.items(second,'unverified')[0]['criterion'],2)
        last=self.read(limit=1,knowledge_offset=2)['sections']['unverified']
        self.assertIsNone(last['next_offset']);self.assertEqual(last['items'][0]['criterion'],3)

    def test_other_profiles_and_courses_cannot_supply_evidence(self):
        self.f.save();other=fixtures.UnitLearning();other.setUp();self.addCleanup(other.tearDown)
        own=self.read();friend=self.read(profile='friend')
        separate=adaptive.study_next(other.args(knowledge_map=True))['knowledge_map']
        self.assertNotEqual(own['course']['id'],separate['course']['id'])
        self.assertEqual(self.items(friend,'confirmed'),[]);self.assertEqual(self.items(separate,'confirmed'),[])

    def test_corrupt_learning_data_never_yields_confident_map(self):
        self.f.save()
        for path in (self.f.path,self.f.path.with_name('learning-intent.json'),self.f.path.with_name('diagnostic-learning.json'),self.f.f.path):
            original=path.read_bytes() if path.exists() else None
            try:
                path.write_text('{broken');before=self.files();value=self.read()
                self.assertFalse(value['valid']);self.assertEqual(value['sections'],{})
                self.assertEqual(value['next_choices'],[]);self.assertTrue(value['reason'])
                self.assertEqual(self.files(),before)
            finally:
                if original is None:path.unlink(missing_ok=True)
                else:path.write_bytes(original)

    def test_stale_material_is_unavailable_instead_of_confirmed(self):
        self.f.save();unit=self.f.read()['unit']
        path=self.f.f.f.path/unit['sources'][0]['snapshot'];path.chmod(0o600);path.write_text('Changed')
        value=self.read();self.assertEqual(self.items(value,'confirmed'),[])
        self.assertEqual(self.items(value,'unavailable')[0]['unit'],'maps')

    def test_accepted_route_and_legacy_journal_are_unchanged(self):
        for n,change in enumerate((dict(goal=dict(description='Learn maps')),
                dict(proposal=dict(summary='Maps route',steps=['Trace','Practice'])),dict(accept_proposal=True))):
            learning_intent.commit(self.f.args(expected_revision=n,reason='Learner consent'),change)
        self.f.save();raw=json.loads(self.f.path.read_text());raw['schema']='unit_learning_v1'
        self.f.path.write_text(json.dumps(raw));before=self.files();value=self.read()
        self.assertTrue(self.items(value,'confirmed'));self.assertTrue(value['acceptance_required'])
        self.assertEqual(self.files(),before)

    def test_cli_compact_map_and_invalid_offset(self):
        script=Path(course.__file__)
        command=[sys.executable,'-B',str(script),'study-next','--course',str(self.f.args().course),'--knowledge-map','--compact']
        before=self.files();result=subprocess.run(command,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        value=json.loads(result.stdout)['knowledge_map'];self.assertTrue(value['valid'])
        self.assertEqual(self.files(),before)
        for kw in (dict(knowledge_offset=-1),dict(knowledge_map=False,knowledge_offset=1)):
            with self.subTest(kw=kw),self.assertRaises(ValueError):self.read(**kw)


class DiagnosticKnowledgeMap(unittest.TestCase):
    def setUp(self):
        self.f=diagnostics.DiagnosticMap();self.f.setUp();self.addCleanup(self.f.doCleanups)
    def read(self):return adaptive.study_next(self.f.d.f.args(knowledge_map=True))['knowledge_map']
    def rows(self,value,section):return [r for r in value['sections'][section]['items'] if r['source']=='diagnostic']

    def test_confirmed_diagnostic_scope_and_revisions_are_preserved(self):
        self.f.answer();self.f.answer();value=self.read();row=self.rows(value,'confirmed')[0]
        self.assertEqual(row['scope'],'observed_criteria_and_difficulties_only')
        self.assertEqual(row['confirmed_levels'],['applied']);self.assertEqual(len(row['evidence']),2)
        self.assertTrue(all(e['assistance']=='independent' for e in row['evidence']))
        self.assertTrue(all(e['answer_revision'] for e in row['evidence']))
        self.assertEqual(self.rows(value,'unverified')[0]['evidence'],[])

    def test_assistance_is_partial_and_unknown_is_not_a_gap(self):
        self.f.answer(help='assisted');row=self.rows(self.read(),'partial')[0]
        self.assertEqual(row['evidence'][0]['assistance'],'assisted')
        self.f.d.save(dict(action='complete'));self.f.d.accept();self.f.d.prompt();self.f.d.answer('unknown','basic')
        value=self.read();self.assertEqual(self.rows(value,'gaps'),[])
        self.assertTrue(self.rows(value,'unverified'))

    def test_conflicting_diagnostic_results_do_not_turn_into_confirmation(self):
        self.f.answer('incorrect');self.f.answer();value=self.read();row=self.rows(value,'gaps')[0]
        self.assertTrue(row['has_conflicts']);self.assertEqual(self.rows(value,'confirmed'),[])
        self.assertEqual({e['result'] for e in row['evidence']},{'incorrect','correct'})
        self.assertTrue(row['evidence'][0]['critical_errors'])

    def test_unit_success_does_not_erase_even_same_named_diagnostic_gap(self):
        self.f.answer('incorrect');args=self.f.d.f.args()
        source=args.course.parent/'map-source.txt';source.write_text('Equal keys replace values\n')
        path=args.course.parent/'knowledge-input.json'
        path.write_text(json.dumps(dict(id='maps',title='Java duplicate keys',summary='Maps',
            objectives=['Trace keys'],criteria=['Trace a replacement'],prerequisites=[],uncertainties=[],
            sources=[dict(kind='text',role='external',notes='Read',path=str(source),start_line=1,end_line=1)])))
        study_units.unit(self.f.d.f.args(unit_file=path,expected_revision=0))
        unit=study_units.unit(self.f.d.f.args(unit_id='maps'))['unit']
        path.write_text(json.dumps(dict(stage='solved_independently',details='Checked this criterion',
            question='Trace another example',answer='New value replaces the old',hints=0,criteria=[1])))
        unit_learning.learn_unit(self.f.d.f.args(unit_id='maps',event_file=path,session='lesson',
            expected_revision=0,expected_unit_sha256=unit['content_sha256']))
        value=self.read()
        self.assertTrue(any(r['source']=='unit' for r in value['sections']['confirmed']['items']))
        self.assertEqual(self.rows(value,'confirmed'),[])
        self.assertEqual(self.rows(value,'gaps')[0]['competence'],'Java duplicate keys')


class KnowledgeSnapshot(unittest.TestCase):
    def test_concurrent_writer_cannot_mix_knowledge_and_recommendations(self):
        f=snapshots.StudyNextSnapshot();f.setUp();self.addCleanup(f.f.tearDown)
        original=adaptive.recommendations;changed=False
        def interleave(*args,**kw):
            nonlocal changed
            if not changed:changed=True;f.other_chat_answer()
            return original(*args,**kw)
        with patch.object(adaptive,'recommendations',interleave):
            value=adaptive.study_next(f.f.args(knowledge_map=True,session='reader'))
        self.assertTrue(value['valid']);self.assertEqual(value['journal_revision'],2)
        self.assertEqual(value['knowledge_map']['sections']['gaps']['items'],[])
        row=value['knowledge_map']['sections']['confirmed']['items'][0]
        self.assertEqual(row['evidence'][0]['revision'],2)
