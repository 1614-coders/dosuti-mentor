import contextlib,io,json,sys
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course,learning_tools,semantic_context,test_context

class SemanticContext(unittest.TestCase):
    def setUp(self):
        self.f=test_context.ContextRegression();self.f.setUp()
        self.f.add('a',0,30,[(10,25,'The full sentence continues')])
        self.f.add('b',30,60,[(35,45,'The example ends')])
    def tearDown(self):self.f.tearDown()
    def args(self,**kw):return NS(**(vars(self.f.args)|kw))
    def save(self,review=True,**kw):
        v=dict(id='example',title='Full example',summary='Reviewed QA example',keywords=[],start=0,end=60,code_files=[])
        if review:v['boundary_review']=dict(start='complete',end='complete',notes='QA boundary review')
        v.update(kw);p=self.f.directory/'topic.json';p.write_text(json.dumps(v))
        return learning_tools.topic(self.args(topic_file=p))
    def select(self):
        out=io.StringIO()
        with contextlib.redirect_stdout(out):course.context(self.args(topic_id='example'))
        return json.loads(out.getvalue())
    def test_full_unit_spans_packets(self):
        self.save();r=self.select();self.assertEqual(r['requested_window'],[0,60]);self.assertEqual(len(r['speech']),2);self.assertEqual(r['teaching_unit']['id'],'example')
    def test_old_topic_requires_boundary_review(self):
        self.save(review=False)
        with self.assertRaises(ValueError):self.select()
    def test_open_boundary_is_not_taught_as_complete(self):
        for edge in ('start','end'):
            for state in ('continuation','unknown'):
                self.save(boundary_review=dict(start='complete',end='complete',notes='Needs adjacent context')|{edge:state})
                with self.assertRaises(ValueError):self.select()
    def test_stale_topic_rejected(self):
        self.save();self.f.add('c',20,30,[(22,25,'changed')])
        with self.assertRaises(ValueError):self.select()
    def test_unknown_topic_rejected(self):
        with self.assertRaises(ValueError):self.select()
    def test_crossing_sentence_retained_and_flagged(self):
        r=self.f.context(15,5);self.assertEqual(r['boundary_speech'][0]['text'],'The full sentence continues')
        self.assertTrue(r['boundary_speech'][0]['crosses_start']);self.assertTrue(r['boundary_speech'][0]['crosses_end'])
        self.assertIn('speech_crosses_window_boundary',r['warnings'])
    def test_exact_boundaries_do_not_warn(self):
        r=self.f.context(10,15);self.assertEqual(r['boundary_speech'],[])
    def test_bad_review_not_published(self):
        for review in [None,{},dict(start='complete',end='complete',notes=''),dict(start='yes',end='complete',notes='qa')]:
            with self.assertRaises(ValueError):self.save(boundary_review=review)
    def test_unprepared_gap_rejected(self):
        with self.assertRaises(ValueError):self.save(end=70)
