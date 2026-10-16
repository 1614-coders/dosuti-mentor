import json,sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course,learning_tools as lt
import test_semantic_context

class TopicScope(unittest.TestCase):
    def setUp(self):self.f=test_semantic_context.SemanticContext();self.f.setUp();self.f.save()
    def tearDown(self):self.f.tearDown()
    def stale(self):return lt.topic(self.f.args(topic_file=None))['topics'][0]['stale']
    def test_later_packet_preserves_unit(self):
        self.f.f.add('later',60,90,[(65,70,'Later')]);self.assertFalse(self.stale());self.f.select()
    def test_overlap_invalidates_unit(self):
        self.f.f.add('overlap',10,20,[(11,15,'Changed')]);self.assertTrue(self.stale())
    def test_removed_source_invalidates_unit(self):
        p=self.f.f.lesson/'lesson.json';d=course.read(p);d['prepared_fragments'].pop();course.save(p,d);self.assertTrue(self.stale())
    def test_selected_transcript_tamper_invalidates(self):
        p=self.f.f.lesson/'prepared/a/transcript.json';p.write_text('[]');self.assertTrue(self.stale())
    def test_unrelated_corrupt_packet_does_not_invalidate(self):
        p=self.f.f.add('later',60,90,[(65,70,'Later')]);(p/'tutor_packet.json').write_text('{}');self.assertFalse(self.stale())
    def test_legacy_scope_remains_conservative(self):
        p=self.f.f.lesson/'lesson.json';d=course.read(p);d['topics']['example'].pop('evidence_scope');course.save(p,d)
        self.assertFalse(self.stale());self.f.f.add('later',60,90,[(65,70,'Later')]);self.assertTrue(self.stale())
    def test_unknown_scope_rejected(self):
        p=self.f.f.lesson/'lesson.json';d=course.read(p);d['topics']['example']['evidence_scope']='future';course.save(p,d);self.assertTrue(self.stale())
