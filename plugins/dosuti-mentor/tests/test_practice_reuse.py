"""Linear projections must preserve evidence, help provenance and journal isolation."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch
import test_unit_learning
import adaptive_learning as adaptive
import practice_support as practice
import unit_learning as learning

NOW = '2026-10-27T12:00:00+00:00'
SHA = 'a' * 64


def event(revision, kind='assessment', unit='maps', checksum=SHA, **value):
    if kind == 'assessment':
        value = dict(stage='solved_independently', details='Recorded answer',
                     question='Question ' + str(revision), answer='Answer', hints=0,
                     criteria=[1]) | value
    return dict(revision=revision, at=NOW, unit=unit, unit_sha256=checksum,
                session='chat', type=kind, value=value)


def card(name='maps', checksum=SHA, count=3):
    return dict(id=name, content_sha256=checksum, criteria=['Criterion ' + str(i) for i in range(count)], stale=False)


def practice_events(count):
    events=[]
    for i in range(count):
        question='Trace operation ' + str(i)
        events.extend([
            event(len(events)+1, 'practice', action='task', id='task-'+str(i),
                  question=question, criteria=[1], rationale='Check an unverified criterion'),
            event(len(events)+2, 'practice', action='help', task_id='task-'+str(i),
                  level='hint', content='Consider the operation', request=None),
            event(len(events)+3, question=question, task_id='task-'+str(i),
                  stage='practice_with_help', hints=1)])
    return events


class CountingEvent(dict):
    accesses=0

    def __getitem__(self, key):
        if key == 'type': type(self).accesses += 1
        return super().__getitem__(key)


class PracticeReuse(unittest.TestCase):
    def test_recommendations_bound_event_visits_and_keep_all_criteria(self):
        events=[CountingEvent(event(i+1)) for i in range(300)]
        CountingEvent.accesses=0
        result=adaptive.recommendations(card(count=50), dict(events=events), NOW)
        self.assertEqual(result['criteria'][0]['evidence_revision'],300)
        self.assertEqual(result['criteria'][49]['state'],'not_assessed')
        self.assertEqual(len(result['next_actions']),49)
        # An operation budget, not a wall-clock assertion: no full scan per criterion.
        self.assertLessEqual(CountingEvent.accesses, 2*len(events)+50)

    def test_recommendations_keep_latest_decisions_versions_and_explanation_scope(self):
        events=[event(1, stage='explained', question=None, answer=None, criteria=[]),
                event(2, stage='needs_review', criteria=[1], error_tags=['concept']),
                event(3,'review_decision', choice='skip', criteria=[1]),
                event(4, stage='practice_with_help', hints=1, criteria=[2]),
                event(5, unit='other', criteria=[3]),
                event(6, checksum='b'*64, criteria=[3])]
        before=deepcopy(events)
        result=adaptive.recommendations(card(), dict(events=events), NOW)
        rows=result['criteria']
        self.assertEqual([(r['state'],r['action'],r['suppressed']) for r in rows],
            [('needs_review','review_error','user_skipped'),
             ('practice_with_help','fresh_independent_task',None),
             ('not_assessed','check_understanding',None)])
        self.assertEqual(set(result['evidence_events']),{'2','4'})
        self.assertEqual(events,before)
        events.append(event(7,criteria=[1]))
        self.assertIsNone(adaptive.recommendations(card(),dict(events=events),NOW)['criteria'][0]['suppressed'])

    def test_practice_replay_has_linear_normalization_budget(self):
        f=test_unit_learning.UnitLearning();f.setUp();self.addCleanup(f.tearDown)
        events=practice_events(80)
        f.path.parent.mkdir(parents=True,exist_ok=True)
        f.path.write_text(json.dumps(dict(schema=learning.SCHEMA,revision=len(events),events=events)))
        with patch.object(adaptive,'fresh_question',wraps=adaptive.fresh_question) as normalize:
            data=learning.load(f.path)
        self.assertEqual(len(data['_practice_tasks']),80)
        self.assertTrue(all(t['last_attempt']['hints']==1 for t in data['_practice_tasks'].values()))
        self.assertLessEqual(normalize.call_count,5*len(events))

    def test_index_preserves_max_help_and_blocks_relabelled_answers(self):
        f=test_unit_learning.UnitLearning();f.setUp();self.addCleanup(f.tearDown)
        events=practice_events(2)
        # A later task reuses the helped question; omitting task_id must not bypass help.
        events.append(event(7, question=' TRACE operation 0!!! ', criteria=[1]))
        f.path.parent.mkdir(parents=True,exist_ok=True)
        f.path.write_text(json.dumps(dict(schema=learning.SCHEMA,revision=7,events=events)))
        before=f.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'Recorded help'): learning.load(f.path)
        self.assertEqual(before,f.path.read_bytes())

    def test_trial_replay_does_not_mutate_reused_projection(self):
        tasks={}
        for e in practice_events(3): practice.replay(tasks,e,{})
        before=deepcopy(tasks)
        help_event=event(10,'practice',action='help',task_id='task-0',level='concrete',
                         content='A more specific hint',request=None)
        practice.validate_replay(tasks,help_event,{})
        self.assertEqual(tasks,before)
        with self.assertRaisesRegex(ValueError,'help'):
            practice.validate_replay(tasks,event(10,question='Trace operation 0'),{})
        self.assertEqual(tasks,before)
        practice.validate_replay(tasks,event(10,'practice',action='task',id='fresh',
            question='New question',criteria=[1],rationale='Independent check'),{})
        self.assertEqual(tasks,before)

    def test_trial_copies_only_the_task_that_can_change(self):
        class CountedTask(dict):
            copied=0
            def __deepcopy__(self,memo):
                type(self).copied+=1
                return deepcopy(dict(self),memo)
        tasks={}
        for e in practice_events(40):practice.replay(tasks,e,{})
        tasks={key:CountedTask(value) for key,value in tasks.items()}
        practice.validate_replay(tasks,event(121,'practice',action='help',task_id='task-0',
            level='concrete',content='More detail',request=None),{})
        self.assertEqual(CountedTask.copied,1)

    def test_index_matches_unindexed_provenance_across_units_and_versions(self):
        events=practice_events(2)
        events.extend([
            event(7,'practice',action='help',task_id='task-0',level='concrete',content='More',request=None),
            event(8,'practice',action='task',id='copy',question=' TRACE operation 0!!! ',criteria=[1],rationale='Check'),
            event(9,question='Trace operation 0',task_id='copy'),
            event(10,question='Trace operation 0',stage='practice_with_help',hints=1),
            event(11,question='Trace operation 0',stage='practice_with_help',hints=2),
            event(12,unit='other',question='Trace operation 0'),
            event(13,checksum='b'*64,question='Trace operation 0'),
            event(14,'practice',action='help',task_id='task-0',level='solution',content='Solution',request='Show it'),
            event(15,question='Trace operation 0',stage='practice_with_help',hints=2),
            event(16,question='Trace operation 0',stage='practice_with_help',hints=3)])
        plain={};indexed={};index={};errors=[]
        for e in events:
            outcomes=[]
            for tasks,kwargs in ((plain,{}),(indexed,{'help_index':index})):
                try:practice.replay(tasks,e,{},**kwargs);outcomes.append(None)
                except ValueError as error:outcomes.append(str(error))
            self.assertEqual(outcomes[0],outcomes[1])
            self.assertEqual(plain,indexed)
            if outcomes[0]:errors.append(e['revision'])
        self.assertEqual(errors,[9,10,15])

    def test_unit_projection_matches_unfiltered_views_without_mutation(self):
        events=practice_events(3)+[event(10,unit='other'),event(11,checksum='b'*64),
            event(12,'checkpoint',recap='Pause',next_step='Continue',pending_question=None)]
        tasks={}
        for e in events: practice.replay(tasks,e,{})
        data=dict(revision=12,events=events,_practice_tasks=tasks)
        before=deepcopy(data)
        grouped=learning.journals_by_unit(data)
        from types import SimpleNamespace
        args=SimpleNamespace(session='chat',history_limit=100)
        for name in ('maps','other','empty'):
            unit=card(name)
            self.assertEqual(learning.view(args,unit,data),learning.view(args,unit,grouped[name]))
            self.assertEqual(adaptive.recommendations(unit,data,NOW),
                             adaptive.recommendations(unit,grouped[name],NOW))
        self.assertEqual(data,before)

    def test_new_projection_sees_new_events(self):
        data=dict(revision=1,events=[event(1)])
        first=learning.journals_by_unit(data)
        data['events'].append(event(2,stage='needs_review'));data['revision']=2
        second=learning.journals_by_unit(data)
        self.assertEqual(first['maps']['revision'],1)
        self.assertEqual(len(first['maps']['events']),1)
        self.assertEqual(second['maps']['revision'],2)
        self.assertEqual(second['maps']['events'][-1]['value']['stage'],'needs_review')

    def test_progress_only_visits_each_units_events(self):
        f=test_unit_learning.UnitLearning();f.setUp();self.addCleanup(f.tearDown)
        f.save()
        import learning_intent
        import study_units
        data=json.loads(f.f.path.read_text());template=data['units']['maps']
        for i in range(20):
            row=dict(template,id='topic-'+str(i))
            row['content_sha256']=study_units.digest(study_units.semantic(row))
            data['units'][row['id']]=row
        data['revision']=21;f.f.path.write_text(json.dumps(data))
        before=f.path.read_bytes();visits=[];original=learning.view
        def counted(args,unit,journal,**kw):
            visits.append(len(journal['events']))
            return original(args,unit,journal,**kw)
        with patch.object(learning,'view',counted):result=learning_intent.view(f.args())
        self.assertTrue(result['learning_progress']['valid'])
        self.assertIn('maps',result['learning_progress']['demonstrated_units'])
        self.assertEqual(sum(visits),1)
        self.assertEqual(f.path.read_bytes(),before)
