"""A study-next response must never combine different learning revisions."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import test_unit_learning
import adaptive_learning as adaptive
import course
import learning_intent
import learning_segments


class StudyNextSnapshot(unittest.TestCase):
    def setUp(self):
        self.f=test_unit_learning.UnitLearning();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.f.save(self.f.event(stage='needs_review'))
        self.script=Path(course.__file__)

    def files(self):
        return {str(p.relative_to(self.f.f.f.path)):p.read_bytes()
                for p in self.f.f.f.path.rglob('*') if p.is_file()}

    def other_chat_answer(self, stage='solved_independently'):
        # Actual writer locks/CAS in another interpreter: no reader cache leaks.
        data=json.loads(self.f.path.read_text());incoming=self.f.f.f.directory/'other-chat-answer.json'
        incoming.write_text(json.dumps(self.f.event(stage=stage)))
        checksum=json.loads(self.f.f.path.read_text())['units']['maps']['content_sha256']
        result=subprocess.run([sys.executable,'-B',str(self.script),'learn-unit',
            '--course',str(self.f.f.f.path),'--profile','default','--session','other',
            '--unit-id','maps','--event-file',str(incoming),
            '--expected-revision',str(data['revision']),'--expected-unit-sha256',checksum],
            capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    def assert_consistent_or_incomplete(self,value):
        if value['valid']:
            self.assertEqual(value['journal_revision'],value['learning_progress']['unit_event_count'])
            self.assertIn('maps',value['learning_progress']['demonstrated_units'])
            self.assertFalse(any(r['action']=='review_error' for r in value['recommendations']))
        else:self.assertEqual(value['recommendations'],[])

    def other_chat_update(self, command, value):
        incoming=self.f.f.f.directory/'other-chat-update.json'
        incoming.write_text(json.dumps(value))
        result=subprocess.run([sys.executable,'-B',str(self.script),command,
            '--course',str(self.f.f.f.path),'--profile','default','--session','other',
            '--update-file',str(incoming),'--expected-revision','0','--reason','QA concurrent update'],
            capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_other_chat_commits_between_recommendation_and_progress(self):
        original=adaptive.recommendations;changed=False
        def interleave(*args,**kw):
            nonlocal changed
            if not changed:changed=True;self.other_chat_answer()
            return original(*args,**kw)
        with patch.object(adaptive,'recommendations',interleave):
            value=adaptive.study_next(self.f.args(session='reader'))
        self.assert_consistent_or_incomplete(value)
        self.assertTrue(value['valid']);self.assertEqual(value['journal_revision'],2)

    def test_continuous_other_chat_writes_have_bounded_incomplete_result(self):
        original=adaptive.recommendations;writes=0
        def interleave(*args,**kw):
            nonlocal writes
            writes+=1;self.assertLessEqual(writes,12,'Read retry loop is unbounded')
            self.other_chat_answer()
            return original(*args,**kw)
        with patch.object(adaptive,'recommendations',interleave):
            value=adaptive.study_next(self.f.args(session='reader'))
        self.assertFalse(value['valid']);self.assertEqual(value['recommendations'],[])
        self.assertIn('changed',value['reason'].lower());self.assertLessEqual(writes,12)

    def test_normal_read_preserves_files_and_cli_response_fields(self):
        before=self.files();value=adaptive.study_next(self.f.args(session='reader'))
        self.assertTrue(value['valid']);self.assertEqual(value['journal_revision'],1)
        self.assertEqual(value['learning_progress']['unit_event_count'],1)
        self.assertEqual(value['recommendations'][0]['action'],'review_error')
        self.assertEqual(set(value),{'valid','profile','session','journal_revision','as_of',
            'learning_intent','learning_progress','recommendations','recommendation_count','truncated',
            'blocked','blocked_count','continuations','continuation_count','roadmap','policy','verification'})
        self.assertEqual(before,self.files())

    def test_corrupt_journals_never_offer_recommendations_or_write(self):
        for name in ('unit-learning.json','diagnostic-learning.json','learning-intent.json'):
            path=self.f.path.with_name(name);old=path.read_bytes() if path.exists() else None
            try:
                path.write_text('{broken');before=self.files()
                value=adaptive.study_next(self.f.args(session='reader'))
                self.assertFalse(value['valid'],name);self.assertEqual(value['recommendations'],[])
                self.assertEqual(before,self.files())
            finally:
                if old is None:path.unlink(missing_ok=True)
                else:path.write_bytes(old)

    def test_snapshot_cache_does_not_survive_the_request(self):
        first=adaptive.study_next(self.f.args(session='reader'))
        self.other_chat_answer();before=self.files()
        second=adaptive.study_next(self.f.args(session='reader'))
        self.assertEqual(first['journal_revision'],1);self.assertEqual(second['journal_revision'],2)
        self.assert_consistent_or_incomplete(second);self.assertEqual(before,self.files())

    def test_answer_committed_after_last_progress_read_is_detected(self):
        original=learning_intent.view;changed=False
        def interleave(*args,**kw):
            nonlocal changed
            value=original(*args,**kw)
            if not changed:changed=True;self.other_chat_answer()
            return value
        with patch.object(learning_intent,'view',interleave):
            value=adaptive.study_next(self.f.args(session='reader'))
        self.assertTrue(value['valid']);self.assertEqual(value['journal_revision'],2)
        self.assert_consistent_or_incomplete(value)

    def test_creation_of_missing_intent_and_diagnostic_invalidates_snapshot(self):
        original=learning_intent.view
        for command,update in [('intent',{'goal':{'description':'New goal'}}),
                               ('diagnostic',{'action':'decline'})]:
            with self.subTest(command=command):
                changed=False
                def interleave(*args,**kw):
                    nonlocal changed
                    value=original(*args,**kw)
                    if not changed:changed=True;self.other_chat_update(command,update)
                    return value
                with patch.object(learning_intent,'view',interleave):
                    value=adaptive.study_next(self.f.args(session='reader'))
                self.assertTrue(value['valid'])
                if command=='intent':
                    self.assertEqual(value['learning_intent']['goal']['description'],'New goal')
                else:self.assertEqual(value['learning_progress']['diagnostic']['status'],'declined')

    def test_corruption_after_read_returns_incomplete_without_reader_writes(self):
        original=learning_intent.view;changed=False;damaged=None
        def interleave(*args,**kw):
            nonlocal changed,damaged
            value=original(*args,**kw)
            if not changed:
                changed=True;self.f.path.write_text('{broken');damaged=self.files()
            return value
        with patch.object(learning_intent,'view',interleave):
            value=adaptive.study_next(self.f.args(session='reader'))
        self.assertFalse(value['valid']);self.assertEqual(value['recommendations'],[])
        self.assertEqual(damaged,self.files())

    def test_profiles_do_not_share_cached_journals(self):
        reader=adaptive.study_next(self.f.args(profile='default',session='reader'))
        other=adaptive.study_next(self.f.args(profile='other',session='reader'))
        self.assertEqual(reader['journal_revision'],1);self.assertEqual(other['journal_revision'],0)
        self.assertEqual(other['learning_progress']['unit_event_count'],0)

    def test_segment_corruption_after_cached_read_is_not_hidden(self):
        first=json.loads(self.f.path.read_text())['events'][0]
        events=[dict(first,revision=i+1) for i in range(100)]
        with patch.object(learning_segments,'LIMIT',5000):
            learning_segments.write(self.f.path,dict(schema='unit_learning_v2',revision=100,events=events))
        manifest=json.loads(self.f.path.read_text())
        segment=self.f.path.parent/'unit-learning-segments'/(manifest['segments'][0]['sha256']+'.json')
        original=learning_intent.view;changed=False;damaged=None
        def interleave(*args,**kw):
            nonlocal changed,damaged
            value=original(*args,**kw)
            if not changed:
                changed=True;raw=segment.read_bytes();segment.write_bytes(b'!'+raw[1:]);damaged=self.files()
            return value
        with patch.object(learning_intent,'view',interleave):
            value=adaptive.study_next(self.f.args(session='reader'))
        self.assertFalse(value['valid']);self.assertEqual(value['recommendations'],[])
        self.assertEqual(damaged,self.files())


if __name__=='__main__':unittest.main()
