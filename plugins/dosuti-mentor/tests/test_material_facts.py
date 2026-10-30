"""Fact-review provenance is not an oracle of semantic truth."""
import json
import subprocess
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import topic_research as tr
import test_research_source as fixtures
import unit_learning

class MaterialFacts(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ResearchSource();self.f.setUp();self.addCleanup(self.f.tearDown)
    def claim(self,status='supported',**changes):
        return dict(id='c1',statement='Incorrect assertion: HashMap is thread-safe.',status=status,
            start_line=1,end_line=1,notes='Synthetic model assessment; not an independent fact check',
            evidence=[dict(source_id='doc',start_line=1,end_line=1,quote='Keys and values')])|changes
    def setup_sources(self):self.f.event(self.f.request);self.f.event(self.f.capture)
    def test_incorrect_but_accepted_claim_is_never_fact_verified(self):
        self.setup_sources();self.f.event(self.f.review|dict(claims=[self.claim()]))
        self.f.save();value=self.f.get()['source_contexts'][0]['fact_assurance']
        self.assertFalse(value['fact_verified']);self.assertFalse(value['independent_check'])
        self.assertEqual(value['claims'][0]['recorded_status'],'supported')
        self.assertEqual(value['claims'][0]['fact_status'],'unverified')
        self.assertIn('Incorrect assertion',value['claims'][0]['statement'])
        self.assertEqual(value['claims'][0]['evidence'][0]['quote'],'Keys and values')
    def test_conflicting_sources_are_retained_without_a_winner(self):
        self.setup_sources();self.f.text.write_text('HashMap is thread-safe.\n')
        self.f.event(self.f.capture|dict(id='other'))
        refs=self.claim()['evidence']+[dict(source_id='other',start_line=1,end_line=1,quote='HashMap is thread-safe.')]
        self.f.event(self.f.review|dict(claims=[self.claim('contradictory',evidence=refs)]))
        self.f.save();unit=self.f.get()
        self.assertEqual(unit['fact_assurance']['status'],'contradictory')
        self.assertEqual(unit['material_status']['status'],'limited')
        self.assertEqual(len(unit['source_contexts'][0]['fact_assurance']['claims'][0]['evidence']),2)
        self.assertFalse(unit['fact_assurance']['fact_verified'])
    def test_missing_or_fabricated_reference_cannot_register_a_supported_claim(self):
        self.setup_sources();path=self.f.root/'research/lookup/research.json';before=path.read_bytes()
        for changes in [dict(evidence=[]),dict(evidence=[dict(source_id='missing',start_line=1,end_line=1,quote='Keys and values')]),
                        dict(evidence=[dict(source_id='doc',start_line=1,end_line=1,quote='Invented')]),dict(end_line=100)]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                self.f.event(self.f.review|dict(claims=[self.claim(**changes)]))
            self.assertEqual(path.read_bytes(),before)
    def test_legacy_review_is_unverified_and_read_only(self):
        self.f.ready();self.f.save();before={p:p.read_bytes() for p in self.f.root.rglob('*') if p.is_file()}
        unit=self.f.get();assurance=unit['source_contexts'][0]['fact_assurance']
        self.assertFalse(assurance['fact_verified']);self.assertEqual(assurance['claims'],[])
        self.assertEqual(assurance['status'],'unverified')
        self.assertEqual({p:p.read_bytes() for p in before},before)
    def test_failed_review_write_preserves_previous_journal_and_snapshots(self):
        self.f.ready();before={p:p.read_bytes() for p in self.f.root.rglob('*') if p.is_file()}
        with patch.object(tr,'atomic_json',side_effect=OSError('review fixture failure')):
            with self.assertRaises(OSError):self.f.event(self.f.review|dict(claims=[self.claim()]))
        self.assertEqual({p:p.read_bytes() for p in before},before)
    def test_claim_change_invalidates_unit_and_compact_lesson_keeps_assurance(self):
        self.setup_sources();self.f.event(self.f.review|dict(claims=[self.claim()]))
        self.f.save();self.f.event(self.f.review|dict(claims=[self.claim('insufficient',evidence=[])]))
        self.assertTrue(self.f.get()['stale'])
        self.f.save();view=unit_learning.learn_unit(self.f.f.args(unit_id='maps',compact=True))
        self.assertEqual(view['unit']['fact_assurance']['status'],'insufficient')
        self.assertFalse(view['unit']['fact_assurance']['fact_verified'])
        command=[sys.executable,'-B',str(Path(unit_learning.__file__).with_name('course.py')),'learn-unit',
            '--course',str(self.f.root),'--unit-id','maps','--profile','default','--session','new-chat','--compact']
        result=subprocess.run(command,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['unit']['fact_assurance']['status'],'insufficient')

    def test_cited_snapshot_corruption_is_not_hidden_by_main_source_integrity(self):
        self.setup_sources();self.f.text.write_text('Counterexample\n')
        self.f.event(self.f.capture|dict(id='other'))
        references=self.claim()['evidence']+[dict(source_id='other',start_line=1,end_line=1,quote='Counterexample')]
        value=self.f.event(self.f.review|dict(claims=[self.claim('contradictory',evidence=references)]))
        self.f.save();row=next(r for r in value['sources'] if r['id']=='other')
        path=self.f.root/'research/lookup'/row['snapshot'];path.chmod(0o600);path.write_text('Changed')
        before=self.f.f.path.read_bytes();self.assertTrue(self.f.get()['stale'])
        self.assertEqual(self.f.f.path.read_bytes(),before)
    def test_reading_does_not_infer_a_winner_for_unannotated_conflicting_texts(self):
        self.f.ready();self.f.text.write_text('Contrary assertion\n')
        self.f.event(self.f.capture|dict(id='other'));self.f.event(self.f.review|dict(source_id='other'))
        self.f.f.save(self.f.f.card(sources=[self.f.source,self.f.source|dict(source_id='other',end_line=1)]))
        unit=self.f.get();self.assertEqual(len(unit['source_contexts']),2)
        self.assertFalse(unit['fact_assurance']['fact_verified'])
        self.assertEqual(unit['fact_assurance']['cross_source_consistency'],'not_automatically_checked')
    def test_malformed_review_and_self_granted_verification_are_rejected(self):
        self.setup_sources();path=self.f.root/'research/lookup/research.json';before=path.read_bytes()
        for change in [dict(evidence=[None]),dict(evidence=['link']),dict(fact_verified=True),dict(status='verified')]:
            with self.subTest(change=change),self.assertRaises(ValueError):self.f.event(self.f.review|dict(claims=[self.claim(**change)]))
            self.assertEqual(path.read_bytes(),before)

    def test_stale_claim_review_is_rejected_before_binding_or_writing(self):
        self.setup_sources();path=self.f.root/'research/lookup/research.json';before=path.read_bytes()
        event=self.f.f.f.directory/'stale.json';event.write_text(json.dumps(self.f.review|dict(claims=[self.claim()])))
        with patch.object(tr,'bind_claims',side_effect=AssertionError('Stale request must stop first')):
            with self.assertRaisesRegex(ValueError,'Research changed'):
                tr.research(self.f.f.args(research_id='lookup',event_file=event,expected_revision=0))
        self.assertEqual(path.read_bytes(),before)

    def test_lesson_receives_only_claims_for_selected_lines(self):
        self.setup_sources()
        second=self.claim('insufficient',id='c2',start_line=2,end_line=2,evidence=[])
        self.f.event(self.f.review|dict(claims=[self.claim(),second]))
        self.f.save(self.f.source|dict(end_line=1));unit=self.f.get()
        context=unit['source_contexts'][0]
        self.assertEqual([r['id'] for r in context['review']['claims']],['c1'])
        self.assertEqual([r['id'] for r in context['fact_assurance']['claims']],['c1'])
        self.assertEqual(len(tr.research(self.f.f.args(research_id='lookup'))['sources'][0]['review']['claims']),2)

    def test_reference_cannot_launder_an_incomplete_pdf_marker(self):
        self.setup_sources();self.f.text.write_text('[requires_ocr]\nReference fragment\n')
        self.f.event(self.f.capture|dict(id='incomplete'))
        refs=[dict(source_id='incomplete',start_line=2,end_line=2,quote='Reference fragment')]
        self.f.event(self.f.review|dict(claims=[self.claim(evidence=refs)]));self.f.save()
        assurance=self.f.get()['source_contexts'][0]['fact_assurance']
        self.assertIn('requires_ocr',assurance['warnings'])
        self.assertEqual(assurance['status'],'insufficient')
        self.assertFalse(assurance['fact_verified'])

class ExtractionWarnings(unittest.TestCase):
    def test_pdf_warning_outside_unit_excerpt_is_preserved_with_page_locator(self):
        import test_pdf_import as pdf_tests
        import pdf_fixtures as pdf
        import material_sources as ms
        f=pdf_tests.PDFImport();f.setUp();self.addCleanup(f.doCleanups)
        f.path.write_bytes(pdf.document([pdf.text('Definition fragment'),'q 200 0 0 200 72 500 cm /Im0 Do Q']))
        item=f.attach('resources')['items'][0];item=f.resource('extract',item['id'])['items'][0]
        ms.resource(f.f.args(action='review',resource_id=item['id'],expected_revision=item['revision'],notes='Manually accepted, incomplete scan remains'))
        lines=Path(item['extracted_file']).read_text().splitlines();line=next(i for i,v in enumerate(lines,1) if 'Definition fragment' in v)
        source=dict(kind='resource',role='external',notes='Read page1 only',lesson='lesson-01',resource_id=item['id'],start_line=line,end_line=line)
        f.f.save(f.f.card(sources=[source]));unit=f.f.get();quality=unit['source_contexts'][0]['fact_assurance']
        self.assertIn('requires_ocr',quality['warnings']);self.assertEqual(quality['locators']['pages'],[1])
        self.assertNotIn('requires_ocr',unit['source_contexts'][0]['text'])
        self.assertEqual(unit['material_status']['status'],'limited');self.assertFalse(quality['fact_verified'])
    def test_html_review_marker_and_missing_text_provenance_are_not_proof(self):
        from material_facts import extraction_context,source_assurance
        lines=['# Definition','[review: aside]','Unverified caveat']
        value=source_assurance(dict(kind='text',text=lines[0],start_line=1,end_line=1,
            extraction_quality=extraction_context(lines,1,1)))
        self.assertIn('[review:',value['warnings']);self.assertFalse(value['fact_verified'])
    def test_unclear_speech_remains_uncertain_in_unit_and_compact_learning(self):
        import test_study_units as units
        import course,tutor_packet
        f=units.StudyUnits();f.setUp();self.addCleanup(f.tearDown)
        folder=f.f.add('unclear',0,20,[(1,3,'Unclear ASR term')])
        rows=course.read(folder/'transcript.json');rows[0].update(avg_logprob=-2,no_speech_prob=.9)
        course.save(folder/'transcript.json',rows)
        manifest=course.read(folder/'manifest.json');manifest['evidence_sha256']['transcript.json']=course.file_hash(folder/'transcript.json');course.save(folder/'manifest.json',manifest)
        packet=tutor_packet.build_packet(folder);f.f.save_packet(folder,packet)
        source=dict(kind='video',role='teacher',notes='Recorded source, uncertain words',lesson='lesson-01',start=0,end=20)
        f.save(f.card(sources=[source]))
        value=unit_learning.learn_unit(f.args(unit_id='maps',compact=True))['unit']['source_contexts'][0]['fact_assurance']
        self.assertIn('individual_asr_segments_require_review',value['warnings']);self.assertFalse(value['fact_verified'])

if __name__=='__main__':unittest.main()
