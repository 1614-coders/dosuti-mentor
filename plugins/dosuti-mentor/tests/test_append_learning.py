"""Bounded write/read state must preserve full-history behavior."""
import copy,json,sys
from pathlib import Path
from unittest.mock import patch
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import test_streaming_learning as streaming
import unit_learning as ul
import learning_segments as seg

class AppendLearning(unittest.TestCase):
    def setUp(self):self.f=streaming.Streaming();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def test_bounded_view_matches_full_counts_history_and_checkpoint(self):
        f=self.f.f;f.save();f.save(f.event(stage='explained',answer=None,criteria=[],question=None))
        f.save(dict(recap='Recap',next_step='Next',pending_question='Question'),checkpoint=True)
        f.f.save(f.f.card(title='Revised unit'));f.save(f.event(stage='needs_review'))
        full=ul.load(f.path);small=ul.load(f.path,unit_id='maps',compact=True,history_limit=3);unit=f.read()['unit'];args=f.args(session='a',history_limit=3)
        self.assertEqual(ul.view(args,unit,full),ul.view(args,unit,small));self.assertEqual(len(small['_history']),3)
    def test_save_does_not_request_full_load_and_counts_all_attempts(self):
        self.f.seed(count=160);f=self.f.f;original=ul.load
        def load(*a,**kw):
            self.assertTrue(kw.get('compact'),'Write or response requested full history')
            return original(*a,**kw)
        with patch.object(ul,'load',side_effect=load):result=f.save()
        self.assertEqual(result['learning']['attempt_count'],161);self.assertEqual(result['learning']['revision'],161)
        self.assertEqual(ul.load(f.path)['revision'],161)
    def test_append_rejects_stale_revision_without_changes(self):
        self.f.seed();p=self.f.f.path;before=p.read_bytes();event=copy.deepcopy(ul.load(p)['events'][-1]);event['revision']=161
        with self.assertRaises(ValueError):seg.append(p,event,159)
        self.assertEqual(before,p.read_bytes())
    def test_corrupt_unrelated_archive_blocks_write(self):
        self.f.seed(other=True);f=self.f.f;m=json.loads(f.path.read_text());p=f.path.parent/'unit-learning-segments'/(m['segments'][-1]['sha256']+'.json');p.write_text('[]');before=f.path.read_bytes()
        with self.assertRaises(ValueError):f.save(revision=160)
        self.assertEqual(before,f.path.read_bytes())
    def test_history_zero_holds_no_historical_answer_buffer(self):
        self.f.seed();f=self.f.f;small=ul.load(f.path,unit_id='maps',compact=True,history_limit=0)
        self.assertEqual(small['_history'],[]);self.assertEqual(len(small['events']),1)
        self.assertEqual(ul.view(f.args(history_limit=0),f.read()['unit'],small)['attempt_count'],160)

    def test_small_text_uses_file_size_bound_and_detects_change(self):
        import study_units as su
        p=self.f.f.f.text;original=Path.open;requested=[]
        class Stream:
            def __enter__(self):self.handle=original(p,'rb');return self
            def __exit__(self,*args):self.handle.close()
            def read(self,size):requested.append(size);return self.handle.read(size)
        expected=p.read_bytes()
        with patch.object(Path,'open',return_value=Stream()):self.assertEqual(su.text_bytes(p),expected)
        self.assertLess(requested[0],1024)
        from source_integrity import identity
        before=identity(p)
        with patch('source_integrity.identity',side_effect=[before,before[:2]+(before[2]+1,)+before[3:]]):
            with self.assertRaisesRegex(ValueError,'changed'):su.text_bytes(p)
