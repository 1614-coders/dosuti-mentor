"""Clock-controlled delayed checks are QA, not actual elapsed learning evidence."""
import copy
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import adaptive_learning as al
import unit_learning as ul
import test_unit_learning

DAY0='2026-01-01T00:00:00+00:00';DAY1='2026-01-02T00:00:00+00:00';DAY2='2026-01-03T00:00:00+00:00';DAY9='2026-01-10T00:00:00+00:00'
class Adaptive(unittest.TestCase):
    def setUp(self):self.f=test_unit_learning.UnitLearning();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def read(self,at=DAY0,**kw):
        with patch('course.now',return_value=at):return self.f.read(**kw)
    def save(self,value=None,at=DAY0):
        with patch('course.now',return_value=at):return self.f.save(value)
    def later(self,revision=1,**kw):return self.f.event(stage='checked_later',question='A fresh transfer task?',recheck_of={'1':revision})|kw
    def decision(self,value,at=DAY0,**kwargs):
        x=self.read(at);p=self.f.f.f.directory/'decision.json';p.write_text(json.dumps(value))
        with patch('course.now',return_value=at):return ul.learn_unit(self.f.args(review_file=p,expected_revision=x['learning']['revision'],expected_unit_sha256=x['unit']['content_sha256'],**kwargs))
    def choice(self,**kw):return dict(choice='skip',criteria=[1],until=None,reason='Actual request represented as QA')|kw
    def next(self,at=DAY0,**kw):
        with patch('course.now',return_value=at):return al.study_next(self.f.f.args(limit=5,session='a',**kw))
    def test_explanation_changes_suggestion_not_grade(self):
        self.assertEqual(self.read()['learning']['adaptive']['next_actions'][0]['action'],'initial_check')
        x=self.save(self.f.event(stage='explained',answer=None,question=None,criteria=[]))
        self.assertEqual(x['learning']['adaptive']['next_actions'][0]['action'],'check_understanding');self.assertFalse(x['learning']['all_criteria_demonstrated'])
    def test_error_and_assistance_have_distinct_actions_and_tags(self):
        x=self.save(self.f.event(stage='needs_review',error_tags=['concept']));self.assertEqual(x['learning']['adaptive']['next_actions'][0]['action'],'review_error')
        self.assertEqual(x['learning']['adaptive']['criteria'][0]['error_tags'],['concept'])
        x=self.save(self.f.event(stage='practice_with_help',hints=1,error_tags=['application']));self.assertEqual(x['learning']['adaptive']['next_actions'][0]['action'],'fresh_independent_task')
    def test_independent_solution_review_due_after_day_not_immediately(self):
        self.save();self.assertEqual(self.read()['learning']['adaptive']['next_actions'],[])
        self.assertEqual(self.read(DAY1)['learning']['adaptive']['next_actions'][0]['action'],'recheck_due')
    def test_later_check_before_day_is_rejected_without_write(self):
        self.save();before=self.f.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'24'):self.save(self.later())
        self.assertEqual(self.f.path.read_bytes(),before)
    def test_later_check_records_distinct_state_and_next_due_seven_days(self):
        self.save();x=self.save(self.later(),DAY1);self.assertEqual(x['learning']['criteria']['1'],'checked_later');self.assertTrue(x['learning']['all_criteria_demonstrated'])
        self.assertEqual(x['learning']['adaptive']['later_verified_criteria'],[1]);self.assertEqual(x['learning']['adaptive']['next_actions'],[])
        self.assertEqual(self.read(DAY9)['learning']['adaptive']['next_actions'][0]['action'],'recheck_due')
    def test_recheck_must_reference_latest_independent_answer(self):
        self.save();self.save(self.f.event(stage='needs_review'),DAY1)
        with self.assertRaisesRegex(ValueError,'latest independent'):self.save(self.later(),DAY2)
    def test_recheck_does_not_accept_assisted_baseline(self):
        self.save(self.f.event(stage='practice_with_help',hints=1))
        with self.assertRaises(ValueError):self.save(self.later(),DAY1)
    def test_repeated_question_ignoring_case_space_rejected(self):
        self.save()
        with self.assertRaisesRegex(ValueError,'fresh'):self.save(self.later(question='  WHAT does a map store?  '),DAY1)
    def test_later_check_needs_correct_mapping_and_no_hints(self):
        self.save()
        for kw in [dict(recheck_of={}),dict(recheck_of={'1':True}),dict(hints=1),dict(error_tags=['concept'])]:
            with self.subTest(kw=kw),self.assertRaises(ValueError):self.save(self.later(**kw),DAY1)
    def test_old_schema_read_only_and_write_upgrade_preserves_events(self):
        self.save();data=json.loads(self.f.path.read_text());data['schema']=ul.LEGACY_SCHEMA;self.f.path.write_text(json.dumps(data));before=self.f.path.read_bytes()
        self.assertTrue(self.read()['learning']['valid']);self.assertEqual(self.f.path.read_bytes(),before)
        self.save(at=DAY1);new=json.loads(self.f.path.read_text());self.assertEqual(new['schema'],ul.SCHEMA);self.assertEqual(new['events'][0],data['events'][0])
    def test_legacy_schema_cannot_claim_new_events(self):
        self.save();self.save(self.later(),DAY1);data=json.loads(self.f.path.read_text());data['schema']=ul.LEGACY_SCHEMA;self.f.path.write_text(json.dumps(data))
        self.assertFalse(self.read()['learning']['valid'])
    def test_corrupted_later_reference_is_invalid_journal(self):
        self.save();self.save(self.later(),DAY1);data=json.loads(self.f.path.read_text());data['events'][-1]['value']['recheck_of']['1']=99;self.f.path.write_text(json.dumps(data));before=self.f.path.read_bytes()
        self.assertFalse(self.read()['learning']['valid']);self.assertFalse(self.next()['valid']);self.assertEqual(self.f.path.read_bytes(),before)
    def test_each_criterion_can_reference_different_baseline(self):
        self.f.f.save(self.f.f.card(criteria=['One','Two']));self.save();self.save(self.f.event(criteria=[2]))
        x=self.save(self.later(criteria=[1,2],recheck_of={'1':1,'2':2}),DAY1);self.assertEqual(x['learning']['adaptive']['later_verified_criteria'],[1,2])
    def test_source_change_blocks_suggestions_without_marking_learner_wrong(self):
        self.save();source=self.read()['unit']['sources'][0];p=self.f.f.f.path/source['snapshot'];p.chmod(0o600);p.write_text('Bad')
        x=self.read()['learning'];self.assertTrue(x['adaptive']['blocked']);self.assertEqual(x['adaptive']['reason'],'review_sources');self.assertEqual(x['adaptive']['next_actions'],[])
        self.assertEqual(self.next()['blocked_count'],1)
    def test_skip_and_resume_do_not_change_assessment(self):
        self.save(self.f.event(stage='needs_review'));x=self.decision(self.choice());self.assertEqual(x['learning']['adaptive']['next_actions'],[])
        self.assertEqual(x['learning']['criteria']['1'],'needs_review');self.assertEqual(x['learning']['attempt_count'],1)
        x=self.decision(self.choice(choice='resume'));self.assertEqual(x['learning']['adaptive']['next_actions'][0]['action'],'review_error')
    def test_defer_expires_but_does_not_create_later_grade(self):
        self.save();self.decision(self.choice(choice='defer',until=DAY2))
        self.assertEqual(self.read(DAY1)['learning']['adaptive']['next_actions'],[])
        x=self.read(DAY2)['learning'];self.assertEqual(x['adaptive']['next_actions'][0]['action'],'recheck_due');self.assertEqual(x['criteria']['1'],'solved_independently')
    def test_invalid_defer_deadline_does_not_write(self):
        self.save();before=self.f.path.read_bytes()
        for until in ['2026-01-02',DAY0,'2027-01-01T00:00:00Z']:
            with self.subTest(until=until),self.assertRaises(ValueError):self.decision(self.choice(choice='defer',until=until))
        self.assertEqual(self.f.path.read_bytes(),before)
    def test_later_actual_attempt_supersedes_old_skip(self):
        self.save(self.f.event(stage='needs_review'));self.decision(self.choice());self.save(self.f.event(stage='practice_with_help',hints=1),DAY1)
        self.assertEqual(self.read(DAY1)['learning']['adaptive']['next_actions'][0]['action'],'fresh_independent_task')
    def test_decision_applies_only_to_checked_criteria(self):
        self.f.f.save(self.f.f.card(criteria=['One','Two']));self.decision(self.choice());actions=self.read()['learning']['adaptive']['next_actions'];self.assertEqual([a['criterion'] for a in actions],[2])
    def test_wrong_criterion_decision_rejected(self):
        with self.assertRaises(ValueError):self.decision(self.choice(criteria=[2]))
    def test_decisions_and_answers_are_shared_checkpoints_are_separate(self):
        self.save();
        with patch('course.now',return_value=DAY0):
            self.f.save(dict(recap='A',next_step='Next',pending_question='A?'),checkpoint=True,session='a')
            self.f.save(dict(recap='B',next_step='Next',pending_question='B?'),checkpoint=True,session='b')
        self.decision(self.choice())
        self.assertEqual(self.read(session='a')['learning']['checkpoint']['event']['value']['pending_question'],'A?')
        self.assertEqual(self.read(session='b')['learning']['adaptive']['next_actions'],[])
    def test_course_suggestions_order_errors_before_assistance(self):
        self.f.f.save(self.f.f.card('other'));self.save(self.f.event(stage='practice_with_help',hints=1))
        args=self.f.f.args(unit_id='other');state=ul.learn_unit(args);p=self.f.f.f.directory/'other-answer.json';p.write_text(json.dumps(self.f.event(stage='needs_review')))
        with patch('course.now',return_value=DAY0):ul.learn_unit(self.f.f.args(unit_id='other',event_file=p,expected_revision=state['learning']['revision'],expected_unit_sha256=state['unit']['content_sha256']))
        self.assertEqual([r['unit'] for r in self.next()['recommendations']],['other','maps'])
    def test_new_profile_gets_no_other_profile_attempts(self):
        self.save();x=self.next(profile='friend');self.assertEqual(x['recommendations'][0]['state'],'not_assessed')
    def test_read_only_course_queue_creates_no_learner_journal(self):
        self.next();self.assertFalse(self.f.path.exists())
    def test_unknown_error_tags_do_not_write(self):
        for tags in [['unknown','unknown'],['trait'],[True]]:
            with self.subTest(tags=tags),self.assertRaises(ValueError):self.save(self.f.event(stage='needs_review',error_tags=tags))
    def test_future_clock_is_flagged_not_treated_as_review_due(self):
        self.save(at=DAY2);x=self.read(DAY0)['learning']['adaptive']['criteria'][0];self.assertTrue(x['future_timestamp']);self.assertEqual(x['action'],'review_evidence_time')
    def test_timestamp_with_offset_compares_elapsed_time(self):
        self.save(at='2026-01-01T03:00:00+03:00');self.save(self.later(),DAY1);self.assertEqual(self.read(DAY1)['learning']['criteria']['1'],'checked_later')
    def test_later_recheck_can_use_previous_checked_later_evidence(self):
        self.save();self.save(self.later(),DAY1);x=self.save(self.later(revision=2,question='Another fresh task'),DAY9);self.assertEqual(x['learning']['adaptive']['later_verified_criteria'],[1])
    def test_move_preserves_review_decisions(self):
        self.save();self.decision(self.choice());moved=self.f.f.f.path.with_name('moved');shutil.move(self.f.f.f.path,moved);self.f.f.f.path=moved;self.f.f.f.args.course=moved
        self.assertEqual(self.read()['learning']['adaptive']['criteria'][0]['suppressed'],'user_skipped')

    def test_new_unit_version_does_not_inherit_skip_or_independent_result(self):
        self.save();self.decision(self.choice());self.f.f.save(self.f.f.card(summary='Revised teaching basis'))
        x=self.read()['learning'];self.assertEqual(x['adaptive']['criteria'][0]['state'],'not_assessed');self.assertEqual(x['adaptive']['next_actions'][0]['action'],'initial_check')
    def test_old_unparseable_timestamp_requests_review_instead_of_fake_due(self):
        self.save();data=json.loads(self.f.path.read_text());data['events'][0]['at']='legacy unknown';data['schema']=ul.LEGACY_SCHEMA;self.f.path.write_text(json.dumps(data))
        self.assertEqual(self.read()['learning']['adaptive']['criteria'][0]['action'],'review_evidence_time')
    def test_future_checked_later_not_reported_as_current_retention(self):
        self.save();self.save(self.later(),DAY1);x=self.read(DAY0)['learning']['adaptive'];self.assertEqual(x['later_verified_criteria'],[])

    def test_current_answer_context_deduplicated_and_version_bound(self):
        self.f.f.save(self.f.f.card(criteria=['One','Two']));self.save(self.f.event(criteria=[1,2]))
        x=self.read()['learning']['adaptive'];self.assertEqual(list(x['evidence_events']),['1']);self.assertEqual(x['evidence_events']['1']['value']['answer'],'Keys and values')
        self.assertEqual(self.read(profile='friend')['learning']['adaptive']['evidence_events'],{})
        self.f.f.save(self.f.f.card(summary='New basis',criteria=['One','Two']));self.assertEqual(self.read()['learning']['adaptive']['evidence_events'],{})
