"""Operation counts and exact quality oracles, no timing-dependent pass thresholds."""
import copy
import io
import json
import random
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import preparation_control as control
import material_facts
import topic_research as research
import tutor_packet
from read_snapshot import operation
import test_material_facts as facts
import test_visual_materials as visuals

class QualityOptimization(unittest.TestCase):
    def test_small_capture_does_not_allocate_a_full_capture_budget(self):
        calls=[];original=control.tempfile.TemporaryFile
        class Capture:
            def __init__(self):self.file=original()
            def __getattr__(self,key):return getattr(self.file,key)
            def __enter__(self):return self
            def __exit__(self,*args):self.file.close()
            def read(self,n=-1):calls.append(n);return self.file.read(n)
        with patch.object(control.tempfile,'TemporaryFile',Capture):
            result=control.run([sys.executable,'-B','-c','import sys; sys.stdout.buffer.write(b"exact output\\n")'],capture_output=True,timeout=3)
        self.assertEqual(result.stdout,b'exact output\n')
        self.assertLessEqual(max(calls),100)
    def test_capture_size_drift_is_rejected_instead_of_truncated(self):
        original=control.tempfile.TemporaryFile;files=[]
        class Capture:
            def __init__(self):self.file=original();files.append(self.file)
            def __getattr__(self,key):return getattr(self.file,key)
            def __enter__(self):return self
            def __exit__(self,*args):self.file.close()
            def read(self,n=-1):
                value=self.file.read(n)
                return value+b'unexpected' if value else value
        with patch.object(control.tempfile,'TemporaryFile',Capture):
            with self.assertRaisesRegex(RuntimeError,'changed during collection'):
                control.run([sys.executable,'-B','-c','print("real bytes")'],capture_output=True,timeout=3)
        self.assertTrue(all(f.closed for f in files))
    def fixture(self):
        f=facts.MaterialFacts();f.setUp();self.addCleanup(f.doCleanups);f.setup_sources()
        f.f.event(f.f.review|dict(claims=[f.claim()]));return f.f
    def test_claim_snapshot_is_read_once_per_operation_and_again_next_operation(self):
        f=self.fixture();source=f.source
        import research_source
        original=Path.read_bytes;reads=[]
        def read(path,*a,**kw):
            if path.parent.name=='snapshots':reads.append(str(path))
            return original(path,*a,**kw)
        with patch.object(Path,'read_bytes',read):
            with operation():research_source.evidence(f.root,source,writing=True)
            self.assertEqual(len(reads),1)
            with operation():research_source.evidence(f.root,source,writing=True)
            self.assertEqual(len(reads),2)
    def test_snapshot_tampering_in_the_same_operation_is_not_cached_as_valid(self):
        f=self.fixture();folder,data,state=research.load_research(f.root,'lookup');capture=state['sources']['doc']
        path=folder/capture['snapshot']
        with operation():
            self.assertIn('Keys',research.snapshot(folder,capture))
            path.chmod(0o600);path.write_text('Different text')
            with self.assertRaises(ValueError):research.snapshot(folder,capture)
    def test_marker_scan_does_not_match_a_regex_for_each_unmarked_line(self):
        lines=['Ordinary source line']*10000+['[PDF page 2; layout]','[requires_ocr]']
        original=material_facts.re.match;calls=[]
        def match(*a,**kw):calls.append(1);return original(*a,**kw)
        with patch.object(material_facts.re,'match',match):result=material_facts.extraction_context(lines,1,2)
        self.assertIn('requires_ocr',result['warnings'])
        self.assertLess(len(calls),10)
    def test_marker_order_pages_and_caveats_exactly_match_reference(self):
        # Independent frozen reference from 0.8.4, including warnings outside excerpt.
        def expected(lines,start,end):
            pages=[];warnings=[];page=None;truncated=False
            markers=('requires_ocr','no_text_layer','unmapped_glyphs','images not interpreted','missing/merged: review','headers unverified','[review:')
            for n,line in enumerate(lines,1):
                m=material_facts.re.match(r'\[PDF page (\d+)(?:;|\])',line)
                if m:page=int(m[1])
                if start<=n<=end and page is not None and page not in pages:
                    if len(pages)<200:pages.append(page)
                    else:truncated=True
                for marker in markers:
                    if marker in line and marker not in warnings:warnings.append(marker)
            return dict(pages=pages,pages_truncated=truncated,warnings=warnings,
                scope='explicit_markers_in_whole_source_not_semantic_validation',page_binding='extracted_text_marker_not_independent_attestation')
        rng=random.Random(86)
        lines=[rng.choice(['plain','requires_ocr headers unverified','[PDF page 7; layout]','[review: aside]','unmapped_glyphs no_text_layer']) for _ in range(200)]
        self.assertEqual(material_facts.extraction_context(lines,9,50),expected(lines,9,50))
    def test_navigation_preserves_crossings_ids_and_boundary_frames(self):
        speech=[dict(id='s'+str(i),start=i*13,end=i*13+150) for i in range(200)]
        frames=[dict(id='f'+str(i),approx_seconds=i*30) for i in range(101)]
        expected=[]
        for start in range(0,3000,120):
            stop=min(start+120,3000)
            expected.append(dict(start=start,end=stop,boundary_type='time_window',
                speech_ids=[r['id'] for r in speech if r['start']<stop and r['end']>start],
                frame_ids=[f['id'] for f in frames if start<=f['approx_seconds']<stop]))
        self.assertEqual(tutor_packet.navigation_windows(speech,frames,0,3000),expected)
    def test_duplicate_selected_image_is_not_inspected_twice(self):
        f=visuals.VideoVisuals();f.setUp();self.addCleanup(f.doCleanups)
        f.f.save(f.f.card(sources=[f.source]));context=f.f.get()['source_contexts'][0]
        frames=context['context']['frames'][:2];self.assertEqual(frames[0]['sha256'],frames[1]['sha256'])
        source=f.source|dict(visuals=[dict(id=r['id'],caption='Same state',notes='Inspected temporal link') for r in frames])
        from visual_materials import bind
        from PIL import Image
        with patch.object(Image,'open',wraps=Image.open) as opened:
            selected,result=bind(source,copy.deepcopy(context),writing=True)
        self.assertEqual(len(selected),2);self.assertEqual(opened.call_count,1)

    def test_markdown_keeps_each_term_time_and_individual_warning(self):
        import test_context as fixtures
        import course
        f=fixtures.ContextRegression();f.setUp();self.addCleanup(f.tearDown)
        folder=f.add('quality',0,20,[(1,3,'TERM_SAFE'),(4,6,'TERM_UNCERTAIN')])
        rows=course.read(folder/'transcript.json');rows[1].update(avg_logprob=-2,no_speech_prob=.9)
        course.save(folder/'transcript.json',rows)
        manifest=course.read(folder/'manifest.json');manifest['evidence_sha256']['transcript.json']=course.file_hash(folder/'transcript.json');course.save(folder/'manifest.json',manifest)
        tutor_packet.build_packet(folder)
        value=(folder/'tutor_context.md').read_text()
        for term in ('TERM_SAFE','TERM_UNCERTAIN','00:00:01','00:00:04','suspicious_asr_metrics'):
            self.assertIn(term,value)
        self.assertEqual(value.count('ASR не проверен'),2)

if __name__=='__main__':unittest.main()
