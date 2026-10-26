"""Operation-local reuse must retain integrity and CAS guarantees."""
import json
from unittest import TestCase
from unittest.mock import patch
import test_unit_learning
import adaptive_learning
import study_units
import unit_learning


class OperationReuse(TestCase):
    def setUp(self):
        self.f=test_unit_learning.UnitLearning();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.f.save()

    def test_shared_text_is_read_once_and_rechecked_next_request(self):
        root=self.f.args().course
        data=json.loads(self.f.f.path.read_text());template=data['units']['maps']
        for i in range(1,20):
            row=dict(template,id='topic-'+str(i))
            row['content_sha256']=study_units.digest(study_units.semantic(row))
            data['units'][row['id']]=row
        data['revision']=20;self.f.f.path.write_text(json.dumps(data))
        with patch.object(study_units,'text_bytes',wraps=study_units.text_bytes) as reads:
            first=adaptive_learning.study_next(self.f.args())
            self.assertTrue(first['valid']);self.assertEqual(reads.call_count,1)
            second=adaptive_learning.study_next(self.f.args())
            self.assertTrue(second['valid']);self.assertEqual(reads.call_count,2)

    def test_write_does_not_replay_the_postwrite_journal_twice(self):
        from learning_segments import write
        data=json.loads(self.f.path.read_text());event=data['events'][0]
        write(self.f.path,dict(schema=data['schema'],revision=50,
            events=[dict(event,revision=i+1) for i in range(50)]))
        checksum=self.f.read()['unit']['content_sha256']
        incoming=self.f.f.f.directory/'answer.json';incoming.write_text(json.dumps(self.f.event()))
        with patch.object(unit_learning,'validate',wraps=unit_learning.validate) as validations:
            value=unit_learning.learn_unit(self.f.args(event_file=incoming,expected_revision=50,expected_unit_sha256=checksum,session='a'))
        self.assertTrue(value['learning']['valid'])
        self.assertEqual(value['learning']['revision'],51)
        self.assertEqual(value['learning_progress']['unit_event_count'],51)
        self.assertLessEqual(validations.call_count,103)

    def test_read_projections_match_and_do_not_mutate_files(self):
        from read_snapshot import operation
        before={p:p.read_bytes() for p in self.f.args().course.rglob('*') if p.is_file()}
        expected=unit_learning.load(self.f.path,unit_id='maps',compact=True,history_limit=1)
        with operation():
            unit_learning.load(self.f.path,compact=True)
            actual=unit_learning.load(self.f.path,unit_id='maps',compact=True,history_limit=1)
        self.assertEqual(expected,actual)
        self.assertEqual(before,{p:p.read_bytes() for p in before})

    def test_cas_rejects_concurrent_answer_even_with_cached_read(self):
        from read_snapshot import operation
        from test_study_next_snapshot import StudyNextSnapshot
        helper=StudyNextSnapshot();helper.f=self.f
        import course
        from pathlib import Path
        helper.script=Path(course.__file__)
        with operation():
            unit_learning.load(self.f.path,compact=True)
            helper.other_chat_answer()
            with self.assertRaisesRegex(ValueError,'changed in another chat'):
                self.f.save(revision=1)
            self.assertEqual(unit_learning.load(self.f.path,compact=True)['revision'],2)

    def test_source_corruption_after_cache_hit_is_detected(self):
        import learning_intent
        root=self.f.args().course
        template=json.loads(self.f.f.path.read_text())['units']['maps']
        source=root/template['sources'][0]['snapshot']
        original=learning_intent.view;changed=False
        def interleave(*args,**kwargs):
            nonlocal changed
            value=original(*args,**kwargs)
            if not changed:
                changed=True;source.chmod(0o600);source.write_text('corrupted')
            return value
        with patch.object(learning_intent,'view',interleave):
            value=adaptive_learning.study_next(self.f.args())
        self.assertEqual(value['recommendations'],[])
        self.assertTrue(value['blocked'] or not value['valid'])
        with self.assertRaisesRegex(ValueError,'source changed'):
            self.f.save(revision=1)

    def test_segment_damage_cannot_be_hidden_by_operation_cache(self):
        import learning_segments
        from read_snapshot import operation
        data=json.loads(self.f.path.read_text());event=data['events'][0]
        with patch.object(learning_segments,'LIMIT',5000):
            learning_segments.write(self.f.path,dict(schema=data['schema'],revision=100,
                events=[dict(event,revision=i+1) for i in range(100)]))
        manifest=json.loads(self.f.path.read_text())
        segment=self.f.path.parent/'unit-learning-segments'/(manifest['segments'][0]['sha256']+'.json')
        with operation():
            unit_learning.load(self.f.path,compact=True)
            segment.write_bytes(b'!'+segment.read_bytes()[1:])
            with self.assertRaises(ValueError):unit_learning.load(self.f.path,compact=True)

    def test_text_reuse_has_bounded_retention(self):
        from read_snapshot import operation
        root=self.f.args().course
        a=root/'a.txt';b=root/'b.txt';a.write_text('a');b.write_text('b')
        with operation(), patch.object(study_units,'text_bytes',wraps=study_units.text_bytes) as reads:
            study_units._text_lines(a);study_units._text_lines(a)
            self.assertEqual(reads.call_count,1)
            study_units._text_lines(b);study_units._text_lines(a)
            self.assertEqual(reads.call_count,3)

    def test_change_during_read_does_not_cache_old_value_under_new_stamp(self):
        from read_snapshot import cached, observed, operation
        path=self.f.args().course/'changing.txt';path.write_text('old')
        changed=False
        @cached
        @observed
        def read(source):
            nonlocal changed
            value=source.read_text()
            if not changed:
                changed=True;source.write_text('new')
            return value
        with operation():
            self.assertEqual(read(path),'old')
            self.assertEqual(read(path),'new')
