"""Streaming reads must preserve validation and recommendation semantics."""
import copy,hashlib,json,sys
from pathlib import Path
from unittest.mock import patch
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import test_unit_learning
import unit_learning as ul
import learning_segments as seg
import adaptive_learning as al
import study_units as su

class Streaming(unittest.TestCase):
    def setUp(self):self.f=test_unit_learning.UnitLearning();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def seed(self,count=160,other=False):
        self.f.save();base=ul.load(self.f.path)['events'][0];folder=self.f.path.parent/'unit-learning-segments';folder.mkdir(exist_ok=True)
        refs=[]
        for start in range(0,count,40):
            events=[]
            for n in range(start,min(start+40,count)):
                event=copy.deepcopy(base);event['revision']=n+1
                if other and n:event['unit']='other'
                event['value']['answer']='x'*2000;events.append(event)
            raw=seg.encoded(events);sha=hashlib.sha256(raw).hexdigest();(folder/(sha+'.json')).write_bytes(raw);refs.append(dict(sha256=sha,count=len(events)))
        self.f.path.write_text(json.dumps(dict(schema=seg.SCHEMA,revision=count,segments=refs,events=[])))
    def test_compact_keeps_latest_not_all_answers(self):
        self.seed();full=ul.load(self.f.path);compact=ul.load(self.f.path,compact=True)
        self.assertEqual(len(full['events']),160);self.assertEqual(len(compact['events']),1);self.assertEqual(compact['revision'],160)
        unit=self.f.read()['unit'];now='2099-01-01T00:00:00Z'
        self.assertEqual(al.recommendations(unit,full,now),al.recommendations(unit,compact,now))
    def test_selected_unit_discards_other_history_but_counts_global_revision(self):
        self.seed(other=True);selected=ul.load(self.f.path,unit_id='maps');self.assertEqual(len(selected['events']),1);self.assertEqual(selected['revision'],160)
        self.assertEqual(self.f.read()['learning']['attempt_count'],1)
    def test_damage_in_unselected_segment_is_not_skipped(self):
        self.seed(other=True);manifest=json.loads(self.f.path.read_text());entry=manifest['segments'][-1];p=self.f.path.parent/'unit-learning-segments'/(entry['sha256']+'.json');p.write_text('[]')
        with self.assertRaises(ValueError):ul.load(self.f.path,unit_id='maps')
        with self.assertRaises(ValueError):ul.load(self.f.path,compact=True)
    def test_compaction_preserves_multicriterion_precedence_and_checkpoints(self):
        self.f.f.save(self.f.f.card(criteria=['A','B']))
        self.f.save(self.f.event(criteria=[1,2]));self.f.save(self.f.event(stage='needs_review',criteria=[1]))
        self.f.save(dict(recap='A',next_step='Next',pending_question='Pending?'),checkpoint=True,session='a')
        self.f.save(dict(recap='B',next_step='Next B',pending_question=None),checkpoint=True,session='b')
        d=ul.load(self.f.path);compact=ul.load(self.f.path,compact=True);unit=self.f.read()['unit'];now='2099-01-01T00:00:00Z'
        self.assertEqual(al.recommendations(unit,d,now),al.recommendations(unit,compact,now))
        for session in ['a','b']:
            args=self.f.args(session=session);self.assertEqual(ul.view(args,unit,d)['checkpoint'],ul.view(args,unit,compact)['checkpoint'])
    def test_missing_or_bad_legacy_event_count_rejected(self):
        self.f.save();d=json.loads(self.f.path.read_text());d['revision']+=1;self.f.path.write_text(json.dumps(d))
        with self.assertRaises(ValueError):ul.load(self.f.path,compact=True)
    def test_contexts_not_retained_but_source_damage_still_detected(self):
        root=self.f.f.f.path;registry=su.load(root);rows=su.checked_units(root,registry,include_contexts=False)
        self.assertEqual(rows['maps']['source_contexts'],[]);self.assertFalse(rows['maps']['stale'])
        p=root/registry['units']['maps']['sources'][0]['snapshot'];p.chmod(0o600);p.write_text('Corrupt')
        self.assertTrue(su.checked_units(root,registry,include_contexts=False)['maps']['stale'])
    def test_segmented_write_does_not_serialize_all_old_events(self):
        self.seed();journal=ul.load(self.f.path);event=copy.deepcopy(journal['events'][-1]);event['revision']=161;journal['events'].append(event);journal['revision']=161
        original=seg.encoded
        def checked(value):
            if isinstance(value,dict) and value.get('schema')==ul.SCHEMA and len(value.get('events',[]))>160:raise AssertionError('Entire archive serialized')
            return original(value)
        with patch.object(seg,'encoded',side_effect=checked):seg.write(self.f.path,journal)
        self.assertEqual(ul.load(self.f.path)['revision'],161)
