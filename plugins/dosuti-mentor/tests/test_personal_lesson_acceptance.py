"""Scripted component/CLI acceptance; no live model or pedagogical evaluation."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import test_unit_learning as fixtures
import unit_learning as ul
import learning_intent as intent
import learning_tools as tools
import course


AREAS={
    'backend':dict(title='Java Backend: duplicate keys',goal='Explain replacement without changing map size',
        source='HashMap.put replaces the value for an existing key. Size counts distinct keys.',
        criterion='Trace duplicate-key replacement and map size',
        question='put(a,2); put(a,3): value(a) and size?',wrong='3 and 2',answer='3 and 1: one distinct key',
        fresh='put(x,4); put(y,8); put(x,9): value(x), value(y), size?',fresh_answer='9, 8, 2',
        hints=['Count distinct keys','The second put uses the same key','Replacement changes a value, not the number of keys']),
    'writing':dict(title='Writing: claims and evidence',goal='Support a claim with relevant evidence',
        source='An argument separates a claim from supporting evidence. An opinion alone is not evidence.',
        criterion='Distinguish a claim from supporting evidence in a paragraph',
        question='Library hours should increase because I prefer evenings: what evidence is missing?',
        wrong='My preference already proves demand',answer='Demand data is missing, for example evening visitor counts',
        fresh='A town proposes a bus route: how could you support the claim that it is needed?',
        fresh_answer='Measure travel demand and compare existing access; personal preference alone is insufficient',
        hints=['Separate the claim from its support','A personal preference says little about other visitors',
               'Visitor counts at different times could support the demand claim']),
}


class PersonalLessonAcceptance(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.UnitLearning();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.folder=self.f.f.f.directory;self.unit='backend';self.lesson=None
        for name,area in AREAS.items():
            source=self.folder/(name+'.txt');source.write_text(area['source'])
            self.f.f.save(self.f.f.card(name,title=area['title'],objectives=[area['goal']],criteria=[area['criterion']],
                sources=[dict(kind='text',role='assistant',notes='Scripted QA learning material',path=str(source),start_line=1,end_line=1)]))

    def args(self,**kw):return self.f.f.args(unit_id=self.unit,**kw)
    def read(self,session='chat-a'):return ul.learn_unit(self.args(session=session,lesson_id=self.lesson))
    def write(self,option,value,revision=None,session='chat-a'):
        state=self.read(session);p=self.folder/'acceptance-input.json';p.write_text(json.dumps(value))
        return ul.learn_unit(self.args(**{option:p},session=session,lesson_id=self.lesson,
            expected_revision=state['learning']['revision'] if revision is None else revision,
            expected_unit_sha256=state['unit']['content_sha256']))
    def begin(self,kind='new_topic'):
        self.lesson=None;area=AREAS[self.unit]
        state=self.write('lesson_context_file',dict(kind=kind,topic=area['title'],goal=area['goal'],
            plan=['Explain the key distinction','Try a small task','Check independent application']))
        self.lesson=state['learning']['lesson_context']['event']['value']['id'];return state
    def checkpoint(self):return dict(recap='Theory and current attempt saved',next_step='Continue the pending practice',pending_question=AREAS[self.unit]['question'])
    def state(self,action,**values):
        if action in ('pause','complete','confirm_switch'):values.setdefault('checkpoint',self.checkpoint())
        return self.write('lesson_state_file',dict(action=action,**values))
    def task(self,identity,question):
        return self.write('practice_file',dict(action='task',id=identity,question=question,criteria=[1],
            rationale=AREAS[self.unit]['goal']+'; verify an unconfirmed criterion or earlier error'))
    def attempt(self,stage,question,answer,task_id=None,hints=0):
        value=dict(stage=stage,details='Scripted evaluation of the stated criterion',question=question,answer=answer,
            criteria=[] if stage=='explained' else [1],hints=hints)
        if task_id:value['task_id']=task_id
        return self.write('event_file',value)
    def decide(self,value):
        intent.commit(self.args(expected_revision=intent.view(self.args())['revision'],reason='Explicit scripted QA learner decision'),value)
        return intent.view(self.args())
    def files(self):return {str(p):p.read_bytes() for p in self.args().course.rglob('*') if p.is_file()}
    def cli(self):
        process=subprocess.run([sys.executable,'-B',str(Path(course.__file__)),'learn-unit','--course',str(self.args().course),
            '--profile','default','--unit-id',self.unit,'--session','chat-b','--lesson-id',self.lesson],
            capture_output=True,text=True,timeout=20,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(process.returncode,0,process.stderr);return json.loads(process.stdout)

    def test_full_cycle_in_java_backend_and_nontechnical_area(self):
        for name,area in AREAS.items():
            with self.subTest(area=name):
                self.unit=name;self.lesson=None
                self.decide(dict(goal=dict(description=area['goal'])))
                initial=self.begin();context=initial['learning']['lesson_context']['event']['value']
                self.assertEqual(context['basis']['learning_goal']['description'],area['goal'])
                self.assertEqual(len(context['plan']),3)
                self.assertIn(area['source'],initial['unit']['source_contexts'][0]['text'])
                self.assertEqual(initial['preferences']['effective']['checks'],'adaptive')
                self.attempt('explained',None,None)
                self.write('checkpoint_file',self.checkpoint()|dict(recap='Explained theory; learner said: понятно'))
                self.assertNotIn(name,self.read()['learning_progress']['demonstrated_units'])
                self.task(name+'-first',area['question'])
                self.attempt('needs_review',area['question'],area['wrong'],name+'-first')
                for level,content in zip(('hint','concrete','explanation'),area['hints']):
                    self.write('practice_file',dict(action='help',task_id=name+'-first',level=level,content=content,request=None))
                self.write('practice_file',dict(action='help',task_id=name+'-first',level='solution',
                    content=area['answer']+'. '+area['source'],request='Show the full solution with explanation'))
                before=self.files()
                with self.assertRaisesRegex(ValueError,'help'):
                    self.attempt('solved_independently',area['question'],area['answer'],name+'-first')
                self.assertEqual(self.files(),before)
                self.attempt('practice_with_help',area['question'],area['answer'],name+'-first',hints=4)
                self.state('pause');before=self.files();restored=self.cli()
                self.assertEqual(self.files(),before)
                self.assertEqual(restored['learning']['lesson_state']['status'],'paused')
                self.assertTrue(restored['learning']['practice'][0]['solution_shown'])
                self.assertNotIn(name,restored['learning_progress']['demonstrated_units'])
                self.state('resume');self.task(name+'-fresh',area['fresh'])
                self.attempt('solved_independently',area['fresh'],area['fresh_answer'],name+'-fresh')
                finished=self.state('complete')
                self.assertIn(name,finished['learning_progress']['demonstrated_units'])
                self.assertNotIn(self.lesson,[r.get('lesson_id') for r in finished['learning_progress']['continuations']])
                before=self.files();restored=self.cli()
                self.assertEqual(restored['learning']['lesson_state']['status'],'completed')
                self.assertEqual(restored['learning']['attempt_count'],3)
                self.assertEqual(self.files(),before)

    def test_five_lesson_types_in_sequence_preserve_identity_and_preference(self):
        tools.settings(self.args(scope='profile',set=['checks=on_request'],reset=False,onboarding=None))
        ids=[]
        for kind in ('new_topic','continuation','review','practice','gap_work'):
            state=self.begin(kind);ids.append(self.lesson)
            self.assertEqual(state['preferences']['effective']['checks'],'on_request')
            self.assertEqual(state['learning']['lesson_context']['event']['value']['kind'],kind)
            self.attempt('explained',None,None)
            self.state('complete')
        self.assertEqual(len(set(ids)),5)
        state=self.cli()
        self.assertEqual(len(state['learning']['lesson_contexts']),5)
        self.assertEqual(state['learning']['attempt_count'],0)
        self.assertEqual(state['learning_progress']['demonstrated_units'],[])
        self.assertEqual(state['learning_progress']['continuations'],[])

    def test_switch_retry_cas_and_distinct_lesson_continuations(self):
        self.begin();first=self.lesson;self.task('unfinished',AREAS['backend']['question'])
        request=self.state('request_switch',request_id='switch-writing',target_topic=AREAS['writing']['title'])
        before=self.files();self.state('request_switch',request_id='switch-writing',target_topic=AREAS['writing']['title'])
        self.assertEqual(self.files(),before)
        with self.assertRaisesRegex(ValueError,'another chat'):
            self.write('lesson_state_file',dict(action='cancel_switch',request_id='switch-writing'),revision=request['learning']['revision']-1)
        self.assertEqual(self.files(),before)
        self.state('confirm_switch',request_id='switch-writing');before=self.files()
        self.state('confirm_switch',request_id='switch-writing');self.assertEqual(self.files(),before)
        self.unit='writing';self.begin();second=self.lesson
        state=self.read()['learning_progress']
        self.assertTrue(state['continuation_choice_required']);self.assertIsNone(state['stopped_at'])
        self.assertEqual({r['lesson_id'] for r in state['continuations']},{first,second})
        self.state('complete');self.unit='backend';self.lesson=first
        restored=self.cli();self.assertEqual(restored['learning']['lesson_state']['status'],'paused')
        self.assertFalse(restored['learning']['lesson_state']['confirmation_required'])
        self.assertEqual(restored['learning']['practice'][0]['id'],'unfinished')
        self.assertIsNone(restored['learning']['practice'][0]['last_attempt'])
        self.state('resume')
        self.state('request_switch',request_id='cancel-new-switch',target_topic='Other topic')
        self.state('cancel_switch',request_id='cancel-new-switch');before=self.files()
        self.state('cancel_switch',request_id='cancel-new-switch')
        self.assertEqual(self.files(),before);self.assertEqual(self.read()['learning']['lesson_state']['status'],'active')

    def test_outside_roadmap_learning_preserved_but_plan_changes_require_acceptance(self):
        self.decide(dict(goal=dict(description='Learn backend and communicate technical ideas')))
        self.decide(dict(proposal=dict(summary='Start with backend',steps=['Backend maps'],step_bases=[dict(unit_ids=['backend'])])))
        original=self.decide(dict(accept_proposal=True))['plan']
        self.unit='writing';self.begin();area=AREAS['writing']
        self.task('outside-route',area['fresh']);self.attempt('solved_independently',area['fresh'],area['fresh_answer'],'outside-route')
        state=self.state('complete')
        self.assertIn('writing',state['learning_progress']['demonstrated_units'])
        self.assertEqual(state['learning_intent']['plan']['accepted_revision'],original['accepted_revision'])
        self.assertEqual(state['learning_progress']['completed_steps'],[])
        proposed=self.decide(dict(proposal=dict(summary='Include writing',steps=['Backend maps','Evidence-based writing'],
            step_bases=[dict(unit_ids=['backend']),dict(unit_ids=['writing'])])))
        self.assertEqual(proposed['plan']['steps'],['Backend maps'])
        before=self.files();self.cli();self.assertEqual(self.files(),before)  # No response/acceptance leaves old plan intact.
        accepted=self.decide(dict(accept_proposal=True))
        self.assertEqual(accepted['plan']['steps'],['Backend maps','Evidence-based writing'])
        self.assertIn('writing',accepted['learning_progress']['demonstrated_units'])

    def test_legacy_understood_is_a_claim_not_criterion_evidence(self):
        with contextlib.redirect_stdout(io.StringIO()):
            course.progress(self.args(topic='Legacy backend',stage='understood',details='Old checkbox without an assessed answer'))
        before=self.files();legacy=intent.view(self.args())['learning_progress']
        self.assertEqual(self.files(),before)
        self.assertEqual(legacy['demonstrated_units'],[])
        claim=legacy['confirmed_lesson_knowledge'][0]
        self.assertEqual(claim['stage'],'understood');self.assertEqual(claim['verification'],'legacy_recorded_claim')
        self.assertIn('legacy_lesson_checks_are_recorded_claims_without_unit_criteria',legacy['limitations'])
        self.begin();area=AREAS['backend'];self.task('criterion-check',area['fresh'])
        state=self.attempt('solved_independently',area['fresh'],area['fresh_answer'],'criterion-check')
        self.assertEqual(state['learning_progress']['demonstrated_units'],['backend'])
        self.assertEqual(state['learning_progress']['confirmed_lesson_knowledge'],legacy['confirmed_lesson_knowledge'])
        self.assertEqual(state['learning_progress']['completed_unit_tasks'][0]['criteria'],[1])


if __name__=='__main__':unittest.main()
